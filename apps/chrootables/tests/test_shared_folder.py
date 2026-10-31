"""Exercise session parsing and mount orchestration without root or real mounts."""
import itertools
from pathlib import Path
import subprocess
import tempfile
import unittest


MANAGER = Path(__file__).resolve().parents[1] / "chrootable-manager"


class SharedFolderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name)
        self.source = self.path / "host folder; $(literal) [project],data"
        self.source.mkdir()
        (self.source / "host-file").write_text("host content")
        self.root = self.path / "root"
        self.root.mkdir()
        self.calls = self.path / "calls"

    def bash(self, script, *arguments):
        return subprocess.run(
            ["bash", "-c", 'source "$1"\nshift\n' + script,
             "shared-folder-test", str(MANAGER), *map(str, arguments)],
            capture_output=True,
        )

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def parse(self, *arguments):
        return self.bash(
            'validate_cli "$@"\n'
            'printf "%s\\0" "$SESSION_NETWORK" "$SESSION_SHARE" "$SESSION_READ_ONLY"\n'
            'if (( ${#SESSION_COMMAND[@]} )); then printf "%s\\0" "${SESSION_COMMAND[@]}"; fi',
            *arguments,
        )

    @staticmethod
    def fields(data):
        return [part.decode() for part in data.split(b"\0")[:-1]]

    def recorded_calls(self):
        if not self.calls.exists():
            return []
        return [self.fields(record + b"\0") for record in self.calls.read_bytes().split(b"\0\0") if record]

    def test_session_flags_in_any_order_preserve_command_arguments(self):
        command = ["/bin/sh", "-c", "printf '%s' \"$1\"", "literal", "--share", "a; $(id) b"]
        options = [("--isolated",), ("--share", str(self.source)), ("--read-only",)]
        for operation in ("--exec", "--try"):
            for permutation in itertools.permutations(options):
                flags = [arg for option in permutation for arg in option]
                with self.subTest(operation=operation, flags=flags):
                    result = self.parse(operation, "lab", *flags, "--", *command)
                    self.assert_success(result)
                    self.assertEqual(self.fields(result.stdout), ["isolated", str(self.source), "1", *command])

    def test_shell_sessions_and_existing_forms(self):
        for operation, network in (("--enter", "shared"), ("--enter-isolated", "isolated"), ("--try", "shared")):
            for flags in ([], ["--share", self.source], ["--read-only", "--share", self.source]):
                with self.subTest(operation=operation, flags=flags):
                    result = self.parse(operation, "lab", *flags)
                    self.assert_success(result)
                    self.assertEqual(self.fields(result.stdout), [network, str(self.source) if flags else "", "1" if "--read-only" in flags else "0"])
        result = self.parse("--exec", "lab", "--", "printf", "--isolated")
        self.assert_success(result)
        self.assertEqual(self.fields(result.stdout), ["shared", "", "0", "printf", "--isolated"])

    def test_invalid_option_combinations(self):
        cases = [
            ["--enter", "lab", "--share"],
            ["--try", "lab", "--share", "--read-only"],
            ["--enter", "lab", "--read-only"],
            ["--try", "lab", "--read-only", "--", "true"],
            ["--try", "lab", "--share", self.source, "--share", self.source],
            ["--try", "lab", "--share", self.source, "--read-only", "--read-only"],
            ["--try", "lab", "--isolated", "--isolated"],
            ["--enter", "lab", "--isolated"],
            ["--enter-isolated", "lab", "--isolated"],
            ["--enter", "lab", "--", "true"],
            ["--try", "lab", "--unknown"],
            ["--exec", "lab"],
            ["--exec", "lab", "--share", self.source],
            ["--exec", "lab", "true"],
            ["--exec", "lab", "--"],
            ["--try", "lab", "--"],
        ]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                result = self.parse(*arguments)
                self.assertNotEqual(result.returncode, 0)

    def test_source_validation_and_canonicalization(self):
        root_alias = self.path / "root-alias"
        root_alias.symlink_to("/", target_is_directory=True)
        for source in ("relative", "/", root_alias, self.source / "host-file", self.path / "missing"):
            with self.subTest(source=source):
                self.assertNotEqual(self.parse("--enter", "lab", "--share", source).returncode, 0)
        alias = self.path / "host-alias"
        alias.symlink_to(self.source, target_is_directory=True)
        result = self.parse("--enter", "lab", "--share", alias / ".." / alias.name)
        self.assert_success(result)
        self.assertEqual(self.fields(result.stdout)[1], str(self.source))
        for separator in ("\n", "\r"):
            odd_source = self.path / ("odd" + separator + "folder")
            odd_source.mkdir()
            odd_alias = self.path / ("alias" + str(ord(separator)))
            odd_alias.symlink_to(odd_source, target_is_directory=True)
            for source in (odd_source, odd_alias):
                self.assertNotEqual(self.parse("--enter", "lab", "--share", source).returncode, 0)

    def test_cli_routes_lifecycle_network_share_and_exact_command(self):
        script = '''
resolve_image() { printf '/store/%s.img' "$1"; }
image_lifecycle() { printf persistent; }
enter_chrootable() { printf '%s\\0' "$@"; }
run_cli "$@"
'''
        for operation, lifecycle, network in (("--enter", "persistent", "shared"), ("--enter-isolated", "persistent", "isolated"), ("--try", "ephemeral", "shared"), ("--exec", "persistent", "shared")):
            command = ["--", "printf", "a b; $(id)"] if operation in ("--exec", "--try") else []
            result = self.bash(script, operation, "lab", "--read-only", "--share", self.source, *command)
            self.assert_success(result)
            self.assertEqual(self.fields(result.stdout), ["/store/lab.img", lifecycle, network, str(self.source), "1", *command[1:]])

    def test_namespace_worker_receives_share_and_command_as_separate_arguments(self):
        script = '''
lock_image() { :; }
assert_offline() { :; }
begin_operation() { :; }
WORK=$1
make_workdir() { :; }
unshare() { printf '%s\\0' "$@"; }
enter_chrootable /store/lab.img ephemeral isolated "$2" 1 printf 'a b; $(id)'
'''
        result = self.bash(script, self.path, self.source)
        self.assert_success(result)
        fields = self.fields(result.stdout)
        self.assertIn("--net", fields)
        worker_start = fields.index("--worker")
        self.assertEqual(fields[worker_start:], ["--worker", "enter", "/store/lab.img", str(self.path), "lab", "ephemeral", "isolated", str(self.source), "1", "printf", "a b; $(id)"])

    def test_plain_bind_and_optional_read_only_remount(self):
        script = '''
WORKER_ROOT=$1
CALLS=$4
mount() { printf '%s\\0' "$@" >>"$CALLS"; printf '\\0' >>"$CALLS"; }
mount_shared_folder "$2" "$3"
printf '%s' "$WORKER_SHARE_CREATED"
'''
        for read_only in (0, 1):
            with self.subTest(read_only=read_only):
                self.calls.unlink(missing_ok=True)
                result = self.bash(script, self.root, self.source, read_only, self.calls)
                self.assert_success(result)
                expected = [["--bind", "--", str(self.source), str(self.root / "shared")]]
                if read_only:
                    expected.append(["-o", "remount,bind,ro", "--", str(self.root / "shared")])
                self.assertEqual(self.recorded_calls(), expected)
                self.assertEqual((self.source / "host-file").read_text(), "host content")
                (self.root / "shared").rmdir()

    def test_guest_target_file_or_symlink_is_rejected_without_host_changes(self):
        target = self.root / "shared"
        outside = self.path / "outside"
        outside.mkdir()
        (outside / "keep").write_text("unchanged")
        script = '''
WORKER_ROOT=$1
CALLS=$3
mount() { printf mount >>"$CALLS"; }
mount_shared_folder "$2" 0
'''
        for kind in ("file", "symlink", "dangling-symlink"):
            with self.subTest(kind=kind):
                if kind == "file":
                    target.write_text("guest content")
                else:
                    target.symlink_to(outside if kind == "symlink" else self.path / "missing", target_is_directory=True)
                result = self.bash(script, self.root, self.source, self.calls)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.calls.exists())
                self.assertEqual((outside / "keep").read_text(), "unchanged")
                if kind == "file":
                    self.assertEqual(target.read_text(), "guest content")
                target.unlink()

    # Only record mount/chroot operations. Every namespace, mount, signal and
    # process operation is replaced before calling the real worker function.
    WORKER_MOCKS = '''
CALLS=$1
record() { printf '%s\\0' "$@" >>"$CALLS"; printf '\\0' >>"$CALLS"; }
require_root() { :; }
kill() { :; }
sleep() { :; }
hostname() { :; }
ip() { :; }
setup_resolv_conf() { record resolver; }
mount_session_devices() { record devices; }
mount() {
    record mount "$@"
    [[ $1 != --make-rprivate ]] || return 0
    if [[ $1 == -o && $2 == remount,bind,ro ]]; then return "$REMOUNT_STATUS"; fi
    if [[ $1 == --bind && $BIND_STATUS != 0 ]]; then return "$BIND_STATUS"; fi
    local target=${@: -1}
    touch -- "$target.mock-mounted"
}
mountpoint() { local target=${@: -1}; [[ -e $target.mock-mounted ]]; }
umount() { record umount "$@"; local target=${@: -1}; rm -f -- "$target.mock-mounted"; }
chroot() { record chroot "$@"; return "$GUEST_STATUS"; }
REMOUNT_STATUS=$5 BIND_STATUS=$6 GUEST_STATUS=$7
worker_main enter fake.img "$2" lab "$3" "$4" "$8" 1 printf 'literal argument'
'''

    def worker(self, lifecycle="persistent", network="shared", remount_status=0, bind_status=0, guest_status=0):
        return self.bash(self.WORKER_MOCKS, self.calls, self.path, lifecycle, network, remount_status, bind_status, guest_status, self.source)

    def test_worker_mounts_before_command_and_unmounts_share_before_image(self):
        for lifecycle, network in itertools.product(("persistent", "ephemeral"), ("shared", "isolated")):
            with self.subTest(lifecycle=lifecycle, network=network):
                self.calls.unlink(missing_ok=True)
                result = self.worker(lifecycle=lifecycle, network=network)
                self.assert_success(result)
                records = self.recorded_calls()
                share_mount = ["mount", "--bind", "--", str(self.source), str(self.root / "shared")]
                share_remount = ["mount", "-o", "remount,bind,ro", "--", str(self.root / "shared")]
                guest_index = next(i for i, item in enumerate(records) if item[0] == "chroot")
                self.assertLess(records.index(share_mount), records.index(share_remount))
                self.assertLess(records.index(share_remount), guest_index)
                self.assertLess(records.index(["umount", "--", str(self.root / "shared")]), records.index(["umount", "-R", str(self.root)]))
                self.assertEqual(["resolver"] in records, network == "shared")
                self.assertEqual(records[guest_index][-2:], ["printf", "literal argument"])
                self.assertFalse((self.root / "shared").exists())
                self.assertFalse(list(self.path.rglob("*.mock-mounted")))

    def test_mount_failures_prevent_guest_command_and_clean_new_placeholder(self):
        for remount_status, bind_status in ((23, 0), (0, 19)):
            with self.subTest(remount_status=remount_status, bind_status=bind_status):
                self.calls.unlink(missing_ok=True)
                result = self.worker(remount_status=remount_status, bind_status=bind_status)
                self.assertEqual(result.returncode, remount_status or bind_status, result.stderr.decode())
                records = self.recorded_calls()
                self.assertFalse(any(item[0] == "chroot" for item in records))
                self.assertFalse((self.path / "started").exists())
                self.assertFalse((self.root / "shared").exists())
                self.assertFalse(list(self.path.rglob("*.mock-mounted")))

    def test_existing_guest_directory_and_files_survive_success_and_failure(self):
        target = self.root / "shared"
        target.mkdir()
        (target / "guest-file").write_text("guest content")
        for remount_status in (0, 23):
            with self.subTest(remount_status=remount_status):
                result = self.worker(remount_status=remount_status)
                self.assertEqual(result.returncode, remount_status, result.stderr.decode())
                self.assertEqual((target / "guest-file").read_text(), "guest content")
                self.assertEqual((self.source / "host-file").read_text(), "host content")

    def test_guest_command_failure_keeps_status_and_cleans_share(self):
        result = self.worker(guest_status=17)
        self.assertEqual(result.returncode, 17, result.stderr.decode())
        self.assertFalse((self.root / "shared").exists())
        self.assertFalse(list(self.path.rglob("*.mock-mounted")))


if __name__ == "__main__":
    unittest.main()
