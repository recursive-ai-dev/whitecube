#!/usr/bin/env python3
"""Compile the vendored MPL-2.0 Public Suffix List into sorted ASCII rules."""
import hashlib
from pathlib import Path
import sys
source = Path(sys.argv[1]).read_bytes()
rules = set()
for line in source.decode().splitlines():
    line = line.strip()
    if not line or line.startswith('//'):
        continue
    prefix = '!' if line.startswith('!') else '*.' if line.startswith('*.') else ''
    rules.add(prefix + line[len(prefix):].encode('idna').decode().lower())
Path(sys.argv[2]).write_text('/* Generated from publicsuffix.org, MPL-2.0. SHA256: '+hashlib.sha256(source).hexdigest()+' */\n'
                           'static const char *const suffix_rules[] = {\n' + ''.join('    "'+rule+'",\n' for rule in sorted(rules))+'};\n')
