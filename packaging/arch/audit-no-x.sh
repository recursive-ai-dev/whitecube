#!/bin/bash
# audit-no-x.sh ROOT : fail if ROOT holds an X server, XWayland, a capture tool or a remote desktop server.
set -euo pipefail
root=${1:?usage: audit-no-x.sh ROOT}
[[ -d $root/usr/bin ]] || { echo "audit-no-x: $root is not a system root" >&2; exit 2; }
found=()
for name in X Xorg Xwayland Xvfb Xephyr Xnest Xvnc x11vnc xwd grim wf-recorder wayvnc; do
  [[ ! -e $root/usr/bin/$name && ! -L $root/usr/bin/$name ]] || found+=("/usr/bin/$name")
done
for pacman_name in xorg-server xorg-server-xwayland xorg-server-xvfb xorg-xwd grim wf-recorder wayvnc; do
  if compgen -G "$root/var/lib/pacman/local/$pacman_name-[0-9]*" >/dev/null; then found+=("package $pacman_name"); fi
done
if (( ${#found[@]} )); then
  printf 'audit-no-x: FAILED, found: %s\n' "${found[*]}" >&2
  exit 1
fi
echo 'audit-no-x: PASS, no X server, XWayland, capture tool or remote desktop server'
