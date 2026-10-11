#!/usr/bin/env python3
"""Keep C and command-line version identifiers derived from VERSION."""
from pathlib import Path
import re
import sys

root = Path(__file__).resolve().parents[1]
version = (root / 'VERSION').read_text().strip()
if not re.fullmatch(r'\d+\.\d+\.\d+(?:-[a-z0-9.]+)?', version):
    raise SystemExit('VERSION must be a semantic version')
series = '.'.join(version.split('.')[:2])
header = ('#ifndef CB_VERSION_H\n#define CB_VERSION_H\n'
          '/* Generated from VERSION by scripts/version.py. */\n'
          f'#define CB_VERSION "{version}"\n#define CB_VERSION_SERIES "{series}"\n#endif\n')
target = root / 'src/version.h'
if '--check' in sys.argv:
    if not target.exists() or target.read_text() != header:
        raise SystemExit('Run python3 scripts/version.py to synchronize VERSION')
elif not target.exists() or target.read_text() != header:
    target.write_text(header)
