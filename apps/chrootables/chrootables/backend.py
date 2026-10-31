"""Validated desktop API for the image manager.

Adapted for Whitecube: external sessions prefer Whitecube Terminal and exports
use the desktop user's sudo identity when dropping privileges.
"""
from __future__ import annotations

import codecs
from contextlib import contextmanager
import fcntl
from datetime import datetime
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import signal
import stat
import subprocess
import threading
import time
import uuid
from .paths import store_path

ROOT = Path(__file__).resolve().parent.parent
MANAGER = ROOT / "chrootable-manager"
DEFAULT_STORE = Path("/var/lib/chrootable-manager")
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
OPERATION_ID_RE = re.compile(r"[a-f0-9]{32}\Z")
EXPORT_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\.tar\.gz\Z")
MUTATIONS = {
    "create", "import", "clone", "snapshot", "restore", "drop-snapshot",
    "resize", "mode", "repair", "delete", "export", "setup",
    "recover-operation", "discard-operation", "prune-logs", "delete-export", "archive-sync",
}
# Cancelling is allowed only where stopping abandons staged work. Publication,
# expansion, repair and package installation change live data and must finish.
CANCELLABLE_ACTIONS = {"create", "import", "clone", "snapshot", "restore", "resize", "export", "archive-sync"}
CANCELLABLE_STAGES = {
    "starting", "allocating", "formatting", "bootstrapping", "importing", "exporting",
    "copying", "copying-snapshot", "copying-restore", "downloading-archive",
}
CANCEL_RESEND_SECONDS = 1.0


class OperationCancelled(RuntimeError):
    """The user cancelled an operation and it stopped before publishing anything."""


def load_capabilities():
    """Use the installed CLI as the desktop's capability authority, once at start."""
    try:
        result = subprocess.run([str(MANAGER), "--capabilities", "--json"],
                                capture_output=True, text=True, timeout=5,
                                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "CHROOTABLE_AUTO_INSTALL": "0"})
        data = json.loads(result.stdout)
        limits = data["limits"]
        valid = (result.returncode == 0 and type(data.get("schema_version")) is int
                 and data["schema_version"] == 1
                 and all(type(limits[key]) is int for key in ("min_size_mib", "max_size_mib"))
                 and 512 <= limits["min_size_mib"] <= limits["max_size_mib"] <= 1048576)
        for key in ("profiles", "suites"):
            items = data[key]
            valid = (valid and isinstance(items, list) and bool(items)
                     and all(isinstance(item, dict) and isinstance(item.get("id"), str)
                             and NAME_RE.fullmatch(item["id"])
                             and isinstance(item.get("title"), str) for item in items)
                     and len({item["id"] for item in items}) == len(items))
        valid = (valid and all(isinstance(item.get("description"), str)
                              and type(item.get("recommended_size_mib")) is int
                              and limits["min_size_mib"] <= item["recommended_size_mib"] <= limits["max_size_mib"]
                              for item in data["profiles"]))
        if not valid:
            raise ValueError("invalid fields")
        return data
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError("Could not read installed workspace manager capabilities: " + str(error)) from error
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise RuntimeError("The installed workspace manager returned invalid capabilities.") from error


