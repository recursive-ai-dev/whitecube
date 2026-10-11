#!/usr/bin/env python3
"""Embed a repository-owned UTF-8 source as a C string."""
from pathlib import Path
import json
import sys
source=Path(sys.argv[1]).read_text()
Path(sys.argv[2]).write_text('static const char cb_web_bootstrap[] =\n'+''.join(json.dumps(line,ensure_ascii=True)+'\n' for line in source.splitlines(keepends=True))+';\n')
