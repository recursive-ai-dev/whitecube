#!/bin/sh
# Build pinned, X-free graphics and SQLite dependencies without system changes.
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
deps="$root/.deps"
prefix="$deps/prefix"
mkdir -p "$deps/downloads" "$prefix/lib/pkgconfig" "$prefix/include"

fetch() {
    url=$1 archive=$2 hash=$3
    if [ ! -f "$archive" ]; then
        curl --fail --silent --show-error --location --retry 2 \
            --output "$archive.part" "$url"
        mv "$archive.part" "$archive"
    fi
    printf '%s  %s\n' "$hash" "$archive" | sha256sum --check --status
}

fetch https://cairographics.org/releases/cairo-1.18.4.tar.xz \
    "$deps/downloads/cairo-1.18.4.tar.xz" \
    445ed8208a6e4823de1226a74ca319d3600e83f6369f99b14265006599c32ccb
if [ ! -f "$deps/cairo-1.18.4/meson.build" ]; then
    tar -xf "$deps/downloads/cairo-1.18.4.tar.xz" -C "$deps"
fi
if [ ! -f "$deps/cairo-build/build.ninja" ]; then
    meson setup "$deps/cairo-build" "$deps/cairo-1.18.4" \
        --prefix="$prefix" --libdir=lib --buildtype=release \
        -Dxlib=disabled -Dxcb=disabled -Dxlib-xcb=disabled \
        -Dtests=disabled -Dgtk2-utils=disabled -Dgtk_doc=false \
        -Dspectre=disabled -Dsymbol-lookup=disabled \
        -Dfontconfig=enabled -Dfreetype=enabled -Dpng=enabled
fi
meson compile -C "$deps/cairo-build" -j "${JOBS:-4}"
meson install -C "$deps/cairo-build"

# SHA-256 pinned after checking the official download page's SHA3-256:
# 628a44cfe82c66aed1ccbbe85a562d2e33ebe64b3288981ed76285612227934e
# https://www.sqlite.org/download.html (SQLite 3.53.4, 2026-07-24).
fetch https://www.sqlite.org/2026/sqlite-amalgamation-3530400.zip \
    "$deps/downloads/sqlite-amalgamation-3530400.zip" \
    1e71ddf93849c6a6ecf58b827c0692073d2dd7ee40196158068f7b29f422e87d
if [ ! -f "$deps/sqlite-amalgamation-3530400/sqlite3.c" ]; then
    unzip -q "$deps/downloads/sqlite-amalgamation-3530400.zip" -d "$deps"
fi
if [ ! -f "$prefix/lib/libsqlite3.a" ]; then
    "${CC:-cc}" -O2 -fPIC -DSQLITE_THREADSAFE=1 -DSQLITE_OMIT_LOAD_EXTENSION \
        -DSQLITE_DQS=0 -c "$deps/sqlite-amalgamation-3530400/sqlite3.c" \
        -o "$deps/sqlite3.o"
    "${AR:-ar}" rcs "$prefix/lib/libsqlite3.a" "$deps/sqlite3.o"
fi
cp "$deps/sqlite-amalgamation-3530400/sqlite3.h" "$prefix/include/"
cat > "$prefix/lib/pkgconfig/sqlite3.pc" <<EOF
prefix=$prefix
libdir=\${prefix}/lib
includedir=\${prefix}/include

Name: SQLite
Description: Pinned local SQLite amalgamation
Version: 3.53.4
Libs: -L\${libdir} -lsqlite3 -lm -pthread
Cflags: -I\${includedir}
EOF
"$root/scripts/check-links.sh" "$prefix/lib/libcairo.so.2"
printf 'Local dependencies ready. Build with PKG_CONFIG_PATH=%s/lib/pkgconfig\n' "$prefix"
