#!/bin/bash
# Builds Cairo WITHOUT X11/XCB and stages it under build-cairo/stage. Needs no root and installs nothing.
# Why: MAINTENANCE.md requires an X-free desktop; the distro libcairo drags libX11/libxcb into every
# client. Ubuntu/Debian cairo in /usr/local wins over /lib at runtime (ldconfig order), so installing
# this stage into /usr/local fixes all consumers at once.
#
#   tools/build-xfree-cairo.sh [--sha256 HASH] [--version 1.18.4]
#
# The tarball comes from cairographics.org over HTTPS. Pass --sha256 with the value published for that
# release to make the build refuse a different file; without it the hash is printed so you can compare.
set -euo pipefail
version=1.18.4; want=
while [[ $# -gt 0 ]]; do
  case $1 in
    --sha256) want=${2:?}; shift 2 ;;
    --version) version=${2:?}; shift 2 ;;
    *) echo "unknown argument $1" >&2; exit 2 ;;
  esac
done
[[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "bad version" >&2; exit 2; }
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
work=$here/build-cairo
rm -rf "$work"; mkdir -p "$work/src" "$work/stage"
tarball=$work/cairo-$version.tar.xz
curl -fsSL --proto '=https' -o "$tarball" "https://cairographics.org/releases/cairo-$version.tar.xz"
got=$(sha256sum "$tarball" | cut -d' ' -f1)
echo "sha256 $got"
[[ -z $want || $want == "$got" ]] || { echo "hash mismatch: refusing to build" >&2; exit 1; }
tar -xf "$tarball" -C "$work/src" --strip-components=1        # extracted into its own empty directory
( cd "$work/src" && meson setup build-nx --prefix=/usr/local -Dxlib=disabled -Dxcb=disabled \
    -Dxlib-xcb=disabled -Dtests=disabled >"$work/meson.log" 2>&1 && ninja -C build-nx >"$work/ninja.log" 2>&1 \
    && DESTDIR="$work/stage" ninja -C build-nx install >>"$work/ninja.log" 2>&1 ) \
  || { echo "build failed; see $work/meson.log and $work/ninja.log" >&2; exit 1; }
lib=$(find "$work/stage" -name 'libcairo.so.2' | head -1)
[[ -n $lib ]] || { echo "libcairo.so.2 not staged" >&2; exit 1; }
x=$(ldd "$lib" | grep -ci -E 'libX11|libxcb' || true)
echo "staged: $lib  (X11/XCB links: $x)"
(( x == 0 )) || { echo "the staged library still links X11/XCB" >&2; exit 1; }
cat <<MSG

Nothing was installed. To install (needs root; backs up nothing, it only adds files under /usr/local):
  sudo cp -a "$work/stage/usr/local/." /usr/local/ && sudo ldconfig
Check afterwards:  ldd /usr/local/bin/* build/whitecube | grep -ci 'libX11|libxcb'   (compositor: expect 0)
To undo: sudo rm /usr/local/lib/x86_64-linux-gnu/libcairo* and run sudo ldconfig.
MSG
