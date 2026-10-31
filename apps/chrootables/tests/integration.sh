#!/usr/bin/env bash
# Opt-in: real signed Debian download, loop mounts, namespaces, and recovery.
set -Eeuo pipefail

manager_source="$(readlink -f "$(dirname "$0")/../chrootable-manager")"
test_root="$(mktemp -d /tmp/chrootable-manager-test.XXXXXX)"
manager_path="$test_root/chrootable-manager"
inspect_mount="$test_root/inspect"
shared_dir="$test_root/shared host; [project],data"
shared_nested="$shared_dir/nested"

[[ $EUID -eq 0 ]] || { printf 'Run this test as root.\n' >&2; exit 1; }

cleanup() {
    local status=$?
    mountpoint -q "$inspect_mount" && umount "$inspect_mount" || true
    mountpoint -q "$shared_nested" && umount "$shared_nested" || true
    if (( status != 0 )); then
        printf 'Integration failure; operation logs follow.\n' >&2
        tail -n 35 "$test_root/store/logs/"*.log >&2 || true
    fi
    rm -rf -- "$test_root"
}
trap cleanup EXIT

# Keep shell reads stable if the development checkout changes during a long download.
cp -- "$manager_source" "$manager_path"
chmod 0755 "$manager_path"
mkdir -p "$inspect_mount"
export CHROOTABLE_HOME="$test_root/store"
export CHROOTABLE_DEBIAN_SUITE=trixie
export CHROOTABLE_AUTO_INSTALL=0
profile=${1:-minimal}
[[ $profile == minimal || $profile == python ]] || { printf 'Use minimal or python profile.\n' >&2; exit 2; }
size=1024
[[ $profile != python ]] || size=2048

"$manager_path" --create persistent-test "$size" persistent "$profile"
if [[ $profile == python ]]; then
    "$manager_path" --exec persistent-test -- python3 -c 'import venv, ssl; print("Python profile ready")'
fi
"$manager_path" --exec persistent-test -- /bin/sh -c 'printf original > /persistent-marker'
"$manager_path" --exec persistent-test --restricted-dev -- /bin/sh -ec 'test -c /dev/null; test -c /dev/urandom; test -c /dev/ptmx; test ! -e /dev/loop-control; test ! -e /dev/sda'
# /dev/fd and friends resolve through /proc, so a restricted session still needs it.
"$manager_path" --exec persistent-test --restricted-dev -- /bin/sh -ec 'test -r /proc/self/status; test -d /run; test -d /dev/shm; ls /dev/fd/ >/dev/null'

# A guest resolver link is followed inside the guest. An absolute target must not
# create a host file, and it must survive the /run tmpfs so DNS configuration works.
resolver_probe=/run/chrootable-integration-resolver
# Isolated sessions do not bind the resolver, so its link can be edited there.
"$manager_path" --exec persistent-test --isolated -- /bin/sh -ec "rm -f /etc/resolv.conf; ln -s $resolver_probe/resolv.conf /etc/resolv.conf"
"$manager_path" --exec persistent-test -- /bin/sh -ec 'grep -q nameserver /etc/resolv.conf'
test ! -e "$resolver_probe"
"$manager_path" --exec persistent-test --isolated -- /bin/sh -ec 'rm -f /etc/resolv.conf; : >/etc/resolv.conf'
mount -o loop,ro "$CHROOTABLE_HOME/images/persistent-test.img" "$inspect_mount"
test "$(cat "$inspect_mount/persistent-marker")" = original
umount "$inspect_mount"

"$manager_path" --snapshot persistent-test clean
"$manager_path" --exec persistent-test -- /bin/sh -c 'printf changed > /persistent-marker'
"$manager_path" --restore persistent-test clean --yes
test "$("$manager_path" --exec persistent-test -- cat /persistent-marker)" = original
test "$("$manager_path" --snapshots persistent-test | wc -l)" -eq 2

"$manager_path" --try persistent-test -- touch /disposable-marker
"$manager_path" --exec persistent-test -- test ! -e /disposable-marker
"$manager_path" --exec persistent-test --isolated -- /bin/sh -c 'test "$(ls /sys 2>/dev/null | wc -l)" -eq 0; ! grep -q eth /proc/net/dev'

