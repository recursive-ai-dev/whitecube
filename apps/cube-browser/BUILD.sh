#!/usr/bin/env bash
set -euo pipefail
browser_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$browser_root"
python3 scripts/version.py
if [[ ! -f .deps/prefix/lib/pkgconfig/cairo.pc || ! -f .deps/prefix/lib/pkgconfig/sqlite3.pc || ! -f .deps/prefix/lib/pkgconfig/libjpeg.pc || ! -f .deps/lib/libquickjs-core.a || ! -f .deps/lib/libmbedtls.a ]]; then
  echo 'Local dependencies are missing. Run scripts/bootstrap-cairo.sh, scripts/bootstrap-mbedtls.sh scripts/bootstrap-jpeg.sh and scripts/bootstrap-quickjs.sh first.' >&2
  exit 1
fi
export PKG_CONFIG_PATH="$browser_root/.deps/prefix/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
if [[ -f build/build.ninja ]]; then meson setup --reconfigure build; else meson setup build; fi
meson compile -C build
meson test -C build --print-errorlogs
printf 'Built %s/build/cube-browser\n' "$browser_root"
