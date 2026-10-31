"""Exercise CLI failure paths using temporary images and no privileged operations."""
import fcntl
import json
import os
import re
from pathlib import Path
import subprocess
import tempfile
import unittest


MANAGER = Path(__file__).resolve().parents[1] / "chrootable-manager"


class CliStabilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = Path(self.temporary.name)
        for directory in ("images", "logs", "exports", "snapshots/lab"):
            (self.store / directory).mkdir(parents=True)
        self.image = self.store / "images/lab.img"
        with self.image.open("wb") as image:
            image.truncate(1024 * 1024)

    def bash(self, script, *arguments):
        return subprocess.run(
            ["bash", "-c", 'source "$1"\nSTORE_DIR=$2\nconfigure_paths\nshift 2\n' + script,
             "cli-stability-test", str(MANAGER), str(self.store), *map(str, arguments)],
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_resize_rechecks_capacity_after_acquiring_image_lock(self):
        # Simulate another expansion finishing between initial resolution and
        # lock acquisition. No filesystem utilities or real mounts are used.
        result = self.bash('''
lock_image() { command truncate -s 3M "$1"; }
assert_offline() { :; }
begin_operation() { OP_LOG="$STORE_DIR/operation.log"; }
fsck_preflight() { printf fsck >>"$STORE_DIR/filesystem-operations"; }
resize2fs() { printf resize >>"$STORE_DIR/filesystem-operations"; }
resize_image "$IMAGE_DIR/lab.img" 2
''')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("larger than 3 MiB", result.stderr)
        self.assertEqual(self.image.stat().st_size, 3 * 1024 * 1024)
        self.assertFalse((self.store / "filesystem-operations").exists())

    def test_internal_list_lock_probe_switch_does_not_affect_inspect_or_mutations(self):
        lock = Path(str(self.image) + ".lock")
        with lock.open("a") as descriptor:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for setting, expected in (("1", "busy"), ("0", "unknown")):
                result = self.bash('CHROOTABLE_LIST_PROBE_LOCKS=$1 list_images json', setting)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)[0]["status"], expected)
            inspect = self.bash('CHROOTABLE_LIST_PROBE_LOCKS=0 run_cli --inspect lab --json')
            self.assertEqual(inspect.returncode, 0, inspect.stderr)
            self.assertEqual(json.loads(inspect.stdout)["status"], "busy")
            mutation = self.bash('CHROOTABLE_LIST_PROBE_LOCKS=0 lock_image "$IMAGE_DIR/lab.img" held')
            self.assertNotEqual(mutation.returncode, 0)
            self.assertIn("already in use", mutation.stderr)

    def test_inventory_propagates_partial_stat_failure(self):
        # stat can fail for one image removed after globbing while continuing
        # to print measurements for later images. They must not be reassigned.
        with (self.store / "images/other.img").open("wb") as image:
            image.truncate(2 * 1024 * 1024)
        for name in ("lab", "other"):
            (self.store / "snapshots/lab" / (name + ".img")).touch()
        script = '''
stat() {
    local format=$2 path status=0
    shift 3
    for path in "$@"; do
        if [[ $path == */lab.img ]]; then
            printf 'simulated stat failure\\n' >&2
            status=25
        else
            command stat -c "$format" -- "$path" || return
        fi
    done
    return "$status"
}
"$@"
'''
        for command in (("list_images", "json"), ("list_snapshots", "lab"),
                        ("workspace_summary",), ("dashboard",)):
            with self.subTest(command=command):
                result = self.bash(script, *command)
                self.assertEqual(result.returncode, 25, result.stdout + result.stderr)
                self.assertIn("simulated stat failure", result.stderr)

    def test_unterminated_metadata_record_preserves_ephemeral_lifecycle(self):
        self.image.with_suffix(".img.meta").write_text("suite=trixie\nlifecycle=ephemeral")
        commands = {
            "value": 'metadata_value "$IMAGE_DIR/lab.img" lifecycle persistent',
            "lifecycle": 'image_lifecycle "$IMAGE_DIR/lab.img"',
            "json": 'image_json "$IMAGE_DIR/lab.img"',
            "copy": 'copy_metadata "$IMAGE_DIR/lab.img" "$IMAGE_DIR/copy.img"\n'
                    'metadata_value "$IMAGE_DIR/copy.img" lifecycle persistent',
            "details": 'image_details "$IMAGE_DIR/lab.img"',
            "summary": "workspace_summary",
            "dashboard": "dashboard",
        }
        for name, command in commands.items():
            with self.subTest(reader=name):
                result = self.bash(command)
                self.assertEqual(result.returncode, 0, result.stderr)
                if name == "json":
                    self.assertEqual(json.loads(result.stdout)["lifecycle"], "ephemeral")
                elif name in ("summary", "dashboard"):
                    self.assertIn("0 persistent   1 disposable", result.stdout)
                else:
                    self.assertIn("ephemeral", result.stdout)

    def stub_package_manager(self, name):
        stubs = self.store / (name + "-stubs")
        stubs.mkdir(exist_ok=True)
        stub = stubs / name
        stub.write_text(
            f'#!/bin/bash\nprintf "{name}: %s\\n" "$*" >>{self.store / (name + ".log")}\n'
        )
        stub.chmod(0o755)
        return stubs

    def test_package_mapping_prefers_pacman_and_zypper_over_generic_fallback(self):
        for name, expected in (
            ("pacman", ["libnewt", "gnupg", "inetutils", "gawk"]),
            ("zypper", ["newt", "gpg2", "hostname", "gawk"]),
        ):
            with self.subTest(manager=name):
                stubs = self.stub_package_manager(name)
                result = self.bash(f'''
PATH="{stubs}"
package_for_command whiptail; printf '\\n'
package_for_command gpgv; printf '\\n'
package_for_command hostname; printf '\\n'
package_for_command awk; printf '\\n'
''')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.split(), expected)

    def test_check_prerequisites_installs_through_pacman(self):
        stubs = self.stub_package_manager("pacman")
        result = self.bash(f'''
PATH="{stubs}"
CHROOTABLE_DEP_LOG="$STORE_DIR/dep.log"
NON_INTERACTIVE=0
require_root() {{ :; }}
check_prerequisites
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        invocation = (self.store / "pacman.log").read_text()
        self.assertTrue(invocation.startswith("pacman: -Sy --noconfirm --needed "),
                        invocation)
        for package in ("gawk", "gnupg", "inetutils", "newt", "util-linux",
                        "e2fsprogs", "coreutils"):
            self.assertIn(package, invocation)
        self.assertTrue((self.store / "dep.log").exists())

    def test_check_prerequisites_installs_through_zypper(self):
        stubs = self.stub_package_manager("zypper")
        result = self.bash(f'''
PATH="{stubs}"
CHROOTABLE_DEP_LOG="$STORE_DIR/dep.log"
NON_INTERACTIVE=0
require_root() {{ :; }}
check_prerequisites
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        invocations = (self.store / "zypper.log").read_text().splitlines()
        self.assertEqual(len(invocations), 2, invocations)
        self.assertEqual(invocations[0], "zypper: --non-interactive refresh")
        self.assertTrue(invocations[1].startswith("zypper: --non-interactive install "),
                        invocations[1])
        for package in ("gawk", "gpg2", "newt", "util-linux", "e2fsprogs"):
            self.assertIn(package, invocations[1])

    def test_check_prerequisites_reports_missing_manager_when_none_supported(self):
        empty = self.store / "empty-stubs"
        empty.mkdir(exist_ok=True)
        result = self.bash(f'''
PATH="{empty}"
CHROOTABLE_AUTO_INSTALL=1
require_root() {{ :; }}
check_prerequisites
''')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Install these packages first:", result.stderr)

    def test_new_profiles_are_valid_listed_and_installable(self):
        for profile in ("rust", "go", "web"):
            with self.subTest(profile=profile):
                result = self.bash(f'''
if ! valid_profile {profile}; then printf 'invalid profile\n' >&2; exit 1; fi
packages=$(profile_packages {profile}) || {{ printf 'no packages\n' >&2; exit 1; }}
[[ -n $packages ]] || {{ printf 'empty package list\n' >&2; exit 1; }}
''')
                self.assertEqual(result.returncode, 0, result.stderr)
        listing = self.bash("list_profiles")
        self.assertEqual(listing.returncode, 0, listing.stderr)
        for profile in ("rust", "go", "web"):
            self.assertIn(profile, listing.stdout)

    def test_restricted_dev_session_flag_parses_once_per_operation(self):
        for operation, allowed in (("--enter", True), ("--enter-isolated", True),
                                   ("--exec", True), ("--try", True)):
            with self.subTest(operation=operation):
                arguments = [operation, "lab", "--restricted-dev"]
                if operation in ("--exec", "--try"):
                    arguments += ["--", "true"]
                result = self.bash("validate_session_arguments \"$@\"",
                                  *arguments)
                self.assertEqual(allowed, result.returncode == 0, result.stderr)
        for operation in ("--enter", "--exec", "--try"):
            arguments = [operation, "lab", "--restricted-dev", "--restricted-dev"]
            if operation in ("--exec", "--try"):
                arguments += ["--", "true"]
            result = self.bash("validate_session_arguments \"$@\"", *arguments)
            self.assertNotEqual(result.returncode, 0)

    def test_enter_chrootable_hands_restricted_dev_to_worker(self):
        for flag, expected in ((1, "1"), (0, "0")):
            with self.subTest(flag=flag):
                result = self.bash(f'''
SESSION_RESTRICTED_DEV={flag}
lock_image() {{ return 0; }}
assert_offline() {{ return 0; }}
begin_operation() {{ return 0; }}
make_workdir() {{ WORK="$STORE_DIR/work"; mkdir -p "$WORK"; return 0; }}
unshare() {{ printf '%s' "$CHROOTABLE_RESTRICTED_DEV" >"$STORE_DIR/restricted-flag"; return 0; }}
enter_chrootable "$STORE_DIR/images/lab.img" persistent shared "" 0
''')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((self.store / "restricted-flag").read_text(), expected)

    def test_operation_cleanup_removes_resolver_artifacts_and_preserves_status(self):
        for status in (0, 17):
            with self.subTest(status=status):
                work = self.store / "work"
                for directory in ("root", "lower", "overlay"):
                    (work / directory).mkdir(parents=True, exist_ok=True)
                for name in ("started", "resolv.conf", "resolv.conf.tmp"):
                    (work / name).write_text("temporary content")
                result = self.bash('''
begin_operation session lab
WORK=$1
exit "$2"
''', work, status)
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertFalse(work.exists())
                self.assertIn("\tfailed\t" if status else "\tok\t",
                              (self.store / "activity.tsv").read_text())

    def test_archive_covers_only_synced_profiles(self):
        archive = self.store / "archive/trixie-amd64"
        archive.mkdir(parents=True)
        (archive / "base.tgz").write_bytes(b"tarball")
        (archive / "mirror").write_text("https://deb.debian.org/debian\n")
        (archive / "profiles").write_text("build\ngo\n")
        result = self.bash('''
for profile in minimal build go python; do
    if archive_has_profile "$(archive_path trixie amd64)" "$profile"; then echo "$profile yes"; else echo "$profile no"; fi
done
CHROOTABLE_ARCHIVE=0 archive_has_profile "$(archive_path trixie amd64)" build || echo disabled
archive_has_profile "$(archive_path bookworm amd64)" minimal || echo missing
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(),
                         ["minimal yes", "build yes", "go yes", "python no", "disabled", "missing"])

    def test_archive_sync_arguments_are_validated(self):
        for arguments, valid in ((["--archive-sync", "trixie"], True),
                                 (["--archive-sync", "trixie", "build", "web"], True),
                                 (["--archive-sync"], False),
                                 (["--archive-sync", "../etc"], False),
                                 (["--archive-sync", "trixie", "bogus"], False)):
            result = self.bash('validate_cli "$@"', *arguments)
            self.assertEqual(result.returncode == 0, valid, arguments)

    def test_doctor_json_is_read_only_and_reports_missing_commands(self):
        before = sorted(path.relative_to(self.store) for path in self.store.rglob("*"))
        result = self.bash('''
REQUIRED_COMMANDS=(bash chrootables_nonexistent_command)
doctor json
''')
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["schema_version"], 1)
        self.assertFalse(report["ready"])
        checks = {item["id"]: item for item in report["checks"]}
        self.assertEqual(checks["command:chrootables_nonexistent_command"]["status"], "error")
        self.assertIn("--setup", checks["command:chrootables_nonexistent_command"]["remedy"])
        self.assertTrue(report["storage"]["available"])
        self.assertGreater(report["storage"]["total_bytes"], 0)
        self.assertIn("Mount permissions are not tested", report["output"])
        self.assertEqual(report["errors"], sum(c["status"] == "error" for c in checks.values()))
        self.assertEqual(sorted(path.relative_to(self.store) for path in self.store.rglob("*")), before)

    def test_doctor_warns_on_low_space_and_checks_store_permissions(self):
        result = self.bash('''
REQUIRED_COMMANDS=(bash)
df() { printf ' Size Avail\\n 10737418240 1048576\\n'; }
stat() { printf '0 777\\n'; }
doctor json
''')
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        checks = {item["id"]: item for item in report["checks"]}
        self.assertEqual(checks["store-permissions"]["status"], "error")
        self.assertEqual(checks["store-capacity"]["status"], "warning")
        self.assertEqual(report["storage"]["free_bytes"], 1048576)
        self.assertEqual(report["storage"]["total_bytes"], 10737418240)

    def test_doctor_handles_missing_store_and_escapes_unusual_paths(self):
        unusual = self.store / 'future "store"\nfolder'
        result = self.bash('''
STORE_DIR=$1
REQUIRED_COMMANDS=(bash)
doctor json
''', unusual)
        report = json.loads(result.stdout)
        self.assertEqual(report["store"], str(unusual))
        self.assertTrue(report["storage"]["available"])
        self.assertFalse(unusual.exists())
        self.assertIn("first write operation", report["output"])

    def test_doctor_rejects_store_subdirectory_symlinks(self):
        (self.store / "exports").rmdir()
        (self.store / "exports").symlink_to(self.store / "images", target_is_directory=True)
        result = self.bash('REQUIRED_COMMANDS=(bash)\ndoctor json')
        report = json.loads(result.stdout)
        checks = {item["id"]: item for item in report["checks"]}
        self.assertEqual(checks["store:exports"]["status"], "error")

    def test_store_init_and_doctor_reject_unsafe_managed_directories(self):
        outside = self.store.parent / (self.store.name + "-outside")
        outside.mkdir()
        self.addCleanup(outside.rmdir)
        for name in ("images", "logs", "exports", "snapshots", "operations", "archive"):
            directory = self.store / name
            directory.mkdir(exist_ok=True)
            with self.subTest(directory=name, problem="permissions"):
                original_mode = directory.stat().st_mode & 0o7777
                directory.chmod(0o777)
                try:
                    result = self.bash('init_store')
                    self.assertNotEqual(result.returncode, 0, result.stderr)
                    self.assertIn("not writable by group/others", result.stderr)
                    self.assertEqual(directory.stat().st_mode & 0o7777, 0o777)
                    report = json.loads(self.bash('REQUIRED_COMMANDS=(bash)\ndoctor json').stdout)
                    checks = {check["id"]: check for check in report["checks"]}
                    self.assertEqual(checks["store:" + name]["status"], "error")
                finally:
                    directory.chmod(original_mode)
            saved = self.store / ("saved-" + name)
            directory.rename(saved)
            try:
                for kind in ("symlink", "file", "fifo"):
                    with self.subTest(directory=name, problem=kind):
                        if kind == "symlink":
                            directory.symlink_to(outside, target_is_directory=True)
                        elif kind == "file":
                            directory.write_text("preserve me")
                        else:
                            os.mkfifo(directory)
                        try:
                            result = self.bash('init_store')
                            self.assertNotEqual(result.returncode, 0, result.stderr)
                            report = json.loads(self.bash('REQUIRED_COMMANDS=(bash)\ndoctor json').stdout)
                            checks = {check["id"]: check for check in report["checks"]}
                            self.assertEqual(checks["store:" + name]["status"], "error")
                            if kind == "file":
                                self.assertEqual(directory.read_text(), "preserve me")
                        finally:
                            directory.unlink()
            finally:
                saved.rename(directory)
        self.assertEqual(list(outside.iterdir()), [])

    def test_store_init_rejects_foreign_directory_owner_before_creating_paths(self):
        result = self.bash('''
stat() {
    if [[ ${@: -1} == "$IMAGE_DIR" ]]; then printf '%s 755\\n' "$((EUID + 1))";
    else command stat "$@"; fi
}
init_store
''')
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("must be owned", result.stderr)
        self.assertFalse((self.store / "operations").exists())

    def test_store_init_preserves_safe_existing_directory_permissions(self):
        (self.store / "images").chmod(0o750)
        result = self.bash('init_store')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.store / "images").stat().st_mode & 0o777, 0o750)
        self.assertEqual((self.store / "operations").stat().st_mode & 0o777, 0o700)

    def test_doctor_json_argument_validation_and_version(self):
        for arguments, valid in ((["--doctor"], True), (["--doctor", "--json"], True),
                                 (["--doctor", "--json", "extra"], False),
                                 (["--doctor", "--other"], False)):
            result = self.bash('validate_cli "$@"', *arguments)
            self.assertEqual(result.returncode == 0, valid, arguments)
        result = subprocess.run([str(MANAGER), "--version"], capture_output=True, text=True, check=True)
        declared = re.search(r"^APP_VERSION=([^\n]+)$", MANAGER.read_text(), re.MULTILINE)
        self.assertIsNotNone(declared)
        self.assertEqual(result.stdout.strip(), declared.group(1))

    def test_activity_log_compaction_and_inspect_enrichment(self):
        activity = self.store / "activity.tsv"
        activity.write_text("".join(f"2026-10-15T10:00:{i:02d}\tcreate\tlab-{i}\tok\t\n" for i in range(40)))
        (self.store / "exports/lab-20261015-120000.tar.gz").write_bytes(b"export")
        (self.store / "operations").mkdir(exist_ok=True)
        op_file = self.store / "operations" / ("d" * 32 + ".state")
        op_file.write_text("format=1\nid=" + "d" * 32 + "\npid=2147483647\nprocess_start=1\nstatus=running\n"
                           "stage=publish-metadata\naction=clone\nname=lab\n"
                           "pending=images/.copy.ABC123.partial\ndestination=images/lab.img\n")
        (self.store / "images").mkdir(exist_ok=True)
        (self.store / "images/lab.img").write_bytes(b"image")
        result = self.bash('''
CHROOTABLE_ACTIVITY_MAX_BYTES=200 CHROOTABLE_ACTIVITY_KEEP_LINES=10 record_event snapshot lab ok ""
image_details "$IMAGE_DIR/lab.img"
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = activity.read_text().splitlines()
        self.assertEqual(len(lines), 10)
        self.assertTrue(lines[-1].endswith("\tsnapshot\tlab\tok\t"))
        self.assertRegex(result.stdout, r"Origin\s+none")
        self.assertRegex(result.stdout, r"Exports\s+1")
        self.assertIn("--recover-operation", result.stdout)

    def test_terminal_menu_import_collects_values_and_runs_the_operation(self):
        answers = self.store / "answers"
        answers.write_text("/backups/lab.tar.gz\nrestored\n\npersistent\n")
        result = self.bash('''
exec 9<"$1"
ui() {
    case " $* " in
        *--inputbox*|*--menu*) IFS= read -r reply <&9; printf '%s' "$reply" >&2 ;;
    esac
    return 0
}
ui_run() { printf 'RUN'; printf ' [%s]' "$@"; printf '\n'; }
UI_WIDTH=80 UI_HEIGHT=22
ui_import
''', answers)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "RUN [import_image] [restored] [/backups/lab.tar.gz] [] [persistent] [minimal]")

    def test_terminal_menu_lists_only_interrupted_operations(self):
        operations = self.store / "operations"
        operations.mkdir(exist_ok=True)
        for identifier, status, stage in (("a" * 32, "running", "copying"), ("b" * 32, "completed", "completed")):
            (operations / f"{identifier}.state").write_text(
                f"format=1\nid={identifier}\npid=2147483647\nprocess_start=1\nstatus={status}\n"
                f"stage={stage}\naction=clone\nname=lab\n")
        result = self.bash('''
ui() { printf '%s\n' "$*" >>"$STORE_DIR/menu"; return 1; }
UI_WIDTH=80 UI_HEIGHT=22
ui_interrupted
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        menu = (self.store / "menu").read_text()
        self.assertIn("a" * 32, menu)
        self.assertNotIn("b" * 32, menu)

    def test_recommended_size_mib_covers_all_seven_profiles(self):
        result = self.bash('''
for p in minimal build python network rust go web; do
    printf '%s=%s\n' "$p" "$(recommended_size_mib "$p")"
done
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            "minimal=2048", "build=4096", "python=4096", "network=2048",
            "rust=6144", "go=6144", "web=4096",
        ])


if __name__ == "__main__":
    unittest.main()
