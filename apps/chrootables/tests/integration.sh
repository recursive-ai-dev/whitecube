#!/usr/bin/env bash
# Runs against real Debian downloads, loop mounts, and namespaces.
set -Eeuo pipefail

manager_path="$(readlink -f "$(dirname "$0")/../chrootable-manager")"
test_root="$(mktemp -d /tmp/chrootable-manager-test.XXXXXX)"
inspect_mount="$test_root/inspect"

[[ $EUID -eq 0 ]] || { printf 'Run this test as root.\n' >&2; exit 1; }

cleanup() {
    mountpoint -q "$inspect_mount" && umount "$inspect_mount" || true
    rm -rf -- "$test_root"
}
trap cleanup EXIT

mkdir -p "$inspect_mount"
export CHROOTABLE_HOME="$test_root/store"

"$manager_path" --create persistent-test 1024 persistent
printf 'touch /persistent-marker\nexit\n' | "$manager_path" --enter persistent-test
mount -o loop,ro "$CHROOTABLE_HOME/images/persistent-test.img" "$inspect_mount"
test -e "$inspect_mount/persistent-marker"
umount "$inspect_mount"

"$manager_path" --create ephemeral-test 1024 ephemeral
printf 'touch /ephemeral-marker\nexit\n' | "$manager_path" --enter ephemeral-test
mount -o loop,ro "$CHROOTABLE_HOME/images/ephemeral-test.img" "$inspect_mount"
test ! -e "$inspect_mount/ephemeral-marker"
umount "$inspect_mount"

printf 'integration checks passed\n'
