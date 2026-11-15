"""Cube Strap engine: turns a build plan into a deb-strap run and a finished whitecube.

Two kinds of cube:
  chroot  deb-strap exports a rootfs tarball (no kernel, no bootloader), which
          chrootable-manager imports as a workspace; it then shows in Cube Chrootables.
  vm      deb-strap builds a raw UEFI disk image (EFI + ext4 root, GRUB, cloud kernel),
          which import_vm stores sparsely under ~/.whitecube/images/ and QEMU + OVMF boots.

The build itself is one privileged step (cube-strap-build, through sudo). Everything
else runs as the desktop user. No GTK in this module, so it is testable on its own.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

HERE = Path(__file__).resolve().parent
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z')
USERNAME = re.compile(r'[a-z_][a-z0-9_-]{0,31}\Z')
HOSTNAME = re.compile(r'[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\Z')
MIRROR = re.compile(r'https?://[^\s"#]+\Z')
KINDS = ('chroot', 'vm')
LIFECYCLES = ('persistent', 'ephemeral')
SUITES = (
    ('trixie', 'Debian 13 Trixie', 'stable'),
    ('forky', 'Debian 14 Forky', 'testing'),
    ('sid', 'Debian Sid', 'unstable'),
    ('bookworm', 'Debian 12 Bookworm', 'oldstable'),
)
# key: (title, detail, kinds, deb-strap settings)
RECIPES = {
    'minimal': ('Minimal', 'Debian minbase and a user. The smallest cube.', KINDS,
                {'bootstrap_variant': 'minbase', 'opt_packages': 'none', 'desktop': '1', 'enable_ssh': 'n'}),
    'tools': ('Everyday tools', 'Command-line essentials: editors, curl, htop, git.', KINDS,
              {'bootstrap_variant': 'standard', 'opt_packages': 'cli', 'desktop': '1', 'enable_ssh': 'n'}),
    'developer': ('Developer', 'Compilers, build tools and the everyday tools.', KINDS,
                  {'bootstrap_variant': 'standard', 'opt_packages': 'cli,dev', 'desktop': '1', 'enable_ssh': 'n'}),
    'server': ('Server', 'Tools plus OpenSSH; forward port 2222 to reach it.', ('vm',),
               {'bootstrap_variant': 'standard', 'opt_packages': 'cli', 'desktop': '1', 'enable_ssh': 'y'}),
    'desktop': ('Desktop', 'XFCE with LightDM, booted in a QEMU window.', ('vm',),
                {'bootstrap_variant': 'standard', 'opt_packages': 'cli', 'desktop': '4', 'display_manager': '4',
                 'gpu_driver': '1'}),
}
DEFAULT_SIZE_GIB = {'chroot': 4, 'vm': 12}
MIN_SIZE_GIB = {'chroot': 1, 'vm': 4}
MAX_SIZE_GIB = 1024
STATE = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state') / 'cube-strap'
CACHE = Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache') / 'cube-strap'
# debootstrap needs a filesystem that allows device nodes; /home is often nodev.
# cube-strap-build bootstraps under ROOT_WORK/<build id> and removes it afterwards.
ROOT_WORK = Path('/var/lib/cube-strap')
BUILD_ID = re.compile(r'build-[A-Za-z0-9_]{1,32}\Z')


@dataclass
class Plan:
    name: str = ''
    kind: str = 'chroot'
    suite: str = 'trixie'
    recipe: str = 'minimal'
    size_gib: int = 4
    username: str = 'cube'
    password: str = field(default='', repr=False)
    hostname: str = ''
    lifecycle: str = 'persistent'
    mirror: str = 'https://deb.debian.org/debian'

    def host(self) -> str:
        return self.hostname or hostname_for(self.name)

    def public(self) -> dict:
        """Everything except the password: safe for drafts and history."""
        values = asdict(self)
        values.pop('password')
        return values


def hostname_for(name: str) -> str:
    text = re.sub(r'[^a-z0-9-]+', '-', name.lower()).strip('-')[:63].strip('-')
    return text or 'cube'


def recipes_for(kind: str) -> list[str]:
    return [key for key, (_t, _d, kinds, _s) in RECIPES.items() if kind in kinds]


def problems(plan: Plan, taken=()) -> list[str]:
    """Every reason the plan cannot be built, in form order. Empty means ready."""
    found = []
    if not NAME.match(plan.name):
        found.append('Names use 1-64 letters, digits, dots, underscores or hyphens and start with a letter or digit.')
    elif plan.name in set(taken):
        found.append(f'A cube called {plan.name} already exists.')
    if plan.kind not in KINDS:
        found.append('Choose a chroot workspace or a VM.')
    if plan.suite not in {key for key, *_ in SUITES}:
        found.append('Choose a Debian release.')
    if plan.recipe not in RECIPES or plan.kind not in RECIPES[plan.recipe][2]:
        found.append('That recipe is not available for this kind of cube.')
    low = MIN_SIZE_GIB.get(plan.kind, 1)
    if not isinstance(plan.size_gib, int) or not low <= plan.size_gib <= MAX_SIZE_GIB:
        found.append(f'Choose a size from {low} to {MAX_SIZE_GIB} GiB.')
    if not USERNAME.match(plan.username) or plan.username == 'root':
        found.append('User names are lowercase letters, digits, - or _, starting with a letter (not root).')
    if plan.hostname and not HOSTNAME.match(plan.hostname):
        found.append('Host names are lowercase letters, digits and hyphens.')
    if len(plan.password) < 4:
        found.append('Choose a password of at least 4 characters for the cube user.')
    elif plan.password != plan.password.strip() or any(c in plan.password for c in '"#:\\') \
            or not plan.password.isprintable():
        found.append('Passwords cannot contain " # : \\, control characters, or start or end with a space.')
    if plan.lifecycle not in LIFECYCLES:
        found.append('Choose persistent or ephemeral sessions.')
    if not MIRROR.match(plan.mirror):
        found.append('The mirror must be an http:// or https:// address.')
    return found


def config_text(plan: Plan, workdir: Path) -> str:
    """The deb-strap configuration for this plan. Contains the password: write it 0600."""
    _title, _detail, _kinds, recipe = RECIPES[plan.recipe]
    values = {
        'target_suite': plan.suite, 'target_arch': 'amd64', 'bootstrap_engine': 'debootstrap',
        'mirror_choice': '1' if plan.mirror == 'https://deb.debian.org/debian' else '3',
        'custom_mirror': plan.mirror, 'use_deb822': 'y',
        'enable_contrib': 'n', 'enable_nonfree': 'n', 'enable_nonfree_firmware': 'n',
        'enable_security': 'n' if plan.suite == 'sid' else 'y',
        'enable_updates': 'n' if plan.suite == 'sid' else 'y', 'enable_backports': 'n',
        'hostname': plan.host(), 'timezone': 'UTC', 'locale': 'en_US.UTF-8', 'keymap': 'us',
        'username': plan.username, 'user_shell': '/bin/bash', 'sudo_mode': '1',
        'user_password': plan.password, 'init_system': '1', 'secure_boot': 'n',
        'display_manager': '7', 'gpu_driver': '8', 'ssh_permit_root': 'n',
    }
    if plan.kind == 'chroot':
        # A workspace shares the host kernel and network: no kernel, bootloader or network daemon.
        values.update({'target_mode': 'tar', 'target_dir': str(root_workdir(workdir) / 'rootfs'),
                       'tar_output': str(workdir / f'{plan.name}.tar.gz'),
                       'kernel': '7', 'bootloader': '5', 'network_manager': '2'})
    else:
        values.update({'target_mode': 'image', 'image_file': str(workdir / f'{plan.name}.img'),
                       'image_size': f'{plan.size_gib}G', 'layout': '3', 'fs_root': 'ext4',
                       'size_efi': '512M', 'size_root': '0', 'user_addon': 'n', 'bios_compat': 'n',
                       'use_lvm': 'n', 'use_raid': 'n', 'encrypt_root': 'n',
                       'kernel': '3', 'bootloader': '1', 'network_manager': '2'})
    values.update(recipe)
    lines = ['# Written by Cube Strap; deleted when the build ends.']
    for key, value in values.items():
        value = str(value)
        if any(c in value for c in '\n\r"'):
            raise ValueError(f'unsafe value for {key}')
        lines.append(f'{key} = "{value}"')
    return '\n'.join(lines) + '\n'


def root_workdir(workdir: Path) -> Path:
    """Where cube-strap-build bootstraps for this build (the same rule lives in the helper)."""
    if not BUILD_ID.match(workdir.name):
        raise ValueError(f'not a Cube Strap build directory: {workdir}')
    return ROOT_WORK / workdir.name


def build_params(plan: Plan, workdir: Path) -> dict:
    """The root broker derives trusted executables, configuration and owner itself."""
    return {'workdir': str(workdir), 'kind': plan.kind}


OVMF_PAIRS = (
    ('/usr/share/OVMF/OVMF_CODE_4M.fd', '/usr/share/OVMF/OVMF_VARS_4M.fd'),
    ('/usr/share/OVMF/OVMF_CODE.fd', '/usr/share/OVMF/OVMF_VARS.fd'),
    ('/usr/share/ovmf/OVMF.fd', None),
)


def import_args(plan: Plan, archive: Path) -> list[str]:
    """chrootable-manager arguments that turn the tarball into a workspace."""
    return ['--import', plan.name, str(archive), str(plan.size_gib * 1024), plan.lifecycle]


def valid_vm_name(name: str) -> str:
    """Validate a VM image name against path traversal and QEMU option syntax."""
    import re
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', name):
        raise ValueError(f'Invalid VM name: {name!r}')
    if '..' in name or ',' in name or ':' in name:
        raise ValueError(f'Invalid VM name: {name!r}')
    return name


def qemu_command() -> str | None:
    return shutil.which('qemu-system-x86_64')


def ovmf_pair() -> tuple[Path, Path | None] | None:
    """Locate UEFI firmware (OVMF code and optional NVRAM variables template)."""
    custom_code = os.environ.get('WHITECUBE_OVMF_CODE', '').strip()
    custom_vars = os.environ.get('WHITECUBE_OVMF_VARS', '').strip()
    if custom_code:
        code = Path(custom_code)
        if code.is_file() and ',' not in str(code):
            vars_tmpl = Path(custom_vars) if custom_vars else None
            if vars_tmpl is None or (vars_tmpl.is_file() and ',' not in str(vars_tmpl)):
                return code, vars_tmpl
        return None
    for code_raw, vars_raw in OVMF_PAIRS:
        code = Path(code_raw)
        if not code.is_file() or ',' in str(code):
            continue
        if vars_raw is None:
            return code, None
        vars_tmpl = Path(vars_raw)
        if vars_tmpl.is_file() and ',' not in str(vars_tmpl):
            return code, vars_tmpl
    return None


def vm_boot_missing() -> list[tuple[str, str]]:
    """Return missing runtime prerequisites as (description, debian_package)."""
    missing: list[tuple[str, str]] = []
    if not qemu_command():
        missing.append(('QEMU x86_64 emulator', 'qemu-system-x86'))
    if not ovmf_pair():
        missing.append(('OVMF UEFI firmware', 'ovmf'))
    return missing


def vm_can_kvm() -> bool:
    return os.path.exists('/dev/kvm') and os.access('/dev/kvm', os.R_OK | os.W_OK)


def whitecube_vm_command() -> str | None:
    """Locate the whitecube-vm CLI (checkout bin/ wins over PATH)."""
    local = Path(__file__).resolve().parents[2] / 'bin/whitecube-vm'
    if local.is_file() and os.access(local, os.X_OK):
        return str(local)
    return shutil.which('whitecube-vm')


def whitecube_store() -> Path:
    env = os.environ.get('WHITECUBE_HOME')
    return Path(env).expanduser() if env else Path.home() / '.whitecube'


def vm_dir(name: str) -> Path:
    valid_vm_name(name)
    store = whitecube_store()
    images = store / 'images'
    target = images / name
    for component in (store, images, target):
        if component.is_symlink():
            raise OSError(f'Refusing symlinked VM store path: {component}')
    return target


def sparsify(path: Path) -> None:
    """Punch zero-block holes out of a sparse raw VM disk image."""
    if shutil.which('fallocate') and path.is_file():
        subprocess.run(['fallocate', '--dig-holes', str(path)], capture_output=True, timeout=1800)


def _copy_sparse(src: Path, dst: Path) -> None:
    """Copy a VM disk image while preserving sparse zero holes."""
    import errno
    chunk_size = 1024 * 1024
    zeros = b'\x00' * chunk_size
    with open(src, 'rb') as source:
        sfd = source.fileno()
        total_size = os.fstat(sfd).st_size
        dfd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.ftruncate(dfd, total_size)
            if hasattr(os, 'SEEK_DATA') and hasattr(os, 'SEEK_HOLE'):
                try:
                    pos = 0
                    while pos < total_size:
                        data_start = os.lseek(sfd, pos, os.SEEK_DATA)
                        hole_start = os.lseek(sfd, data_start, os.SEEK_HOLE)
                        offset = data_start
                        while offset < hole_start:
                            buf = os.pread(sfd, min(chunk_size, hole_start - offset), offset)
                            if not buf:
                                break
                            if not (len(buf) == chunk_size and buf == zeros) and any(buf):
                                written = 0
                                while written < len(buf):
                                    written += os.pwrite(dfd, buf[written:], offset + written)
                            offset += len(buf)
                        pos = hole_start
                    os.fsync(dfd)
                    return
                except OSError as exc:
                    if exc.errno == errno.ENXIO:
                        os.fsync(dfd)
                        return
                    os.lseek(sfd, 0, os.SEEK_SET)
            offset = 0
            while offset < total_size:
                buf = source.read(min(chunk_size, total_size - offset))
                if not buf:
                    break
                if not (len(buf) == chunk_size and buf == zeros) and any(buf):
                    written = 0
                    while written < len(buf):
                        written += os.pwrite(dfd, buf[written:], offset + written)
                offset += len(buf)
            os.fsync(dfd)
        finally:
            os.close(dfd)


def ensure_vm_vars(vars_path: Path, vars_template: Path) -> Path:
    """Create a per-VM writable UEFI NVRAM copy when absent."""
    if vars_path.is_symlink():
        raise OSError(f'Refusing symlinked UEFI vars path: {vars_path}')
    if not vars_path.is_file():
        tmp = vars_path.with_name(f'.vars.{os.getpid()}.tmp')
        if tmp.is_symlink():
            tmp.unlink()
        shutil.copyfile(vars_template, tmp)
        os.chmod(tmp, 0o600)
        os.replace(tmp, vars_path)
    return vars_path


def _open_vm_lock(target: Path):
    import fcntl
    lock_path = target / 'vm.lock'
    if lock_path.is_symlink():
        raise OSError(f'Refusing symlinked VM lock: {lock_path}')
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        os.close(fd)
        raise
    return fd


def vm_is_running(name: str) -> bool:
    """Return True if the VM runtime lock is currently held."""
    import fcntl
    try:
        target = vm_dir(name)
    except (OSError, ValueError):
        return False
    lock_path = target / 'vm.lock'
    if not lock_path.exists():
        return False
    if lock_path.is_symlink():
        return False
    try:
        fd = os.open(lock_path, os.O_RDWR)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def vm_meta(name: str) -> dict:
    """Load normalized metadata for a stored VM."""
    import json
    target = vm_dir(name)
    meta_path = target / 'meta.json'
    if not meta_path.is_file() or meta_path.is_symlink():
        raise FileNotFoundError(f'VM {name!r} does not exist in {whitecube_store() / "images"}')
    raw = json.loads(meta_path.read_text())
    disk_name = raw.get('disk') or ('disk.qcow2' if (target / 'disk.qcow2').is_file() else 'disk.img')
    if '/' in disk_name or disk_name in ('', '.', '..'):
        raise ValueError(f'Invalid disk name in VM metadata: {disk_name!r}')
    disk_path = target / disk_name
    if disk_path.is_symlink():
        raise OSError(f'Refusing symlinked VM disk path: {disk_path}')
    fmt = raw.get('format') or ('qcow2' if disk_path.suffix.lower() == '.qcow2' else 'raw')
    return {
        **raw,
        'name': name,
        'disk': disk_name,
        'disk_path': str(disk_path),
        'format': fmt if fmt in ('raw', 'qcow2') else 'raw',
        'memory_mib': int(raw.get('memory_mib') or 2048),
        'cpus': int(raw.get('cpus') or 2),
    }


def _write_vm_meta(target: Path, meta: dict) -> None:
    """Atomically write VM meta.json with private permissions."""
    import json
    tmp_meta = target / f'.meta.{os.getpid()}.json.tmp'
    try:
        fd = os.open(tmp_meta, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as out:
            json.dump(meta, out, indent=2)
            out.write('\n')
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp_meta, target / 'meta.json')
    finally:
        tmp_meta.unlink(missing_ok=True)


def import_vm(name: str, image: Path, *, suite: str = '', arch: str = 'amd64',
              recipe: str = '', memory_mib: int = 2048, cpus: int = 2,
              disk_format: str | None = None) -> dict:
    """Import a raw or qcow2 disk image into ~/.whitecube/images/<name>."""
    from datetime import datetime, timezone
    valid_vm_name(name)
    source = Path(image)
    if not source.is_file():
        raise FileNotFoundError(f'VM disk image not found: {source}')
    if source.stat().st_size <= 0:
        raise ValueError(f'VM disk image is empty: {source}')
    fmt = disk_format or ('qcow2' if source.suffix.lower() == '.qcow2' else 'raw')
    if fmt not in ('raw', 'qcow2'):
        raise ValueError(f'Unsupported VM disk format: {fmt!r}')
    mem = max(512, min(int(memory_mib), 131072))
    smp = max(1, min(int(cpus), 64))

    store = whitecube_store()
    images = store / 'images'
    target = vm_dir(name)
    store.mkdir(mode=0o700, exist_ok=True)
    images.mkdir(mode=0o700, exist_ok=True)
    target.mkdir(mode=0o700, exist_ok=True)
    os.chmod(target, 0o700)

    lock_fd = _open_vm_lock(target)
    try:
        disk_name = 'disk.qcow2' if fmt == 'qcow2' else 'disk.img'
        disk_path = target / disk_name
        tmp_disk = target / f'.{disk_name}.{os.getpid()}.tmp'
        try:
            _copy_sparse(source, tmp_disk)
            if fmt == 'raw':
                sparsify(tmp_disk)
            os.chmod(tmp_disk, 0o600)
            os.replace(tmp_disk, disk_path)
        finally:
            tmp_disk.unlink(missing_ok=True)

        ovmf = ovmf_pair()
        if ovmf and ovmf[1] is not None:
            vars_path = target / 'vars.fd'
            vars_path.unlink(missing_ok=True)
            ensure_vm_vars(vars_path, ovmf[1])

        st = disk_path.stat()
        meta = {
            'schema': 1,
            'name': name,
            'type': 'ext4',
            'disk': disk_name,
            'format': fmt,
            'uefi': True,
            'memory_mib': mem,
            'cpus': smp,
            'suite': suite,
            'arch': arch,
            'recipe': recipe,
            'size_bytes': st.st_size,
            'created_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        }
        _write_vm_meta(target, meta)
        return meta
    finally:
        os.close(lock_fd)


def clone_vm(src: str, dst: str, *, memory_mib: int | None = None,
             cpus: int | None = None) -> dict:
    """Clone a non-running VM sparsely with fresh per-VM UEFI NVRAM."""
    from datetime import datetime, timezone
    valid_vm_name(src)
    valid_vm_name(dst)
    if src == dst:
        raise ValueError(f'Source and destination VM names must differ: {dst!r}')
    src_dir = vm_dir(src)
    dst_dir = vm_dir(dst)
    if not (src_dir / 'meta.json').is_file():
        raise FileNotFoundError(f'Source VM {src!r} does not exist in {whitecube_store() / "images"}')
    if dst_dir.exists():
        raise FileExistsError(f'Destination VM {dst!r} already exists')

    src_lock = _open_vm_lock(src_dir)
    created_dst = False
    try:
        meta = vm_meta(src)
        src_disk = Path(meta['disk_path'])
        if not src_disk.is_file():
            raise FileNotFoundError(f'Source VM disk image is missing: {src_disk}')
        mem = max(512, min(int(memory_mib if memory_mib is not None else meta['memory_mib']), 131072))
        smp = max(1, min(int(cpus if cpus is not None else meta['cpus']), 64))

        dst_dir.mkdir(mode=0o700, parents=True)
        created_dst = True
        dst_disk = dst_dir / meta['disk']
        _copy_sparse(src_disk, dst_disk)
        if meta['format'] == 'raw':
            sparsify(dst_disk)
        os.chmod(dst_disk, 0o600)

        ovmf = ovmf_pair()
        if ovmf and ovmf[1] is not None:
            ensure_vm_vars(dst_dir / 'vars.fd', ovmf[1])

        new_meta = {
            'schema': 1,
            'name': dst,
            'type': meta.get('type', 'ext4'),
            'disk': meta['disk'],
            'format': meta['format'],
            'uefi': bool(meta.get('uefi', True)),
            'memory_mib': mem,
            'cpus': smp,
            'suite': meta.get('suite', ''),
            'arch': meta.get('arch', 'amd64'),
            'recipe': meta.get('recipe', ''),
            'size_bytes': dst_disk.stat().st_size,
            'cloned_from': src,
            'created_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        }
        _write_vm_meta(dst_dir, new_meta)
        return new_meta
    except BaseException:
        if created_dst:
            shutil.rmtree(dst_dir, ignore_errors=True)
        raise
    finally:
        os.close(src_lock)


def compact_vm(name: str) -> dict:
    """Reclaim sparse zero blocks on a stopped VM disk image."""
    meta = vm_meta(name)
    target = vm_dir(name)
    disk_path = Path(meta['disk_path'])
    if not disk_path.is_file():
        raise FileNotFoundError(f'VM disk image is missing: {disk_path}')
    lock_fd = _open_vm_lock(target)
    try:
        before = disk_path.stat().st_blocks * 512
        fmt = meta['format']
        if fmt == 'raw':
            sparsify(disk_path)
        elif fmt == 'qcow2' and shutil.which('qemu-img'):
            tmp = disk_path.with_name(f'.{disk_path.name}.{os.getpid()}.compact')
            try:
                subprocess.run(
                    ['qemu-img', 'convert', '-O', 'qcow2', '-c', str(disk_path), str(tmp)],
                    check=True, capture_output=True, text=True, timeout=1800,
                )
                os.chmod(tmp, 0o600)
                os.replace(tmp, disk_path)
            finally:
                tmp.unlink(missing_ok=True)
        after = disk_path.stat().st_blocks * 512
        return {
            'name': name,
            'format': fmt,
            'before_bytes': before,
            'after_bytes': after,
            'reclaimed_bytes': max(0, before - after),
        }
    finally:
        os.close(lock_fd)


def vm_run_argv(name: str, memory_mib: int | None = None, cpus: int | None = None,
                forward_ssh: bool = False, ssh_port: int = 2222,
                headless: bool = False, serial_stdio: bool = False,
                snapshot: bool = False, cdrom: Path | None = None,
                serial_log: Path | None = None) -> list[str]:
    """Build the native QEMU + OVMF command line to boot a stored VM."""
    meta = vm_meta(name)
    disk_path = Path(meta['disk_path'])
    if not disk_path.is_file():
        raise FileNotFoundError(f'VM disk image is missing: {disk_path}')
    if ',' in str(disk_path):
        raise ValueError(f'QEMU disk path cannot contain commas: {disk_path}')
    qemu = qemu_command()
    if not qemu:
        raise RuntimeError('QEMU x86_64 emulator is not installed (install package "qemu-system-x86").')
    ovmf = ovmf_pair()
    if not ovmf:
        raise RuntimeError('UEFI OVMF firmware is not installed (install package "ovmf").')

    mem = max(512, min(int(memory_mib if memory_mib is not None else meta['memory_mib']), 131072))
    smp = max(1, min(int(cpus if cpus is not None else meta['cpus']), 64))
    port = int(ssh_port)
    if not 1 <= port <= 65535:
        raise ValueError(f'SSH forward port must be 1..65535, got {ssh_port!r}')
    if serial_stdio and serial_log is not None:
        raise ValueError('Choose either interactive serial console or --serial-log, not both')

    argv = [qemu, '-name', f'whitecube-{name}', '-machine', 'q35', '-m', str(mem), '-smp', str(smp)]
    if vm_can_kvm():
        argv += ['-enable-kvm', '-cpu', 'host']
    else:
        argv += ['-cpu', 'max']

    code_path, vars_template = ovmf
    if vars_template is not None:
        vars_path = ensure_vm_vars(vm_dir(name) / 'vars.fd', vars_template)
        if ',' in str(vars_path):
            raise ValueError(f'QEMU firmware path cannot contain commas: {vars_path}')
        vars_opts = f'if=pflash,format=raw,file={vars_path}'
        if snapshot:
            vars_opts += ',snapshot=on'
        argv += ['-drive', f'if=pflash,format=raw,readonly=on,file={code_path}', '-drive', vars_opts]
    else:
        argv += ['-bios', str(code_path)]

    drive = f'file={disk_path},format={meta["format"]},if=virtio'
    if snapshot:
        drive += ',snapshot=on'
    argv += ['-drive', drive]

    if cdrom is not None:
        iso_path = Path(cdrom)
        if not iso_path.is_file():
            raise FileNotFoundError(f'CD-ROM image not found: {iso_path}')
        if ',' in str(iso_path):
            raise ValueError(f'QEMU CD-ROM path cannot contain commas: {iso_path}')
        argv += ['-drive', f'file={iso_path},media=cdrom,readonly=on', '-boot', 'order=dc']

    net = 'user,model=virtio-net-pci'
    if forward_ssh:
        net += f',hostfwd=tcp:127.0.0.1:{port}-:22'
    argv += ['-nic', net, '-device', 'virtio-rng-pci']

    if headless:
        argv += ['-display', 'none']
    else:
        argv += ['-vga', 'virtio', '-display', 'gtk']
    if serial_stdio:
        argv += ['-serial', 'mon:stdio']
    elif serial_log is not None:
        slog = Path(serial_log)
        if slog.is_symlink():
            raise OSError(f'Refusing symlinked serial log path: {slog}')
        if ',' in str(slog):
            raise ValueError(f'QEMU serial log path cannot contain commas: {slog}')
        slog.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        argv += ['-serial', f'file:{slog}']
    return argv


def vm_launch_argv(name: str, memory_mib: int | None = None, cpus: int | None = None,
                   forward_ssh: bool = False, ssh_port: int = 2222) -> list[str]:
    """Prefer whitecube-vm so GUI boots hold vm.lock and log stderr cleanly."""
    cli = whitecube_vm_command()
    if cli:
        argv = [cli, 'run', name]
        if memory_mib is not None:
            argv += ['--memory', str(int(memory_mib))]
        if cpus is not None:
            argv += ['--cpus', str(int(cpus))]
        if forward_ssh:
            argv += ['--ssh', '--ssh-port', str(int(ssh_port))]
        return argv
    return vm_run_argv(name, memory_mib=memory_mib, cpus=cpus, forward_ssh=forward_ssh, ssh_port=ssh_port)


def list_vms() -> list[dict]:
    """List every stored VM directory cleanly."""
    images = whitecube_store() / 'images'
    if not images.is_dir() or images.is_symlink():
        return []
    found: list[dict] = []
    for entry in sorted(images.iterdir(), key=lambda p: p.name):
        if entry.is_symlink() or not entry.is_dir():
            continue
        try:
            meta = vm_meta(entry.name)
        except (OSError, ValueError):
            continue
        disk_path = Path(meta['disk_path'])
        apparent = disk_path.stat().st_size if disk_path.is_file() else 0
        actual = disk_path.stat().st_blocks * 512 if disk_path.is_file() else 0
        found.append({
            **meta,
            'disk_exists': disk_path.is_file(),
            'apparent_bytes': apparent,
            'actual_bytes': actual,
            'running': vm_is_running(entry.name),
        })
    return found


def remove_vm(name: str) -> bool:
    """Remove a non-running VM from the store."""
    target = vm_dir(name)
    if not target.exists():
        return False
    lock_fd = _open_vm_lock(target)
    try:
        shutil.rmtree(target)
        return True
    finally:
        os.close(lock_fd)


def new_workdir() -> Path:
    CACHE.mkdir(parents=True, exist_ok=True, mode=0o700)
    return Path(tempfile.mkdtemp(prefix='build-', dir=CACHE))


def write_config(plan: Plan, workdir: Path) -> Path:
    text = config_text(plan, workdir)
    path = workdir / 'cube.conf'
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        stream.write(text)
    return path


# --- progress -----------------------------------------------------------------
# deb-strap prints "═══ Stage ═══" banners; map them to a fraction of the whole build.
STAGES = (
    ('Checking Host Dependencies', .02), ('Pre-flight Validation', .04), ('Executing Disk Partitioning', .06),
    ('Formatting', .08), ('Executing Debian Bootstrap', .10), ('Configuring System Inside Target Chroot', .45),
    ('Exporting Rootfs', .85), ('Debian Installation Complete', .92),
)
ANSI = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')


def clean_line(line: str) -> str:
    return ANSI.sub('', line).rstrip()


def stage_fraction(line: str) -> float | None:
    for title, fraction in STAGES:
        if title in line:
            return fraction
    return None


# --- history ------------------------------------------------------------------
def history_path() -> Path:
    return STATE / 'cubes.json'


def load_history() -> list[dict]:
    try:
        data = json.loads(history_path().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    return [item for item in data if isinstance(item, dict) and NAME.match(str(item.get('name', '')))] \
        if isinstance(data, list) else []


def save_history(items: list[dict]) -> None:
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = history_path().with_suffix('.tmp')
    temporary.write_text(json.dumps(items[-200:], indent=1), encoding='utf-8')
    os.replace(temporary, history_path())


def record(plan: Plan, status: str, seconds: float, detail: str = '') -> dict:
    entry = dict(plan.public(), status=status, seconds=round(seconds), detail=detail[:300],
                 finished=time.strftime('%Y-%m-%d %H:%M'))
    items = [item for item in load_history() if not (item.get('name') == plan.name and item.get('kind') == plan.kind)]
    save_history(items + [entry])
    return entry


def forget(name: str, kind: str) -> None:
    save_history([item for item in load_history() if not (item.get('name') == name and item.get('kind') == kind)])


def vm_exists(name: str) -> bool:
    try:
        meta = vm_meta(name)
    except (OSError, ValueError):
        return False
    return Path(meta['disk_path']).is_file()


def load_draft() -> dict:
    try:
        data = json.loads((STATE / 'draft.json').read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_draft(plan: Plan) -> None:
    try:
        STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
        (STATE / 'draft.json').write_text(json.dumps(plan.public()), encoding='utf-8')
    except OSError:
        pass


def plan_from(values: dict) -> Plan:
    plan = Plan()
    for key, value in values.items():
        if key != 'password' and hasattr(plan, key) and isinstance(value, type(getattr(plan, key))):
            setattr(plan, key, value)
    return plan


def human_seconds(seconds: float) -> str:
    seconds = int(seconds)
    minutes, seconds = divmod(seconds, 60)
    return f'{minutes}m {seconds:02d}s' if minutes else f'{seconds}s'
