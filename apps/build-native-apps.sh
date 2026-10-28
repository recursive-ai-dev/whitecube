#!/bin/bash
# build-native-apps.sh OUTDIR : configure, compile and test the meson apps (cube-browser, cube-player).
#
# Each app builds in OUTDIR/<app> and is installed (meson install --destdir) into OUTDIR/native-apps-root
# with /usr/lib/whitecube as its runtime library path, ready for packaging/install-tree.sh:
#   /usr/libexec/whitecube/cube-browser/{cube-browser,cube-renderd,cube-netd}  (the browser starts its
#                                        workers from its own directory)
#   /usr/bin/cube-player                (libmpv comes from the libmpv2 package at run time)
# Private build inputs live in apps/<app>/.deps (not in git); recreate them with the app's bootstrap scripts.
# Tests that need a live Wayland session (meson suite "integration") are skipped; run them from the app.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
out=$(realpath -m -- "${1:?usage: build-native-apps.sh OUTDIR}")
dest=$out/native-apps-root
rm -rf -- "$dest"

build_app() {
  local app=$1 bindir=$2 src=$root/apps/$1 dir=$out/$1
  local hardening=(-Db_pie=true -Db_asneeded=true
    '-Dc_args=-fstack-protector-strong -D_FORTIFY_SOURCE=3 -Wformat -Werror=format-security'
    '-Dc_link_args=-Wl,-z,relro,-z,now')
  if [[ -f $dir/build.ninja ]]; then
    meson setup --reconfigure "$dir" "$src" --prefix=/usr --bindir="$bindir" -Dprivate_libdir=/usr/lib/whitecube "${hardening[@]}" > "$dir.setup.log"
  else
    meson setup "$dir" "$src" --prefix=/usr --bindir="$bindir" -Dprivate_libdir=/usr/lib/whitecube "${hardening[@]}" > "$dir.setup.log"
  fi
  meson compile -C "$dir"
  meson test -C "$dir" --no-suite integration --print-errorlogs
  meson install -C "$dir" --destdir "$dest" --no-rebuild > /dev/null
}

browser=$root/apps/cube-browser
for f in .deps/prefix/lib/pkgconfig/cairo.pc .deps/prefix/lib/pkgconfig/sqlite3.pc .deps/prefix/lib/pkgconfig/libjpeg.pc \
         .deps/lib/libquickjs-core.a .deps/lib/libmbedtls.a; do
  [[ -f $browser/$f ]] || { echo "build-native-apps.sh: apps/cube-browser/$f is missing; run its scripts/bootstrap-*.sh" >&2; exit 1; }
done
python3 -I "$browser/scripts/version.py" --check
PKG_CONFIG_PATH="$browser/.deps/prefix/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}" \
  build_app cube-browser libexec/whitecube/cube-browser

[[ -f $root/apps/cube-player/.deps/usr/include/mpv/client.h ]] || {
  echo 'build-native-apps.sh: apps/cube-player/.deps is missing; run apps/cube-player/scripts/bootstrap.sh' >&2; exit 1; }
build_app cube-player bin

echo "native apps installed into $dest"
