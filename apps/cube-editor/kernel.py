"""Propagate a guest kernel only when its boot mapping is unambiguous."""
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import tempfile

from engine import CubeError, guest_destination, sha256_file


def guest_path(root, path):
    """Resolve guest absolute symlinks within root, never against the host root."""
    root = Path(root).resolve()
    pending = list(PurePosixPath(path).parts)
    resolved = []
    links = 0
    while pending:
        part = pending.pop(0)
        if part in ('.', '') or not part.strip('/'):
            continue
        if part == '..':
            if not resolved:
                raise CubeError(f'Guest boot path escapes the image: {path}')
            resolved.pop()
            continue
        candidate = root.joinpath(*resolved, part)
        if candidate.is_symlink():
            links += 1
            if links > 40:
                raise CubeError(f'Guest boot symlink loop: {path}')
            target = os.readlink(candidate)
            if target.startswith('/'):
                resolved = []
            pending = list(PurePosixPath(target).parts) + pending
        else:
            resolved.append(part)
    result = root.joinpath(*resolved)
    if not result.is_relative_to(root):
        raise CubeError(f'Guest boot path escapes the image: {path}')
    return result


def kernel_inventory(rootfs, check_cancel=lambda: None):
    """Record boot contents, default selections and installed module versions."""
    root = Path(rootfs).resolve()
    boot = guest_path(root, '/boot')
    paths = set()
    for pattern in ('vmlinuz*', 'initrd*', 'initramfs*'):
        if boot.is_dir():
            paths.update('/' + str(path.relative_to(root)) for path in boot.glob(pattern))
    paths.update(('/vmlinuz', '/initrd.img'))
    files = {}
    for relative in sorted(paths):
        check_cancel()
        path = guest_path(root, relative)
        entry = guest_path(root, str(PurePosixPath(relative).parent)) / PurePosixPath(relative).name
        if path.is_file():
            files[relative] = {'target': '/' + str(path.relative_to(root)),
                               'sha256': sha256_file(path, lambda *_: check_cancel())}
        elif entry.exists() or entry.is_symlink():
            files[relative] = {'target': '/' + str(path.relative_to(root)), 'sha256': None}
    modules = guest_path(root, '/lib/modules')
    versions = sorted(p.name for p in modules.iterdir()
                      if guest_path(root, '/' + str(p.relative_to(root))).is_dir()) if modules.is_dir() else []
    return {'files': files, 'module_versions': versions}


def version_key(version):
    match = re.fullmatch(r'(\d+(?:\.\d+)*)(.*)', version)
    if not match:
        raise CubeError(f'Unsupported kernel version: {version}')
    base, suffix = match.groups()
    numbers = tuple(int(n) for n in base.split('.'))
    numbers += (0,) * max(0, 4 - len(numbers))
    natural = tuple((1, int(token)) if token.isdigit() else (0, token)
                    for token in re.findall(r'\d+|\D+', suffix))
    return numbers, 0 if re.search(r'(?:^|[-.])rc\d', suffix) else 1, natural


def selected_pair(root):
    boot = guest_path(root, '/boot')
    selected = None
    for relative in ('/vmlinuz', '/boot/vmlinuz'):
        entry = guest_path(root, str(PurePosixPath(relative).parent)) / PurePosixPath(relative).name
        if entry.exists() or entry.is_symlink():
            selected = guest_path(root, relative)
            if not selected.is_file():
                raise CubeError(f'The selected guest kernel is missing: {relative}')
            break
    if selected is None:
        candidates = [guest_path(root, '/' + str(p.relative_to(root)))
                      for p in boot.glob('vmlinuz-*')] if boot.is_dir() else []
        candidates = [p for p in candidates if p.is_file()]
        if not candidates:
            raise CubeError('No guest kernel is available to refresh the ISO boot files')
        selected = max(candidates, key=lambda p: version_key(p.name.removeprefix('vmlinuz-')))
    if not selected.name.startswith('vmlinuz-'):
        raise CubeError('The selected guest kernel must identify its installed version (vmlinuz-VERSION)')
    version = selected.name[len('vmlinuz-'):]
    names = (f'initrd.img-{version}', f'initramfs-{version}.img', f'initrd-{version}')
    initrds = [guest_path(root, f'/boot/{name}') for name in names]
    initrds = list(dict.fromkeys(p for p in initrds if p.is_file()))
    if len(initrds) != 1:
        raise CubeError(f'Expected one matching initramfs for guest kernel {version}; found {len(initrds)}')
    if initrds[0].name not in names:
        raise CubeError(f'The initramfs symlink does not identify the selected guest kernel version: {version}')
    if not guest_path(root, f'/lib/modules/{version}').is_dir():
        raise CubeError(f'The selected guest kernel has no matching module directory: {version}')
    return selected, initrds[0], version


