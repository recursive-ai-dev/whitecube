"""Stream supported local datasets into capped uint16 byte-token files."""
import json
import hashlib
import random
from pathlib import Path
import tempfile

import numpy as np
try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:
    pa = pq = None

from prepare_parquet import (corpus_integrity, field_type, parquet_values, pick_column,
                             plain_html, TEXT_FIELDS)
from corpus_split import (check_cancelled, document_key, positive_integer, split_documents,
                          utf8_prefix, validate_split_options)
from files import atomic_json, publish_directory

EXTENSIONS = {".parquet", ".txt", ".md", ".jsonl"}
JSONL_LINE_BYTES = 16 * 1024 * 1024


def texts(path, column=None, *, batch_rows=32, stop_requested=None, stats=None):
    path = Path(path)
    stats = stats if stats is not None else {}
    stats.setdefault('rows_scanned', 0)
    stats.setdefault('skipped_documents', 0)
    if path.suffix.lower() == ".parquet":
        if pq is None:
            raise ImportError('Parquet input requires pyarrow')
        with pq.ParquetFile(path) as pf:
            selected = pick_column(pf.schema_arrow, column)
            stats['column'] = selected
            print(f"Reading {path.name}: {selected}, {pf.metadata.num_rows:,} rows", flush=True)
            for text in parquet_values(pf, selected, batch_rows):
                check_cancelled(stop_requested)
                stats['rows_scanned'] += 1
                if text is not None:
                    yield plain_html(text) if selected.endswith("html") else text
                else:
                    stats['skipped_documents'] += 1
    elif path.suffix.lower() == ".jsonl":
        with path.open('rb') as source:
            line_no = 0
            while True:
                check_cancelled(stop_requested)
                line = source.readline(JSONL_LINE_BYTES + 1)
                if not line:
                    break
                line_no += 1
                if len(line) > JSONL_LINE_BYTES:
                    raise ValueError(f"{path}:{line_no}: JSONL line exceeds 16 MiB")
                try:
                    line = line.decode('utf-8-sig' if line_no == 1 else 'utf-8')
                except UnicodeDecodeError as error:
                    raise ValueError(f'{path}:{line_no}: invalid UTF-8: {error.reason}') from error
                if not line.strip():
                    continue
                stats['rows_scanned'] += 1
                try:
                    item = json.loads(line)
                except json.JSONDecodeError as error:
                    raise ValueError(f'{path}:{line_no}:{error.colno}: invalid JSON: {error.msg}') from error
                if isinstance(item, str):
                    yield item
                    continue
                if not isinstance(item, dict):
                    raise ValueError(f"{path}:{line_no}: expected object or string")
                found, null_field = False, False
                for name in [column] if column is not None else TEXT_FIELDS:
                    if name in item:
                        value, present = item[name], True
                    else:
                        value, present = item, True
                        for part in name.split("."):
                            if not isinstance(value, dict) or part not in value:
                                present = False
                                break
                            value = value[part]
                    null_field |= present and value is None
                    if present and isinstance(value, str):
                        yield plain_html(value) if name.endswith("html") else value
                        found = True
                        break
                if not found:
                    if null_field:
                        stats['skipped_documents'] += 1
                    else:
                        raise ValueError(f"{path}:{line_no}: no string text field; use --column")
    else:
        # Plain text is chunked so even multi-GB files do not enter RAM together.
        with path.open(encoding="utf-8-sig", errors="strict") as source:
            while chunk := source.read(65536):
                check_cancelled(stop_requested)
                stats['rows_scanned'] += 1
                yield chunk


