"""Stream a local Parquet text column into a capped Tensegrity byte-token corpus."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
from html.parser import HTMLParser
import re

from corpus_split import (check_cancelled, document_key, positive_integer, split_documents,
                          utf8_prefix, validate_split_options)
from files import atomic_json, file_sha256, publish_directory

import numpy as np
try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:
    pa = pq = None


class PlainHTML(HTMLParser):
    BLOCK_TAGS = {'p', 'div', 'br', 'li', 'tr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
                  'section', 'article', 'header', 'footer', 'blockquote', 'pre', 'hr', 'ul', 'ol'}
    IGNORED_TAGS = {'script', 'style', 'template', 'noscript'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.ignored = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.IGNORED_TAGS:
            self.ignored += 1
        elif tag in self.BLOCK_TAGS and not self.ignored:
            self.parts.append("\n")
        elif tag in ('td', 'th') and not self.ignored:
            self.parts.append(' ')

    def handle_endtag(self, tag):
        if tag in self.IGNORED_TAGS:
            self.ignored = max(0, self.ignored - 1)
        elif tag in self.BLOCK_TAGS and not self.ignored:
            self.parts.append("\n")
        elif tag in ('td', 'th') and not self.ignored:
            self.parts.append(' ')

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data):
        if not self.ignored:
            self.parts.append(data)


def plain_html(text):
    parser = PlainHTML()
    parser.feed(text)
    parser.close()
    lines = [re.sub(r"\s+", " ", line).strip() for line in "".join(parser.parts).splitlines()]
    return "\n".join(line for line in lines if line)


TEXT_FIELDS = ('text', 'content', 'story', 'body', 'document.html')


def field_type(schema, name):
    parts = [name] if name in schema.names else name.split('.')
    typ = schema.field(parts[0]).type
    for part in parts[1:]:
        if pa.types.is_dictionary(typ):
            typ = typ.value_type
        if not pa.types.is_struct(typ):
            raise KeyError(name)
        typ = typ.field(part).type
    return typ.value_type if pa.types.is_dictionary(typ) else typ


def pick_column(schema, requested=None):
    if requested is not None and (not isinstance(requested, str) or not requested.strip()):
        raise ValueError('Text column must be a nonempty string')
    for name in [requested] if requested is not None else TEXT_FIELDS:
        try:
            typ = field_type(schema, name)
            if pa.types.is_string(typ) or pa.types.is_large_string(typ):
                return name
        except (KeyError, AttributeError, IndexError):
            pass
    raise ValueError(f'No recognized text column. Available fields: {schema.names}; use --column')


def parquet_values(parquet, selected, batch_rows=32):
    """Keep projected struct scalars intact so parent nulls remain visible."""
    parts = [selected] if selected in parquet.schema_arrow.names else selected.split('.')
    for batch in parquet.iter_batches(batch_size=batch_rows, columns=[selected], use_threads=False):
        for scalar in batch.column(0):
            value = scalar.as_py()
            for component in parts[1:]:
                value = value.get(component) if isinstance(value, dict) else None
            yield value


def corpus_integrity(directory, records, stop_requested=None):
    groups = {key for _, _, key in records}
    return {
        'schema_version': 2, 'token_dtype': 'uint16', 'token_bytes': 2,
        'byte_order': sys.byteorder, 'eos_token_id': 257,
        'unique_document_groups': len(groups), 'document_records': len(records),
        'repeated_document_records': len(records) - len(groups),
        'artifacts': {name: {'bytes': (directory / name).stat().st_size,
                             'sha256': file_sha256(directory / name, stop_requested)}
                      for name in ('train.bin', 'val.bin')},
    }


def prepare(source, output, *, column=None, max_tokens=2_000_000,
            batch_rows=32, val_fraction=0.05, block_size=256, strip_html=False,
            seed=1337, stop_requested=None, progress_callback=None):
    source, output = Path(source).expanduser().resolve(), Path(output).expanduser().absolute()
    positive_integer(max_tokens, 'Token cap')
    positive_integer(batch_rows, 'Batch rows')
    validate_split_options(val_fraction, block_size, seed)
    check_cancelled(stop_requested)
    if not source.is_file():
        raise FileNotFoundError(f'Dataset must be an existing regular file: {source}')
    if output.is_symlink():
        raise FileExistsError(f'Output already exists: {output}')
    if output.exists():
        raise FileExistsError(f"Output already exists: {output}; choose a new directory")
    if pq is None:
        raise ImportError("Parquet input requires pyarrow")
    initial_stat = source.stat()
    output.parent.mkdir(parents=True, exist_ok=True)
    with pq.ParquetFile(source) as parquet, tempfile.TemporaryDirectory(prefix=".parquet-prep-", dir=output.parent) as temp:
        column = pick_column(parquet.schema_arrow, column)
        if not parquet.metadata.num_rows:
            raise ValueError(f'Empty dataset: {source}')
        work = Path(temp)
        stage = work / 'corpus'
        stage.mkdir()
        combined = work / "tokens.bin"
        records = []
        total = rows_scanned = documents = truncated = skipped = 0
        eos = np.array([257], dtype=np.uint16).tobytes()
        with combined.open("wb") as stream:
            for text in parquet_values(parquet, column, batch_rows):
                check_cancelled(stop_requested)
                rows_scanned += 1
                if strip_html and text:
                    text = plain_html(text)
                if not text or not text.strip():
                    skipped += 1
                    continue
                remaining = max_tokens - total
                if remaining < 2:
                    break
                raw, was_truncated = utf8_prefix(text, remaining - 1)
                if not raw:
                    break
                truncated += int(was_truncated)
                for offset in range(0, len(raw), 65536):
                    check_cancelled(stop_requested)
                    stream.write(np.frombuffer(raw[offset:offset + 65536], dtype=np.uint8).astype(np.uint16).tobytes())
                stream.write(eos)
                records.append((total, len(raw) + 1, document_key(text, stop_requested)))
                total += len(raw) + 1
                documents += 1
                if progress_callback:
                    progress_callback({'phase': 'reading', 'path': str(source), 'tokens': total,
                                       'documents': documents, 'rows_scanned': rows_scanned})
                if was_truncated:
                    break
                if total >= max_tokens - 1:
                    break
        current_stat = source.stat()
        if (initial_stat.st_size, initial_stat.st_mtime_ns, initial_stat.st_ino) != (current_stat.st_size, current_stat.st_mtime_ns, current_stat.st_ino):
            raise ValueError(f'Dataset changed during preparation: {source}')
        if progress_callback:
            progress_callback({'phase': 'splitting', 'tokens': total, 'documents': documents})
        n_train, n_val = split_documents(combined, stage, records, val_fraction=val_fraction,
                                        block_size=block_size, seed=seed, stop_requested=stop_requested)
        report = dict(source=str(source), column=column, tokenizer="Tensegrity bytes + EOS=257",
                      max_tokens=max_tokens, total_tokens=total, train_tokens=n_train,
                      val_tokens=n_val, rows_scanned=rows_scanned, documents=documents,
                      truncated_documents=truncated, block_size=block_size,
                      val_fraction=val_fraction, batch_rows=batch_rows, strip_html=strip_html,
                      split_method="document_hash_groups_v1", split_seed=seed,
                      skipped_documents=skipped, actual_val_fraction=n_val / total,
                      source_bytes=initial_stat.st_size, source_mtime_ns=initial_stat.st_mtime_ns,
                      **corpus_integrity(stage, records, stop_requested))
        atomic_json(stage / 'corpus.json', report)
        check_cancelled(stop_requested)
        if progress_callback:
            progress_callback({'phase': 'publishing', 'tokens': total, 'documents': documents})
        check_cancelled(stop_requested)
        publish_directory(stage, output)
    print(json.dumps(report, indent=2))
    print(f"Prepared corpus: {output}")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("parquet", type=Path)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--column", help="Default: first matching text/content/story/body column")
    parser.add_argument("--strip-html", action="store_true", help="Remove markup, scripts and styles from HTML text")
    parser.add_argument("--max-tokens", type=int, default=2_000_000,
                        help="Total byte tokens including EOS and validation; default 2M")
    parser.add_argument("--batch-rows", type=int, default=32)
    parser.add_argument("--val-fraction", type=float, default=0.05)
    parser.add_argument("--block-size", type=int, default=256)
    parser.add_argument("--seed", type=int, default=1337, help='Seed for deterministic whole-document validation')
    args = parser.parse_args()
    try:
        prepare(args.parquet, args.out_dir, column=args.column,
                max_tokens=args.max_tokens, batch_rows=args.batch_rows,
                val_fraction=args.val_fraction, block_size=args.block_size, strip_html=args.strip_html,
                seed=args.seed)
    except (ValueError, OSError, ImportError) + ((pa.ArrowException,) if pa is not None else ()) as error:
        parser.exit(1, f"Error: {error}\n")


if __name__ == "__main__":
    main()