CAPABILITIES = load_capabilities()
PROFILES = {item["id"]: (item["title"], item["description"],
                         max(1, item["recommended_size_mib"] // 1024))
            for item in CAPABILITIES["profiles"]}
SUITES = {item["id"]: item["title"] for item in CAPABILITIES["suites"]}


def name(value):
    if not isinstance(value, str) or not NAME_RE.fullmatch(value):
        raise ValueError("Use 1–64 letters, digits, dots, underscores or hyphens; start with a letter or digit.")
    return value


def size(value):
    if (type(value) is not int or not
            CAPABILITIES["limits"]["min_size_mib"] <= value <= CAPABILITIES["limits"]["max_size_mib"]):
        raise ValueError("Capacity must be between 512 MiB and 1 TiB.")
    return str(value)


def operation_id(value):
    if not isinstance(value, str) or not OPERATION_ID_RE.fullmatch(value):
        raise ValueError("Operation ID must be 32 lowercase hexadecimal characters.")
    return value


def export_name(value):
    if not isinstance(value, str) or not EXPORT_NAME_RE.fullmatch(value):
        raise ValueError("Invalid export archive filename.")
    return value


def keep_count(value):
    if type(value) is not int or not 0 <= value <= 10000:
        raise ValueError("Keep count must be a whole number from 0 to 10000.")
    return value


def archive_file(value):
    """Validate and resolve a host .tar.gz or .tgz archive without following a final symlink."""
    if not isinstance(value, str) or not value or any(c in value for c in "\x00\r\n"):
        raise ValueError("Choose an existing .tar.gz or .tgz archive with an absolute path.")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("Archive path must be an absolute path.")
    if os.path.islink(path):
        raise ValueError("Archive file must not be a symlink.")
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_file() or os.path.islink(resolved):
            raise ValueError("Archive must be an existing regular file.")
    except (OSError, RuntimeError) as error:
        raise ValueError("Archive file is unavailable.") from error
    if any(c in str(resolved) for c in "\r\n") or not str(resolved).endswith((".tar.gz", ".tgz")):
        raise ValueError("Archive must be a .tar.gz or .tgz file.")
    return str(resolved)


def build_command(action, params):
    """Return an allowlisted argv and environment additions, never a command string."""
    if not isinstance(params, dict):
        raise ValueError("Parameters must be an object.")
    args = [str(MANAGER)]
    env = {}
    if action == "setup":
        return args + ["--setup"], {"CHROOTABLE_AUTO_INSTALL": "1"}
    if action not in MUTATIONS:
        raise ValueError("Unsupported operation.")
    if action in ("recover-operation", "discard-operation"):
        if params.get("confirmed") is not True:
            raise ValueError("This operation requires confirmation.")
        return args + ["--" + action, operation_id(params.get("id")), "--yes"], env
    if action == "prune-logs":
        if params.get("confirmed") is not True:
            raise ValueError("This operation requires confirmation.")
        keep = keep_count(params.get("keep", 20))
        return args + ["--prune-logs", str(keep), "--yes"], env
    if action == "delete-export":
        if params.get("confirmed") is not True:
            raise ValueError("This operation requires confirmation.")
        return args + ["--delete-export", export_name(params.get("filename")), "--yes"], env
    if action == "archive-sync":
        suite = params.get("suite", "trixie")
        if suite not in SUITES and suite != "stable":
            raise ValueError("Unsupported Debian release.")
        profiles = params.get("profiles", [])
        if not isinstance(profiles, list) or any(not isinstance(p, str) or p not in PROFILES for p in profiles):
            raise ValueError("Unsupported profile.")
        return args + ["--archive-sync", suite, *profiles], env
    target = name(params.get("name"))
    args += ["--" + action, target]
    if action == "create":
        mode = params.get("lifecycle", "persistent")
        profile = params.get("profile", "minimal")
        suite = params.get("suite", "trixie")
        if mode not in ("persistent", "ephemeral") or profile not in PROFILES or suite not in SUITES:
            raise ValueError("Unsupported lifecycle, profile or Debian release.")
        args += [size(params.get("size_mib")), mode, profile]
        env["CHROOTABLE_DEBIAN_SUITE"] = suite
    elif action == "import":
        mode = params.get("lifecycle", "persistent")
        profile = params.get("profile", "minimal")
        suite = params.get("suite", "trixie")
        if mode not in ("persistent", "ephemeral") or profile not in PROFILES or suite not in SUITES:
            raise ValueError("Unsupported lifecycle, profile or Debian release.")
        # An empty capacity asks the manager to fit the image to the archive.
        requested = params.get("size_mib")
        args += [archive_file(params.get("archive")), "" if requested is None else size(requested), mode, profile]
        env["CHROOTABLE_DEBIAN_SUITE"] = suite
    elif action in ("clone", "snapshot", "restore", "drop-snapshot"):
        args.append(name(params.get("target")))
    elif action == "resize":
        args.append(size(params.get("size_mib")))
    elif action == "mode":
        if params.get("lifecycle") not in ("persistent", "ephemeral"):
            raise ValueError("Choose persistent or ephemeral.")
        args.append(params["lifecycle"])
    if action in ("restore", "drop-snapshot", "repair", "delete"):
        if params.get("confirmed") is not True:
            raise ValueError("This operation requires confirmation.")
        args.append("--yes")
    return args, env


def read_regular(path, limit=256 * 1024, offset=None):
    """Read bounded regular files without following a final symlink."""
    return _read_regular_chunk(path, limit, offset)[0]


@contextmanager
def _open_regular(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Expected a regular file.")
        stream = os.fdopen(descriptor, "rb")
        descriptor = None  # The stream now owns the descriptor.
        with stream:
            yield stream, info
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_regular_chunk(path, limit=256 * 1024, offset=None):
    """Return bounded text and the byte position actually consumed."""
    with _open_regular(path) as (stream, info):
        start = max(0, info.st_size - limit) if offset is None else (
            offset if 0 <= offset <= info.st_size else 0)
        stream.seek(start)
        data = stream.read(max(0, min(limit, info.st_size - start)))
        return data.decode("utf-8", errors="replace"), stream.tell()


class _OperationLogTail:
    """Stream bounded chunks without losing bytes or splitting UTF-8 characters."""

    def __init__(self, path, output):
        self.path = path
        self.output = output
        self.offset = 0
        self.identity = None
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def _emit(self, data=b"", *, final=False):
        text = self.decoder.decode(data, final=final)
        if text:
            self.output(text)

    def read(self, *, final=False):
        try:
            with _open_regular(self.path) as (stream, info):
                identity = (info.st_dev, info.st_ino)
                if self.identity != identity or info.st_size < self.offset:
                    # An incomplete character belongs to the old file, never to
                    # its replacement or to bytes written after truncation.
                    self._emit(final=True)
                    self.decoder.reset()
                    self.offset = 0
                self.identity = identity
                stream.seek(self.offset)
                # Limit each live poll so cancellation remains responsive. The
                # final read drains the entire size observed when it opens the log.
                remaining = info.st_size - self.offset
                if not final:
                    remaining = min(remaining, 4 * 256 * 1024)
                while remaining:
                    data = stream.read(min(remaining, 256 * 1024))
                    if not data:
                        break
                    self.offset = stream.tell()
                    remaining -= len(data)
                    self._emit(data)
        except (OSError, ValueError):
            pass  # A log may not yet exist, or may have been pruned/replaced.
        finally:
            if final:
                self._emit(final=True)


def _probe_lock_status(lock_path):
    try:
        fd = os.open(lock_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return "ready"
    except OSError:
        return "ready" if os.path.islink(lock_path) else "unknown"
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return "ready"
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return "busy"
        fcntl.flock(fd, fcntl.LOCK_UN)
        return "ready"
    finally:
        os.close(fd)


def deliver_export(archive, destination):
    """Open the archive as root; create the user's destination after dropping privileges."""
    target = Path(destination)
    if not target.is_absolute():
        raise ValueError("Export destination must be an absolute path.")
    uid = int(os.environ.get("SUDO_UID") or os.environ.get("PKEXEC_UID") or os.getuid())
    if uid == 0:
        raise ValueError("Desktop exports need the invoking user's identity.")
    account = pwd.getpwuid(uid)
    source = os.open(archive, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(source).st_mode):
            raise ValueError("Export source must be a regular archive file.")
    except BaseException:
        os.close(source)
        raise
    script = '''import os, shutil, sys
source, destination = int(sys.argv[1]), sys.argv[2]
created = False
try:
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    created = True
    with os.fdopen(fd, "wb") as output, os.fdopen(source, "rb") as archive:
        try:
            while True:
                if os.copy_file_range(archive.fileno(), output.fileno(), 1 << 30) == 0:
                    break
        except (AttributeError, OSError):
            shutil.copyfileobj(archive, output, 1024 * 1024)
        output.flush()
        os.fsync(output.fileno())
except BaseException:
    if created:
        os.unlink(destination)
    raise
'''
    try:
        options = ({"user": uid, "group": account.pw_gid,
                    "extra_groups": os.getgrouplist(account.pw_name, account.pw_gid)}
                   if os.geteuid() == 0 else {})
        result = subprocess.run(["/usr/bin/python3", "-I", "-S", "-c", script, str(source), str(target)],
                                pass_fds=(source,), capture_output=True, text=True, **options)
        if result.returncode:
            raise RuntimeError("Archive created, but saving the chosen copy failed: " + result.stderr.strip())
    finally:
        os.close(source)


def metadata(path):
    try:
        return dict(line.split("=", 1) for line in read_regular(path).splitlines() if "=" in line)
    except FileNotFoundError:
        return {}


def _operation_state(path, identifier):
    """Read the CLI state schema without accepting duplicate or unknown fields."""
    allowed = {
        "format", "id", "pid", "process_start", "status", "stage", "action", "name",
        "pending", "destination", "image_inode", "image_size", "meta_inode", "meta_size",
    }
    text, consumed = _read_regular_chunk(path, 256 * 1024 + 1, offset=0)
    if consumed > 256 * 1024:
        raise ValueError("Operation record is too large.")
    lines = text.split("\n")
    if lines and not lines[-1]:
        lines.pop()
    state = {}
    for line in lines:
        field, separator, value = line.partition("=")
        if not separator or field not in allowed or field in state:
            raise ValueError("Invalid operation record fields.")
        state[field] = value
    if (state.get("format") != "1" or state.get("id") != identifier
            or not re.fullmatch(r"[1-9][0-9]*", state.get("pid", ""))
            or not re.fullmatch(r"[0-9]+", state.get("process_start", ""))
            or state.get("status") not in {"running", "completed", "failed", "interrupted", "recovered"}
            or not re.fullmatch(r"[a-z][a-z0-9-]{0,47}", state.get("stage", ""))
            or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", state.get("action", ""))
            or not NAME_RE.fullmatch(state.get("name", ""))):
        raise ValueError("Invalid operation record values.")
    return state


def _process_alive(pid, expected_start):
    try:
        if not isinstance(pid, str) or not pid.isdigit() or not expected_start:
            return False
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        after_comm = text.rsplit(") ", 1)[1].split()
        return len(after_comm) > 19 and after_comm[19] == expected_start
    except (OSError, IndexError, ValueError):
        return False


class Store:
    def __init__(self, path=DEFAULT_STORE):
        self.path = store_path(path)
        self._images_sig = None
        self._images_cache = []
        self._snap_meta_cache = {}
        self._activity_sig = None
        self._activity_cache = []
        self._operations_sig = None
        self._operations_cache = []
        self._operations_has_running = False
        self._archives_sig = None
        self._archives_cache = []
        self._exports_cache = []
        self._inventory_lock = threading.Lock()
        self._storage_cache = {}
        self._storage_signature = None
        self._storage_checked = 0.0
        self._active_lock = threading.Lock()
        self._active_process = None
        self._active_stage = ""
        self._active_action = ""
        self._cancel_requested = False
        self._cancel_signalled_at = 0.0

    def environment(self, extra=None):
        result = {
            "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": "C.UTF-8", "HOME": "/root" if os.geteuid() == 0 else str(Path.home()),
            "CHROOTABLE_HOME": str(self.path), "CHROOTABLE_AUTO_INSTALL": "0",
        }
        result.update(extra or {})
        return result

    def inventory(self):
        # Broker reads may overlap; publish each cache signature with its data.
        with self._inventory_lock:
            return self._inventory()

    def _inventory(self):
        # Check directories explicitly: glob can silently conceal permission errors.
        image_dir_entries = None
        if self.path.exists():
            for part in (self.path, self.path / "images", self.path / "snapshots", self.path / "logs", self.path / "exports"):
                if part.is_symlink():
                    raise ValueError("Store directories must not be symlinks.")
                if part.exists():
                    with os.scandir(part) as entries:
                        if part.name == "images":
                            image_dir_entries = []
                            for entry in entries:
                                try:
                                    if entry.is_symlink():
                                        image_dir_entries.append((entry.name, True, 0, 0, 0, 0))
                                    elif entry.name.endswith((".img", ".meta", ".mode")) and entry.is_file(follow_symlinks=False):
                                        st = entry.stat(follow_symlinks=False)
                                        image_dir_entries.append((entry.name, False, st.st_ino, st.st_mtime_ns, st.st_size, st.st_blocks))
                                except FileNotFoundError:
                                    continue
                            image_dir_entries.sort()
        snapshots_root = self.path / "snapshots"
        raw_snapshots = []
        snap_counts = {}
        if snapshots_root.is_dir() and not snapshots_root.is_symlink():
            with os.scandir(snapshots_root) as folder_entries:
                folders = sorted(
                    (e for e in folder_entries if not e.is_symlink() and e.is_dir(follow_symlinks=False) and NAME_RE.fullmatch(e.name)),
                    key=lambda e: e.name,
                )
            for folder in folders:
                imgs = []
                meta_stats = {}
                try:
                    with os.scandir(folder.path) as img_entries:
                        for entry in img_entries:
                            try:
                                if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                                    continue
                                if entry.name.endswith(".img") and NAME_RE.fullmatch(entry.name[:-4]):
                                    imgs.append(entry)
                                elif entry.name.endswith(".img.meta"):
                                    meta_stats[entry.name] = entry.stat(follow_symlinks=False)
                            except FileNotFoundError:
                                continue
                except FileNotFoundError:
                    continue
                imgs.sort(key=lambda e: e.name)
                snap_counts[folder.name] = 0
                for image in imgs:
                    try:
                        img_st = image.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    snap_counts[folder.name] += 1
                    meta_st = meta_stats.get(image.name + ".meta")
                    cache_key = (image.path, meta_st.st_ino, meta_st.st_mtime_ns, meta_st.st_size) if meta_st else None
                    if cache_key is not None and cache_key in self._snap_meta_cache:
                        created_at = self._snap_meta_cache[cache_key]
                    else:
                        meta = metadata(Path(image.path + ".meta")) if meta_st else {}
                        created_at = meta.get("snapshot_at", "unknown")
                        if cache_key is not None:
                            if len(self._snap_meta_cache) >= 2048:
                                self._snap_meta_cache.clear()
                            self._snap_meta_cache[cache_key] = created_at
                    raw_snapshots.append((folder.name, image.name[:-4], img_st.st_size // 1048576, created_at, img_st.st_blocks * 512))
        images_sig = (tuple(image_dir_entries), tuple(sorted(snap_counts.items()))) if image_dir_entries is not None else None
        if self._images_sig is not None and images_sig is not None and images_sig == self._images_sig:
            images = []
            images_root = self.path / "images"
            for item in self._images_cache:
                status = _probe_lock_status(images_root / (item["name"] + ".img.lock"))
                images.append(item if item["status"] == status else {**item, "status": status})
        else:
            # The helper probes locks in-process on both cold and cached reads.
            # Avoid one CLI subprocess per image solely to repeat that probe.
            result = subprocess.run([str(MANAGER), "--list", "--json"],
                                    env=self.environment({"CHROOTABLE_LIST_PROBE_LOCKS": "0"}),
                                    capture_output=True, encoding="utf-8", errors="replace", timeout=20)
            if result.returncode:
                raise PermissionError(result.stderr.strip() or "Cannot read the workspace store.")
            images = json.loads(result.stdout)
            images = [{**item, "status": _probe_lock_status(self.path / "images" / (item["name"] + ".img.lock"))}
                      for item in images]
            self._images_sig = images_sig
            self._images_cache = images
        image_names = {x["name"] for x in images}
        snapshots = [
            {"name": ws_name, "label": label, "size_mib": size_mib, "created_at": created_at,
             "allocated_bytes": allocated, "orphan": ws_name not in image_names}
            for ws_name, label, size_mib, created_at, allocated in raw_snapshots
        ]
        activity_path = self.path / "activity.tsv"
        try:
            act_st = activity_path.lstat()
            act_sig = (act_st.st_ino, act_st.st_mtime_ns, act_st.st_size)
            if act_sig == self._activity_sig:
                activity = self._activity_cache
            else:
                parsed = []
                for line in read_regular(activity_path).splitlines()[-100:]:
                    fields = line.split("\t")
                    if len(fields) == 5:
                        parsed.append(dict(zip(("time", "action", "name", "status", "log"), fields)))
                activity = list(reversed(parsed))
                self._activity_sig = act_sig
                self._activity_cache = activity
        except FileNotFoundError:
            self._activity_sig = None
            self._activity_cache = []
            activity = []
        existing = self.path
        while not existing.exists():
            existing = existing.parent
        usage = shutil.disk_usage(existing)
        storage = self.storage_summary(images, snapshots)
        return {"images": images, "snapshots": snapshots, "activity": activity,
                "operations": self._scan_operations(), "exports": list(self._exports_cache),
                "archives": self._scan_archives(),
                "store": str(self.path), "free_bytes": usage.free, "total_bytes": usage.total,
                "storage": storage}

    def _scan_operations(self):
        op_dir = self.path / "operations"
        try:
            info = op_dir.lstat()
        except FileNotFoundError:
            self._operations_sig = None
            self._operations_cache = []
            self._operations_has_running = False
            return []
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("Store directories must not be symlinks.")
        with os.scandir(op_dir) as entries:
            state_entries = sorted(
                (e for e in entries if not e.is_symlink() and e.name.endswith(".state") and e.is_file(follow_symlinks=False)),
                key=lambda e: e.name,
            )
        stamps = []
        for entry in state_entries:
            try:
                stamp = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            stamps.append((entry.name, stamp.st_ino, stamp.st_mtime_ns, stamp.st_size))
        sig = (info.st_ino, info.st_mtime_ns, tuple(stamps))
        if sig == self._operations_sig and not self._operations_has_running:
            return self._operations_cache
        records = []
        has_running = False
        for entry in state_entries:
            op_id = entry.name[:-6]
            if not OPERATION_ID_RE.fullmatch(op_id):
                continue
            try:
                state = _operation_state(entry.path, op_id)
            except FileNotFoundError:
                continue
            except ValueError:
                records.append({
                    "id": op_id, "action": "unknown", "name": "store", "status": "invalid",
                    "stage": "invalid-record", "log": str(self.path / "logs" / (op_id + ".log")),
                    "active": False, "recoverable": False, "discardable": True,
                    "reclaimable_bytes": 0, "invalid": True,
                })
                continue
            action = state.get("action", "")
            ws_name = state.get("name", "")
            status = state.get("status", "")
            stage = state.get("stage", "")
            active = _process_alive(state.get("pid", ""), state.get("process_start", ""))
            has_running = has_running or active
            if status == "running":
                has_running = True
                if not active:
                    status = "interrupted"
            recoverable = not active and status == "interrupted" and stage.startswith("publish-")
            discardable = not active and status == "interrupted"
            records.append({
                "id": op_id,
                "action": action,
                "name": ws_name,
                "status": status,
                "stage": stage,
                "log": str(self.path / "logs" / (op_id + ".log")),
                "active": active,
                "recoverable": recoverable,
                "discardable": discardable,
                "reclaimable_bytes": self._staged_bytes(state.get("pending", "")) if discardable else 0,
            })
        self._operations_sig = sig
        self._operations_cache = records
        self._operations_has_running = has_running
        return records

    def _staged_bytes(self, relative):
        """Allocated size of a staged file named by a record; zero when it cannot be read."""
        if not relative or relative.startswith("/") or ".." in relative.split("/"):
            return 0
        try:
            info = (self.path / relative).lstat()
        except OSError:
            return 0
        return info.st_blocks * 512 if stat.S_ISREG(info.st_mode) else 0

    def _scan_archives(self):
        arch_dir = self.path / "archive"
        try:
            info = arch_dir.lstat()
        except FileNotFoundError:
            self._archives_sig = None
            self._archives_cache = []
            return []
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("Store directories must not be symlinks.")
        sig = (info.st_ino, info.st_mtime_ns)
        if sig == self._archives_sig:
            return self._archives_cache
        archives = []
        with os.scandir(arch_dir) as entries:
            subdirs = sorted(
                (e for e in entries if not e.is_symlink() and not e.name.startswith(".") and e.is_dir(follow_symlinks=False)),
                key=lambda e: e.name,
            )
        for subdir in subdirs:
            sub_path = Path(subdir.path)
            base_tgz = sub_path / "base.tgz"
            mirror_file = sub_path / "mirror"
            if not base_tgz.is_file() or base_tgz.is_symlink() or not mirror_file.is_file() or mirror_file.is_symlink():
                continue
            suite, _, arch = subdir.name.rpartition("-")
            if not suite or not arch:
                continue
            try:
                profiles_raw = read_regular(sub_path / "profiles").splitlines() if (sub_path / "profiles").is_file() else []
            except OSError:
                profiles_raw = []
            profiles = ["minimal"] + [p.strip() for p in profiles_raw if p.strip() and p.strip() != "minimal"]
            total_bytes = 0
            for root_dir, _, files in os.walk(sub_path, followlinks=False):
                for fname in files:
                    fpath = Path(root_dir) / fname
                    try:
                        fst = fpath.lstat()
                    except OSError:
                        continue
                    if stat.S_ISREG(fst.st_mode):
                        total_bytes += fst.st_blocks * 512
            st = base_tgz.lstat()
            archives.append({
                "id": subdir.name,
                "suite": suite,
                "architecture": arch,
                "profiles": profiles,
                "size_bytes": total_bytes,
                "updated_at": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            })
        self._archives_sig = sig
        self._archives_cache = archives
        return archives

    def storage_summary(self, images, snapshots):
        """Report allocated blocks, caching archive/log scans between changes.

        Shared CoW extents can be counted in more than one file. Archive/log sizes
        refresh immediately after directory changes, and at most 30 seconds after
        an existing log is appended; workspace locks are still probed every poll.
        """
        directories = [self.path / "exports", self.path / "logs"]
        signature = []
        for directory in directories:
            try:
                info = directory.lstat()
                if not stat.S_ISDIR(info.st_mode):
                    raise ValueError("Store directories must be regular directories.")
                signature.append((info.st_ino, info.st_mtime_ns))
            except FileNotFoundError:
                signature.append(None)
        now = time.monotonic()
        if signature != self._storage_signature or now - self._storage_checked >= 30:
            totals = {}
            exports_list = []
            for directory, stamp in zip(directories, signature):
                total = 0
                if stamp is not None:
                    with os.scandir(directory) as entries:
                        for entry in entries:
                            try:
                                info = entry.stat(follow_symlinks=False)
                            except FileNotFoundError:
                                continue
                            if stat.S_ISREG(info.st_mode):
                                total += info.st_blocks * 512
                                if directory.name == "exports" and EXPORT_NAME_RE.fullmatch(entry.name):
                                    exports_list.append({
                                        "filename": entry.name,
                                        "path": entry.path,
                                        "size_bytes": info.st_size,
                                        "allocated_bytes": info.st_blocks * 512,
                                        "updated_at": datetime.fromtimestamp(info.st_mtime).isoformat(timespec="seconds"),
                                    })
                totals[directory.name + "_bytes"] = total
            exports_list.sort(key=lambda item: (item["updated_at"], item["filename"]), reverse=True)
            self._exports_cache = exports_list
            self._storage_cache = totals
            self._storage_signature = signature
            self._storage_checked = now
        return {**self._storage_cache,
                "images_bytes": sum(item["allocated_bytes"] for item in images),
                "snapshots_bytes": sum(item["allocated_bytes"] for item in snapshots)}

    def logs(self, target, selected_log=None):
        name(target)
        if selected_log:
            path = Path(selected_log)
            if path.parent != self.path / "logs":
                raise ValueError("Log is outside this workspace store.")
            target_path = str(path)
            if not any(
                len(fields := line.split("\t")) == 5 and fields[2] == target and fields[4] == target_path
                for line in read_regular(self.path / "activity.tsv").splitlines()
            ):
                raise ValueError("Log does not belong to the selected workspace operation.")
            try:
                return "Log: " + target_path + "\n\n" + read_regular(path)
            except FileNotFoundError as error:
                raise RuntimeError("This operation log has been pruned.") from error
        result = subprocess.run([str(MANAGER), "--logs", name(target)], env=self.environment(), capture_output=True,
                                encoding="utf-8", errors="replace", timeout=15)
        if result.returncode:
            raise RuntimeError(result.stderr.strip())
        return result.stdout

    def doctor(self):
        result = subprocess.run([str(MANAGER), "--doctor", "--json"], env=self.environment(), capture_output=True,
                                encoding="utf-8", errors="replace", timeout=15)
        try:
            report = json.loads(result.stdout)
        except (ValueError, TypeError) as error:
            raise RuntimeError(result.stderr.strip() or "Host health returned an invalid report.") from error
        if (not isinstance(report, dict) or type(report.get("schema_version")) is not int
                or report["schema_version"] != 1
                or type(report.get("ready")) is not bool
                or not all(isinstance(report.get(key), str)
                           for key in ("output", "host", "architecture", "version", "store"))
                or not all(type(report.get(key)) is int and report[key] >= 0
                           for key in ("errors", "warnings"))
                or not isinstance(report.get("checks"), list)
                or result.returncode not in (0, 1)
                or report["ready"] != (result.returncode == 0)):
            raise RuntimeError("Host health returned an inconsistent report.")
        statuses = {"ok", "info", "warning", "error"}
        seen = set()
        for check in report["checks"]:
            if (not isinstance(check, dict)
                    or not all(isinstance(check.get(key), str)
                               for key in ("id", "status", "message", "remedy"))
                    or check["status"] not in statuses or not check["id"]
                    or check["id"] in seen):
                raise RuntimeError("Host health returned an invalid check.")
            seen.add(check["id"])
        if (report["errors"] != sum(check["status"] == "error" for check in report["checks"])
                or report["warnings"] != sum(check["status"] == "warning" for check in report["checks"])
                or report["ready"] != (report["errors"] == 0)):
            raise RuntimeError("Host health returned inconsistent issue counts.")
        storage = report.get("storage")
        if (not isinstance(storage, dict) or type(storage.get("available")) is not bool
                or not all(type(storage.get(key)) is int and storage[key] >= 0
                           for key in ("free_bytes", "total_bytes"))
                or storage["free_bytes"] > storage["total_bytes"]):
            raise RuntimeError("Host health returned invalid storage capacity.")
        return report

    def cancel_operation(self):
        with self._active_lock:
            process = self._active_process
            stage = self._active_stage
            if process is None or process.poll() is not None:
                raise RuntimeError("No running operation to cancel.")
            if self._active_action not in CANCELLABLE_ACTIONS or stage not in CANCELLABLE_STAGES:
                raise RuntimeError("This step changes live data and cannot be stopped safely. It will finish on its own.")
            self._cancel_requested = True
            # The manager's work happens in a subshell and its children, so only
            # signaling the whole group stops it. Publication ignores the signal.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError as error:
                raise RuntimeError("No running operation to cancel.") from error
            self._cancel_signalled_at = time.monotonic()
            return {"cancelled": True, "stage": stage}

    def _repeat_cancel(self, process):
        # A signal landing just before the shell forks misses the new child, and the
        # shell defers its trap until that child exits, so one signal can be lost.
        with self._active_lock:
            if (not self._cancel_requested or self._active_stage not in CANCELLABLE_STAGES
                    or time.monotonic() - self._cancel_signalled_at < CANCEL_RESEND_SECONDS):
                return
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
            self._cancel_signalled_at = time.monotonic()

    def operate(self, action, params, output, progress=None):
        if action == "import" and params.get("export") is not None:
            # A retained export is named, not given by path: the caller may be
            # unable to see inside the root-only store. Resolved here, as root.
            params = {**params, "archive": str(self.path / "exports" / export_name(params["export"]))}
        args, extra = build_command(action, params)
        operation_id = uuid.uuid4().hex
        env = self.environment({**extra, "CHROOTABLE_OPERATION_ID": operation_id})
        operation_log = self.path / "logs" / (operation_id + ".log")
        # A reader thread drains the child while the main thread tails its operation log.
        # A private process group lets cancellation reach every child at once.
        process = subprocess.Popen(args, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   encoding="utf-8", errors="replace", start_new_session=True)
        with self._active_lock:
            self._active_process = process
            self._active_stage = "starting"
            self._active_action = action
            self._cancel_requested = False
        archives = []
        def drain():
            for line in process.stdout:
                if line.startswith("CHROOTABLE_EVENT "):
                    try:
                        event = json.loads(line[len("CHROOTABLE_EVENT "):])
                    except (ValueError, TypeError):
                        event = None
                    if (isinstance(event, dict) and event.get("id") == operation_id
                            and isinstance(event.get("stage"), str)
                            and 0 < len(event["stage"]) <= 256
                            and not any(ord(character) < 32 for character in event["stage"])):
                        with self._active_lock:
                            self._active_stage = event["stage"]
                        if progress:
                            progress(event["stage"])
                        continue
                candidate = Path(line.strip())
                if action == "export" and candidate.parent == self.path / "exports" and candidate.suffixes[-2:] == [".tar", ".gz"]:
                    archives.append(candidate)
                output(line)
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        log_tail = _OperationLogTail(operation_log, output)
        try:
            while process.poll() is None:
                log_tail.read()
                self._repeat_cancel(process)
                time.sleep(0.2)
            reader.join()
            process.stdout.close()
            log_tail.read(final=True)
        finally:
            with self._active_lock:
                self._active_process = None
                self._active_stage = ""
                self._active_action = ""
                cancelled = self._cancel_requested
                self._cancel_requested = False
        self._storage_signature = None
        self._operations_sig = None
        self._archives_sig = None
        # A cancel that arrived during publication loses the race: exit 0 means the
        # work really finished, so it is reported as finished.
        if cancelled and process.returncode != 0:
            raise OperationCancelled(f"{action.replace('-', ' ').capitalize()} was cancelled. Staged files were removed.")
        if process.returncode == 0 and action == "export" and params.get("destination"):
            if len(archives) != 1:
                raise RuntimeError("Could not identify the completed export archive.")
            deliver_export(archives[0], params["destination"])
            output("Saved export: " + params["destination"] + "\n")
        return process.returncode


def shared_folder(value):
    """Validate and resolve one host directory before handing it to sudo."""
    if not isinstance(value, str) or not value or any(c in value for c in "\x00\r\n"):
        raise ValueError("Choose an existing host folder with an absolute path.")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("Shared folder must be an absolute path.")
    try:
        path = path.resolve(strict=True)
        if not path.is_dir():
            raise ValueError("Shared folder must be an existing directory.")
    except (OSError, RuntimeError) as error:
        raise ValueError("Shared folder is unavailable. Choose an existing host folder.") from error
    if path == Path("/"):
        raise ValueError("Choose a folder other than the host filesystem root.")
    if any(c in str(path) for c in "\r\n"):
        raise ValueError("Shared folder paths cannot contain line breaks.")
    return str(path)


def session_command(target, store=DEFAULT_STORE, *, disposable=False, isolated=False,
                    share=None, read_only=False, restricted_dev=False):
    """The validated sudo argv that starts a workspace session."""
    target = name(target)
    if not isinstance(read_only, bool) or not isinstance(restricted_dev, bool):
        raise ValueError("Read-only and restricted-devices must be true or false.")
    if read_only and share is None:
        raise ValueError("Choose a shared folder before enabling read-only access.")
    action = "--try" if disposable else "--enter-isolated" if isolated else "--enter"
    if not shutil.which("sudo") and shutil.which("pkexec"):
        # pkexec clears the environment, which sudo's env_reset would have kept.
        command = ["pkexec", "/usr/bin/env", "TERM=" + os.environ.get("TERM", "xterm-256color"),
                   "LANG=" + os.environ.get("LANG", "C.UTF-8")]
    else:
        command = ["sudo", "/usr/bin/env"]
    command += ["CHROOTABLE_AUTO_INSTALL=0", "CHROOTABLE_HOME=" + str(store), str(MANAGER), action, target]
    if disposable and isolated:
        command.append("--isolated")
    if restricted_dev:
        command.append("--restricted-dev")
    if share is not None:
        command += ["--share", shared_folder(share)]
        if read_only:
            command.append("--read-only")
    return command


def terminal_command(target, store=DEFAULT_STORE, *, disposable=False, isolated=False,
                     share=None, read_only=False, restricted_dev=False):
    command = session_command(target, store, disposable=disposable, isolated=isolated,
                              share=share, read_only=read_only, restricted_dev=restricted_dev)
    title = "Cube Chrootables · " + target
    for terminal in ("whitecube-terminal", "foot"):
        if shutil.which(terminal):
            return [terminal, "--title", title] + command
    if shutil.which("konsole"):
        return ["konsole", "--separate", "--hold", "-p", "tabtitle=" + target, "-e"] + command
    if shutil.which("gnome-terminal"):
        return ["gnome-terminal", "--wait", "--"] + command
    if shutil.which("ptyxis"):
        return ["ptyxis", "--standalone", "--title", title, "--"] + command
    if shutil.which("kgx"):
        return ["kgx", "--title", title, "--"] + command
    if shutil.which("xfce4-terminal"):
        return ["xfce4-terminal", "--disable-server", "--hold", "--title", title, "-x"] + command
    if shutil.which("mate-terminal"):
        return ["mate-terminal", "--disable-factory", "--title", title, "-x"] + command
    if shutil.which("tilix"):
        return ["tilix", "--title", title, "-e"] + command
    if shutil.which("alacritty"):
        return ["alacritty", "--hold", "--title", title, "-e"] + command
    if shutil.which("kitty"):
        return ["kitty", "--hold", "--title", title, "--"] + command
    if shutil.which("wezterm"):
        return ["wezterm", "start", "--always-new-process", "--"] + command
    if shutil.which("foot"):
        return ["foot", "--hold", "--title", title, "--"] + command
    if shutil.which("xterm"):
        return ["xterm", "-hold", "-T", title, "-e"] + command
    raise RuntimeError("Install a supported terminal emulator (Konsole, GNOME Terminal, Ptyxis, GNOME Console, XFCE Terminal, Alacritty, Kitty, WezTerm, Foot or xterm) to open workspace shells.")
