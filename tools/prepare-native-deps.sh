#!/usr/bin/env bash
# Run inside the Debian 13 build environment. Build dependencies stay in the
# checkout; this script does not install system packages or need root.
set -euo pipefail

project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
deps_root=$project_root/build/native-deps
prefix=$deps_root/prefix
browser=$project_root/apps/cube-browser
jobs=${JOBS:-4}
case ${1:-} in
  -h|--help)
    cat <<'HELP'
Usage: tools/prepare-native-deps.sh

Prepare local X-free Cairo, wlroots 0.18, browser dependencies and mpv headers.
Requires Debian 13 development packages and authenticated deb-src repositories.
Source build/native-deps/environment.sh before running BUILD.sh or packaging.
Set JOBS to control compilation concurrency.
HELP
    exit 0 ;;
  '') ;;
  *) echo "prepare-native-deps.sh: unknown argument: $1" >&2; exit 2 ;;
esac
[[ $jobs =~ ^[1-9][0-9]*$ ]] || { echo 'JOBS must be a positive integer' >&2; exit 2; }
# WLROOTS_SOURCE_DIR supplies an already verified wlroots tree on distributions without deb-src.
source_tools=(apt-cache apt-get dpkg-source)
[[ -z ${WLROOTS_SOURCE_DIR:-} ]] || source_tools=()
for command in "${source_tools[@]}" meson ninja cc pkg-config curl unzip make ar; do
  command -v "$command" >/dev/null || { echo "prepare-native-deps.sh: missing $command" >&2; exit 1; }
done
mkdir -p "$deps_root/src" "$prefix/lib/pkgconfig"

# The existing browser bootstrap verifies Cairo and SQLite against pinned hashes.
# Recreate Meson's generated state when changing the build distribution; old
# coredata can be unreadable across Meson versions even with --reconfigure.
if [[ -f $browser/.deps/cairo-build/meson-private/coredata.dat ]]; then
  meson setup --wipe "$browser/.deps/cairo-build" "$browser/.deps/cairo-1.18.4" \
    --prefix="$browser/.deps/prefix" --libdir=lib --buildtype=release \
    -Dxlib=disabled -Dxcb=disabled -Dxlib-xcb=disabled \
    -Dtests=disabled -Dgtk2-utils=disabled -Dgtk_doc=false \
    -Dspectre=disabled -Dsymbol-lookup=disabled \
    -Dfontconfig=enabled -Dfreetype=enabled -Dpng=enabled
fi
# The SQLite bootstrap caches its archive; rebuild it with this toolchain too.
rm -f -- "$browser/.deps/sqlite3.o" "$browser/.deps/prefix/lib/libsqlite3.a"
JOBS="$jobs" bash "$browser/scripts/bootstrap-cairo.sh"
JOBS="$jobs" bash "$browser/scripts/bootstrap-jpeg.sh"
# Force a clean rebuild of cached source objects when moving from another libc.
if [[ -d $browser/.deps/src/quickjs-2026-06-04 ]]; then
  make -C "$browser/.deps/src/quickjs-2026-06-04" clean
fi
JOBS="$jobs" bash "$browser/scripts/bootstrap-quickjs.sh"
if [[ -d $browser/.deps/src/mbedtls-3.6.7 ]]; then
  make -C "$browser/.deps/src/mbedtls-3.6.7" clean
fi
JOBS="$jobs" bash "$browser/scripts/bootstrap-mbedtls.sh"
bash "$project_root/apps/cube-player/scripts/bootstrap.sh"

export PKG_CONFIG_PATH="$prefix/lib/pkgconfig:$browser/.deps/prefix/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
export LD_LIBRARY_PATH="$prefix/lib:$browser/.deps/prefix/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

# apt validates source hashes against the configured signed Release metadata.
# Select this API explicitly rather than silently building a newer wlroots ABI.
version=${WLROOTS_SOURCE_VERSION:-}
if [[ -z $version && -z ${WLROOTS_SOURCE_DIR:-} ]]; then
  version=$(apt-cache showsrc wlroots | awk '$1 == "Version:" && $2 ~ /^0\.18\./ {print $2}' | sort -V | tail -1)
fi
[[ -n ${WLROOTS_SOURCE_DIR:-} || $version == 0.18.* ]] || {
  echo 'prepare-native-deps.sh: Debian deb-src metadata must provide wlroots 0.18 (run apt-get update in the build environment)' >&2
  exit 1
}
if [[ -n ${WLROOTS_SOURCE_DIR:-} ]]; then
  source_dir=$WLROOTS_SOURCE_DIR
else
  (
    cd "$deps_root/src"
    apt-get source "wlroots=$version"
  )
  source_dir=$deps_root/src/wlroots-${version%%-*}
fi
[[ -f $source_dir/meson.build ]] || {
  echo "prepare-native-deps.sh: source extraction missing: $source_dir" >&2; exit 1;
}
# Wayland/headless backends and Pixman are unconditional in wlroots 0.18;
# backends/renderers select the optional implementations only.
meson_options=(--prefix="$prefix" --libdir=lib --buildtype=release --wrap-mode=nofallback
  -Dxwayland=disabled -Dxcb-errors=disabled -Dbackends=drm,libinput
  -Drenderers=gles2 -Dexamples=false)
if [[ -f $deps_root/wlroots-build/meson-private/coredata.dat ]]; then
  meson setup --wipe "$deps_root/wlroots-build" "$source_dir" "${meson_options[@]}"
else
  meson setup "$deps_root/wlroots-build" "$source_dir" "${meson_options[@]}"
fi
meson compile -C "$deps_root/wlroots-build" -j "$jobs"
meson install -C "$deps_root/wlroots-build" --no-rebuild

# Refuse an accidental distro/X dependency before compiling the desktop itself.
for library in "$prefix/lib/libwlroots-0.18.so" "$browser/.deps/prefix/lib/libcairo.so.2"; do
  [[ -f $library ]] || { echo "prepare-native-deps.sh: missing $library" >&2; exit 1; }
  closure=$(ldd "$library")
  if printf '%s\n' "$closure" | grep -qE 'libX11|libxcb|not found'; then
    printf 'prepare-native-deps.sh: invalid library closure for %s:\n%s\n' "$library" "$closure" >&2
    exit 1
  fi
done

# Expose compiler metadata only. A global library override would replace GTK's
# distribution Cairo with our X-free Cairo, which lacks GTK's Xlib symbols.
# Core/Meson binaries carry their own runtime search paths.
{
  printf '# Generated by tools/prepare-native-deps.sh; source for this checkout.\n'
  printf 'export PKG_CONFIG_PATH=%q${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}\n' "$prefix/lib/pkgconfig:$browser/.deps/prefix/lib/pkgconfig"
} > "$deps_root/environment.sh"
printf 'Native dependencies ready. Run: source %q\n' "$deps_root/environment.sh"
