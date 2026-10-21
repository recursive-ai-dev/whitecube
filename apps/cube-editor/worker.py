#!/usr/bin/env python3
"""Authenticated, private-namespace image operations. Apache-2.0.

Based on the ISO-editing workflow of Cubic, Copyright 2015, 2020 PJ Singh.
Privilege is requested per operation through sudo in an interactive terminal.
No sudoers or polkit policy is installed by Cube Editor.
"""
from __future__ import annotations

import argparse
import contextlib
from collections import deque
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import threading
import tempfile
import time

from engine import (Project, CubeError, Cancelled, atomic_json, atomic_text, guest_destination,
                    inspect_iso, package_list, utcnow, validate_recipe, recipe_scripts,
                    build_resources, storage_preflight, sha256_file, recovery_report)
from kernel import kernel_inventory, refresh_kernel


def copy_payload(source, destination, root, overwrite='replace'):
    """Copy files recursively, checking each destination before any write."""
    source, destination, root = Path(source), Path(destination), Path(root)
    guest_destination(root, "/" + str(destination.relative_to(root)))
    if destination.exists() and not (source.is_dir() and destination.is_dir()):
        if overwrite == 'skip':
            return
        if overwrite == 'error':
            raise CubeError(f'Payload destination already exists: {destination}')
        if destination.is_dir() != source.is_dir():
            raise CubeError(f'Payload source and destination have different file types: {destination}')
    if source.is_symlink():
        if destination.exists():
            raise CubeError(f"Cannot replace existing payload destination with a symlink: {destination}")
        destination.symlink_to(os.readlink(source))
    elif source.is_dir():
        destination.mkdir(parents=True, exist_ok=True)
        for child in source.iterdir():
            copy_payload(child, destination / child.name, root, overwrite)
        shutil.copystat(source, destination, follow_symlinks=False)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def tree_paths(root):
    """Include symlinks without traversing them; directory whiteouts need parents."""
    root = Path(root)
    result = []
    for parent, directories, files in os.walk(root, followlinks=False):
        for name in directories + files:
            result.append(str((Path(parent) / name).relative_to(root)))
    return sorted(result)


def checksum_tree(tree, excluded=(), check_cancel=lambda: None):
    tree = Path(tree)
    excluded = {str(path).lstrip("/") for path in excluded} | {"md5sum.txt", "MD5SUMS", "SHA256SUMS", "sha256sum.txt",
                "boot.catalog", "isolinux/boot.cat", "boot/grub/i386-pc/eltorito.img"}
    renew_md5 = (tree / "MD5SUMS").exists()
    sha_names = [name for name in ("SHA256SUMS", "sha256sum.txt") if (tree / name).exists()]
    lines = []
    sha_lines = []
    for relative in tree_paths(tree):
        path = tree / relative
        if relative in excluded or path.is_symlink() or not path.is_file():
            continue
        digest = hashlib.md5()
        sha_digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                check_cancel()
                digest.update(block)
                sha_digest.update(block)
        lines.append(f"{digest.hexdigest()}  ./{relative}\n")
        sha_lines.append(f"{sha_digest.hexdigest()}  ./{relative}\n")
    atomic_text(tree / "md5sum.txt", "".join(lines))
    if renew_md5:
        atomic_text(tree / "MD5SUMS", "".join(lines))
    for name in sha_names:
        atomic_text(tree / name, "".join(sha_lines))


def _same_file(path, identity):
    try:
        current = path.lstat()
        return (current.st_dev, current.st_ino) == identity
    except FileNotFoundError:
        return False


def _verified_source(project, destination, check_cancel):
    """Copy and hash the bytes that the exporter will actually read.

    A separate, read-only copy prevents an in-place write or pathname replacement
    of the original ISO from changing boot replay after verification.
    """
    source = Path(project.data["source_iso"])
    try:
        with source.open("rb") as incoming, destination.open("xb") as outgoing:
            before = os.fstat(incoming.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise CubeError("Source ISO must be a regular file")
            digest = hashlib.sha256()
            for block in iter(lambda: incoming.read(4 * 1024 * 1024), b""):
                check_cancel()
                outgoing.write(block)
                digest.update(block)
            after = os.fstat(incoming.fileno())
            current = source.stat()
            identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            if identity(before) != identity(after) or identity(before) != identity(current):
                raise CubeError("Source ISO changed while preparing the verified export copy")
            if before.st_size != project.data.get("source_size") or before.st_mtime_ns != project.data.get("source_mtime_ns"):
                raise CubeError("Source ISO changed since this project was created")
            actual = digest.hexdigest()
            if project.data.get("source_sha256") and actual != project.data["source_sha256"]:
                raise CubeError("Source ISO no longer matches its recorded SHA256")
            outgoing.flush()
            os.fsync(outgoing.fileno())
        destination.chmod(0o444)
        return actual
    except FileNotFoundError as error:
        raise CubeError("Source ISO was moved or deleted") from error


def _publish_noreplace(source, destination):
    try:
        os.link(source, destination, follow_symlinks=False)
        return
    except OSError as error:
        if error.errno not in (errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS, errno.EMLINK):
            raise
        # FAT/exFAT and some network filesystems do not implement hard links.
        # Linux renameat2 provides the same no-clobber guarantee on those paths.
        rename = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
        if rename is None:
            raise CubeError("This output filesystem does not support safe no-replace publication; choose another release folder") from error
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number), str(destination))


