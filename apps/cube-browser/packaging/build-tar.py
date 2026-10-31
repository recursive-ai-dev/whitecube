#!/usr/bin/env python3
"""Create a relocatable deterministic Linux distribution from the validated deb."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile

ROOT=Path(__file__).resolve().parents[1]

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('deb',type=Path)
    parser.add_argument('--output',type=Path,default=ROOT/'dist')
    args=parser.parse_args()
    fields=subprocess.check_output(['dpkg-deb','-f',args.deb,'Package','Version','Architecture'],text=True)
    fields=dict(line.split(': ',1) for line in fields.splitlines())
    version=(ROOT/'VERSION').read_text().strip()
    if fields!={'Package':'cube-browser','Version':version,'Architecture':'amd64'}:
        parser.error('archive package, version or architecture does not match this release')
    args.output.mkdir(parents=True,exist_ok=True)
    name=f'cube-browser-{version}-linux-amd64'
    archive=args.output/(name+'.tar.xz')
    with tempfile.TemporaryDirectory(prefix='cube-tar-') as temp:
        tree=Path(temp)/name;tree.mkdir()
        subprocess.run(['dpkg-deb','--extract',args.deb,tree],check=True)
        for binary in ('cube-browser','cube-renderd','cube-netd'):
            p=tree/'usr/lib/cube-browser'/binary
            if not p.is_file() or not os.access(p,os.X_OK):raise ValueError('missing executable '+binary)
        metadata=tree/'usr/share/doc/cube-browser/build.json'
        data=json.loads(metadata.read_text())
        if data['version']!=version or data['architecture']!='amd64':raise ValueError('mismatched provenance')
        for p in tree.rglob('*'):
            if p.is_symlink() and (p.readlink().is_absolute() or not p.resolve().is_relative_to(tree.resolve())):raise ValueError('unsafe archive symlink')
            if not (p.is_file() or p.is_dir() or p.is_symlink()):raise ValueError('unsupported archive entry')
        (tree/'cube-browser').write_text('#!/bin/sh\nset -eu\napp_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\nexec "$app_dir/usr/bin/cube-browser" "$@"\n')
        (tree/'cube-browser').chmod(0o755)
        sums=''.join(f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(tree).as_posix()}\n' for p in sorted(tree.rglob('*')) if p.is_file() and not p.is_symlink())
        (tree/'SHA256SUMS').write_text(sums)
        with tempfile.NamedTemporaryFile(dir=args.output,prefix='.'+name,suffix='.tmp',delete=False) as out:partial=Path(out.name)
        try:
            with tarfile.open(partial,'w:xz',format=tarfile.USTAR_FORMAT) as tar:
                for p in [tree,*sorted(tree.rglob('*'))]:
                    info=tar.gettarinfo(p,arcname=p.relative_to(tree.parent).as_posix())
                    info.uid=info.gid=0;info.uname=info.gname='';info.mtime=data['source_date_epoch']
                    info.mode=0o755 if p.is_dir() or os.access(p,os.X_OK) and not p.is_symlink() else 0o777 if p.is_symlink() else 0o644
                    if info.isfile():
                        with p.open('rb') as file:tar.addfile(info,file)
                    else:tar.addfile(info)
            partial.chmod(0o644);partial.replace(archive)
        finally:partial.unlink(missing_ok=True)
    digest=hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum=archive.with_suffix('.xz.sha256')
    with tempfile.NamedTemporaryFile(mode='w',dir=args.output,prefix='.'+checksum.name,delete=False) as out:
        out.write(f'{digest}  {archive.name}\n');partial=Path(out.name)
    partial.chmod(0o644);partial.replace(checksum)
    print(archive)

if __name__=='__main__':main()
