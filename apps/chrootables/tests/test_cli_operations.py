"""Operation recovery and capacity checks using temporary files, without mounts/root."""
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import unittest


MANAGER = Path(__file__).resolve().parents[1] / "chrootable-manager"
OPERATION_ID = "a1" * 16


class CliOperationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = Path(self.temporary.name) / "store"
        for directory in ("images", "logs", "exports", "snapshots/lab", "operations"):
            (self.store / directory).mkdir(parents=True)
        self.image = self.store / "images/lab.img"
        self.image.write_bytes(b"original image")
        self.image.with_suffix(".img.meta").write_text("lifecycle=persistent\norigin=original\n")

    def bash(self, script, *arguments, identifier=OPERATION_ID):
        env = {**os.environ, "CHROOTABLE_OPERATION_ID": identifier}
        return subprocess.run(
            ["bash", "-c", 'source "$1"\nSTORE_DIR=$2\nconfigure_paths\numask 077\nshift 2\n' + script,
             "cli-operation-test", str(MANAGER), str(self.store), *map(str, arguments)],
            env=env, capture_output=True, text=True, timeout=15,
        )

    def state(self):
        return dict(line.split("=", 1) for line in
                    (self.store / "operations" / (OPERATION_ID + ".state")).read_text().splitlines())

    def interrupt_publication(self, point="image", abrupt=False):
        failure = 'kill -KILL "$BASHPID"' if abrupt else "return 19"
        result = self.bash('''
begin_operation restore lab
PENDING="$IMAGE_DIR/.lab.ABC123.partial"
printf 'replacement image' >"$PENDING"
printf 'lifecycle=ephemeral\\norigin=replacement\\n' >"$PENDING.meta"
mv() {
    local source=${@: -2:1}
    if [[ $source == "$PENDING''' + (".meta" if point == "metadata" else "") + '''" ]]; then
        ''' + failure + '''
    fi
    command mv "$@"
}
publish_image "$PENDING" "$IMAGE_DIR/lab.img"
''')
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_completed_operation_has_deterministic_log_record_and_stage_events(self):
        result = self.bash('''
begin_operation clone lab
operation_stage copying
printf 'diagnostic output\\n' >>"$OP_LOG"
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.state()
        self.assertEqual((state["id"], state["status"], state["stage"]),
                         (OPERATION_ID, "completed", "completed"))
        log = self.store / "logs" / (OPERATION_ID + ".log")
        self.assertIn("diagnostic output", log.read_text())
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        events = [json.loads(line.removeprefix("CHROOTABLE_EVENT "))
                  for line in result.stderr.splitlines() if line.startswith("CHROOTABLE_EVENT ")]
        self.assertEqual([event["stage"] for event in events], ["starting", "copying", "completed"])
        self.assertTrue(all(event["id"] == OPERATION_ID and event["log"] == str(log) for event in events))
        logs = self.bash('show_logs lab')
        self.assertEqual(logs.returncode, 0, logs.stderr)
        self.assertIn("diagnostic output", logs.stdout)

    def test_operation_ids_are_validated_and_existing_logs_never_overwritten(self):
        result = self.bash('begin_operation clone lab\nprintf first >>"$OP_LOG"')
        self.assertEqual(result.returncode, 0, result.stderr)
        original = (self.store / "logs" / (OPERATION_ID + ".log")).read_bytes()
        duplicate = self.bash('begin_operation clone lab')
        self.assertNotEqual(duplicate.returncode, 0)
        self.assertEqual((self.store / "logs" / (OPERATION_ID + ".log")).read_bytes(), original)
        for invalid in ("../escape", "A" * 32, "$(id)", "a" * 33):
            with self.subTest(identifier=invalid):
                result = self.bash('begin_operation clone lab', identifier=invalid)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("32 lowercase hexadecimal", result.stderr)

    def test_recovery_completes_failure_before_metadata_publication(self):
        self.interrupt_publication("metadata")
        self.assertEqual(self.image.read_bytes(), b"original image")
        self.assertIn("origin=original", self.image.with_suffix(".img.meta").read_text())
        self.assertEqual((self.state()["status"], self.state()["stage"]),
                         ("interrupted", "publish-prepared"))
        self.assert_recovers_publication()

    def test_recovery_completes_failure_between_metadata_and_image_publication(self):
        self.interrupt_publication()
        self.assertEqual(self.image.read_bytes(), b"original image")
        self.assertIn("origin=replacement", self.image.with_suffix(".img.meta").read_text())
        self.assertEqual(self.state()["stage"], "publish-metadata")
        self.assert_recovers_publication()

    def assert_recovers_publication(self):
        blocked = self.bash('lock_image "$IMAGE_DIR/lab.img" descriptor')
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("must be recovered first", blocked.stderr)
        result = self.bash('assert_offline() { :; }\nrecover_operation "$1"', OPERATION_ID)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.image.read_bytes(), b"replacement image")
        self.assertIn("origin=replacement", self.image.with_suffix(".img.meta").read_text())
        self.assertFalse((self.store / "images/.lab.ABC123.partial").exists())
        self.assertEqual(self.state()["status"], "recovered")
        self.assertEqual(self.bash('lock_image "$IMAGE_DIR/lab.img" descriptor').returncode, 0)

    def test_sigkill_is_visible_in_operations_and_read_only_doctor(self):
        result = self.interrupt_publication(abrupt=True)
        self.assertEqual(result.returncode, -9)
        self.assertEqual(self.state()["status"], "running")
        inventory = self.bash('list_operations json')
        self.assertEqual(inventory.returncode, 0, inventory.stderr)
        record = json.loads(inventory.stdout)[0]
        self.assertEqual((record["status"], record["active"]), ("interrupted", False))
        before = {path.relative_to(self.store): path.read_bytes() for path in self.store.rglob("*") if path.is_file()}
        doctor = self.bash('doctor json')
        report = json.loads(doctor.stdout)
        checks = {check["id"]: check for check in report["checks"]}
        check = checks["operation:" + OPERATION_ID]
        self.assertEqual(check["status"], "warning")
        self.assertIn("--recover-operation", check["remedy"])
        after = {path.relative_to(self.store): path.read_bytes() for path in self.store.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assert_recovers_publication()

    def test_recovery_handles_image_already_published_before_record_update(self):
        result = self.bash('''
begin_operation restore lab
PENDING="$IMAGE_DIR/.lab.ABC123.partial"
printf 'replacement image' >"$PENDING"
printf 'origin=replacement\\n' >"$PENDING.meta"
operation_stage() {
    OP_STAGE=$1
    if [[ $1 == published ]]; then kill -KILL "$BASHPID"; fi
    write_operation_state
}
publish_image "$PENDING" "$IMAGE_DIR/lab.img"
''')
        self.assertEqual(result.returncode, -9)
        self.assertEqual(self.image.read_bytes(), b"replacement image")
        self.assert_recovers_publication()

    def test_recovery_refuses_live_operation_and_preserves_copying_partials(self):
        live = self.bash('begin_operation clone lab\nrecover_operation "$1"', OPERATION_ID)
        self.assertNotEqual(live.returncode, 0)
        self.assertIn("still running", live.stderr)
        for path in (self.store / "operations").iterdir():
            path.unlink()
        for path in (self.store / "logs").iterdir():
            path.unlink()
        killed = self.bash('''
begin_operation clone lab
PENDING="$IMAGE_DIR/.copy.ABC123.partial"
printf incomplete >"$PENDING"
OP_PENDING=${PENDING#"$STORE_DIR/"}
operation_stage copying
kill -KILL "$BASHPID"
''')
        self.assertEqual(killed.returncode, -9)
        result = self.bash('recover_operation "$1"', OPERATION_ID)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("partial data has been retained", result.stderr)
        self.assertEqual((self.store / "images/.copy.ABC123.partial").read_bytes(), b"incomplete")
        self.assertEqual(self.image.read_bytes(), b"original image")

    def test_recovery_rejects_unsafe_paths_symlinks_and_invalid_records(self):
        self.interrupt_publication()
        record_path = self.store / "operations" / (OPERATION_ID + ".state")
        original = record_path.read_text()
        outside = self.store.parent / "outside.img"
        outside.write_bytes(b"outside")
        for destination in ("../outside.img", "/tmp/outside.img", "images/../outside.img"):
            with self.subTest(destination=destination):
                record_path.write_text(original.replace("destination=images/lab.img", "destination=" + destination))
                result = self.bash('recover_operation "$1"', OPERATION_ID)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(outside.read_bytes(), b"outside")
                self.assertEqual(self.image.read_bytes(), b"original image")
        record_path.write_text(original)
        pending = self.store / "images/.lab.ABC123.partial"
        pending.unlink()
        pending.symlink_to(outside)
        result = self.bash('recover_operation "$1"', OPERATION_ID)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(outside.read_bytes(), b"outside")
        record_path.write_text(original + "$(touch marker)=unsafe\n")
        result = self.bash('lock_image "$IMAGE_DIR/lab.img" descriptor')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Invalid operation record", result.stderr)
        self.assertFalse((self.store / "marker").exists())

    def test_recovery_never_labels_an_old_image_as_an_already_completed_publish(self):
        self.interrupt_publication("metadata")
        (self.store / "images/.lab.ABC123.partial").unlink()
        (self.store / "images/.lab.ABC123.partial.meta").unlink()
        result = self.bash('assert_offline() { :; }\nrecover_operation "$1"', OPERATION_ID)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing or inconsistent", result.stderr)
        self.assertEqual(self.image.read_bytes(), b"original image")
        self.assertEqual(self.state()["status"], "interrupted")

    def test_sparse_allocation_and_resize_capacity_preflight(self):
        sparse = self.store / "sparse.img"
        result = self.bash('OP_LOG="$STORE_DIR/allocation.log"\nallocate_image "$1" 512', sparse)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sparse.stat().st_size, 512 * 1048576)
        self.assertLess(sparse.stat().st_blocks * 512, 1048576)
        original = self.image.read_bytes()
        result = self.bash('''
assert_offline() { :; }
preflight_free_space() { fail 'Not enough free space'; }
begin_operation() { printf started >"$STORE_DIR/unexpected-operation"; }
resize_image "$IMAGE_DIR/lab.img" 512
''')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.image.read_bytes(), original)
        self.assertFalse((self.store / "unexpected-operation").exists())

    def test_failed_copy_removes_partial_and_never_publishes_destination(self):
        result = self.bash('''
assert_offline() { :; }
cp() { printf incomplete >"${@: -1}"; return 28; }
clone_image "$IMAGE_DIR/lab.img" copy
''')
        self.assertEqual(result.returncode, 28, result.stderr)
        self.assertEqual(self.image.read_bytes(), b"original image")
        self.assertFalse((self.store / "images/copy.img").exists())
        self.assertEqual(list((self.store / "images").glob("*.partial")), [])
        self.assertEqual(self.state()["status"], "failed")

    def test_copy_space_preflight_fails_before_copying_or_publishing(self):
        result = self.bash('''
assert_offline() { :; }
preflight_free_space() { fail 'Not enough free space'; }
cp() { printf copied >"$STORE_DIR/unexpected-copy"; }
clone_image "$IMAGE_DIR/lab.img" copy
''')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.store / "unexpected-copy").exists())
        self.assertFalse((self.store / "images/copy.img").exists())
        result = self.bash('''
df() { printf 'Filesystem 1-blocks Used Available Capacity Mounted on\\n/dev/example 100 99 1 99%% /store\\n'; }
preflight_free_space 2
''')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("need 2 bytes, have 1", result.stderr)

    def test_setup_uses_its_operation_log_for_dependency_output(self):
        result = self.bash('''
require_root() { :; }
check_prerequisites() { printf 'dependency install output\\n' >>"$CHROOTABLE_DEP_LOG"; }
main --setup
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.store / "logs" / (OPERATION_ID + ".log")
        self.assertIn("dependency install output", log.read_text())
        self.assertEqual((self.state()["action"], self.state()["status"]), ("setup", "completed"))

    def test_restricted_devices_create_pts_inside_the_new_tmpfs(self):
        root = self.store / "guest"
        (root / "dev").mkdir(parents=True)
        result = self.bash('''
WORKER_ROOT=$1
mount() {
    local target=${@: -1}
    if [[ $2 == tmpfs ]]; then
        # A real tmpfs hides any pts directory made before mounting /dev.
        rmdir -- "$target/pts" 2>/dev/null || true
    else
        [[ -d $target ]] || return 32
    fi
}
mknod() { touch -- "$3"; }
mount_restricted_devices
''', root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((root / "dev/pts").is_dir())
        self.assertTrue((root / "dev/ptmx").is_symlink())
        self.assertTrue((root / "dev/null").is_file())

    def test_capabilities_are_read_only_and_match_cli_validation(self):
        absent = self.store / "not-created"
        result = subprocess.run([str(MANAGER), "--capabilities", "--json"],
                                env={**os.environ, "CHROOTABLE_HOME": str(absent)},
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(absent.exists())
        capabilities = json.loads(result.stdout)
        self.assertEqual(capabilities["schema_version"], 1)
        self.assertEqual(capabilities["storage"], {"allocation": "sparse", "capacity_reserved": False})
        for profile in capabilities["profiles"]:
            with self.subTest(profile=profile["id"]):
                result = self.bash('validate_cli --create lab "$1" persistent "$2"',
                                   profile["recommended_size_mib"], profile["id"])
                self.assertEqual(result.returncode, 0, result.stderr)
        limits = capabilities["limits"]
        for size, valid in ((limits["min_size_mib"], True), (limits["max_size_mib"], True),
                            (limits["min_size_mib"] - 1, False), (limits["max_size_mib"] + 1, False),
                            ("9" * 100, False)):
            with self.subTest(size=size):
                result = self.bash('validate_cli --create lab "$1" persistent minimal', size)
                self.assertEqual(result.returncode == 0, valid)
        features = set(capabilities["features"])
        self.assertTrue({"import", "prune-logs", "delete-export", "recover-operation",
                         "discard-operation", "archive-sync"}.issubset(features))

    IMPORT_STUBS = """
make_workdir() { WORK="$STORE_DIR/work"; mkdir -p "$WORK"; }
mkfs.ext4() { :; }
"""

    def write_archive(self, name="sample.tar.gz", payload=b"13.0\n"):
        archive = self.store / "exports" / name
        root = self.store / "sample-root"
        (root / "etc").mkdir(parents=True, exist_ok=True)
        (root / "etc/debian_version").write_bytes(payload)
        subprocess.run(["tar", "-C", str(root), "-czf", str(archive), "."], check=True)
        return archive

    def write_tar(self, *members, name="crafted.tar.gz"):
        archive = self.store / "exports" / name
        with tarfile.open(archive, "w:gz") as output:
            for member in members:
                output.addfile(member, io.BytesIO(b"x" * member.size) if member.isreg() else None)
        return archive

    @staticmethod
    def member(name, kind=tarfile.REGTYPE, size=0, **fields):
        info = tarfile.TarInfo(name)
        info.type = kind
        info.size = size
        for key, value in fields.items():
            setattr(info, key, value)
        return info

    def test_import_image_validates_archive_and_publishes_metadata(self):
        archive = self.write_archive()
        result = self.bash(self.IMPORT_STUBS + """
run_worker() { printf 'suite=bookworm\\narchitecture=riscv64\\n' >"$WORK/import.info"; }
import_image imported "$1" 2048 persistent python
""", archive)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.store / "images/imported.img").exists())
        meta = (self.store / "images/imported.img.meta").read_text()
        self.assertIn("origin=import:sample.tar.gz", meta)
        self.assertIn("profile=python", meta)
        self.assertIn("suite=bookworm", meta)
        self.assertIn("architecture=riscv64", meta)
        self.assertEqual((self.state()["action"], self.state()["status"]), ("import", "completed"))

    def test_import_rejects_capacity_below_the_archive_requirement_before_writing(self):
        archive = self.write_archive()
        # Report a 700 MiB member without having to write one.
        result = self.bash(self.IMPORT_STUBS + """
tar() { printf -- '-rw-r--r-- 0/0 %s 2026-10-30 12:00 ./big\\n' 734003200; }
gzip() { :; }
run_worker() { :; }
import_image tooSmall "$1" 512 persistent minimal
""", archive)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("needs at least", result.stderr)
        self.assertFalse((self.store / "images/tooSmall.img").exists())
        self.assertFalse((self.store / "operations" / (OPERATION_ID + ".state")).exists())

    def test_import_without_a_size_fits_the_archive_with_headroom(self):
        archive = self.write_archive()
        result = self.bash(self.IMPORT_STUBS + """
tar() { printf -- '-rw-r--r-- 0/0 %s 2026-10-30 12:00 ./big\\n' 5368709120; }
gzip() { :; }
run_worker() { :; }
required=$(required_capacity_mib 5368709120 1)
import_image fitted "$1" "" persistent minimal
printf '%s\\n' "$required"
""", archive)
        self.assertEqual(result.returncode, 0, result.stderr)
        required = int(result.stdout.split()[-1])
        size = int(re.search(r"size_mib=(\d+)", (self.store / "images/fitted.img.meta").read_text()).group(1))
        self.assertEqual(size, required + required // 4)

    def test_validate_tar_archive_rejects_traversal_and_device_nodes(self):
        for member, message in (
                (self.member("../escaped", size=4), "Unsafe path"),
                (self.member("a/../../escaped", size=4), "Unsafe path"),
                (self.member("/absolute", size=4), "Unsafe path"),
                (self.member("link", tarfile.LNKTYPE, linkname="../../etc/passwd"), "Unsafe path"),
                (self.member("disk", tarfile.BLKTYPE, devmajor=8, devminor=0), "block device"),
                (self.member("mem", tarfile.CHRTYPE, devmajor=1, devminor=1), "device node"),
                (self.member("nvme", tarfile.CHRTYPE, devmajor=239, devminor=0), "device node")):
            with self.subTest(member=member.name):
                result = self.bash('validate_tar_archive "$1"', self.write_tar(member))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)

    def test_validate_tar_archive_accepts_a_normal_root_filesystem(self):
        archive = self.write_tar(
            self.member("./"  , tarfile.DIRTYPE), self.member("./etc", tarfile.DIRTYPE),
            self.member("./etc/name with -> arrow", size=3),
            self.member("./bin", tarfile.SYMTYPE, linkname="usr/bin"),
            self.member("./abs", tarfile.SYMTYPE, linkname="/usr/share/absolute-targets-are-normal"),
            self.member("./hard", tarfile.LNKTYPE, linkname="./etc/name with -> arrow"),
            self.member("./fifo", tarfile.FIFOTYPE),
            self.member("./dev/null", tarfile.CHRTYPE, devmajor=1, devminor=3),
            self.member("./dev/ptmx", tarfile.CHRTYPE, devmajor=5, devminor=2))
        result = self.bash('validate_tar_archive "$1"; printf "%s" "$IMPORT_REQUIRED_MIB"', archive)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreater(int(result.stdout), 0)

    def test_validate_tar_archive_rejects_unreadable_and_empty_archives(self):
        not_tar = self.store / "exports/plain.tar.gz"
        subprocess.run(["sh", "-c", 'printf "this is not a tar stream, only some text" | gzip > "$0"', str(not_tar)], check=True)
        empty = self.write_tar(name="empty.tar.gz")
        not_gzip = self.store / "exports/raw.tar.gz"
        not_gzip.write_bytes(b"raw bytes")
        for archive in (not_tar, empty, not_gzip):
            with self.subTest(archive=archive.name):
                self.assertNotEqual(self.bash('validate_tar_archive "$1"', archive).returncode, 0)

    @unittest.skipUnless(shutil.which("mkfs.ext4") or Path("/usr/sbin/mkfs.ext4").exists(), "mke2fs is required")
    def test_required_capacity_holds_the_payload_on_a_real_filesystem(self):
        path = os.pathsep.join([os.environ.get("PATH", ""), "/usr/sbin", "/sbin"])
        payloads = [size * 1048576 for size in (0, 1, 100, 400, 480, 1024, 4096, 16384, 70000, 200000)]
        payloads += [(threshold * 1048576 - 1) * 93 // 100 - journal * 1048576
                     for threshold, journal in ((1024, 16), (2048, 32))]
        for payload in payloads:
            with self.subTest(payload=payload):
                result = self.bash('required_capacity_mib "$1" 1000', payload)
                self.assertEqual(result.returncode, 0, result.stderr)
                capacity = int(result.stdout)
                image = Path(self.temporary.name) / "capacity.img"
                image.unlink(missing_ok=True)
                with image.open("wb") as handle:
                    handle.truncate(capacity * 1048576)
                env = {**os.environ, "PATH": path}
                subprocess.run(["mkfs.ext4", "-q", "-F", "-b", "4096", "-I", "256",
                                "-i", "16384", "-m", "5", str(image)],
                               check=True, env=env, capture_output=True)
                dump = subprocess.run(["dumpe2fs", "-h", str(image)], check=True, env=env,
                                      capture_output=True, text=True).stdout
                field = lambda label: int(re.search(rf"^{label}:\s+(\d+)", dump, re.M).group(1))
                unreserved = (field("Free blocks") - field("Reserved block count")) * field("Block size")
                self.assertGreaterEqual(unreserved, payload, f"capacity {capacity} MiB")
                self.assertGreaterEqual(field("Free inodes"), 1000)
                image.unlink()

    def test_required_capacity_honors_the_inode_bound(self):
        result = self.bash('required_capacity_mib 0 1000000')
        self.assertEqual(int(result.stdout), 15626)

    @unittest.skipUnless(shutil.which("mkfs.ext4") or Path("/usr/sbin/mkfs.ext4").exists(), "mke2fs is required")
    def test_required_capacity_leaves_room_for_initial_filesystem_inodes(self):
        env = {**os.environ, "PATH": os.pathsep.join([os.environ.get("PATH", ""), "/usr/sbin", "/sbin"])}
        image = Path(self.temporary.name) / "inode-capacity.img"
        for entries in (32752, 32768, 65536, 1000000):
            with self.subTest(entries=entries):
                result = self.bash('required_capacity_mib 0 "$1"', entries)
                self.assertEqual(result.returncode, 0, result.stderr)
                capacity = int(result.stdout)
                with image.open("wb") as handle:
                    handle.truncate(capacity * 1048576)
                subprocess.run(["mkfs.ext4", "-q", "-F", "-b", "4096", "-I", "256",
                                "-i", "16384", "-m", "5", str(image)],
                               check=True, env=env, capture_output=True)
                dump = subprocess.run(["dumpe2fs", "-h", str(image)], check=True, env=env,
                                      capture_output=True, text=True).stdout
                free_inodes = int(re.search(r"^Free inodes:\s+(\d+)", dump, re.M).group(1))
                self.assertGreaterEqual(free_inodes, entries, f"capacity {capacity} MiB")
                image.unlink()

    def test_required_capacity_accounts_for_journal_growth_after_mib_rounding(self):
        # Choose payloads whose estimate with the smaller journal falls in the
        # final MiB below each threshold; MiB rounding enters the larger tier.
        for threshold, small_journal, large_journal in (
                (1024, 16, 32), (2048, 32, 64), (16384, 64, 128),
                (32768, 128, 256), (65536, 256, 512), (131072, 512, 1024)):
            with self.subTest(threshold_mib=threshold):
                payload = (threshold * 1048576 - 1) * 93 // 100 - small_journal * 1048576
                result = self.bash('required_capacity_mib "$1" 0', payload)
                self.assertEqual(result.returncode, 0, result.stderr)
                capacity = int(result.stdout) * 1048576
                self.assertGreaterEqual(capacity * 93, (payload + large_journal * 1048576) * 100)

    def test_required_capacity_rejects_overflow_and_unsupported_payloads(self):
        for payload, entries in (("100000000000000000", "1"), ("9223372036854775807", "1"),
                                 ("1", "9223372036854775807"), ("1099511627777", "1"),
                                 ("1099511627776", "1"), ("0", "67108849"),
                                 ("-1", "1"), ("08", "1"), ("0", "1+1")):
            with self.subTest(payload=payload, entries=entries):
                result = self.bash('required_capacity_mib "$1" "$2"', payload, entries)
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertIn("capacity limit", result.stderr)
                self.assertEqual(result.stdout, "")
        result = self.bash('required_capacity_mib 0 67108848')
        self.assertEqual((result.returncode, result.stdout), (0, "1048576"), result.stderr)

    def test_archive_capacity_bounds_each_entry_and_cumulative_payload(self):
        # Stub only listing output so pathological logical sizes require neither
        # huge files nor hours of compression; gzip validity is covered separately.
        script = '''
gzip() { :; }
tar() { printf '%s\\n' "$@" >/dev/null; printf '%s\\n' "$LISTING"; }
LISTING=$1
validate_tar_archive ignored.tar.gz
'''
        for listing in ("-rw-r--r-- 0/0 100000000000000000 2026-01-01 00:00 large",
                        "-rw-r--r-- 0/0 1099511627777 2026-01-01 00:00 large",
                        "-rw-r--r-- 0/0 600000000000 2026-01-01 00:00 first\n"
                        "-rw-r--r-- 0/0 600000000000 2026-01-01 00:00 second"):
            with self.subTest(listing=listing):
                result = self.bash(script, listing)
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertIn("capacity limit", result.stderr)

    def test_import_details_are_read_through_guest_links_only(self):
        root = self.store / "guest"
        (root / "etc").mkdir(parents=True)
        (root / "usr/lib").mkdir(parents=True)
        (root / "var/lib/dpkg").mkdir(parents=True)
        (root / "usr/lib/os-release").write_text('NAME="Debian"\nVERSION_CODENAME="bookworm"\n')
        (root / "etc/os-release").symlink_to("/usr/lib/os-release")
        (root / "var/lib/dpkg/arch").write_text("arm64\n")
        work = self.store / "work"
        work.mkdir()
        result = self.bash("""
dpkg() { printf 'amd64\\n'; }
WORKER_ROOT=$1
record_import_details "$2"
""", root, work)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((work / "import.info").read_text(), "suite=bookworm\narchitecture=arm64\n")
        self.assertIn("arm64", result.stderr)

    def test_guest_resolve_cannot_leave_the_root(self):
        root = self.store / "guest"
        (root / "etc").mkdir(parents=True)
        (root / "run/resolve").mkdir(parents=True)
        for link, target in (("abs", "/run/resolve/stub"), ("rel", "../run/resolve/stub"),
                             ("escape", "../../../../outside"), ("chain", "abs")):
            (root / "etc" / link).symlink_to(target)
        (root / "etc/loop").symlink_to("loop")
        expected = {"/etc/abs": "/run/resolve/stub", "/etc/rel": "/run/resolve/stub",
                    "/etc/escape": "/outside", "/etc/chain": "/run/resolve/stub", "/etc/plain": "/etc/plain"}
        for path, target in expected.items():
            with self.subTest(path=path):
                result = self.bash('guest_resolve "$1" "$2"', root, path)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), f"{root}{target}")
        self.assertNotEqual(self.bash('guest_resolve "$1" /etc/loop', root).returncode, 0)

    def test_resolver_setup_never_touches_host_files_through_a_guest_symlink(self):
        root = self.store / "guest"
        (root / "etc").mkdir(parents=True)
        host_target = self.store / "host-file-must-not-appear"
        (root / "etc/resolv.conf").symlink_to(host_target)
        work = self.store / "work"
        work.mkdir()
        result = self.bash("""
mount() { printf '%s\\n' "${@: -1}" >>"$STORE_DIR/mounts"; }
setup_resolv_conf "$1" "$2"
""", work, root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(host_target.exists())
        confined = f"{root}{host_target}"
        self.assertTrue(Path(confined).is_file())
        self.assertEqual((self.store / "mounts").read_text().splitlines()[0], confined)

    def test_restricted_session_gets_proc_run_and_shm_and_refuses_guest_symlinks(self):
        root = self.store / "guest"
        root.mkdir()
        script = """
mount() { printf '%s\\n' "$*" >>"$STORE_DIR/mounts"; }
mknod() { :; }
ln() { :; }
WORKER_ROOT=$1
mount_restricted_devices
"""
        result = self.bash(script, root)
        self.assertEqual(result.returncode, 0, result.stderr)
        mounts = (self.store / "mounts").read_text()
        for line in (f"-t proc proc {root}/proc", f"{root}/run", f"-t devpts", f"{root}/dev/shm"):
            self.assertIn(line, mounts)
        shutil.rmtree(root)
        root.mkdir()
        (self.store / "mounts").unlink()
        (root / "proc").symlink_to(self.store)
        result = self.bash(script, root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("/proc must be a directory", result.stderr)
        self.assertFalse((self.store / "mounts").exists())

    def test_session_teardown_waits_for_processes_before_killing(self):
        result = self.bash("""
count=0
kill() {
    printf '%s\\n' "$*" >>"$STORE_DIR/kills"
    if [[ $1 == -0 ]]; then count=$((count + 1)); (( count < 3 )); return; fi
    return 0
}
sleep() { printf 'sleep\\n' >>"$STORE_DIR/kills"; }
mountpoint() { return 1; }
WORKER_ROOT=/nonexistent WORKER_LOWER=/nonexistent WORKER_OVERLAY=/nonexistent WORKER_SHARE_TARGET=
( worker_cleanup )
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.store / "kills").read_text().splitlines(),
                         ["-TERM -1", "-0 -1", "sleep", "-0 -1", "sleep", "-0 -1", "-KILL -1"])

    def test_session_teardown_wait_is_bounded(self):
        result = self.bash("""
kill() { [[ $1 == -0 ]] && printf 'probe\\n' >>"$STORE_DIR/probes"; return 0; }
sleep() { :; }
mountpoint() { return 1; }
CHROOTABLE_STOP_GRACE_TICKS=5
WORKER_ROOT=/nonexistent WORKER_LOWER=/nonexistent WORKER_OVERLAY=/nonexistent WORKER_SHARE_TARGET=
( worker_cleanup )
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len((self.store / "probes").read_text().splitlines()), 5)

    def test_expansion_and_publication_finish_despite_termination_signals(self):
        result = self.bash("""
fsck_preflight() {
    kill -HUP "$BASHPID"
    kill -INT "$BASHPID"
    kill -TERM "$BASHPID"
    bash -c 'kill -HUP "$$"; kill -INT "$$"; kill -TERM "$$"; printf survived' >"$STORE_DIR/fsck-done"
}
truncate() { :; }
resize2fs() { kill -TERM "$BASHPID"; command sleep 0.2; printf survived >"$STORE_DIR/resize-done"; }
image_size_mib() { printf 600; }
preflight_free_space() { :; }
assert_offline() { :; }
resize_image "$IMAGE_DIR/lab.img" 1024
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.store / "fsck-done").read_text(), "survived")
        self.assertEqual((self.store / "resize-done").read_text(), "survived")
        self.assertIn("size_mib=1024", self.image.with_suffix(".img.meta").read_text())
        result = self.bash("""
begin_operation restore lab
PENDING="$IMAGE_DIR/.lab.ABC123.partial"
printf 'replacement image' >"$PENDING"
printf 'lifecycle=ephemeral\\n' >"$PENDING.meta"
mv() { kill -TERM "$BASHPID"; command mv "$@"; }
publish_image "$PENDING" "$IMAGE_DIR/lab.img"
printf 'continued\\n'
""", identifier="b2" * 16)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("continued", result.stdout)
        self.assertEqual(self.image.read_bytes(), b"replacement image")

    def write_record(self, identifier=OPERATION_ID, status="running", stage="copying", action="clone",
                     pending="", destination="", pid=2147483647, start=1):
        record = self.store / "operations" / f"{identifier}.state"
        record.write_text(f"format=1\nid={identifier}\npid={pid}\nprocess_start={start}\nstatus={status}\nstage={stage}\n"
                          f"action={action}\nname=lab\npending={pending}\ndestination={destination}\n"
                          "image_inode=\nimage_size=\nmeta_inode=\nmeta_size=\n")
        return record

    def test_discard_removes_partial_files_of_interrupted_work_and_unblocks(self):
        partial = self.store / "images/.copy.ABC123.partial"
        partial.write_bytes(b"partial data")
        Path(str(partial) + ".meta").write_text("lifecycle=persistent\n")
        record = self.write_record(pending="images/.copy.ABC123.partial")
        result = self.bash('discard_operation "$1"', OPERATION_ID, identifier="c3" * 16)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(partial.exists())
        self.assertFalse(Path(str(partial) + ".meta").exists())
        self.assertIn("status=failed", record.read_text())
        self.assertIn("stage=discarded", record.read_text())
        self.assertNotEqual(self.bash('discard_operation "$1"', OPERATION_ID).returncode, 0)

    def test_discard_rolls_back_a_prepared_publication_but_not_a_later_one(self):
        partial = self.store / "images/.lab.ABC123.partial"
        meta = Path(str(partial) + ".meta")
        partial.write_bytes(b"staged")
        meta.write_text("lifecycle=ephemeral\n")
        pending = "images/.lab.ABC123.partial"
        self.write_record(stage="publish-metadata", action="restore", pending=pending, destination="images/lab.img")
        result = self.bash('discard_operation "$1"', OPERATION_ID)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--recover-operation", result.stderr)
        self.assertTrue(partial.exists())
        self.write_record(stage="publish-prepared", action="restore", pending=pending, destination="images/lab.img")
        meta.unlink()
        self.assertNotEqual(self.bash('discard_operation "$1"', OPERATION_ID).returncode, 0)
        self.assertTrue(partial.exists())
        meta.write_text("lifecycle=ephemeral\n")
        result = self.bash('discard_operation "$1"', OPERATION_ID)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(partial.exists())
        self.assertEqual(self.image.read_bytes(), b"original image")
        self.assertEqual(self.bash('lock_image "$IMAGE_DIR/lab.img" descriptor').returncode, 0)

    def test_discard_preserves_blocking_record_when_publication_files_disappear(self):
        self.interrupt_publication()
        pending = self.store / "images/.lab.ABC123.partial"
        pending.unlink()
        before = (self.store / "operations" / f"{OPERATION_ID}.state").read_bytes()
        result = self.bash('discard_operation "$1"', OPERATION_ID)
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("--recover-operation", result.stderr)
        self.assertEqual((self.store / "operations" / f"{OPERATION_ID}.state").read_bytes(), before)
        self.assertNotEqual(self.bash('lock_image "$IMAGE_DIR/lab.img" descriptor').returncode, 0)
        self.assertEqual(self.image.read_bytes(), b"original image")
        self.assertIn("origin=replacement", self.image.with_suffix(".img.meta").read_text())

    def test_discard_rejects_unsafe_publication_destinations_without_modifying_evidence(self):
        pending = self.store / "images/.lab.ABC123.partial"
        pending.write_bytes(b"staged")
        Path(str(pending) + ".meta").write_text("origin=staged\n")
        for destination in ("images/../../outside.img", "/tmp/outside.img", "snapshots/../outside.img", ""):
            with self.subTest(destination=destination):
                record = self.write_record(stage="publish-prepared", action="restore",
                                           pending="images/.lab.ABC123.partial", destination=destination)
                before = record.read_bytes()
                result = self.bash('discard_operation "$1"', OPERATION_ID)
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertIn("Unsafe publication paths", result.stderr)
                self.assertEqual(record.read_bytes(), before)
                self.assertEqual(pending.read_bytes(), b"staged")
                self.assertEqual(Path(str(pending) + ".meta").read_text(), "origin=staged\n")

    def test_discard_refuses_unsafe_paths_live_and_finished_operations(self):
        outside = self.store / "precious"
        outside.write_text("keep")
        for pending in ("../precious", "precious", "images/../precious", "images/not-a-partial",
                        "snapshots/../../precious/.x.ABC123.partial", "/etc/passwd"):
            with self.subTest(pending=pending):
                self.write_record(pending=pending)
                result = self.bash('discard_operation "$1"', OPERATION_ID)
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(outside.exists())
        target = self.store / "images/.lab.ABC123.partial"
        target.symlink_to(outside)
        self.write_record(pending="images/.lab.ABC123.partial")
        self.assertNotEqual(self.bash('discard_operation "$1"', OPERATION_ID).returncode, 0)
        self.assertEqual(outside.read_text(), "keep")
        target.unlink()
        self.write_record(status="completed", stage="completed")
        self.assertNotEqual(self.bash('discard_operation "$1"', OPERATION_ID).returncode, 0)
        result = self.bash("""
PROCESS=$$
sed_start=$(process_start_time "$PROCESS")
cat >"$OPERATIONS_DIR/$1.state" <<RECORD
format=1
id=$1
pid=$PROCESS
process_start=$sed_start
status=running
stage=copying
action=clone
name=lab
pending=
destination=
RECORD
discard_operation "$1"
""", OPERATION_ID)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("still running", result.stderr)

    def test_discard_sets_aside_a_damaged_record_so_the_store_is_usable(self):
        record = self.store / "operations" / f"{OPERATION_ID}.state"
        record.write_text("garbage line\n")
        self.assertNotEqual(self.bash('lock_image "$IMAGE_DIR/lab.img" descriptor').returncode, 0)
        listed = self.bash("list_operations json")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(json.loads(listed.stdout), [])
        result = self.bash('discard_operation "$1"', OPERATION_ID, identifier="c3" * 16)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(record.exists())
        self.assertEqual(Path(str(record) + ".invalid").read_text(), "garbage line\n")
        self.assertEqual(self.bash('lock_image "$IMAGE_DIR/lab.img" descriptor').returncode, 0)

    def test_list_operations_reports_what_can_be_done(self):
        self.write_record(identifier="d4" * 16, stage="publish-metadata", pending="images/.x.ABC123.partial", destination="images/lab.img")
        self.write_record(identifier="e5" * 16, stage="copying")
        self.write_record(identifier="f6" * 16, status="completed", stage="completed")
        operations = {item["id"]: item for item in json.loads(self.bash("list_operations json").stdout)}
        self.assertEqual((operations["d4" * 16]["recoverable"], operations["d4" * 16]["discardable"]), (True, True))
        self.assertEqual((operations["e5" * 16]["recoverable"], operations["e5" * 16]["discardable"]), (False, True))
        self.assertEqual((operations["f6" * 16]["recoverable"], operations["f6" * 16]["discardable"]), (False, False))

    def test_operation_history_is_trimmed_automatically_and_keeps_unfinished_work(self):
        for index in range(60):
            identifier = f"{index:032x}"
            self.write_record(identifier=identifier, status="completed", stage="completed")
            log = self.store / "logs" / f"{identifier}.log"
            log.write_text("done\n")
            os.utime(log, (1000 + index, 1000 + index))
        self.write_record(identifier="9" * 32, stage="copying", pending="images/.x.ABC123.partial")
        (self.store / "logs" / ("9" * 32 + ".log")).write_text("stopped\n")
        os.utime(self.store / "logs" / ("9" * 32 + ".log"), (1, 1))
        no_log = self.write_record(identifier="8" * 32, status="completed", stage="completed")
        result = self.bash("""
CHROOTABLE_OPERATION_KEEP=5
begin_operation mode lab
exit 0
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        records = {path.name[:-6] for path in (self.store / "operations").glob("*.state")}
        self.assertLessEqual(len(records), 7)
        self.assertIn("9" * 32, records)
        self.assertIn(OPERATION_ID, records)
        self.assertFalse(no_log.exists())
        self.assertTrue((self.store / "logs" / ("9" * 32 + ".log")).exists())
        self.assertFalse((self.store / "logs" / (f"{0:032x}.log")).exists())

    def test_history_below_the_margin_is_left_alone(self):
        for index in range(10):
            self.write_record(identifier=f"{index:032x}", status="completed", stage="completed")
            (self.store / "logs" / f"{index:032x}.log").write_text("done\n")
        result = self.bash('CHROOTABLE_OPERATION_KEEP=5\nbegin_operation mode lab\nexit 0')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(list((self.store / "operations").glob("*.state"))), 11)

    def archive_sync_script(self, body=""):
        return """
require_root() { :; }
init_store() { :; }
make_workdir() { WORK="$STORE_DIR/work"; mkdir -p "$WORK"; }
dpkg() { printf 'amd64\\n'; }
run_worker() { printf new >"$ARCHIVE_STAGING/base.tgz"; }
ARCHIVE_DIR="$STORE_DIR/archive"
mkdir -p "$ARCHIVE_DIR"
""" + body + "\narchive_sync trixie build\n"

    def test_archive_sync_replaces_the_archive_and_clears_abandoned_directories(self):
        archive = self.store / "archive/trixie-amd64"
        archive.mkdir(parents=True)
        (archive / "base.tgz").write_text("old")
        stale = self.store / "archive/.trixie-amd64.ZZZZZZ"
        stale.mkdir()
        other = self.store / "archive/.bookworm-amd64.ZZZZZZ"
        other.mkdir()
        result = self.bash(self.archive_sync_script())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((archive / "base.tgz").read_text(), "new")
        self.assertFalse(stale.exists())
        self.assertTrue(other.exists())
        self.assertEqual([path.name for path in (self.store / "archive").glob(".trixie*")], [])

    def test_archive_sync_restores_the_old_archive_when_the_swap_fails(self):
        archive = self.store / "archive/trixie-amd64"
        archive.mkdir(parents=True)
        (archive / "base.tgz").write_text("old")
        result = self.bash(self.archive_sync_script("""
mv() {
    if [[ ${@: -2:1} == "$ARCHIVE_DIR"/.trixie-amd64.?????? ]]; then return 1; fi
    command mv "$@"
}
"""))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((archive / "base.tgz").read_text(), "old")

    def test_archive_sync_recovers_an_archive_lost_between_renames(self):
        old = self.store / "archive/.trixie-amd64.ABCDEF.old"
        old.mkdir(parents=True)
        (old / "base.tgz").write_text("survivor")
        result = self.bash(self.archive_sync_script('run_worker() { return 9; }'))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.store / "archive/trixie-amd64/base.tgz").read_text(), "survivor")

    def test_archive_sync_refuses_to_run_twice_at_once(self):
        (self.store / "archive").mkdir()
        result = self.bash(self.archive_sync_script("""
exec {held}>>"$ARCHIVE_DIR/.sync.lock"
flock -n "$held"
(
  exec {held}>&-
  archive_sync trixie build
)
exit $?
"""))
        self.assertNotEqual(result.returncode, 0)

    def test_create_waits_for_a_shared_archive_lock(self):
        (self.store / "archive").mkdir()
        (self.store / "archive/.lock").touch()
        holder = subprocess.Popen(["flock", "-x", str(self.store / "archive/.lock"), "sleep", "1"])
        self.addCleanup(holder.wait)
        import time
        time.sleep(0.2)
        started = time.monotonic()
        result = self.bash("""
preflight_free_space() { :; }
begin_operation() { return 19; }
create_image waited 512 persistent minimal
""")
        self.assertEqual(result.returncode, 19, result.stderr)
        self.assertGreater(time.monotonic() - started, 0.4)

    def test_export_count_does_not_merge_similarly_named_workspaces(self):
        for filename in ("lab-20261015-120000-AbCd12.tar.gz", "lab-20261015-120000.tar.gz", "lab.tar.gz",
                         "lab-bench-20261015-120000-AbCd12.tar.gz", "lab-notes.tar.gz"):
            (self.store / "exports" / filename).write_bytes(b"x")
        self.assertEqual(self.bash("export_count lab").stdout, "3")
        self.assertEqual(self.bash("export_count lab-bench").stdout, "1")

    def test_dropping_the_last_snapshot_removes_its_folder(self):
        for label in ("one", "two"):
            (self.store / f"snapshots/lab/{label}.img").write_bytes(b"s")
        script = """
assert_offline() { :; }
drop_snapshot lab "$1"
"""
        self.assertEqual(self.bash(script, "one", identifier="c3" * 16).returncode, 0)
        self.assertTrue((self.store / "snapshots/lab").exists())
        self.assertEqual(self.bash(script, "two", identifier="d4" * 16).returncode, 0)
        self.assertFalse((self.store / "snapshots/lab").exists())

    def test_prune_logs_and_delete_export(self):
        for idx in range(5):
            log_file = self.store / "logs" / f"old-{idx}.log"
            log_file.write_text(f"log {idx}\n")
        export_file = self.store / "exports/lab-20261015-120000.tar.gz"
        export_file.write_bytes(b"archive-bytes")
        result = self.bash('prune_logs 2 --yes && delete_export "lab-20261015-120000.tar.gz" --yes')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(export_file.exists())
        remaining = [p for p in (self.store / "logs").glob("*.log") if p.name != f"{OPERATION_ID}.log"]
        self.assertLessEqual(len(remaining), 2)


if __name__ == "__main__":
    unittest.main()
