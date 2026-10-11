#!/bin/sh
# Pinned upstream MIT interpreter core; deliberately omit quickjs-libc (OS/files).
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
deps="$root/.deps"
version=2026-06-04
sha256=b376e839b322978313d929fd20663b11ba58b75df5a46c126dd19ea2fa70ad2a
archive="quickjs-$version.tar.xz"
mkdir -p "$deps/src" "$deps/include" "$deps/lib" "$deps/licenses"
cd "$deps/src"
if [ ! -f "$archive" ]; then
    curl --fail --location --proto '=https' --tlsv1.2 --max-time 120 \
        "https://bellard.org/quickjs/$archive" -o "$archive.part"
    mv "$archive.part" "$archive"
fi
printf '%s  %s\n' "$sha256" "$archive" | sha256sum --check --status
if [ ! -d "quickjs-$version" ]; then tar -xf "$archive"; fi
cd "quickjs-$version"
make -j"${JOBS:-4}" .obj/quickjs.o .obj/dtoa.o .obj/libregexp.o .obj/libunicode.o .obj/cutils.o
ar rcs "$deps/lib/libquickjs-core.a" .obj/quickjs.o .obj/dtoa.o .obj/libregexp.o .obj/libunicode.o .obj/cutils.o
cp quickjs.h "$deps/include/"
cp LICENSE "$deps/licenses/quickjs-LICENSE"
printf 'QuickJS %s core ready in %s\n' "$version" "$deps/lib"
