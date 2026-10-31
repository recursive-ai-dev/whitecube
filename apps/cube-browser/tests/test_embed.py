#!/usr/bin/env python3
"""Verify exact bytes after the real C compiler has processed generated source."""
import importlib.util
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('embed', Path(__file__).resolve().parents[1]/'scripts/embed.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class Embed(unittest.TestCase):
    def test_compiler_roundtrip(self):
        source = ('const text = "??= ??/ ??\' ??( ??) ??! ??< ??> ??- \\n café 日本語 😀";\r\n'.encode()
                  + bytes(range(1, 32)) + b'0123456789\\\\\"\n')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'test.c').write_text('#include <stdio.h>\n'+module.embed(source)+'int main(void){return fwrite(cb_web_bootstrap,1,sizeof cb_web_bootstrap-1,stdout)!=sizeof cb_web_bootstrap-1;}\n')
            subprocess.run(shlex.split(os.environ.get('CC','cc'))+['-std=c11','-trigraphs','-Wall','-Werror',str(root/'test.c'),'-o',str(root/'test')],check=True)
            self.assertEqual(subprocess.check_output([str(root/'test')]),source)
    def test_empty_and_invalid(self):
        self.assertIn('""',module.embed(b''))
        for source in [b'x\x00y',b'\xff']:
            with self.assertRaises((ValueError,UnicodeError)):module.embed(source)

if __name__=='__main__':unittest.main()
