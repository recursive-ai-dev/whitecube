#!/bin/sh
set -eu
# Resolve a per-user launcher symlink back to the project. No DISPLAY or D-Bus.
script=$(readlink -f -- "$0")
root=$(CDPATH= cd -- "$(dirname -- "$script")" && pwd)
build=${CUBE_BROWSER_BUILD:-$root/build}
unset DISPLAY
if [ ! -x "$build/cube-browser" ]; then
    echo "cube-browser: build missing; run $root/BUILD.sh" >&2
    exit 1
fi
exec "$build/cube-browser" "$@"