def rebuild_iso(project, run, check_cancel=lambda: None):
    """Publish a coordinated release without replacing any existing artifact.

    A destination lock coordinates different projects. No-replace publication
    fails if an uncoordinated writer creates any destination. Publish
    the ISO last so its appearance indicates both sidecars are already present.
    The helper also works unprivileged for synthetic integration tests.
    """
    output = project.output_path
    output.parent.mkdir(parents=True, exist_ok=True)
    lock_path = output.with_name("." + output.name + ".publish.lock")
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    created = []
    try:
        if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
            raise CubeError("Release publication lock must be a regular file")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise CubeError(f"Another build is publishing this output: {output.name}") from error
        owner = project.directory.stat()
        if os.geteuid() == 0:
            os.fchown(lock_fd, owner.st_uid, owner.st_gid)
        destinations = [Path(str(output) + suffix) for suffix in (".sha256", ".release.json")] + [output]
        for destination in destinations:
            if destination.exists() or destination.is_symlink():
                raise CubeError(f"Release artifact already exists; choose another output name: {destination.name}")
        legacy = output.with_name("." + output.name + ".building")
        if legacy.exists() or legacy.is_symlink():
            raise CubeError(f"An unfinished output already exists: {legacy}. Run Recover project before retrying.")
        with tempfile.TemporaryDirectory(prefix="." + output.name + ".", suffix=".building", dir=output.parent) as directory:
            staging = Path(directory)
            # Root owns the source copy; the desktop exporter can read it and
            # writes only in its separate subdirectory.
            staging.chmod(0o755)
            source_copy = staging / "source.iso"
            source_sha = _verified_source(project, source_copy, check_cancel)
            export = staging / "export"
            export.mkdir(mode=0o700)
            if os.geteuid() == 0:
                os.chown(export, owner.st_uid, owner.st_gid)
            temporary = export / output.name
            command = ["xorriso", "-indev", str(source_copy), "-outdev", str(temporary),
                       "-boot_image", "any", "replay", "-update_r", str(project.iso_tree), "/",
                       "-volid", project.data.get("volume_id", "CUBE_EDITOR")[:32], "-commit", "-end"]
            run(command)
            sha = sha256_file(temporary, lambda done, total: check_cancel())
            release = {"path": str(output), "sha256": sha, "size_bytes": temporary.stat().st_size,
                       "completed_at": utcnow(), "source_iso": project.data["source_iso"],
                       "volume_id": project.data.get("volume_id"), "compression": project.data.get("compression"),
                       "recipe": project.data.get("recipe", {}), "source_sha256": source_sha,
                       "processors": project.data.get("processors", 2), "memory_mb": project.data.get("memory_mb", 1024),
                       "compression_level": project.data.get("compression_level", 0)}
            checksum = staging / "checksum"
            manifest = staging / "release.json"
            checksum.write_text(f"{sha}  {output.name}\n")
            manifest.write_text(json.dumps(release, indent=2, ensure_ascii=False) + "\n")
            for staged, destination in zip((checksum, manifest, temporary), destinations):
                check_cancel()
                info = staged.lstat()
                if not stat.S_ISREG(info.st_mode):
                    raise CubeError("Exporter produced an invalid release artifact")
                try:
                    _publish_noreplace(staged, destination)
                except FileExistsError as error:
                    raise CubeError(f"Release artifact appeared during export; nothing was overwritten: {destination.name}") from error
                created.append((destination, (info.st_dev, info.st_ino)))
            return release
    except BaseException:
        for artifact, identity in reversed(created):
            # Another writer may have replaced an artifact after publication.
            # Never delete that writer's file while rolling back our attempt.
            if _same_file(artifact, identity):
                artifact.unlink(missing_ok=True)
        raise
    finally:
        os.close(lock_fd)
        # Keep the lock inode: unlinking it would split concurrent flock users.


