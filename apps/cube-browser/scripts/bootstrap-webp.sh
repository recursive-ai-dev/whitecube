#!/usr/bin/env bash
# Build the pinned, static decoder locally; never install host packages.
set -euo pipefail
browser_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
deps="$browser_root/.deps"
prefix="$deps/prefix"
version=1.6.0
sha256=e4ab7009bf0629fd11982d4c2aa83964cf244cffba7347ecd39019a9e38c4564
# Upstream HTTPS release; digest independently matches Homebrew's webp formula.
archive="$deps/downloads/libwebp-$version.tar.gz"
source_dir="$deps/libwebp-$version"
build_dir="$deps/webp-build"
jobs=${JOBS:-4}
[[ $jobs =~ ^[1-9][0-9]*$ ]] || { echo 'JOBS must be a positive integer' >&2; exit 2; }
mkdir -p "$deps/downloads" "$prefix/include/webp" "$prefix/lib/pkgconfig" "$deps/licenses"
if [[ ! -f $archive ]]; then
  curl --fail --silent --show-error --location --retry 2 \
    --output "$archive.part" "https://storage.googleapis.com/downloads.webmproject.org/releases/webp/libwebp-$version.tar.gz"
  printf '%s  %s\n' "$sha256" "$archive.part" | sha256sum --check --status
  mv -- "$archive.part" "$archive"
fi
printf '%s  %s\n' "$sha256" "$archive" | sha256sum --check --status
if [[ ! -f $source_dir/configure ]]; then tar -xzf "$archive" -C "$deps"; fi
compiler=${CC:-cc}
c_flags=${CFLAGS:--O2 -fPIC}
fingerprint=$({ "$compiler" --version; printf '%s\n' "$c_flags" "$version" "$sha256" "$(uname -m)"; } | sha256sum)
if [[ -f $build_dir/.cube-toolchain && $(cat "$build_dir/.cube-toolchain") != "$fingerprint" ]]; then
  rm -rf -- "$build_dir"
fi
mkdir -p "$build_dir"
log="$deps/webp-build.log"
if ! (
  cd "$build_dir"
  CFLAGS="$c_flags" "$source_dir/configure" --prefix="$prefix" --libdir="$prefix/lib" \
    --disable-shared --enable-static --enable-libwebpdecoder --disable-libwebpmux \
    --disable-libwebpdemux --disable-threading --disable-png --disable-jpeg --disable-tiff --disable-gif
  make -j"$jobs"
) >"$log" 2>&1; then tail -80 "$log" >&2; exit 1; fi
install -m 0644 "$build_dir/src/.libs/libwebpdecoder.a" "$prefix/lib/libwebpdecoder.a"
install -m 0644 "$source_dir/src/webp/decode.h" "$source_dir/src/webp/types.h" "$prefix/include/webp/"
install -m 0644 "$source_dir/COPYING" "$deps/licenses/webp-COPYING"
install -m 0644 "$source_dir/PATENTS" "$deps/licenses/webp-PATENTS"
cat >"$prefix/lib/pkgconfig/libwebpdecoder.pc" <<EOF
prefix=$prefix
libdir=\${prefix}/lib
includedir=\${prefix}/include

Name: libwebpdecoder
Description: Pinned static WebP decoder without threading or host I/O
Version: $version
Libs: -L\${libdir} -lwebpdecoder
Libs.private: -lm
Cflags: -I\${includedir}
EOF
printf '%s\n' "$fingerprint" >"$build_dir/.cube-toolchain"
printf 'Local WebP decoder %s ready in %s\n' "$version" "$prefix"
