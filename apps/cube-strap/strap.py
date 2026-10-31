"""Cube Strap engine: turns a build plan into a deb-strap run and a finished whitecube.

Two kinds of cube:
  chroot  deb-strap exports a rootfs tarball (no kernel, no bootloader), which
          chrootable-manager imports as a workspace; it then shows in Cube Chrootables.
  vm      deb-strap builds a raw UEFI disk image (EFI + ext4 root, GRUB, cloud kernel),
          which `whitecube import` adds to the image store; `whitecube run` boots it.

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


def import_args(plan: Plan, archive: Path) -> list[str]:
    """chrootable-manager arguments that turn the tarball into a workspace."""
    return ['--import', plan.name, str(archive), str(plan.size_gib * 1024), plan.lifecycle]


def whitecube_command() -> str | None:
    return shutil.which('whitecube')


def vm_import_argv(plan: Plan, image: Path) -> list[str]:
    return [whitecube_command() or 'whitecube', 'import', '--type', 'ext4', str(image), plan.name]


def vm_run_argv(name: str, memory_mib: int = 2048, forward_ssh: bool = False) -> list[str]:
    argv = [whitecube_command() or 'whitecube', 'run', name, '--uefi', '--display', '--memory', str(int(memory_mib))]
    if forward_ssh:
        argv += ['--forward', '2222:22']
    return argv


def whitecube_store() -> Path:
    env = os.environ.get('WHITECUBE_HOME')
    return Path(env).expanduser() if env else Path.home() / '.whitecube'


def sparsify(path: Path) -> None:
    """`whitecube import` copies the disk densely; punch the zero runs back out."""
    if shutil.which('fallocate') and path.is_file():
        subprocess.run(['fallocate', '--dig-holes', str(path)], capture_output=True, timeout=1800)


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
    return (whitecube_store() / 'images' / name / 'meta.json').is_file()


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
