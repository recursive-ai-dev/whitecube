#!/usr/bin/env python3
"""Verify checksums and actual relocated workers from a release tarball."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

archive=Path(sys.argv[1]).resolve()
with tempfile.TemporaryDirectory(prefix='cube-relocated-') as temp:
    base=Path(temp)
    with tarfile.open(archive) as tar:tar.extractall(base,filter='data')
    roots=list(base.iterdir());assert len(roots)==1
    root=roots[0];root.rename(base/'relocated app');root=base/'relocated app'
    for line in (root/'SHA256SUMS').read_text().splitlines():
        digest,relative=line.split('  ',1);p=root/relative
        assert p.resolve().is_relative_to(root.resolve())
        assert hashlib.sha256(p.read_bytes()).hexdigest()==digest,relative
    data=json.loads((root/'usr/share/doc/cube-browser/build.json').read_text())
    env=dict(os.environ,HOME=str(base/'home'),XDG_CONFIG_HOME=str(base/'config'),XDG_DATA_HOME=str(base/'data'),XDG_STATE_HOME=str(base/'state'))
    for name in ('LD_LIBRARY_PATH','LD_PRELOAD','DISPLAY','WAYLAND_DISPLAY','CUBE_BROWSER_BUILD'):env.pop(name,None)
    def run(*args):return subprocess.check_output([root/'cube-browser',*args],env=env,cwd=base,text=True,stderr=subprocess.STDOUT,timeout=30)
    assert 'cube-browser '+data['version'] in run('--version')
    assert 'cube-renderd: OK' in run('--check')
    page=base/'input.html';page.write_text('<p id="out">initial</p><script>document.getElementById("out").textContent="ARCHIVE_JS_OK"</script>')
    # Script preference is enabled explicitly in this isolated profile.
    config=base/'config/cube-browser';config.mkdir(parents=True,exist_ok=True);(config/'config').write_text('javascript=true\n')
    assert 'ARCHIVE_JS_OK' in run('--dump',page.as_uri())
    image=base/'output.png';run('--render','cube:home',str(image),'800x600')
    assert image.read_bytes()[:8]==b'\x89PNG\r\n\x1a\n'
print('tar: relocated executable, worker sandbox, JavaScript, image export and complete SHA-256 manifest passed')
