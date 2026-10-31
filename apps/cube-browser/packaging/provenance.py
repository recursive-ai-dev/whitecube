"""Deterministic source manifest for shipped browser artifacts."""
import hashlib
import json
import shlex
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]

def manifest():
    paths=[]
    for name in ('src','scripts','packaging','tests','third_party'):
        paths.extend(p for p in (ROOT/name).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc')
    paths.extend(ROOT/name for name in ('VERSION','meson.build','meson_options.txt','BUILD.sh','cube-browser.sh','cube-browser.svg','README.md','COMPATIBILITY.md','RELEASE.md','VALIDATION.md','TODO.md') if (ROOT/name).is_file())
    return {p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}

def write(path,epoch,arch,env):
    sources=manifest()
    def git(*arguments):
        try:
            result=subprocess.run(['git',*arguments],cwd=ROOT,env=env,text=True,capture_output=True,timeout=30)
        except (OSError,subprocess.TimeoutExpired):
            return None
        return result.stdout.strip() if result.returncode==0 else None
    commit=git('rev-parse','HEAD')
    status=git('status','--porcelain','--untracked-files=normal','--','.')
    source={'commit':commit,'prefix':git('rev-parse','--show-prefix'),
            'dirty':bool(status) if status is not None else None}
    versions={}
    for name,command in [('compiler',['cc','--version']),('meson',['meson','--version']),('linker',['ld','--version'])]:
        versions[name]=subprocess.check_output(command,text=True,env=env).splitlines()[0]
    data={'format':1,'version':(ROOT/'VERSION').read_text().strip(),'architecture':arch,'source_date_epoch':int(epoch),
          'source_sha256':hashlib.sha256(json.dumps(sources,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
          'sources':sources,'source':source,'toolchain':versions,'build_options':{'buildtype':'release','lto':True,'private_libdir':'$ORIGIN/lib','cflags':[flag.replace(str(ROOT),'<source-root>') for flag in shlex.split(env.get('CFLAGS',''))]},'dependencies':{'libwebp':'1.6.0','QuickJS':'2026-06-04','Mbed TLS':'3.6.7','Cairo':'1.18.4','SQLite':'3.53.4','libjpeg-turbo':'2.1.5'}}
    path.write_text(json.dumps(data,sort_keys=True,indent=2)+'\n')
