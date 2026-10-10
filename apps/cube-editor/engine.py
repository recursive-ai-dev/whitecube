#!/usr/bin/env python3
"""Cube Editor's project and process API. GPL-3.0-or-later.

ISO workflow informed by Cubic, Copyright 2015, 2020 PJ Singh.
The original sources and copyright notices are retained in upstream/.
This module never needs root; privileged filesystem work lives in worker.py.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

DEFAULT_PROJECTS = Path.home() / "cubic/work"
DEFAULT_OUTPUT = Path.home() / "cubic/complete"
DEFAULT_SOURCE = Path.home() / "cubic/iso/lubuntu-26.04.1-desktop-amd64.iso"
WORKER = Path(__file__).resolve().with_name("worker.py")
REQUIRED = {"xorriso": "Read and replay ISO boot records", "unsquashfs": "Extract live filesystems",
            "mksquashfs": "Rebuild live filesystems", "sudo": "Authenticate filesystem operations",
            "unshare": "Keep mounts private", "foot": "Interactive terminal and authentication",
            "chroot": "Run commands inside the image", "mount": "Private guest mounts"}
OPTIONAL = {"qemu-system-x86_64": "Boot-test the completed ISO", "dpkg-deb": "Inspect desktop packages"}


class CubeError(RuntimeError):
    pass


class Cancelled(CubeError):
    pass


def utcnow():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_text(path, contents):
    """Replace a metadata file without following an existing leaf symlink."""
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def dependencies():
    return [{"name": name, "purpose": purpose, "available": bool(shutil.which(name)), "required": required}
            for required, mapping in ((True, REQUIRED), (False, OPTIONAL)) for name, purpose in mapping.items()]


def package_list(value):
    items = value.split() if isinstance(value, str) else list(value or [])
    for package in items:
        if not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*(?::[a-z0-9_-]+)?(?:=[A-Za-z0-9.+:~_-]+)?", package):
            raise CubeError(f"Invalid package name: {package!r}")
    return items


def guest_destination(root, destination):
    """Resolve a guest path without following a symlink outside the image."""
    root = Path(root).resolve()
    parts = Path(destination).parts
    if not destination.startswith("/") or ".." in parts or destination == "/":
        raise CubeError("Choose an absolute destination inside the image, such as /etc/example.conf")
    current = root
    for part in parts[1:]:
        current = current / part
        if current.is_symlink():
            raise CubeError(f"Payload destination crosses a symlink: {destination}")
    if not current.is_relative_to(root):
        raise CubeError("Payload destination escapes the image")
    return current


class Project:
    def __init__(self, directory, data):
        self.directory = Path(directory).expanduser().resolve()
        self.data = data
        self._lock_depth = 0

    @classmethod
    def create(cls, source_iso, name="My Cube", base_dir=DEFAULT_PROJECTS, output_dir=DEFAULT_OUTPUT):
        source = Path(source_iso).expanduser().resolve()
        if not source.is_file():
            raise CubeError(f"Source ISO does not exist: {source}")
        base = Path(base_dir).expanduser().resolve()
        base.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:48] or "cube"
        directory = base / (slug + "-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
        directory.mkdir(mode=0o700)
        data = {"schema": 1, "id": uuid.uuid4().hex, "name": name.strip() or "My Cube",
                "created_at": utcnow(), "updated_at": utcnow(), "source_iso": str(source),
                "source_size": source.stat().st_size, "source_mtime_ns": source.stat().st_mtime_ns,
                "output_dir": str(Path(output_dir).expanduser().resolve()), "output_name": slug + ".iso",
                "volume_id": "CUBE_EDITOR", "compression": "xz", "state": "new", "squashfs_path": "",
                "inspection": {}, "recipe": {"install_packages": [], "remove_packages": [],
                    "commands": [], "payloads": [], "desktop_deb": ""}, "last_error": "", "last_build": {}}
        project = cls(directory, data)
        project.save()
        project.remember()
        return project

    @classmethod
    def load(cls, path):
        directory = Path(path).expanduser()
        if directory.name == "cube-editor.json":
            directory = directory.parent
        with (directory / "cube-editor.json").open() as stream:
            data = json.load(stream)
        if data.get("schema") != 1:
            raise CubeError("This project uses an unsupported schema")
        return cls(directory, data)

    def save(self, **updates):
        if not self._lock_depth:
            with self.lock():
                # A stale window may update recipe fields, but must preserve
                # the most recent worker state and release metadata.
                path = self.directory / "cube-editor.json"
                if path.exists():
                    self.data = json.loads(path.read_text())
                return self.save(**updates)
        self.data.update(updates)
        self.data["updated_at"] = utcnow()
        atomic_json(self.directory / "cube-editor.json", self.data)
        return self

    def remember(self):
        path = Path.home() / ".config/cube-editor/recent.json"
        recent = recent_projects(raw=True)
        recent = [str(self.directory)] + [p for p in recent if p != str(self.directory)]
        atomic_json(path, recent[:30])

    @property
    def rootfs(self):
        return self.directory / "rootfs"

    @property
    def iso_tree(self):
        return self.directory / "iso"

    @property
    def output_path(self):
        name = self.data.get("output_name", "cube-editor.iso")
        if Path(name).name != name or not name.lower().endswith(".iso"):
            raise CubeError("Output name must be an ISO filename without directory components")
        output = Path(self.data["output_dir"]).expanduser().resolve() / name
        if output == Path(self.data["source_iso"]).resolve():
            raise CubeError("The output cannot overwrite your source ISO")
        return output

    @contextlib.contextmanager
    def lock(self):
        if self._lock_depth:
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1
            return
        with (self.directory / ".operation.lock").open("a+") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise CubeError("Another operation is already using this project") from error
            try:
                self._lock_depth = 1
                yield
            finally:
                self._lock_depth = 0
                fcntl.flock(stream, fcntl.LOCK_UN)

    def check_available(self):
        with self.lock():
            return True


def recent_projects(raw=False):
    try:
        entries = json.loads((Path.home() / ".config/cube-editor/recent.json").read_text())
    except (OSError, ValueError):
        entries = []
    paths = [p for p in entries if isinstance(p, str) and (Path(p) / "cube-editor.json").is_file()]
    if raw:
        return paths
    projects = []
    for path in paths:
        try:
            project = Project.load(path)
            projects.append({**project.data, "directory": str(project.directory)})
        except (OSError, ValueError, CubeError):
            continue
    return projects


def inspect_iso(source_iso, selected=""):
    source = Path(source_iso).expanduser().resolve()
    if not source.is_file():
        raise CubeError(f"Source ISO does not exist: {source}")
    try:
        result = subprocess.run(["xorriso", "-indev", str(source), "-pvd_info", "-report_el_torito", "plain",
            "-find", "/", "-name", "*.squashfs", "-exec", "echo"], capture_output=True, text=True, timeout=120)
    except FileNotFoundError as error:
        raise CubeError("Install xorriso to inspect ISO images") from error
    if result.returncode:
        raise CubeError(result.stderr.strip() or "xorriso could not read the ISO")
    text = result.stdout + "\n" + result.stderr
    paths = []
    for line in result.stdout.splitlines():
        if line.startswith("'"):
            try:
                path = shlex.split(line)[0]
            except (ValueError, IndexError):
                continue
            if path.endswith(".squashfs") and path.startswith(("/casper/", "/live/")):
                paths.append(path)
    paths = sorted(set(paths), key=lambda p: (p.count("."), len(p), p))
    if selected and selected not in paths:
        raise CubeError("The selected squashfs is not present in this ISO")
    chosen = selected or (paths[-1] if paths else "")
    stem = chosen.removesuffix(".squashfs")
    chain = [p for p in paths if p == chosen or stem.startswith(p.removesuffix(".squashfs") + ".")]
    warnings = []
    if not paths:
        warnings.append("No Debian/Ubuntu live squashfs was found under /casper or /live.")
    if len(paths) > len(chain):
        warnings.append("This ISO contains alternative live roots. Select the intended squashfs before extraction.")
    volume = re.search(r"Volume Id\s*:\s*(.+)", text)
    boot_paths = re.findall(r"El Torito img path\s*:\s*\d+\s+(/[^\n]+)", text)
    catalog = re.search(r"El Torito cat path\s*:\s*(/[^\n]+)", text)
    return {"volume_id": volume.group(1).strip() if volume else source.stem[:32], "size_bytes": source.stat().st_size,
            "squashfs_paths": paths, "squashfs_path": chosen, "layer_chain": chain, "layers": len(chain) > 1,
            "can_rebuild": bool(paths), "bootable": "El Torito boot img" in text,
            "bios": bool(re.search(r"El Torito boot img.*BIOS", text)),
            "uefi": bool(re.search(r"El Torito boot img.*UEFI", text)), "warnings": warnings, "kernel_paths": [],
            "extracted": False, "boot_image_paths": boot_paths, "boot_catalog": catalog.group(1).strip() if catalog else ""}


class Engine:
    def __init__(self, project, on_event=None):
        self.project = project
        self.on_event = on_event or (lambda event: None)
        self.process = None
        self._cancelled = threading.Event()
        self.job_id = uuid.uuid4().hex
        self._worker_started = False
        self._cancel_time = None

    def emit(self, event):
        self.on_event(event)

    def inspect_source(self):
        self.project.check_available()
        result = inspect_iso(self.project.data["source_iso"], self.project.data.get("squashfs_path", ""))
        result["extracted"] = (self.project.directory / ".extracted").exists()
        if result["extracted"]:
            result["kernel_paths"] = [str(p.relative_to(self.project.rootfs)) for p in (self.project.rootfs / "boot").glob("vmlinuz*")]
        self.project.save(inspection=result, squashfs_path=result["squashfs_path"])
        return result

    def worker_command(self, action):
        if action not in ("extract", "customize", "terminal", "build"):
            raise CubeError(f"Unknown worker action: {action}")
        return ["foot", "--title", f"Cube Editor · {action.title()}", "sudo", "--", "/usr/bin/python3",
                str(WORKER), "--project", str(self.project.directory), "--action", action, "--job", self.job_id]

    def extract_command(self):
        return self.worker_command("extract")

    def customize_command(self):
        return self.worker_command("customize")

    def terminal_command(self):
        return self.worker_command("terminal")

    def build_command(self):
        return self.worker_command("build")

    def plan(self, action):
        """Human-reviewable argv arrays; generating a plan changes no image."""
        p = self.project
        selected = p.data.get("squashfs_path") or "/casper/filesystem.squashfs"
        if action == "extract":
            commands = [["xorriso", "-osirrox", "on", "-indev", p.data["source_iso"], "-extract", "/", str(p.iso_tree)]]
            chain = p.data.get("inspection", {}).get("layer_chain", [selected])
            targets = []
            for index, squash in enumerate(chain):
                target = p.directory / "layers" / str(index) if len(chain) > 1 else p.rootfs
                targets.append(target)
                commands.append(["unsquashfs", "-processors", "2", "-d", str(target), str(p.iso_tree / squash.lstrip("/"))])
            if len(chain) > 1:
                commands += [["mount", "-t", "overlay", "overlay", "-o", "ro,lowerdir=" + ":".join(map(str, reversed(targets))), str(p.directory / "overlay")],
                             ["cp", "-a", str(p.directory / "overlay") + "/.", str(p.rootfs)]]
            return commands
        if action == "customize":
            recipe = p.data.get("recipe", {})
            commands = []
            install = package_list(recipe.get("install_packages", []))
            remove = package_list(recipe.get("remove_packages", []))
            if install or recipe.get("desktop_deb"):
                commands.append(["chroot", str(p.rootfs), "/usr/bin/apt-get", "update"])
            if install:
                commands.append(["chroot", str(p.rootfs), "/usr/bin/apt-get", "install", "-y", "--", *install])
            if remove:
                commands.append(["chroot", str(p.rootfs), "/usr/bin/apt-get", "purge", "-y", "--", *remove])
            if recipe.get("desktop_deb"):
                commands.append(["chroot", str(p.rootfs), "/usr/bin/apt-get", "install", "-y", "/tmp/cube-editor-desktop.deb"])
            for command in recipe.get("commands", []):
                commands.append(["chroot", str(p.rootfs), "/bin/bash", "-euc", command])
            return commands
        if action == "terminal":
            return [["chroot", str(p.rootfs), "/usr/bin/env", "TERM=xterm-256color", "/bin/bash", "--login"]]
        if action == "build":
            return [["mksquashfs", str(p.rootfs), str(p.iso_tree / selected.lstrip("/")), "-noappend", "-comp", p.data.get("compression", "xz")],
                    ["xorriso", "-indev", p.data["source_iso"], "-outdev", str(p.output_path), "-boot_image", "any", "replay",
                     "-update_r", str(p.iso_tree), "/", "-volid", p.data.get("volume_id", "CUBE_EDITOR"), "-commit", "-end"]]
        raise CubeError(f"Unknown action: {action}")

    def run(self, command):
        """Launch an argv array, following the worker event log through foot."""
        if not isinstance(command, (tuple, list)) or not command or any(not isinstance(arg, str) for arg in command):
            raise CubeError("Commands must be argument arrays")
        self._cancelled.clear()
        self._worker_started = False
        self._cancel_time = None
        cancel_file = self.project.directory / ".cancel"
        cancel_file.unlink(missing_ok=True)
        events = self.project.directory / "events.jsonl"
        offset = events.stat().st_size if events.exists() else 0
        self.emit({"type": "state", "message": "Starting interactive worker"})
        try:
            self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
        except FileNotFoundError as error:
            raise CubeError(f"Required command is missing: {command[0]}") from error
        output_lines = []
        def read_output():
            for line in self.process.stdout:
                output_lines.append(line)
                self.emit({"type": "log", "message": line.rstrip()})
        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        terminal = None
        partial = ""
        while self.process.poll() is None:
            if events.exists():
                with events.open() as stream:
                    stream.seek(offset)
                    chunk = stream.read()
                    offset = stream.tell()
                partial += chunk
                lines = partial.split("\n")
                partial = lines.pop()
                for line in lines:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if event.get("job") not in (None, self.job_id):
                        continue
                    self.emit(event)
                    if event.get("type") == "started":
                        self._worker_started = True
                    if event.get("type") == "finished":
                        terminal = event
            if self._cancelled.is_set() and not self._worker_started and self._cancel_time and time.monotonic() - self._cancel_time > 1:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGTERM)
            time.sleep(0.1)
        reader.join(timeout=2)
        # The final event can arrive between the last tail and terminal exit.
        if events.exists():
            with events.open() as stream:
                stream.seek(offset)
                for line in (partial + stream.read()).splitlines():
                    try:
                        event = json.loads(line)
                        if event.get("job") not in (None, self.job_id):
                            continue
                        self.emit(event)
                        if event.get("type") == "finished":
                            terminal = event
                    except ValueError:
                        continue
        code = self.process.returncode
        if self.process.stdout:
            self.process.stdout.close()
        self.process = None
        if self._cancelled.is_set() or (terminal and terminal.get("cancelled")):
            raise Cancelled("Operation cancelled. The original ISO is unchanged.")
        if terminal:
            if not terminal.get("success"):
                raise CubeError(terminal.get("message", "Worker failed"))
            return terminal
        if "foot" == Path(command[0]).name:
            raise CubeError("Worker did not finish. Authentication may have been cancelled, or the terminal closed. " + "".join(output_lines)[-1000:])
        if code:
            raise CubeError(f"Command exited with status {code}")
        return {"success": True, "returncode": code}

    def cancel(self):
        self._cancelled.set()
        self._cancel_time = time.monotonic()
        (self.project.directory / ".cancel").touch()
        # The privileged worker observes the file, terminates its process group,
        # and unwinds guest mounts before foot exits. No sudo kill is necessary.
        if self.process and Path(self.process.args[0]).name != "foot":
            os.killpg(self.process.pid, signal.SIGTERM)

    def qemu_command(self, memory=4096, uefi=False):
        iso = self.project.output_path
        if not iso.is_file():
            raise CubeError("Build an ISO before opening the virtual machine")
        if not shutil.which("qemu-system-x86_64"):
            raise CubeError("Install qemu-system-x86 to test your image")
        command = ["qemu-system-x86_64", "-m", str(max(512, min(int(memory), 65536))), "-smp", "2",
                   "-boot", "d", "-cdrom", str(iso), "-nic", "user,model=virtio-net-pci", "-display", "gtk", "-snapshot"]
        if os.access("/dev/kvm", os.R_OK | os.W_OK):
            command += ["-enable-kvm", "-cpu", "host"]
        if uefi:
            firmware = next((p for p in (Path("/usr/share/OVMF/OVMF_CODE_4M.fd"), Path("/usr/share/OVMF/OVMF_CODE.fd")) if p.is_file()), None)
            if firmware is None:
                raise CubeError("Install ovmf for a UEFI boot test")
            command += ["-bios", str(firmware)]
        return command

    def verify_release(self):
        iso = self.project.output_path
        sidecar = Path(str(iso) + ".sha256")
        if not iso.is_file() or not sidecar.is_file():
            raise CubeError("The ISO and its SHA256 sidecar must both exist")
        fields = sidecar.read_text().split()
        if not fields or not re.fullmatch(r"[a-fA-F0-9]{64}", fields[0]):
            raise CubeError("The SHA256 sidecar is empty or malformed")
        expected = fields[0].lower()
        digest = hashlib.sha256()
        with iso.open("rb") as stream:
            for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                digest.update(block)
        actual = digest.hexdigest()
        return {"success": actual == expected, "expected": expected, "actual": actual, "path": str(iso)}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Cube Editor ISO workflow")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("dependencies")
    inspect = sub.add_parser("inspect")
    inspect.add_argument("iso")
    create = sub.add_parser("create")
    create.add_argument("iso")
    create.add_argument("--name", default="My Cube")
    create.add_argument("--base-dir", default=str(DEFAULT_PROJECTS))
    create.add_argument("--output-dir", default=str(DEFAULT_OUTPUT))
    for operation in ("plan", "run", "verify"):
        p = sub.add_parser(operation)
        p.add_argument("project")
        if operation != "verify":
            p.add_argument("action", choices=("extract", "customize", "terminal", "build"))
    args = parser.parse_args(argv)
    try:
        if args.command == "dependencies":
            value = dependencies()
        elif args.command == "inspect":
            value = inspect_iso(args.iso)
        elif args.command == "create":
            project = Project.create(args.iso, args.name, args.base_dir, args.output_dir)
            Engine(project).inspect_source()
            value = {"directory": str(project.directory), **project.data}
        else:
            engine = Engine(Project.load(args.project), lambda e: print(json.dumps(e), flush=True) if args.command == "run" else None)
            if args.command == "plan":
                value = {"action": args.action, "commands": engine.plan(args.action), "launch": engine.worker_command(args.action), "dry_run": True}
            elif args.command == "verify":
                value = engine.verify_release()
            else:
                value = engine.run(engine.worker_command(args.action))
        print(json.dumps(value, indent=2))
        return 1 if args.command == "verify" and not value["success"] else 0
    except (CubeError, OSError, ValueError) as error:
        print(f"cube-editor: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