# Shared host files outlive every image lifecycle. The unusual path also checks
# that shell metacharacters and spaces stay literal throughout the worker call.
mkdir -p "$shared_nested"
printf host-original >"$shared_dir/host-marker"
"$manager_path" --exec persistent-test --share "$shared_dir" -- /bin/sh -ec 'test "$(cat /shared/host-marker)" = host-original; printf persistent > /shared/persistent-write'
test "$(cat "$shared_dir/persistent-write")" = persistent
"$manager_path" --exec persistent-test -- test ! -e /shared
"$manager_path" --try persistent-test --share "$shared_dir" -- /bin/sh -ec 'printf disposable > /shared/disposable-write; touch /disposable-shared-marker'
test "$(cat "$shared_dir/disposable-write")" = disposable
"$manager_path" --exec persistent-test -- /bin/sh -ec 'test ! -e /shared; test ! -e /disposable-shared-marker'

"$manager_path" --exec persistent-test --read-only --share "$shared_dir" -- /bin/sh -ec 'test "$(cat /shared/host-marker)" = host-original; ! touch /shared/readonly-write 2>/dev/null; ! printf changed > /shared/host-marker'
test ! -e "$shared_dir/readonly-write"
test "$(cat "$shared_dir/host-marker")" = host-original
printf outside-writable >"$shared_dir/outside-write"
"$manager_path" --try persistent-test --read-only --share "$shared_dir" --isolated -- /bin/sh -ec 'test -f /shared/host-marker; ! touch /shared/isolated-readonly-write 2>/dev/null; ! grep -q eth /proc/net/dev'
test ! -e "$shared_dir/isolated-readonly-write"

# Nested host mounts are excluded by a plain bind, including with read-only sharing.
printf underlying >"$shared_nested/underlying-file"
mount -t tmpfs -o size=1m,nosuid,nodev tmpfs "$shared_nested"
touch "$shared_nested/nested-mount-file"
"$manager_path" --exec persistent-test --share "$shared_dir" --read-only -- /bin/sh -ec 'test -f /shared/nested/underlying-file; test ! -e /shared/nested/nested-mount-file; ! touch /shared/nested/write 2>/dev/null'
umount "$shared_nested"

# Guest failures still detach the share and retain the guest exit status.
guest_status=0
"$manager_path" --exec persistent-test --share "$shared_dir" -- /bin/sh -ec 'exit 17' || guest_status=$?
test "$guest_status" -eq 17
"$manager_path" --exec persistent-test -- test ! -e /shared
test -z "$(losetup -j "$CHROOTABLE_HOME/images/persistent-test.img")"

# A pre-existing directory and its guest files reappear after the share exits.
"$manager_path" --exec persistent-test -- /bin/sh -ec 'mkdir /shared; printf guest-data > /shared/guest-file'
"$manager_path" --exec persistent-test --share "$shared_dir" -- test ! -e /shared/guest-file
"$manager_path" --exec persistent-test -- /bin/sh -ec 'test "$(cat /shared/guest-file)" = guest-data; rm /shared/guest-file; rmdir /shared'

# A file or symlink at /shared must never redirect a mount into the host.
"$manager_path" --exec persistent-test -- /bin/sh -ec 'printf guest-file > /shared'
if "$manager_path" --exec persistent-test --share "$shared_dir" -- touch /shared/unsafe; then
    printf 'A guest file at /shared was accepted.\n' >&2; exit 1
fi
"$manager_path" --exec persistent-test -- /bin/sh -ec 'test "$(cat /shared)" = guest-file; rm /shared'
mkdir "$test_root/escape-target"
"$manager_path" --exec persistent-test -- /bin/sh -ec 'ln -s "$1" /shared' sh "$test_root/escape-target"
if "$manager_path" --exec persistent-test --share "$shared_dir" -- touch /shared/unsafe; then
    printf 'A guest symlink at /shared was accepted.\n' >&2; exit 1
fi
test ! -e "$test_root/escape-target/unsafe"
"$manager_path" --exec persistent-test -- rm /shared

"$manager_path" --clone persistent-test ephemeral-test
"$manager_path" --mode ephemeral-test ephemeral
"$manager_path" --exec ephemeral-test -- touch /ephemeral-marker
"$manager_path" --exec ephemeral-test --share "$shared_dir" -- /bin/sh -ec 'printf ephemeral > /shared/ephemeral-write; touch /ephemeral-shared-marker'
test "$(cat "$shared_dir/ephemeral-write")" = ephemeral
"$manager_path" --exec ephemeral-test -- /bin/sh -ec 'test ! -e /shared; test ! -e /ephemeral-shared-marker'
mount -o loop,ro "$CHROOTABLE_HOME/images/ephemeral-test.img" "$inspect_mount"
test ! -e "$inspect_mount/ephemeral-marker"
umount "$inspect_mount"

