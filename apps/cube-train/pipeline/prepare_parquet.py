"""Stream a local Parquet text column into a capped Tensegrity byte-token corpus."""
import argparse
import json
import math
from pathlib import Path
import shutil
import tempfile
from html.parser import HTMLParser
import re

from corpus_split import document_key, split_documents

import numpy as np
try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:
    pa = pq = None


class PlainHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.ignored = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.ignored += 1
        elif tag in ("p", "div", "br", "li", "tr", "h1", "h2", "h3") and not self.ignored:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.ignored = max(0, self.ignored - 1)
        elif tag in ("p", "div", "li", "tr", "td", "h1", "h2", "h3") and not self.ignored:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.ignored:
            self.parts.append(data)


def plain_html(text):
    parser = PlainHTML()
    parser.feed(text)
    parser.close()
    lines = [re.sub(r"\s+", " ", line).strip() for line in "".join(parser.parts).splitlines()]
    return "\n".join(line for line in lines if line)


def prepare(source, output, *, column=None, max_tokens=2_000_000,
            batch_rows=32, val_fraction=0.05, block_size=256, strip_html=False):
    source, output = Path(source).resolve(), Path(output).resolve()
    if max_tokens < 1 or batch_rows < 1 or block_size < 1:
        raise ValueError("Token cap, batch rows and block size must be positive")
    if not math.isfinite(val_fraction) or not 0 < val_fraction < 1:
        raise ValueError("Validation fraction must be between 0 and 1")
    if output.exists():
        raise FileExistsError(f"Output already exists: {output}; choose a new directory")
    if pq is None:
        raise ImportError("Parquet input requires pyarrow")
    parquet = pq.ParquetFile(source)
    schema = parquet.schema_arrow
    if column is None:
        column = next((name for name in ("text", "content", "story", "body")
                       if name in schema.names), None)
    path = column.split(".") if column else []
    if not path or path[0] not in schema.names:
        raise ValueError(f"Choose --column from: {', '.join(schema.names)}")
    field_type = schema.field(path[0]).type
    for component in path[1:]:
        if not pa.types.is_struct(field_type) or component not in [f.name for f in field_type]:
            raise ValueError(f"Unknown nested column: {column}")
        field_type = field_type.field(component).type
    if pa.types.is_dictionary(field_type):
        field_type = field_type.value_type
    if not (pa.types.is_string(field_type) or pa.types.is_large_string(field_type)):
        raise ValueError(f"Column {column!r} must contain strings, got {field_type}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".parquet-prep-", dir=output.parent) as temp:
        work = Path(temp)
        combined = work / "tokens.bin"
        records = []
        total = rows_scanned = documents = truncated = 0
        eos = np.array([257], dtype=np.uint16).tobytes()
        with combined.open("wb") as stream:
            for batch in parquet.iter_batches(batch_size=batch_rows, columns=[column], use_threads=False):
                values = batch.column(0)
                for component in path[1:]:
                    values = values.field(component)
                for scalar in values:
                    if total >= max_tokens:
                        break
                    rows_scanned += 1
                    text = scalar.as_py()
                    if not text:
                        continue
                    if strip_html:
                        text = plain_html(text)
                        if not text:
                            continue
                    remaining = max_tokens - total
                    if remaining < 2:
                        break
                    # Slice characters first to bound UTF-8 encoding work for long rows.
                    prefix = text[:remaining - 1]
                    encoded = prefix.encode("utf-8")
                    raw = encoded[:remaining - 1]
                    truncated += int(len(prefix) < len(text) or len(encoded) > len(raw))
                    # Every byte is a token for this model; reserve one token for EOS.
                    stream.write(np.frombuffer(raw, dtype=np.uint8).astype(np.uint16).tobytes())
                    stream.write(eos)
                    records.append((total, len(raw) + 1, document_key(text)))
                    total += len(raw) + 1
                    documents += 1
                if total >= max_tokens - 1:
                    break
        n_train, n_val = split_documents(combined, work, records,
                                          val_fraction=val_fraction, block_size=block_size)
        report = dict(source=str(source), column=column, tokenizer="Tensegrity bytes + EOS=257",
                      max_tokens=max_tokens, total_tokens=total, train_tokens=n_train,
                      val_tokens=n_val, rows_scanned=rows_scanned, documents=documents,
                      truncated_documents=truncated, block_size=block_size,
                      val_fraction=val_fraction, batch_rows=batch_rows, strip_html=strip_html,
                      split_method="document_hash_groups_v1", split_seed=1337)
        (work / "corpus.json").write_text(json.dumps(report, indent=2) + "\n")
        # mkdir refuses an existing destination, including one created during extraction.
        output.mkdir()
        for name in ("train.bin", "val.bin", "corpus.json"):
            shutil.move(str(work / name), str(output / name))
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
    args = parser.parse_args()
    try:
        prepare(args.parquet, args.out_dir, column=args.column,
                max_tokens=args.max_tokens, batch_rows=args.batch_rows,
                val_fraction=args.val_fraction, block_size=args.block_size, strip_html=args.strip_html)
    except (ValueError, OSError, ImportError) + ((pa.ArrowException,) if pa is not None else ()) as error:
        parser.exit(1, f"Error: {error}\n")


if __name__ == "__main__":
    main()