def prepare_folder(source, output, *, max_tokens=20_000_000, column=None, val_fraction=0.05,
                   seed=1337, shuffle_files=False, stop_requested=None, block_size=256,
                   batch_rows=32, progress_callback=None):
    source, output = Path(source).expanduser().resolve(), Path(output).expanduser().absolute()
    validate_split_options(val_fraction, block_size, seed)
    positive_integer(max_tokens, 'Token cap', minimum=6000)
    positive_integer(batch_rows, 'Batch rows')
    if column is not None and (not isinstance(column, str) or not column.strip()):
        raise ValueError('Text column must be a nonempty string')
    if not isinstance(shuffle_files, bool):
        raise ValueError('Shuffle files must be a boolean')
    check_cancelled(stop_requested)
    if not source.exists():
        raise FileNotFoundError(f'Dataset does not exist: {source}')
    if not source.is_file() and not source.is_dir():
        raise ValueError(f'Dataset must be a regular file or directory: {source}')
    if output.exists() or output.is_symlink():
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
    snapshots = {}
    for path in files:
        check_cancelled(stop_requested)
        stat = path.stat()
        snapshots[path] = (stat.st_size, stat.st_mtime_ns, stat.st_ino)
        if not stat.st_size:
            raise ValueError(f"Empty or unfinished dataset: {path}")
        if path.suffix.lower() == ".parquet":
            if pq is None:
                raise ImportError("Parquet input requires pyarrow")
            with pq.ParquetFile(path) as parquet:
                pick_column(parquet.schema_arrow, column)
                if not parquet.metadata.num_rows:
                    raise ValueError(f'Empty dataset: {path}')
        if progress_callback:
            progress_callback({'phase': 'checking', 'path': str(path),
                               'files_checked': len(snapshots), 'available_files': len(files)})
    output.parent.mkdir(parents=True, exist_ok=True)
    reports, total = [], 0
    records = []
    with tempfile.TemporaryDirectory(prefix=".prepare-", dir=output.parent) as temp:
        work = Path(temp)
        stage = work / 'corpus'
        stage.mkdir()
        combined = work / "tokens.bin"
        cap_reached = False
        with combined.open("wb") as dest:
            for path in files:
                check_cancelled(stop_requested)
                if cap_reached or total >= max_tokens - 1:
                    break
                report = {"path": str(path), "tokens": 0, "documents_or_chunks": 0,
                          "truncated": 0, 'rows_scanned': 0, 'skipped_documents': 0,
                          'source_bytes': snapshots[path][0], 'source_mtime_ns': snapshots[path][1]}
                file_key = None
                if path.suffix.lower() in (".txt", ".md"):
                    digest = hashlib.sha256()
                    try:
                        with path.open(encoding='utf-8-sig', errors='strict') as raw_file:
                            while chunk := raw_file.read(65536):
                                check_cancelled(stop_requested)
                                digest.update(chunk.encode('utf-8'))
                    except UnicodeError as error:
                        raise ValueError(f'{path}: invalid UTF-8 text: {error}') from error
                    file_key = digest.hexdigest()
                iterator = texts(path, column, batch_rows=batch_rows, stop_requested=stop_requested, stats=report)
                try:
                    for text in iterator:
                        check_cancelled(stop_requested)
                        if not text or not text.strip():
                            report['skipped_documents'] += 1
                            continue
                        remaining = max_tokens - total
                        if remaining < 2:
                            cap_reached = True
                            break
                        raw, was_truncated = utf8_prefix(text, remaining - 1)
                        if not raw:
                            cap_reached = True
                            break
                        report['truncated'] += int(was_truncated)
                        for offset in range(0, len(raw), 65536):
                            check_cancelled(stop_requested)
                            dest.write(np.frombuffer(raw[offset:offset+65536], dtype=np.uint8)
                                       .astype(np.uint16).tobytes())
                        dest.write(np.array([257], dtype=np.uint16).tobytes())
                        count = len(raw) + 1
                        records.append((total, count, file_key or document_key(text, stop_requested)))
                        total += count
                        report['tokens'] += count
                        report['documents_or_chunks'] += 1
                        if progress_callback:
                            progress_callback({'phase': 'reading', 'path': str(path), 'tokens': total,
                                               'documents': len(records), 'files_consumed': len(reports) + 1})
                        if was_truncated or total >= max_tokens - 1:
                            cap_reached = True
                            break
                except UnicodeError as error:
                    raise ValueError(f'{path}: invalid UTF-8 text: {error}') from error
                finally:
                    iterator.close()
                reports.append(report)
                print(f"Prepared {path.name}: {report['tokens']:,} tokens", flush=True)
        for path, snapshot in snapshots.items():
            check_cancelled(stop_requested)
            stat = path.stat()
            if snapshot != (stat.st_size, stat.st_mtime_ns, stat.st_ino):
                raise ValueError(f'Dataset changed during preparation: {path}')
        if progress_callback:
            progress_callback({'phase': 'splitting', 'tokens': total, 'documents': len(records)})
        n_train, n_val = split_documents(combined, stage, records, val_fraction=val_fraction,
                                        seed=seed, block_size=block_size, stop_requested=stop_requested)
        manifest = {"source": str(source), "max_tokens": max_tokens, "total_tokens": total,
                    "train_tokens": n_train, "val_tokens": n_val,
                    "available_files": [str(p) for p in files], "consumed_files": reports,
                    "column": column, "tokenizer": "UTF-8 bytes + EOS=257",
                    "selection": ("seeded shuffled files" if shuffle_files else "sorted files") +
                                 ", source rows in order; seeded document-group split",
                    "split_seed": seed, "val_fraction": val_fraction,
                    "split_method": "document_hash_groups_v1",
                    "text_chunk_characters": 65536, 'block_size': block_size,
                    'batch_rows': batch_rows, 'shuffle_files': shuffle_files,
                    'actual_val_fraction': n_val / total, 'cap_reached': cap_reached,
                    'rows_scanned': sum(r['rows_scanned'] for r in reports),
                    'skipped_documents': sum(r['skipped_documents'] for r in reports),
                    'truncated_documents': sum(r['truncated'] for r in reports),
                    **corpus_integrity(stage, records, stop_requested)}
        atomic_json(stage / 'corpus.json', manifest)
        check_cancelled(stop_requested)
        if progress_callback:
            progress_callback({'phase': 'publishing', 'tokens': total, 'documents': len(records)})
        check_cancelled(stop_requested)
        publish_directory(stage, output)
    print(f"Corpus: {n_train:,} training + {n_val:,} validation tokens", flush=True)
    return manifest
