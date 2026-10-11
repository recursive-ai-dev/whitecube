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
from corpus_index import DocumentIndex

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
                stats['source_row'] = line_no
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


def plain_metadata(path, stop_requested):
    """Hash one normalized text document without retaining its contents."""
    digest, size, nonblank = hashlib.sha256(), 0, False
    with path.open(encoding='utf-8-sig', errors='strict') as source:
        while chunk := source.read(65536):
            check_cancelled(stop_requested)
            raw = chunk.encode('utf-8')
            digest.update(raw)
            size += len(raw)
            nonblank |= bool(chunk.strip())
    return digest.hexdigest(), size, nonblank


def plain_prefix(path, limit, stop_requested):
    with path.open(encoding='utf-8-sig', errors='strict') as source:
        while limit and (chunk := source.read(65536)):
            check_cancelled(stop_requested)
            raw, truncated = utf8_prefix(chunk, limit)
            if raw:
                yield raw
            limit -= len(raw)
            if truncated:
                break


def write_document(dest, chunks, stop_requested):
    digest, size = hashlib.sha256(), 0
    for raw in chunks:
        check_cancelled(stop_requested)
        digest.update(raw)
        size += len(raw)
        for offset in range(0, len(raw), 65536):
            check_cancelled(stop_requested)
            dest.write(np.frombuffer(raw[offset:offset + 65536], dtype=np.uint8).astype(np.uint16).tobytes())
    return size, digest.hexdigest()


def prepare_folder(source, output, *, max_tokens=20_000_000, column=None, val_fraction=0.05,
                   seed=1337, shuffle_files=False, stop_requested=None, block_size=256,
                   batch_rows=32, progress_callback=None, deduplicate=False,
                   min_document_bytes=0, max_document_bytes=0):
    source, output = Path(source).expanduser().resolve(), Path(output).expanduser().absolute()
    validate_split_options(val_fraction, block_size, seed)
    positive_integer(max_tokens, 'Token cap', minimum=6000)
    positive_integer(batch_rows, 'Batch rows')
    positive_integer(min_document_bytes, 'Minimum document bytes', minimum=0)
    positive_integer(max_document_bytes, 'Maximum document bytes', minimum=0)
    if max_document_bytes and min_document_bytes > max_document_bytes:
        raise ValueError('Minimum document bytes cannot exceed the maximum')
    if column is not None and (not isinstance(column, str) or not column.strip()):
        raise ValueError('Text column must be a nonempty string')
    for name, value in [('Shuffle files', shuffle_files), ('Deduplicate', deduplicate)]:
        if not isinstance(value, bool):
            raise ValueError(f'{name} must be a boolean')
    check_cancelled(stop_requested)
    if not source.exists():
        raise FileNotFoundError(f'Dataset does not exist: {source}')
    if not source.is_file() and not source.is_dir():
        raise ValueError(f'Dataset must be a regular file or directory: {source}')
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"Prepared corpus already exists: {output}")
    if source.is_dir() and (source / 'corpus.json').is_file():
        from corpus_artifacts import reuse_corpus
        return reuse_corpus(source, output, block_size=block_size, stop_requested=stop_requested,
                            progress_callback=progress_callback)
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
    with (tempfile.TemporaryDirectory(prefix=".prepare-", dir=output.parent) as temp,
          DocumentIndex(Path(temp) / 'documents.sqlite', seed, stop_requested) as records):
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
                          'duplicate_documents': 0, 'short_documents': 0,
                          'document_limit_truncations': 0, 'corpus_limit_truncations': 0,
                          'source_bytes': snapshots[path][0], 'source_mtime_ns': snapshots[path][1]}
                plain = path.suffix.lower() in ('.txt', '.md')
                iterator = None
                try:
                    if plain:
                        if progress_callback:
                            progress_callback({'phase': 'reading', 'path': str(path), 'tokens': total,
                                               'documents': len(records)})
                        full_key, byte_count, nonblank = plain_metadata(path, stop_requested)
                        iterator = iter([None])
                        report['rows_scanned'] = 1
                    else:
                        iterator = texts(path, column, batch_rows=batch_rows, stop_requested=stop_requested, stats=report)
                    for text in iterator:
                        check_cancelled(stop_requested)
                        if not (nonblank if plain else text and text.strip()):
                            report['skipped_documents'] += 1
                            continue
                        source_key = full_key if plain else document_key(text, stop_requested)
                        if deduplicate and records.seen(source_key):
                            report['duplicate_documents'] += 1
                            report['skipped_documents'] += 1
                            continue
                        remaining = max_tokens - total
                        if remaining < 2:
                            cap_reached = True
                            break
                        limit = min(remaining - 1, max_document_bytes or remaining - 1)
                        if plain:
                            chunks = plain_prefix(path, limit, stop_requested)
                        else:
                            raw, truncated = utf8_prefix(text, limit)
                            chunks = iter([raw])
                        start = dest.tell()
                        size, key = write_document(dest, chunks, stop_requested)
                        if not size or size < min_document_bytes or deduplicate and records.contains(key):
                            dest.seek(start)
                            dest.truncate()
                            report['skipped_documents'] += 1
                            if deduplicate and size and records.contains(key):
                                report['duplicate_documents'] += 1
                                records.remember(source_key)
                            else:
                                report['short_documents'] += 1
                            if not size and limit == remaining - 1:
                                cap_reached = True
                                break
                            continue
                        was_truncated = byte_count > size if plain else truncated
                        report['truncated'] += int(was_truncated)
                        report['document_limit_truncations'] += int(was_truncated and limit == max_document_bytes)
                        corpus_truncation = was_truncated and limit == remaining - 1
                        report['corpus_limit_truncations'] += int(corpus_truncation)
                        dest.write(np.array([257], dtype=np.uint16).tobytes())
                        count = size + 1
                        records.append((total, count, key), source=str(path), row=report.get('source_row', report['rows_scanned']),
                                       truncated=was_truncated, source_key=source_key)
                        total += count
                        report['tokens'] += count
                        report['documents_or_chunks'] += 1
                        if progress_callback:
                            progress_callback({'phase': 'reading', 'path': str(path), 'tokens': total,
                                               'documents': len(records), 'files_consumed': len(reports) + 1,
                                               'duplicate_documents': sum(r['duplicate_documents'] for r in reports) + report['duplicate_documents']})
                        if corpus_truncation or total >= max_tokens - 1:
                            cap_reached = True
                            break
                except UnicodeError as error:
                    raise ValueError(f'{path}: invalid UTF-8 text: {error}') from error
                finally:
                    if hasattr(iterator, 'close'):
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
                    "split_method": "source_and_stored_document_hash_groups_v2",
                    "text_chunk_characters": 65536, 'block_size': block_size,
                    'batch_rows': batch_rows, 'shuffle_files': shuffle_files, 'deduplicate': deduplicate,
                    'min_document_bytes': min_document_bytes, 'max_document_bytes': max_document_bytes,
                    'actual_val_fraction': n_val / total, 'cap_reached': cap_reached,
                    'rows_scanned': sum(r['rows_scanned'] for r in reports),
                    'skipped_documents': sum(r['skipped_documents'] for r in reports),
                    'duplicate_documents': sum(r['duplicate_documents'] for r in reports),
                    'short_documents': sum(r['short_documents'] for r in reports),
                    'document_limit_truncations': sum(r['document_limit_truncations'] for r in reports),
                    'corpus_limit_truncations': sum(r['corpus_limit_truncations'] for r in reports),
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
