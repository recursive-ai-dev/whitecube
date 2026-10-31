#!/usr/bin/env python3
"""Exercise production deadlines and recovery through the real broker/renderer."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_subresources import server, solid_png

ROOT = Path(__file__).resolve().parents[1]
BROWSER = Path(os.environ.get('CUBE_BROWSER_BUILD', ROOT/'build'))/'cube-browser'


class ResourceDeadlines(unittest.TestCase):
    def test_timed_out_script_unblocks_queue_and_later_image_batch(self):
        html = b'''<p id="out">initial</p><script src="/slow.js"></script><script>
        const out=document.getElementById('out');out.textContent='TIMEOUT_SURVIVED';
        const image=new Image();image.onload=()=>out.textContent+=' RECOVERED';
        image.onerror=()=>out.textContent+=' FAILED';image.src='/late.png';
        </script>'''
        routes = {'/page': (200, 'text/html', html, 0),
                  '/slow.js': (200, 'text/javascript', b'document.title="STALE";', 22),
                  '/late.png': (200, 'image/png', solid_png((36, 147, 188)), 0)}
        with tempfile.TemporaryDirectory(prefix='cube-deadlines-') as temp:
            root = Path(temp)
            env = dict(os.environ, HOME=temp, XDG_CONFIG_HOME=str(root/'config'),
                       XDG_DATA_HOME=str(root/'data'), XDG_STATE_HOME=str(root/'state'))
            with server(routes) as (url, requests, _):
                result = subprocess.run([str(BROWSER), '--console', '--dump', url+'/page'],
                                        env=env, capture_output=True, text=True, timeout=32)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('TIMEOUT_SURVIVED RECOVERED', result.stdout)
                self.assertEqual(requests.count('/late.png'), 1)
                self.assertNotIn('STALE', result.stderr)


if __name__ == '__main__':
    unittest.main()
