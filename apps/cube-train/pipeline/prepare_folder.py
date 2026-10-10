"""Stream supported local datasets into capped uint16 byte-token files."""
import json
import hashlib
import random
from pathlib import Path
import shutil
import tempfile

import numpy as np
try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:
    pa = pq = None

from prepare_parquet import plain_html
from corpus_split import document_key, split_documents

EXTENSIONS = {".parquet", ".txt", ".md", ".jsonl"}
TEXT_FIELDS = ("text", "content", "story", "body", "document.html")


def field_type(schema, name):
    parts = name.split(".")
    typ = schema.field(parts[0]).type
    for part in parts[1:]:
        typ = typ.field(part).type
    if pa.types.is_dictionary(typ):
        typ = typ.value_type
    return typ


def pick_column(schema, requested=None):
    for name in [requested] if requested else TEXT_FIELDS:
        try:
            typ = field_type(schema, name)
            if pa.types.is_string(typ) or pa.types.is_large_string(typ):
                return name
        except (KeyError, AttributeError, IndexError):
            pass
    raise ValueError(f"No recognized text column. Available fields: {schema.names}; use --column")


def texts(path, column=None):
    if path.suffix.lower() == ".parquet":
        pf = pq.ParquetFile(path)
        selected = pick_column(pf.schema_arrow, column)
        print(f"Reading {path.name}: {selected}, {pf.metadata.num_rows:,} rows", flush=True)
        for batch in pf.iter_batches(batch_size=1, columns=[selected], use_threads=False):
            values = batch.column(0)
            for part in selected.split(".")[1:]:
                values = values.field(part)
            for value in values:
                text = value.as_py()
                if text:
                    yield plain_html(text) if selected.endswith("html") else text
    elif path.suffix.lower() == ".jsonl":
        with path.open(encoding="utf-8", errors="strict") as source:
            line_no = 0
            while True:
                line = source.readline(16 * 1024 * 1024 + 1)
                if not line:
                    break
                line_no += 1
                if len(line) > 16 * 1024 * 1024:
                    raise ValueError(f"{path}:{line_no}: JSONL line exceeds 16 MiB")
                if not line.strip():
                    continue
                item = json.loads(line)
                if isinstance(item, str):
                    yield item
                    continue
                if not isinstance(item, dict):
                    raise ValueError(f"{path}:{line_no}: expected object or string")
                found = False
                for name in [column] if column else TEXT_FIELDS:
                    value = item
                    for part in name.split("."):
                        value = value.get(part) if isinstance(value, dict) else None
                    if isinstance(value, str):
                        yield plain_html(value) if name.endswith("html") else value
                        found = True
                        break
                if not found:
                    raise ValueError(f"{path}:{line_no}: no text field; use --column")
    else:
        # Plain text is chunked so even multi-GB files do not enter RAM together.
        with path.open(encoding="utf-8", errors="strict") as source:
            while chunk := source.read(65536):
                yield chunk


def prepare_folder(source, output, *, max_tokens=20_000_000, column=None, val_fraction=0.05, seed=1337, shuffle_files=False, stop_requested=None):
    source, output = Path(source).resolve(), Path(output).resolve()
    if not 0 < val_fraction < 1:
        raise ValueError("Validation fraction must be between 0 and 1")
    if max_tokens < 6000:
        raise ValueError("Use at least 6,000 tokens to leave a usable validation split")
    if output.exists():
        raise FileExistsError(f"Prepared corpus already exists: {output}")
    files = [source] if source.is_file() else sorted(
        p for p in source.rglob("*") if p.is_file() and p.suffix.lower() in EXTENSIONS
    )
    if shuffle_files:
        random.Random(seed).shuffle(files)
    if not files:
        raise ValueError(f"No .parquet/.txt/.md/.jsonl datasets in {source}")
    if any(p.suffix.lower() not in EXTENSIONS for p in files):
        raise ValueError("Supported datasets: Parquet, TXT, Markdown and JSONL")
    # Check every shard's footer/schema first, including files beyond the cap.
    for path in files:
        if stop_requested and stop_requested():
            raise InterruptedError('Corpus preparation stopped.')
        if not path.stat().st_size:
            raise ValueError(f"Empty or unfinished dataset: {path}")
        if path.suffix.lower() == ".parquet":
            if pq is None:
                raise ImportError("Parquet input requires pyarrow")
            pick_column(pq.ParquetFile(path).schema_arrow, column)
    output.parent.mkdir(parents=True, exist_ok=True)
    reports, total = [], 0
    records = []
    with tempfile.TemporaryDirectory(prefix=".prepare-", dir=output.parent) as temp:
        stage = Path(temp)
        combined = stage / "tokens.bin"
        with combined.open("wb") as dest:
            for path in files:
                if stop_requested and stop_requested():
                    raise InterruptedError('Corpus preparation stopped.')
                if total >= max_tokens - 1:
                    break
                report = {"path": str(path), "tokens": 0, "documents_or_chunks": 0,
                          "truncated": 0}
                file_key = None
                if path.suffix.lower() in (".txt", ".md"):
                    digest = hashlib.sha256()
                    with path.open("rb") as raw_file:
                        while chunk := raw_file.read(1024 * 1024):
                            digest.update(chunk)
                    file_key = digest.hexdigest()
                for text in texts(path, column):
                    if stop_requested and stop_requested():
                        raise InterruptedError('Corpus preparation stopped.')
                    if not text:
                        continue
                    remaining = max_tokens - total
                    if remaining < 2:
                        break
                    prefix = text[:remaining - 1]
                    encoded = prefix.encode("utf-8")
                    raw = encoded[:remaining - 1]
                    report["truncated"] += int(len(prefix) < len(text) or len(raw) < len(encoded))
                    # Convert in small chunks, including when a document is huge.
                    for offset in range(0, len(raw), 65536):
                        dest.write(np.frombuffer(raw[offset:offset+65536], dtype=np.uint8)
                                   .astype(np.uint16).tobytes())
                    dest.write(np.array([257], dtype=np.uint16).tobytes())
                    count = len(raw) + 1
                    records.append((total, count, file_key or document_key(text)))
                    total += count
                    report["tokens"] += count
                    report["documents_or_chunks"] += 1
                    if total >= max_tokens - 1:
                        break
                reports.append(report)
                print(f"Prepared {path.name}: {report['tokens']:,} tokens", flush=True)
        n_train, n_val = split_documents(combined, stage, records,
                                          val_fraction=val_fraction, seed=seed)
        manifest = {"source": str(source), "max_tokens": max_tokens, "total_tokens": total,
                    "train_tokens": n_train, "val_tokens": n_val,
                    "available_files": [str(p) for p in files], "consumed_files": reports,
                    "column": column, "tokenizer": "UTF-8 bytes + EOS=257",
                    "selection": "sorted files, source rows in order; seeded document-group split",
                    "split_seed": seed, "val_fraction": val_fraction,
                    "split_method": "document_hash_groups_v1",
                    "text_chunk_characters": 65536}
        (stage / "corpus.json").write_text(json.dumps(manifest, indent=2) + "\n")
        output.mkdir()
        for name in ("train.bin", "val.bin", "corpus.json"):
            shutil.move(str(stage / name), str(output / name))
    print(f"Corpus: {n_train:,} training + {n_val:,} validation tokens", flush=True)
    return manifest
