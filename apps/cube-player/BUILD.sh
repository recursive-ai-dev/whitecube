#!/usr/bin/env bash
set -euo pipefail
player_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$player_root"
if [[ ! -f .deps/usr/include/mpv/client.h ]]; then
  ./scripts/bootstrap.sh
fi
if [[ -f build/build.ninja ]]; then meson setup --reconfigure build; else meson setup build; fi
meson compile -C build
meson test -C build --print-errorlogs