class Worker:
    def __init__(self, project, uid, gid, action, job=""):
        self.project = project
        self.uid = int(uid)
        self.gid = int(gid)
        self.action = action
        self.job = job
        self.child = None
        self.child_interactive = False
        self.cancelled = threading.Event()
        self.stop_watcher = threading.Event()
        self.mounts = []
        self._event_lock = threading.Lock()

    def validate(self):
        p = self.project
        if self.uid == 0:
            raise CubeError("Launch Cube Editor from your desktop account and authenticate with sudo")
        if p.directory == Path("/") or not p.directory.is_dir():
            raise CubeError("Invalid project directory")
        if any(c in str(p.directory) for c in (",", ":", "\n")):
            raise CubeError("Project paths must not contain commas, colons, or newlines")
        if p.directory.stat().st_uid != self.uid:
            raise CubeError("Project directory must belong to the authenticated desktop user")
        for name in ("cube-editor.json", "events.jsonl", ".operation.lock", "iso", "rootfs", "layers", "overlay", "baseline-paths.json", ".extracted", 'restore-point', '.restore-point.building', '.restore.building', 'recovery', '.guest-environment.json', '.whiteouts.json'):
            if (p.directory / name).is_symlink():
                raise CubeError(f"Project control path cannot be a symlink: {name}")
        if self.action in ("extract", "build"):
            source = Path(p.data["source_iso"]).resolve()
            if not source.is_file():
                raise CubeError("Source ISO was moved or deleted")
            if source.stat().st_size != p.data.get("source_size") or source.stat().st_mtime_ns != p.data.get("source_mtime_ns"):
                raise CubeError("Source ISO changed since this project was created. Create a new project for the changed source.")
        p.output_path
        if self.action == 'build': build_resources(p.data)

    def user_ownership(self, path):
        os.chown(path, self.uid, self.gid, follow_symlinks=False)

    def save(self, **updates):
        self.project.save(**updates)
        self.user_ownership(self.project.directory / "cube-editor.json")

    def emit(self, kind, message, **fields):
        event = {"type": kind, "message": message, "time": utcnow(), "action": self.action, "job": self.job, **fields}
        encoded = json.dumps(event, ensure_ascii=False) + "\n"
        with self._event_lock:
            path = self.project.directory / "events.jsonl"
            fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            try:
                os.fchown(fd, self.uid, self.gid)
                os.write(fd, encoded.encode())
            finally:
                os.close(fd)
        if kind != "log":
            print(message, flush=True)

    def request_cancel(self, *_):
        if self.cancelled.is_set():
            return
        self.cancelled.set()
        if self.child and self.child.poll() is None:
            child, interactive = self.child, self.child_interactive
            with contextlib.suppress(ProcessLookupError):
                if interactive:
                    child.send_signal(signal.SIGHUP)
                else:
                    os.killpg(child.pid, signal.SIGTERM)
            def escalate():
                time.sleep(1.5)
                if child.poll() is None:
                    with contextlib.suppress(ProcessLookupError):
                        if interactive:
                            child.kill()
                        else:
                            os.killpg(child.pid, signal.SIGKILL)
            threading.Thread(target=escalate, daemon=True).start()

    def watch_cancel(self):
        while not self.stop_watcher.wait(0.2):
            if (self.project.directory / ".cancel").exists():
                self.request_cancel()
                return

    def check_cancel(self):
        if self.cancelled.is_set():
            raise Cancelled("Operation cancelled; image changes may be incomplete. Original ISO is unchanged.")

    def run(self, command, user=False, interactive=False, capture=False, check=True):
        self.check_cancel()
        self.emit("log", "$ " + " ".join(__import__("shlex").quote(arg) for arg in command), command=command)
        kwargs = {"start_new_session": not interactive}
        if user:
            kwargs.update(user=self.uid, group=self.gid, extra_groups=[])
        if not interactive:
            # Tool output is logged, not parsed strictly; undecodable bytes must not abort the tool.
            kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        self.child_interactive = interactive
        self.child = subprocess.Popen(command, **kwargs)
        lines = [] if capture else deque(maxlen=100)
        started = time.monotonic()
        try:
            # A cancel requested while Popen ran found no child to signal.
            self.check_cancel()
            if not interactive:
                for line in self.child.stdout:
                    lines.append(line)
                    self.emit("log", line.rstrip())
                    if Path(command[0]).name in ('unsquashfs', 'mksquashfs', 'xorriso'):
                        match = re.search(r'(\d+(?:\.\d+)?)\s*%', line) or re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*', line)
                        if match:
                            fraction = min(1, float(match.group(1)) / 100)
                            elapsed = time.monotonic() - started
                            self.emit('progress', f'{Path(command[0]).name}: {fraction:.0%}',
                                      progress=fraction, phase=Path(command[0]).name, elapsed_seconds=elapsed,
                                      eta_seconds=elapsed * (1 - fraction) / fraction if fraction > .01 else None)
                    print(line, end="", flush=True)
                    self.check_cancel()
            code = self.child.wait()
        finally:
            # Callers clean up the tree after an error; the tool must be gone first.
            self.stop_child()
        self.check_cancel()
        if code and check:
            raise CubeError(f"{command[0]} exited with status {code}: {''.join(lines)[-1200:]}")
        return "".join(lines) if capture else code

    def mount(self, command, target):
        self.run(command)
        self.mounts.append(str(target))

    def stop_child(self):
        """Kill and reap the current tool, if any."""
        child, self.child = self.child, None
        if not child:
            return
        try:
            if child.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    if self.child_interactive:
                        child.kill()
                    else:
                        os.killpg(child.pid, signal.SIGKILL)
            child.wait()
        finally:
            if child.stdout:
                child.stdout.close()

    def cleanup_mounts(self):
        """Unmount in reverse order; failed targets stay listed for a retry."""
        failures, retained = [], []
        for target in reversed(self.mounts):
            result = subprocess.run(["umount", "-l", "--", target], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            if result.returncode:
                retained.append(target)
                failures.append(f"{target}: {(result.stdout or '').strip() or f'umount exited with status {result.returncode}'}")
        self.mounts = list(reversed(retained))
        if failures:
            raise CubeError("Could not unmount " + "; ".join(failures))

    def ensure_extracted(self):
        if not (self.project.directory / ".extracted").is_file() or not self.project.rootfs.is_dir():
            raise CubeError("Extract your source ISO first")
        if self.project.rootfs.stat().st_uid != 0:
            raise CubeError("The image root must retain root ownership. Extract it again in a new project.")

    def check_storage(self, action):
        report = storage_preflight(self.project, action)
        for check in report['checks']:
            self.emit('log', f"Storage estimate at {check['path']}: {check['required_bytes'] / 1024**3:.1f} GiB needed, {check['free_bytes'] / 1024**3:.1f} GiB free")
        if not report['sufficient']:
            raise CubeError('Insufficient free space for the estimated operation. See Activity.')

    def quarantine(self, path):
        recovery = self.project.directory / 'recovery'
        recovery.mkdir(mode=0o700, exist_ok=True)
        self.user_ownership(recovery)
        if path.lstat().st_dev != recovery.stat().st_dev:
            recovery = path.parent / '.cube-editor-recovery'
            if recovery.is_symlink():
                raise CubeError('Output recovery directory cannot be a symlink')
            recovery.mkdir(mode=0o700, exist_ok=True)
            self.user_ownership(recovery)
        target = recovery / (path.name + '-' + __import__('uuid').uuid4().hex[:12])
        path.rename(target)
        self.emit('log', f'Retained recovery artifact: {target}')
        return target

    def ensure_clean_environment(self):
        paths = ('.guest-environment.json', '.backup-etc-resolv.conf', '.backup-usr-sbin-policy-rc.d')
        if any((self.project.directory / name).exists() or (self.project.directory / name).is_symlink() for name in paths):
            raise CubeError('Guest environment restoration is incomplete. Run Recover project before continuing.')

    def snapshot(self):
        self.ensure_extracted()
        self.ensure_clean_environment()
        whiteouts = self.project.directory / '.whiteouts.json'
        if whiteouts.exists() or whiteouts.is_symlink():
            raise CubeError('Deletion markers need recovery before saving a restore point')
        self.check_storage('snapshot')
        p = self.project
        staging = p.directory / '.restore-point.building'
        if staging.exists():
            raise CubeError('An interrupted restore point exists. Run Recover project first.')
        staging.mkdir(mode=0o755)
        self.save(state='snapshotting')
        try:
            for name in ('rootfs', 'iso'):
                self.run(['cp', '-a', '--reflink=auto', '--', str(p.directory / name), str(staging / name)])
            atomic_json(staging / 'manifest.json', {'created_at': utcnow(), 'recipe': p.data.get('recipe', {}),
                                                   'source_sha256': p.data.get('source_sha256', '')})
            old = p.directory / 'restore-point'
            retired = self.quarantine(old) if old.exists() else None
            try:
                staging.rename(old)
            except BaseException:
                if retired:
                    retired.rename(old)
                raise
            self.save(state='ready', restore_point_at=utcnow())
            self.emit('log', 'Restore point saved. Previous points are retained under recovery/.')
        except BaseException:
            if staging.exists():
                self.quarantine(staging)
            raise

    def restore(self):
        p = self.project
        point = p.directory / 'restore-point'
        for name in ('rootfs', 'iso', 'manifest.json'):
            if (point / name).is_symlink() or not (point / name).exists():
                raise CubeError('Restore point is incomplete or contains an unsafe control path')
        manifest = json.loads((point / 'manifest.json').read_text())
        validate_recipe(manifest.get('recipe', {}))
        self.check_storage('restore')
        staging = p.directory / '.restore.building'
        if staging.exists():
            raise CubeError('Interrupted restore staging exists. Run Recover project first.')
        staging.mkdir(mode=0o700)
        self.save(state='restoring')
        moved = {}
        installed = []
        try:
            for name in ('rootfs', 'iso'):
                self.run(['cp', '-a', '--reflink=auto', '--', str(point / name), str(staging / name)])
            self.check_cancel()
            for name in ('rootfs', 'iso'):
                current = p.directory / name
                if current.exists():
                    moved[name] = self.quarantine(current)
                (staging / name).rename(current)
                installed.append(name)
            marker = p.directory / '.extracted'
            marker.write_text(utcnow() + '\n')
            self.user_ownership(marker)
            self.save(state='ready', recipe=manifest.get('recipe', {}), last_error='')
            if (p.directory / '.whiteouts.json').exists(): self.quarantine(p.directory / '.whiteouts.json')
            for value in recovery_report(p)['environment_backups']:
                self.quarantine(Path(value))
            self.emit('log', 'Restore complete. Replaced trees are retained under recovery/.')
        except BaseException:
            for name in reversed(installed):
                self.quarantine(p.directory / name)
            for name, previous in moved.items():
                previous.rename(p.directory / name)
            raise
        finally:
            if staging.exists():
                self.quarantine(staging)

    def recover(self):
        p = self.project
        report = recovery_report(p)
        journal = p.directory / '.guest-environment.json'
        environment = json.loads(journal.read_text()) if journal.is_file() else {}
        if not isinstance(environment, dict) or any(key not in ('etc/resolv.conf', 'usr/sbin/policy-rc.d') or type(value) is not bool
                                                    for key, value in environment.items()):
            raise CubeError('Guest environment recovery journal is invalid; original backups have been retained')
        if report['extracted']:
            for relative, existed in environment.items():
                backup = p.directory / ('.backup-' + relative.replace('/', '-'))
                if existed and not (backup.exists() or backup.is_symlink()):
                    raise CubeError(f'Original guest environment backup is missing for /{relative}; restore a saved point or resolve it manually before clearing the recovery journal')
            for relative in ('etc/resolv.conf', 'usr/sbin/policy-rc.d'):
                destination = guest_destination(p.rootfs, '/' + str(Path(relative).parent)) / Path(relative).name
                backup = p.directory / ('.backup-' + relative.replace('/', '-'))
                if backup.exists() or backup.is_symlink():
                    if destination.exists() or destination.is_symlink(): self.quarantine(destination)
                    backup.rename(destination)
                    self.emit('log', f'Restored guest environment file: /{relative}')
                elif environment.get(relative) is False and (destination.exists() or destination.is_symlink()):
                    self.quarantine(destination)
                if relative in environment:
                    environment.pop(relative)
                    atomic_json(journal, environment)
                    self.user_ownership(journal)
            if journal.exists(): self.quarantine(journal)
        if not report['extracted']:
            for name in ('rootfs', 'iso', 'layers', 'overlay', 'baseline-paths.json', '.extracted'):
                path = p.directory / name
                if path.exists():
                    self.quarantine(path)
            for value in report['environment_backups']:
                self.quarantine(Path(value))
        for name in ('.restore-point.building', '.restore.building'):
            path = p.directory / name
            if path.exists():
                self.quarantine(path)
        for value in report['staging']:
            path = Path(value)
            if not path.exists() or path.is_symlink():
                continue
            if path.parent == p.output_path.parent:
                lock_path = path.parent / ('.' + p.output_path.name + '.publish.lock')
                lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                try:
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError as error:
                        raise CubeError('Another project is publishing this output; retry recovery after it finishes') from error
                    self.user_ownership(lock_path)
                    if path.exists():
                        self.quarantine(path)
                finally:
                    os.close(lock_fd)
            else:
                self.quarantine(path)
        whiteouts = p.directory / '.whiteouts.json'
        if whiteouts.exists():
            for relative in json.loads(whiteouts.read_text()):
                path = guest_destination(p.rootfs, '/' + relative)
                if path.exists():
                    info = path.lstat()
                    if stat.S_ISCHR(info.st_mode) and info.st_rdev == os.makedev(0, 0):
                        path.unlink()
                        self.emit('log', f'Removed interrupted boot-layer deletion marker: /{relative}')
            self.quarantine(whiteouts)
        self.save(state='ready' if report['extracted'] else 'new', last_error='')
        self.emit('log', 'Recovery finished. Review retained artifacts in recovery/ before retrying.')

    def packages(self):
        self.ensure_extracted()
        self.ensure_clean_environment()
        recipe = self.project.data.get('recipe', {})
        validate_recipe(recipe, self.project.rootfs, check_files=True)
        output = []
        desktop = recipe.get('desktop_deb', '')
        staged = None
        if desktop:
            staged = guest_destination(self.project.rootfs, '/tmp/cube-editor-desktop.deb')
            if staged.exists(): raise CubeError('Guest desktop staging path already exists')
            staged.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(Path(desktop).expanduser(), staged)
            staged.chmod(0o644)
        try:
            with self.chroot_environment():
                for operation, key in (('install', 'install_packages'), ('purge', 'remove_packages')):
                    packages = package_list(recipe.get(key, []))
                    if operation == 'install' and desktop: packages.append('/tmp/cube-editor-desktop.deb')
                    if packages:
                        output.append(self.guest(['/usr/bin/apt-get', '--simulate', operation, '--', *packages], capture=True))
                        if operation == 'install':
                            output.append(self.guest(['/usr/bin/apt-get', '--print-uris', '--download-only', '--yes', 'install', '--', *packages], capture=True))
        finally:
            if staged: staged.unlink(missing_ok=True)
        text = 'Package simulation uses the guest’s current apt indexes; update them in the image terminal for a fresh preview.\n' + '\n'.join(output)
        atomic_text(self.project.directory / 'package-preview.txt', text)
        self.user_ownership(self.project.directory / 'package-preview.txt')
        self.emit('log', text)

    def extract(self):
        p = self.project
        self.check_storage('extract')
        self.emit('log', 'Hashing source ISO before extraction')
        digest = sha256_file(p.data['source_iso'], lambda done, total: self.check_cancel())
        if p.data.get('source_sha256') and digest != p.data['source_sha256']:
            raise CubeError('Source ISO no longer matches its recorded SHA256')
        self.save(source_sha256=digest)
        if (p.directory / ".extracted").exists():
            raise CubeError("This project is already extracted. Create another project to start again.")
        if p.iso_tree.exists() or p.rootfs.exists() or (p.directory / "layers").exists():
            raise CubeError("A partial extraction exists. Create a fresh project to avoid overwriting files.")
        inspection = inspect_iso(p.data["source_iso"], p.data.get("squashfs_path", ""))
        if not inspection["can_rebuild"]:
            raise CubeError("No supported live filesystem was found")
        self.save(state="extracting", inspection=inspection, squashfs_path=inspection["squashfs_path"], last_error="")
        self.emit("progress", "Extracting ISO tree as your desktop user", progress=0.05)
        p.iso_tree.mkdir(mode=0o700)
        self.user_ownership(p.iso_tree)
        self.run(["xorriso", "-osirrox", "on", "-indev", p.data["source_iso"], "-extract", "/", str(p.iso_tree)], user=True)
        # ISO permissions can be read-only. Grant owner write without touching
        # symlink destinations. The tree stays owned by the desktop user.
        for parent, directories, files in os.walk(p.iso_tree):
            for name in ["."] + directories + files:
                item = Path(parent) / name
                if not item.is_symlink():
                    os.chmod(item, stat.S_IMODE(item.stat().st_mode) | stat.S_IWUSR | (stat.S_IXUSR if item.is_dir() else 0))
        chain = inspection["layer_chain"]
        if len(chain) == 1:
            self.run(["unsquashfs", "-percentage", "-processors", str(self.project.data.get("processors", 2)), "-d", str(p.rootfs), str(p.iso_tree / chain[0].lstrip("/"))])
        else:
            layers = p.directory / "layers"
            layers.mkdir(mode=0o700)
            extracted = []
            for index, path in enumerate(chain):
                self.emit("progress", f"Extracting layer {index + 1}/{len(chain)}: {Path(path).name}", progress=0.15 + index * 0.18)
                target = layers / str(index)
                self.run(["unsquashfs", "-percentage", "-processors", str(self.project.data.get("processors", 2)), "-d", str(target), str(p.iso_tree / path.lstrip("/"))])
                extracted.append(target)
            overlay = p.directory / "overlay"
            overlay.mkdir(mode=0o700)
            lowerdirs = ":".join(str(path) for path in reversed(extracted))
            self.mount(["mount", "-t", "overlay", "overlay", "-o", "ro,lowerdir=" + lowerdirs, str(overlay)], overlay)
            p.rootfs.mkdir(mode=0o755)
            self.emit("progress", "Materializing the complete live system with overlay whiteouts applied", progress=0.75)
            self.run(["cp", "-a", str(overlay) + "/.", str(p.rootfs)])
            self.cleanup_mounts()
        self.check_cancel()
        # Include every original lower-layer path. Existing upper-layer
        # tombstones and opaque directories must keep hiding those paths after
        # the active layer is replaced with the complete customized filesystem.
        baseline = set(tree_paths(p.rootfs))
        if len(chain) > 1:
            for target in extracted:
                baseline.update(tree_paths(target))
        atomic_json(p.directory / "baseline-paths.json", sorted(baseline))
        self.user_ownership(p.directory / "baseline-paths.json")
        (p.directory / ".extracted").write_text(utcnow() + "\n")
        self.user_ownership(p.directory / ".extracted")
        inspection["extracted"] = True
        self.save(state="ready", inspection=inspection, kernel_baseline=kernel_inventory(p.rootfs, self.check_cancel))
        self.emit("progress", "Live filesystem is ready for customization", progress=1.0)

    @contextlib.contextmanager
    def chroot_environment(self):
        root = self.project.rootfs
        self.ensure_clean_environment()
        restored = []
        entries = {}
        journal = self.project.directory / '.guest-environment.json'
        try:
            for directory in ("/proc", "/sys", "/dev", "/run"):
                target = guest_destination(root, directory)
                target.mkdir(parents=True, exist_ok=True)
            self.mount(["mount", "-t", "proc", "-o", "nosuid,nodev,noexec", "proc", str(root / "proc")], root / "proc")
            self.mount(["mount", "-t", "sysfs", "-o", "ro,nosuid,nodev,noexec", "sysfs", str(root / "sys")], root / "sys")
            self.mount(["mount", "-t", "tmpfs", "-o", "nosuid,mode=755", "tmpfs", str(root / "dev")], root / "dev")
            for device in ("null", "zero", "random", "urandom", "tty"):
                target = root / "dev" / device
                target.touch()
                self.mount(["mount", "--bind", "/dev/" + device, str(target)], target)
            (root / "dev/pts").mkdir()
            (root / "dev/shm").mkdir(mode=0o1777)
            self.mount(["mount", "-t", "devpts", "-o", "newinstance,ptmxmode=666,mode=620", "devpts", str(root / "dev/pts")], root / "dev/pts")
            (root / "dev/ptmx").symlink_to("pts/ptmx")
            (root / "dev/fd").symlink_to("/proc/self/fd")
            for name, fd in (("stdin", 0), ("stdout", 1), ("stderr", 2)):
                (root / "dev" / name).symlink_to(f"/proc/self/fd/{fd}")
            self.mount(["mount", "-t", "tmpfs", "-o", "nosuid,nodev,mode=755", "tmpfs", str(root / "run")], root / "run")
            # Replace guest DNS and prevent maintainer scripts starting daemons.
            # Rename originals rather than following guest symlinks into host paths.
            for relative, contents, mode in (("etc/resolv.conf", Path("/etc/resolv.conf").read_bytes(), 0o644),
                    ("usr/sbin/policy-rc.d", b"#!/bin/sh\nexit 101\n", 0o755)):
                destination = guest_destination(root, '/' + str(Path(relative).parent)) / Path(relative).name
                destination.parent.mkdir(parents=True, exist_ok=True)
                parent = destination.parent.stat()
                backup = self.project.directory / (".backup-" + relative.replace("/", "-"))
                if backup.exists() or backup.is_symlink():
                    raise CubeError(f"Unrestored environment backup exists: {backup.name}")
                existed = destination.exists() or destination.is_symlink()
                entries[relative] = existed
                atomic_json(journal, entries)
                self.user_ownership(journal)
                if existed:
                    destination.rename(backup)
                restored.append((relative, destination, backup, existed, (parent.st_dev, parent.st_ino)))
                destination.write_bytes(contents)
                destination.chmod(mode)
            yield
        finally:
            failures = []
            for relative, destination, backup, existed, parent_identity in reversed(restored):
                try:
                    parent = guest_destination(root, '/' + str(Path(relative).parent))
                    if not _same_file(parent, parent_identity):
                        raise CubeError(f"Guest parent directory changed for /{relative}")
                    if destination.is_dir() and not destination.is_symlink():
                        raise CubeError(f"Guest environment file became a directory: /{relative}")
                    if existed and not (backup.exists() or backup.is_symlink()):
                        raise CubeError(f"Original guest environment backup is missing: {backup.name}")
                    destination.unlink(missing_ok=True)
                    if existed:
                        backup.rename(destination)
                    entries.pop(relative, None)
                    # Persist each successful restoration. If interrupted after
                    # rename but before this write, recovery refuses uncertainty.
                    atomic_json(journal, entries)
                    self.user_ownership(journal)
                except (OSError, CubeError) as error:
                    failures.append(str(error))
                    self.emit("log", f"Environment restoration needs attention: {error}; backup at {backup}")
            try:
                self.cleanup_mounts()
            except Exception as error:
                failures.append(f"Guest mount cleanup failed: {error}")
            remaining_backups = any((self.project.directory / name).exists() or (self.project.directory / name).is_symlink()
                                    for name in ('.backup-etc-resolv.conf', '.backup-usr-sbin-policy-rc.d'))
            if not entries and not remaining_backups and not failures:
                journal.unlink(missing_ok=True)
            if failures or entries or remaining_backups:
                detail = '; '.join(failures) or 'Unresolved original environment files remain'
                raise CubeError('Guest environment restoration is incomplete. Run Recover project before continuing. ' + detail)

    def guest(self, command, **kwargs):
        environment = ["/usr/bin/env", "-i", "HOME=/root", "USER=root", "LOGNAME=root", "LC_ALL=C.UTF-8",
                       "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "TERM=xterm-256color",
                       "DEBIAN_FRONTEND=noninteractive", "CUBE_EDITOR=1"]
        return self.run(["chroot", str(self.project.rootfs), *environment, *command], **kwargs)

    def payloads(self, recipe):
        for payload in recipe.get("payloads", []):
            if not isinstance(payload, dict):
                raise CubeError("Each payload needs a source and destination")
            source = Path(payload.get("source", "")).expanduser().resolve()
            if not source.exists():
                raise CubeError(f"Payload does not exist: {source}")
            if source == Path("/") or self.project.directory.is_relative_to(source):
                raise CubeError("Cannot copy your project or its parent into its own image")
            destination = guest_destination(self.project.rootfs, payload.get("destination", ""))
            destination.parent.mkdir(parents=True, exist_ok=True)
            self.emit("log", f"Copying {source} to {payload['destination']}")
            if destination.is_relative_to(source):
                raise CubeError("Cannot copy a directory into itself")
            copy_payload(source, destination, self.project.rootfs, payload.get('overwrite', 'replace'))
            self.check_cancel()

    def customize(self):
        self.ensure_extracted()
        self.ensure_clean_environment()
        recipe = self.project.data.get("recipe", {})
        install = package_list(recipe.get("install_packages", []))
        remove = package_list(recipe.get("remove_packages", []))
        validate_recipe(recipe, self.project.rootfs, check_files=True)
        commands = recipe_scripts(recipe)
        if isinstance(commands, str):
            commands = [commands] if commands.strip() else []
        if any(not isinstance(command, str) for command in commands):
            raise CubeError("Custom commands must be text")
        if self.project.data.get('auto_snapshot', True):
            self.snapshot()
        self.save(state="customizing", last_error="")
        self.payloads(recipe)
        desktop = recipe.get("desktop_deb", "")
        staged_deb = None
        if desktop:
            source = Path(desktop).expanduser().resolve()
            if not source.is_file() or source.suffix != ".deb":
                raise CubeError("Select an existing desktop .deb package")
            staged_deb = guest_destination(self.project.rootfs, "/tmp/cube-editor-desktop.deb")
            if staged_deb.exists(): raise CubeError('Guest desktop staging path already exists')
            staged_deb.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, staged_deb)
            staged_deb.chmod(0o644)
        try:
            with self.chroot_environment():
                if install or desktop:
                    self.guest(["/usr/bin/apt-get", "update"])
                if install:
                    self.guest(["/usr/bin/apt-get", "install", "-y", "--", *install])
                if remove:
                    self.guest(["/usr/bin/apt-get", "purge", "-y", "--", *remove])
                if desktop:
                    self.guest(["/usr/bin/apt-get", "install", "-y", "/tmp/cube-editor-desktop.deb"])
                for command in commands:
                    self.guest(["/bin/bash", "-euc", command])
                if install or remove or desktop:
                    self.guest(["/usr/bin/apt-get", "clean"])
        finally:
            if staged_deb:
                staged_deb.unlink(missing_ok=True)
        self.save(state="customized", recipe_applied_at=utcnow())

    def terminal(self):
        self.ensure_extracted()
        self.ensure_clean_environment()
        if self.project.data.get('auto_snapshot', True):
            self.snapshot()
        self.save(state="terminal", last_error="")
        self.emit("log", "This shell runs inside your live image. Exit the shell to return to Cube Editor.")
        with self.chroot_environment():
            self.guest(["/bin/bash", "--login"], interactive=True)
        self.save(state="customized")

    def prepare_installer(self):
        """Give a layered Calamares ISO a clean, complete installation root.

        Casper needs 0:0 overlay tombstones in its rebuilt highest layer;
        Calamares uses raw rsync and would copy those devices to the installed
        machine. Its separate root image is therefore compressed first.
        """
        p = self.project
        chain = p.data.get("inspection", {}).get("layer_chain", [])
        configuration = p.rootfs / "etc/calamares/modules/unpackfs.conf"
        if len(chain) <= 1 or not configuration.exists():
            return None
        try:
            import yaml
        except ImportError as error:
            raise CubeError("Install python3-yaml to preserve this image's Calamares configuration") from error
        guest_destination(p.rootfs, "/etc/calamares/modules/unpackfs.conf")
        document = yaml.safe_load(configuration.read_text())
        if not isinstance(document, dict) or not isinstance(document.get("unpack"), list):
            raise CubeError("Unsupported Calamares unpackfs configuration; expected an unpack list")
        known = {"/cdrom" + path for path in chain}
        install_path = "/cube-editor/install.squashfs"
        known.add("/cdrom" + install_path)
        mappings = document["unpack"]
        matching = [entry for entry in mappings if isinstance(entry, dict) and entry.get("source") in known
                    and entry.get("sourcefs") == "squashfs" and entry.get("destination", "") in ("", "/")]
        if not matching:
            raise CubeError("This Calamares installer uses custom root sources. Update its configuration for the customized root before building.")
        settings_path = guest_destination(p.rootfs, "/etc/calamares/settings.conf")
        packages_path = guest_destination(p.rootfs, "/etc/calamares/modules/packages.conf")
        if not settings_path.is_file() or not packages_path.is_file():
            raise CubeError("The layered Calamares image needs settings.conf and packages.conf for installation compatibility")
        settings = yaml.safe_load(settings_path.read_text())
        packages = yaml.safe_load(packages_path.read_text())
        if not isinstance(settings, dict) or not isinstance(settings.get("sequence"), list) or not isinstance(packages, dict):
            raise CubeError("Unsupported Calamares job configuration")
        execution = next((group.get("exec") for group in settings["sequence"] if isinstance(group, dict) and isinstance(group.get("exec"), list)), None)
        if not execution or "umount" not in execution or "packages" not in execution:
            raise CubeError("Custom Calamares execution order needs manual installer integration")
        if execution.index("packages") > execution.index("umount"):
            raise CubeError("Calamares package cleanup must run before unmounting the installed system")
        update_initramfs = p.rootfs / "usr/sbin/update-initramfs"
        dracut = p.rootfs / "usr/bin/dracut"
        replacements = {}
        if "dracut" in execution and not dracut.is_file():
            if not update_initramfs.is_file():
                raise CubeError("The installer requires dracut, and no update-initramfs fallback exists in the customized root")
            for module in ("initramfscfg", "initramfs", "shellprocess"):
                candidates = list((p.rootfs / "usr/lib").glob(f"*/calamares/modules/{module}/module.desc"))
                candidates += list((p.rootfs / "usr/lib/calamares/modules").glob(f"{module}/module.desc"))
                if not candidates:
                    raise CubeError(f"The Calamares {module} module is missing; cannot adapt the installer safely")
            replacements = {"dracutlukscfg": "initramfscfg", "dracut": "initramfs"}
        if not update_initramfs.is_file():
            raise CubeError("This layered Calamares installation requires initramfs-tools; install it before building")
        originals = [(configuration, document), (settings_path, settings), (packages_path, packages)]
        for path, _ in originals:
            backup = p.directory / ("calamares-original-" + path.name)
            if not backup.exists():
                atomic_text(backup, path.read_text())
                self.user_ownership(backup)
        new_mappings = []
        inserted = False
        exclude = sorted({item for entry in matching for item in entry.get("exclude", [])})
        for entry in mappings:
            if entry in matching:
                if not inserted:
                    replacement = {"source": "/cdrom" + install_path, "sourcefs": "squashfs", "destination": "", "condition": True}
                    if exclude:
                        replacement["exclude"] = exclude
                    new_mappings.append(replacement)
                    inserted = True
            else:
                new_mappings.append(entry)
        document["unpack"] = new_mappings
        execution[:] = [replacements.get(job, job) for job in execution]
        finalize_id = "cube-editor-install-finalize"
        finalize_job = "shellprocess@" + finalize_id
        instances = settings.setdefault("instances", [])
        if not isinstance(instances, list):
            raise CubeError("Unsupported Calamares instances configuration")
        if not any(isinstance(item, dict) and item.get("id") == finalize_id for item in instances):
            instances.append({"id": finalize_id, "module": "shellprocess", "config": finalize_id + ".conf"})
        if finalize_job not in execution:
            execution.insert(execution.index("umount"), finalize_job)
        status_path = p.rootfs / "var/lib/dpkg/status"
        installed = set()
        if status_path.is_file():
            for paragraph in status_path.read_text(errors="replace").split("\n\n"):
                fields = dict(line.split(": ", 1) for line in paragraph.splitlines() if ": " in line and not line.startswith(" "))
                if fields.get("Status") == "install ok installed":
                    installed.add(fields.get("Package"))
        live_packages = sorted(installed & {"casper", "calamares", "lubuntu-calamares-settings", "lubuntu-installer-prompt", "ubiquity", "ubiquity-casper"})
        operations = packages.setdefault("operations", [])
        if not isinstance(operations, list):
            raise CubeError("Unsupported Calamares package operations")
        if live_packages and {"remove": live_packages} not in operations:
            operations.append({"remove": live_packages})
        prepare_id = "cube-editor-install-prepare"
        prepare_job = "shellprocess@" + prepare_id
        if not any(isinstance(item, dict) and item.get("id") == prepare_id for item in instances):
            instances.append({"id": prepare_id, "module": "shellprocess", "config": prepare_id + ".conf"})
        if prepare_job not in execution:
            execution.insert(execution.index("packages"), prepare_job)
        keep_packages = ["initramfs-tools"] + (["cryptsetup-initramfs"] if "cryptsetup-initramfs" in installed else [])
        prepare_config = guest_destination(p.rootfs, "/etc/calamares/modules/" + prepare_id + ".conf")
        prepare = {"dontChroot": False, "timeout": 60, "script": ["apt-mark manual " + " ".join(keep_packages)]}
        final_config = guest_destination(p.rootfs, "/etc/calamares/modules/" + finalize_id + ".conf")
        # This job runs inside the installed target, after package selections.
        # The live runtime retains Casper and its initramfs configuration.
        finalize = {"dontChroot": False, "timeout": 600, "script": [
            "rm -f /etc/initramfs-tools/conf.d/casperize.conf",
            "update-initramfs -u -k all"]}
        for path, data in originals + [(prepare_config, prepare), (final_config, finalize)]:
            atomic_text(path, yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
        installer = guest_destination(p.iso_tree, install_path)
        installer.parent.mkdir(parents=True, exist_ok=True)
        self.user_ownership(installer.parent)
        self.save(installer_image=install_path, installer_mode="complete-customized-root")
        self.emit("log", "Calamares will install the complete customized root; original configurations are backed up in the project")
        return installer

    def compress_tree(self, destination):
        temporary = destination.with_name("." + destination.name + ".building")
        if temporary.exists() or temporary.is_symlink():
            raise CubeError(f"An unfinished filesystem compression exists: {temporary.name}")
        try:
            self.run(["mksquashfs", str(self.project.rootfs), str(temporary), "-percentage", "-noappend", "-comp", self.project.data.get("compression", "xz"),
                      *build_resources(self.project.data), "-wildcards", "-e", "proc/*", "sys/*", "dev/*", "run/*", "tmp/*"])
            os.replace(temporary, destination)
            self.user_ownership(destination)
        finally:
            temporary.unlink(missing_ok=True)

    def refresh_metadata(self):
        p = self.project
        squash = guest_destination(p.iso_tree, p.data["squashfs_path"])
        base = squash.with_suffix("")
        metadata = [Path(str(base) + suffix) for suffix in (".manifest", ".manifest.full", ".size")]
        metadata += [squash.parent / name for name in ("filesystem.manifest-desktop", "vmlinuz", "initrd")]
        metadata.append(p.iso_tree / ".disk/info")
        for destination in metadata:
            guest_destination(p.iso_tree, "/" + str(destination.relative_to(p.iso_tree)))
        dpkg = p.rootfs / "usr/bin/dpkg-query"
        if dpkg.exists():
            manifest = self.guest(["/usr/bin/dpkg-query", "-W", "-f=${Package} ${Version}\\n"], capture=True)
            atomic_text(Path(str(base) + ".manifest"), manifest)
            if Path(str(base) + ".manifest.full").exists():
                atomic_text(Path(str(base) + ".manifest.full"), manifest)
            if base.name == "filesystem":
                excluded = set()
                for removal in squash.parent.glob("*.manifest-remove"):
                    excluded.update(line.split()[0] for line in removal.read_text().splitlines() if line.strip())
                desktop = squash.parent / "filesystem.manifest-desktop"
                if desktop.exists():
                    atomic_text(desktop, "".join(line + "\n" for line in manifest.splitlines() if line.split()[0] not in excluded))
        size = 0
        for relative in tree_paths(p.rootfs):
            item = p.rootfs / relative
            if not item.is_symlink() and item.is_file():
                size += item.stat().st_size
        atomic_text(Path(str(base) + ".size"), str(size) + "\n")
        refresh_kernel(p, self.emit, self.check_cancel)
        info = p.iso_tree / ".disk/info"
        if info.exists() and not info.is_symlink():
            atomic_text(info, p.data["name"] + " — Cube Editor — " + utcnow() + "\n")

    def deletion_whiteouts(self):
        p = self.project
        if len(p.data.get("inspection", {}).get("layer_chain", [])) <= 1:
            return []
        baseline = json.loads((p.directory / "baseline-paths.json").read_text())
        if not isinstance(baseline, list) or any(not isinstance(item, str) or Path(item).is_absolute()
                or ".." in Path(item).parts or item in ("", ".") for item in baseline):
            raise CubeError("The project baseline inventory contains an unsafe path")
        removed = []
        missing = set()
        for relative in sorted(baseline, key=lambda item: (item.count("/"), item)):
            destination = p.rootfs / relative
            if any((p.rootfs / parent).is_symlink() for parent in Path(relative).parents if str(parent) != "."):
                continue
            if destination.exists() or destination.is_symlink():
                continue
            if any(str(parent) in missing for parent in Path(relative).parents if str(parent) != "."):
                continue
            if destination.parent.is_dir() and not destination.parent.is_symlink():
                removed.append(destination)
                missing.add(relative)
        journal = p.directory / '.whiteouts.json'
        atomic_json(journal, [str(path.relative_to(p.rootfs)) for path in removed])
        self.user_ownership(journal)
        created = []
        try:
            for path in removed:
                self.check_cancel()
                os.mknod(path, stat.S_IFCHR | 0o000, os.makedev(0, 0))
                created.append(path)
        except BaseException:
            for path in created: path.unlink(missing_ok=True)
            journal.unlink(missing_ok=True)
            raise
        return removed

    def build(self):
        self.ensure_extracted()
        self.ensure_clean_environment()
        self.check_storage('build')
        p = self.project
        if (p.directory / '.whiteouts.json').exists() or (p.directory / '.whiteouts.json').is_symlink():
            raise CubeError('Interrupted deletion markers need Recover project before building')
        compression = p.data.get("compression", "xz")
        if compression not in ("xz", "zstd", "gzip", "lz4", "lzo"):
            raise CubeError("Choose a supported squashfs compression")
        if p.output_path.exists():
            raise CubeError("That output ISO already exists. Choose a new output filename to keep the previous build.")
        squash = guest_destination(p.iso_tree, p.data["squashfs_path"])
        self.run(["mkdir", "-p", "--", str(p.output_path.parent)], user=True)
        self.save(state="building", last_error="")
        installer = self.prepare_installer()
        self.refresh_metadata()
        if installer:
            self.emit("progress", "Compressing a clean installation root", progress=0.05)
            self.compress_tree(installer)
        whiteouts = self.deletion_whiteouts()
        try:
            self.emit("progress", "Compressing your customized live filesystem", progress=0.1)
            self.compress_tree(squash)
        finally:
            for whiteout in whiteouts:
                whiteout.unlink(missing_ok=True)
            (p.directory / '.whiteouts.json').unlink(missing_ok=True)
        self.emit("progress", "Refreshing integrity checks", progress=0.65)
        inspection = p.data.get("inspection", {})
        checksum_tree(p.iso_tree, excluded=inspection.get("boot_image_paths", []) + [inspection.get("boot_catalog", "")], check_cancel=self.check_cancel)
        self.emit("progress", "Replaying the original BIOS and UEFI boot records", progress=0.75)
        release = rebuild_iso(p, lambda command: self.run(command, user=True), self.check_cancel)
        self.user_ownership(Path(str(p.output_path) + ".sha256"))
        self.user_ownership(Path(str(p.output_path) + ".release.json"))
        self.save(state="built", last_build=release, releases=[*p.data.get('releases', []), release])
        self.emit("progress", f"Release ready: {p.output_path}", progress=1.0)

    def execute(self):
        self.validate()
        signal.signal(signal.SIGTERM, self.request_cancel)
        signal.signal(signal.SIGINT, self.request_cancel)
        watcher = threading.Thread(target=self.watch_cancel, daemon=True)
        watcher.start()
        try:
            with self.project.lock():
                failure = None
                try:
                    self.user_ownership(self.project.directory / ".operation.lock")
                    self.emit("started", f"Starting {self.action}", pid=os.getpid())
                    if self.action in ('customize', 'terminal', 'packages', 'build'):
                        self.emit("log", "Trust requirement: image binaries and package scripts run with host root privileges. The chroot and namespaces are not a security sandbox; use only trusted images and recipes.")
                    getattr(self, self.action)()
                except (Exception, KeyboardInterrupt) as error:
                    failure = error
                finally:
                    # A failed or cancelled tool can still be writing the tree.
                    # Keep the project locked until it is reaped and unmounted.
                    try:
                        self.stop_child()
                    except Exception as error:
                        if failure is None:
                            failure = error
                    try:
                        self.cleanup_mounts()
                    except Exception as error:
                        if failure is None:
                            failure = error
                        else:
                            with contextlib.suppress(Exception):
                                self.emit("log", f"Guest mount cleanup also failed: {error}")
                if failure is not None:
                    cancelled = isinstance(failure, Cancelled) or self.cancelled.is_set()
                    self.save(state="cancelled" if cancelled else "error", last_error=str(failure))
                    self.emit("finished", str(failure), success=False, cancelled=cancelled)
                    return 130 if cancelled else 1
                self.emit("finished", f"{self.action.title()} complete", success=True)
                return 0
        except (Exception, KeyboardInterrupt) as error:
            # Lock acquisition and status-writing failures must also shut down
            # the controller cleanly, without saving over another operation.
            cancelled = isinstance(error, Cancelled) or self.cancelled.is_set()
            with contextlib.suppress(Exception):
                self.emit("finished", str(error), success=False, cancelled=cancelled)
            return 130 if cancelled else 1
        finally:
            self.stop_watcher.set()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Cube Editor authenticated image worker")
    parser.add_argument("--project", required=True)
    parser.add_argument("--action", required=True, choices=("extract", "customize", "terminal", "build", "snapshot", "restore", "recover", "packages"))
    parser.add_argument("--namespace", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--job", default="")
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        parser.error("Authenticate using Cube Editor's interactive terminal (sudo)")
    uid = int(os.environ.get("SUDO_UID", "0"))
    gid = int(os.environ.get("SUDO_GID", "0"))
    try:
        project = Project.load(args.project)
        if not args.namespace:
            Worker(project, uid, gid, args.action, args.job).validate()
            command = ["unshare", "--mount", "--pid", "--fork", "--kill-child", "/usr/bin/python3", str(Path(__file__).resolve()),
                       "--project", str(project.directory), "--action", args.action, "--namespace", "--job", args.job]
            return subprocess.call(command)
        subprocess.run(["mount", "--make-rprivate", "/"], check=True)
        return Worker(project, uid, gid, args.action, args.job).execute()
    except (CubeError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"cube-editor worker: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