"$manager_path" --resize persistent-test "$((size + 256))"
test "$(stat -c%s "$CHROOTABLE_HOME/images/persistent-test.img")" -eq $(((size + 256) * 1048576))
"$manager_path" --repair persistent-test --yes
python3 - "$manager_source" "$CHROOTABLE_HOME" <<'PY'
import json, pathlib, subprocess, sys
broker = pathlib.Path(sys.argv[1]).parent / 'chrootables/broker.py'
request = {'id': 1, 'action': 'snapshot', 'params': {'name': 'persistent-test', 'target': 'broker-check'}}
result = subprocess.run([sys.executable, str(broker), '--store', sys.argv[2], '--writable'], input=json.dumps(request)+'\n', capture_output=True, text=True, check=True)
events = [json.loads(line) for line in result.stdout.splitlines()]
assert events[0]['writable']
assert events[-1]['ok'], events
print('Privileged desktop broker snapshot passed')
PY
archive=$("$manager_path" --export persistent-test)
tar -tzf "$archive" >"$test_root/archive-files"
grep -qx './persistent-marker' "$test_root/archive-files"

# Export and import round trip, including file capabilities (not user.* xattrs).
mount -o loop "$CHROOTABLE_HOME/images/persistent-test.img" "$inspect_mount"
: >"$inspect_mount/capability-file"
python3 -c 'import os, sys; os.setxattr(sys.argv[1], "security.capability", bytes.fromhex("0100000200200000000000000000000000000000"))' "$inspect_mount/capability-file"
umount "$inspect_mount"
archive=$("$manager_path" --export persistent-test)
"$manager_path" --import imported-test "$archive"
test "$("$manager_path" --exec imported-test -- cat /persistent-marker)" = original
test "$("$manager_path" --exec imported-test -- cat /etc/hostname)" = imported-test
"$manager_path" --inspect imported-test --json | python3 -c 'import json, sys; item = json.load(sys.stdin); assert item["origin"].startswith("import:"), item; assert item["suite"] == "trixie", item'
mount -o loop,ro "$CHROOTABLE_HOME/images/imported-test.img" "$inspect_mount"
python3 -c 'import os, sys; assert os.getxattr(sys.argv[1], "security.capability").hex().startswith("01000002"), "capability lost"' "$inspect_mount/capability-file"
umount "$inspect_mount"
"$manager_path" --delete imported-test --yes
mkdir -p "$test_root/not-a-root/etc"
printf x >"$test_root/not-a-root/etc/file"
tar -C "$test_root/not-a-root" -czf "$test_root/not-a-root.tar.gz" .
if "$manager_path" --import not-a-root "$test_root/not-a-root.tar.gz"; then
    printf 'An archive without a root filesystem was imported.\n' >&2; exit 1
fi
test ! -e "$CHROOTABLE_HOME/images/not-a-root.img"

# Interrupted work can be abandoned, which removes its staged file and unblocks.
discard_id=$(printf 'd%.0s' {1..32})
printf partial >"$CHROOTABLE_HOME/images/.discard-test.ABC123.partial"
printf 'interrupted\n' >"$CHROOTABLE_HOME/logs/$discard_id.log"
printf 'format=1\nid=%s\npid=2147483647\nprocess_start=1\nstatus=running\nstage=copying\naction=clone\nname=persistent-test\npending=images/.discard-test.ABC123.partial\ndestination=\n' "$discard_id" >"$CHROOTABLE_HOME/operations/$discard_id.state"
"$manager_path" --discard-operation "$discard_id" --yes
test ! -e "$CHROOTABLE_HOME/images/.discard-test.ABC123.partial"

"$manager_path" --delete persistent-test --yes
test ! -e "$CHROOTABLE_HOME/images/persistent-test.img"
test -e "$CHROOTABLE_HOME/snapshots/persistent-test/clean.img"
"$manager_path" --restore persistent-test clean --yes
"$manager_path" --exec persistent-test -- test -e /persistent-marker
"$manager_path" --drop-snapshot persistent-test clean --yes
test ! -e "$CHROOTABLE_HOME/snapshots/persistent-test/clean.img"

# Every real operation publishes its identity/stage durably. Guest exit 17 and
# intentionally rejected share mounts may fail; none may remain interrupted.
"$manager_path" --operations --json >"$test_root/operations.json"
python3 - "$test_root/operations.json" <<'PY'
import json, pathlib, sys
operations = json.loads(pathlib.Path(sys.argv[1]).read_text())
assert operations, 'No durable operations were recorded'
assert all(item['status'] in ('completed', 'failed') and not item['active'] for item in operations), operations
assert all(pathlib.Path(item['log']).is_file() for item in operations), operations
assert any(item['action'] == 'restore' and item['status'] == 'completed' for item in operations), operations
print('Durable operation records passed')
PY

printf 'integration checks passed\n'
