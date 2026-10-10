#!/usr/bin/env python3
"""Authenticated, private-namespace image operations. Apache-2.0.

Based on the ISO-editing workflow of Cubic, Copyright 2015, 2020 PJ Singh.
Privilege is requested per operation through sudo in an interactive terminal.
No sudoers or polkit policy is installed by Cube Editor.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time

from engine import Project, CubeError, Cancelled, atomic_json, atomic_text, guest_destination, inspect_iso, package_list, utcnow


def copy_payload(source, destination, root):
    """Copy files recursively, checking each destination before any write."""
    source, destination, root = Path(source), Path(destination), Path(root)
    guest_destination(root, "/" + str(destination.relative_to(root)))
    if source.is_symlink():
        if destination.exists():
            raise CubeError(f"Cannot replace existing payload destination with a symlink: {destination}")
        destination.symlink_to(os.readlink(source))
    elif source.is_dir():
        destination.mkdir(parents=True, exist_ok=True)
        for child in source.iterdir():
            copy_payload(child, destination / child.name, root)
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


def rebuild_iso(project, run, check_cancel=lambda: None):
    """Replay original BIOS/UEFI/system area, including hidden EFI partitions.

    Public helper is intentionally usable unprivileged for synthetic integration
    tests. ISO extraction and customization are done by Worker beforehand.
    """
    output = project.output_path
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() or output.is_symlink():
        raise CubeError(f"Output already exists. Choose another output name: {output.name}")
    temporary = output.with_name("." + output.name + ".building")
    if temporary.exists() or temporary.is_symlink():
        raise CubeError(f"An unfinished output already exists: {temporary}. Move it before retrying.")
    for suffix in (".sha256", ".release.json"):
        sidecar = Path(str(output) + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            raise CubeError(f"An existing release sidecar would be overwritten: {sidecar.name}")
    command = ["xorriso", "-indev", project.data["source_iso"], "-outdev", str(temporary),
               "-boot_image", "any", "replay", "-update_r", str(project.iso_tree), "/",
               "-volid", project.data.get("volume_id", "CUBE_EDITOR")[:32], "-commit", "-end"]
    promoted = False
    created = []
    try:
        run(command)
        digest = hashlib.sha256()
        with temporary.open("rb") as stream:
            for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                check_cancel()
                digest.update(block)
        os.replace(temporary, output)
        promoted = True
        sha = digest.hexdigest()
        sidecar = Path(str(output) + ".sha256")
        atomic_text(sidecar, f"{sha}  {output.name}\n")
        created.append(sidecar)
        release = {"path": str(output), "sha256": sha, "size_bytes": output.stat().st_size,
                   "completed_at": utcnow(), "source_iso": project.data["source_iso"],
                   "volume_id": project.data.get("volume_id"), "compression": project.data.get("compression"),
                   "recipe": project.data.get("recipe", {})}
        atomic_json(Path(str(output) + ".release.json"), release)
        created.append(Path(str(output) + ".release.json"))
        return release
    except BaseException:
        temporary.unlink(missing_ok=True)
        for artifact in created:
            artifact.unlink(missing_ok=True)
        if promoted:
            output.unlink(missing_ok=True)
        raise


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
        for name in ("cube-editor.json", "events.jsonl", ".operation.lock", "iso", "rootfs", "layers", "overlay", "baseline-paths.json", ".extracted"):
            if (p.directory / name).is_symlink():
                raise CubeError(f"Project control path cannot be a symlink: {name}")
        source = Path(p.data["source_iso"]).resolve()
        if not source.is_file():
            raise CubeError("Source ISO was moved or deleted")
        if source.stat().st_size != p.data.get("source_size") or source.stat().st_mtime_ns != p.data.get("source_mtime_ns"):
            raise CubeError("Source ISO changed since this project was created. Create a new project for the changed source.")
        p.output_path

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
            kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.child_interactive = interactive
        self.child = subprocess.Popen(command, **kwargs)
        lines = []
        if not interactive:
            for line in self.child.stdout:
                lines.append(line)
                self.emit("log", line.rstrip())
                print(line, end="", flush=True)
                self.check_cancel()
        code = self.child.wait()
        if self.child.stdout:
            self.child.stdout.close()
        self.child = None
        self.check_cancel()
        if code and check:
            raise CubeError(f"{command[0]} exited with status {code}: {''.join(lines)[-1200:]}")
        return "".join(lines) if capture else code

    def mount(self, command, target):
        self.run(command)
        self.mounts.append(str(target))

    def cleanup_mounts(self):
        for target in reversed(self.mounts):
            subprocess.run(["umount", "-l", "--", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.mounts.clear()

    def ensure_extracted(self):
        if not (self.project.directory / ".extracted").is_file() or not self.project.rootfs.is_dir():
            raise CubeError("Extract your source ISO first")
        if self.project.rootfs.stat().st_uid != 0:
            raise CubeError("The image root must retain root ownership. Extract it again in a new project.")

    def extract(self):
        p = self.project
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
            self.run(["unsquashfs", "-processors", "2", "-d", str(p.rootfs), str(p.iso_tree / chain[0].lstrip("/"))])
        else:
            layers = p.directory / "layers"
            layers.mkdir(mode=0o700)
            extracted = []
            for index, path in enumerate(chain):
                self.emit("progress", f"Extracting layer {index + 1}/{len(chain)}: {Path(path).name}", progress=0.15 + index * 0.18)
                target = layers / str(index)
                self.run(["unsquashfs", "-processors", "2", "-d", str(target), str(p.iso_tree / path.lstrip("/"))])
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
        self.save(state="ready", inspection=inspection)
        self.emit("progress", "Live filesystem is ready for customization", progress=1.0)

    @contextlib.contextmanager
    def chroot_environment(self):
        root = self.project.rootfs
        restored = []
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
                destination = root / relative
                if destination.parent.resolve().is_relative_to(root.resolve()) is False:
                    raise CubeError(f"Guest path escapes image: {relative}")
                backup = self.project.directory / (".backup-" + relative.replace("/", "-"))
                if backup.exists() or backup.is_symlink():
                    raise CubeError(f"Unrestored environment backup exists: {backup.name}")
                existed = destination.exists() or destination.is_symlink()
                if existed:
                    destination.rename(backup)
                restored.append((destination, backup, existed))
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(contents)
                destination.chmod(mode)
            yield
        finally:
            try:
                for destination, backup, existed in reversed(restored):
                    try:
                        if not destination.parent.resolve().is_relative_to(root.resolve()):
                            self.emit("log", f"Retained original backup because guest path changed: {backup}")
                            continue
                        if destination.is_dir() and not destination.is_symlink():
                            self.emit("log", f"Retained original backup because guest file became a directory: {backup}")
                            continue
                        destination.unlink(missing_ok=True)
                        if existed:
                            backup.rename(destination)
                    except OSError as error:
                        self.emit("log", f"Environment restoration needs attention: {error}; backup at {backup}")
            finally:
                self.cleanup_mounts()

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
            copy_payload(source, destination, self.project.rootfs)
            self.check_cancel()

    def customize(self):
        self.ensure_extracted()
        recipe = self.project.data.get("recipe", {})
        install = package_list(recipe.get("install_packages", []))
        remove = package_list(recipe.get("remove_packages", []))
        commands = recipe.get("commands", [])
        if isinstance(commands, str):
            commands = [commands] if commands.strip() else []
        if any(not isinstance(command, str) for command in commands):
            raise CubeError("Custom commands must be text")
        self.save(state="customizing", last_error="")
        self.payloads(recipe)
        desktop = recipe.get("desktop_deb", "")
        staged_deb = None
        if desktop:
            source = Path(desktop).expanduser().resolve()
            if not source.is_file() or source.suffix != ".deb":
                raise CubeError("Select an existing desktop .deb package")
            staged_deb = guest_destination(self.project.rootfs, "/tmp/cube-editor-desktop.deb")
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
            self.run(["mksquashfs", str(self.project.rootfs), str(temporary), "-noappend", "-comp", self.project.data.get("compression", "xz"),
                      "-processors", "2", "-wildcards", "-e", "proc/*", "sys/*", "dev/*", "run/*", "tmp/*"])
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
        # Refresh the kernel/initramfs as a matched pair when both were generated
        # inside the guest. Retain original filenames referenced by boot menus.
        boot = guest_destination(p.rootfs, "/boot")
        kernels = sorted((path for path in boot.glob("vmlinuz-*") if path.is_file() and not path.is_symlink()), key=lambda path: path.stat().st_mtime_ns)
        if kernels:
            kernel = kernels[-1]
            initrd = kernel.with_name("initrd.img-" + kernel.name.removeprefix("vmlinuz-"))
            guest_destination(p.rootfs, "/" + str(initrd.relative_to(p.rootfs)))
            if initrd.is_file() and (squash.parent / "vmlinuz").exists() and (squash.parent / "initrd").exists():
                shutil.copy2(kernel, squash.parent / "vmlinuz")
                shutil.copy2(initrd, squash.parent / "initrd")
                self.emit("log", f"Refreshed live kernel and initramfs: {kernel.name}")
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
                os.mknod(destination, stat.S_IFCHR | 0o000, os.makedev(0, 0))
                removed.append(destination)
                missing.add(relative)
        return removed

    def build(self):
        self.ensure_extracted()
        p = self.project
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
        self.emit("progress", "Refreshing integrity checks", progress=0.65)
        inspection = p.data.get("inspection", {})
        checksum_tree(p.iso_tree, excluded=inspection.get("boot_image_paths", []) + [inspection.get("boot_catalog", "")], check_cancel=self.check_cancel)
        self.emit("progress", "Replaying the original BIOS and UEFI boot records", progress=0.75)
        release = rebuild_iso(p, lambda command: self.run(command, user=True), self.check_cancel)
        self.user_ownership(Path(str(p.output_path) + ".sha256"))
        self.user_ownership(Path(str(p.output_path) + ".release.json"))
        self.save(state="built", last_build=release)
        self.emit("progress", f"Release ready: {p.output_path}", progress=1.0)

    def execute(self):
        self.validate()
        signal.signal(signal.SIGTERM, self.request_cancel)
        signal.signal(signal.SIGINT, self.request_cancel)
        watcher = threading.Thread(target=self.watch_cancel, daemon=True)
        watcher.start()
        locked = False
        try:
            with self.project.lock():
                locked = True
                self.user_ownership(self.project.directory / ".operation.lock")
                self.emit("started", f"Starting {self.action}", pid=os.getpid())
                getattr(self, self.action)()
                self.emit("finished", f"{self.action.title()} complete", success=True)
            return 0
        except (Exception, KeyboardInterrupt) as error:
            cancelled = isinstance(error, Cancelled) or self.cancelled.is_set()
            if locked:
                self.save(state="cancelled" if cancelled else "error", last_error=str(error))
            self.emit("finished", str(error), success=False, cancelled=cancelled)
            return 130 if cancelled else 1
        finally:
            if self.child and self.child.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    if self.child_interactive:
                        self.child.kill()
                    else:
                        os.killpg(self.child.pid, signal.SIGKILL)
                self.child.wait()
            self.cleanup_mounts()
            self.stop_watcher.set()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Cube Editor authenticated image worker")
    parser.add_argument("--project", required=True)
    parser.add_argument("--action", required=True, choices=("extract", "customize", "terminal", "build"))
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
