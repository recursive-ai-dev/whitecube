#!/usr/bin/env python3
"""Embed exact UTF-8 bytes without trigraphs or ambiguous C escapes."""
from pathlib import Path
import sys

def embed(source: bytes) -> str:
    # Fixed-width octal escapes cannot consume a following digit, form a C
    # trigraph, or depend on the compiler's source/execution character set.
    if b"\0" in source:
        raise ValueError("bootstrap source contains a NUL byte")
    source.decode("utf-8")
    chunks = [source[i:i+64] for i in range(0, len(source), 64)] or [b""]
    return "static const char cb_web_bootstrap[] =\n" + "".join(
        '\"' + "".join(f"\\{byte:03o}" for byte in chunk) + '\"\n' for chunk in chunks
    ) + ";\n"

if __name__ == "__main__":
    Path(sys.argv[2]).write_text(embed(Path(sys.argv[1]).read_bytes()), encoding="ascii")