def boot_mapping(project):
    tree = project.iso_tree
    live = PurePosixPath(project.data['squashfs_path']).parent
    kernels, initrds = set(), set()
    unresolved = False

    def add(targets, token, config):
        nonlocal unresolved
        if any(char in token for char in ('$','(', ')', '{', '}')):
            unresolved = True
            return
        if token.startswith('/'):
            paths = [PurePosixPath(token)]
        else:
            paths = [PurePosixPath('/') / config.parent.relative_to(tree) / token,
                     PurePosixPath('/') / token]
        matches = set()
        for path in paths:
            destination = guest_destination(tree, str(path))
            if destination.is_file():
                matches.add(str(path))
        if len(matches) != 1:
            unresolved = True
        targets.update(matches)

    for parent, directories, names in os.walk(tree, followlinks=False):
        directories[:] = [name for name in directories if not (Path(parent) / name).is_symlink()]
        for name in names:
            config = Path(parent) / name
            if config.is_symlink() or config.suffix.lower() not in ('.cfg', '.conf'):
                continue
            for line in config.read_text(errors='replace').splitlines():
                try:
                    tokens = shlex.split(line, comments=True)
                except ValueError:
                    continue
                if len(tokens) < 2:
                    continue
                command = tokens[0].lower()
                if command in ('linux', 'linuxefi', 'linux16', 'kernel'):
                    add(kernels, tokens[1], config)
                elif command in ('initrd', 'initrdefi', 'initrd16'):
                    for token in tokens[1:]:
                        if 'ucode' not in PurePosixPath(token).name:
                            add(initrds, token, config)
                elif command == 'append':
                    for token in tokens[1:]:
                        if token.startswith('initrd='):
                            for part in token[len('initrd='):].split(','):
                                if 'ucode' not in PurePosixPath(part).name:
                                    add(initrds, part, config)
    if unresolved:
        raise CubeError('Unsupported ISO boot configuration; cannot identify every kernel/initramfs mapping')
    # A multi-purpose ISO can have unrelated installer/memory-test boot entries.
    # Prefer a complete pair beside the selected live filesystem.
    live_kernels = {p for p in kernels if PurePosixPath(p).parent == live}
    live_initrds = {p for p in initrds if PurePosixPath(p).parent == live}
    if live_kernels and live_initrds:
        kernels, initrds = live_kernels, live_initrds
    if kernels or initrds:
        if len(kernels) == len(initrds) == 1:
            return next(iter(kernels)), next(iter(initrds))
        raise CubeError('Ambiguous ISO kernel/initramfs boot mappings; update the boot configuration before building')
    kernels = {str(live / name) for name in ('vmlinuz', 'vmlinuz.efi')
               if guest_destination(tree, str(live / name)).is_file()}
    initrds = {str(live / name) for name in ('initrd', 'initrd.img', 'initrd.lz', 'initrd.gz', 'initrd.xz')
               if guest_destination(tree, str(live / name)).is_file()}
    if len(kernels) == len(initrds) == 1:
        return next(iter(kernels)), next(iter(initrds))
    raise CubeError('Unsupported or ambiguous ISO kernel layout; cannot propagate guest kernel changes')


def refresh_kernel(project, emit, check_cancel=lambda: None):
    inventory = kernel_inventory(project.rootfs, check_cancel)
    baseline = project.data.get('kernel_baseline')
    unchanged = baseline is not None and baseline == inventory
    has_kernel = any(PurePosixPath(path).name.startswith('vmlinuz') for path in inventory['files'])
    if not has_kernel and (baseline is None or unchanged):
        return
    try:
        mapping = boot_mapping(project)
        kernel, initrd, version = selected_pair(project.rootfs)
    except CubeError as error:
        if unchanged:
            emit('log', f'Guest kernel files are unchanged; retaining original ISO boot files. {error}')
            return
        raise
    staged = []
    try:
        for source, relative in zip((kernel, initrd), mapping):
            check_cancel()
            destination = guest_destination(project.iso_tree, relative)
            fd, temporary = tempfile.mkstemp(prefix='.' + destination.name + '.', suffix='.building', dir=destination.parent)
            os.close(fd)
            staged.append((Path(temporary), destination))
            shutil.copy2(source, temporary)
        check_cancel()
        for temporary, destination in staged:
            os.replace(temporary, destination)
        emit('log', f'Refreshed ISO kernel and initramfs from guest kernel {version}')
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)
