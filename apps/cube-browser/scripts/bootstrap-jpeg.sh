#!/bin/sh
# Extract a pinned, signed-repository Debian development package locally.
# No package manager installation or system paths are modified.
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
deps="$root/.deps"
prefix="$deps/prefix"
version=2.1.5-4
archive="$deps/downloads/libjpeg62-turbo-dev_${version}_amd64.deb"
sha256=e687e3c8b24a3aa74974928b8e0b07f6df2442187492d85cbcea7dacd91e3aba
# Hash from Debian trixie's signed Packages metadata, version 1:2.1.5-4.
# This local binary dependency is for the whitecube Linux x86-64 target.
if [ "$(uname -m)" != x86_64 ]; then
    echo 'bootstrap-jpeg.sh targets Linux x86-64; supply libjpeg development files for this architecture.' >&2
    exit 1
fi
mkdir -p "$deps/downloads" "$prefix/include" "$prefix/lib/pkgconfig" \
    "$prefix/share/licenses/libjpeg-turbo"
if [ ! -f "$archive" ]; then
    curl --fail --silent --show-error --location --retry 2 \
        --output "$archive.part" \
        "https://deb.debian.org/debian/pool/main/libj/libjpeg-turbo/libjpeg62-turbo-dev_${version}_amd64.deb"
    mv "$archive.part" "$archive"
fi
printf '%s  %s\n' "$sha256" "$archive" | sha256sum --check --status
unpacked="$deps/jpeg-dev-$version"
mkdir -p "$unpacked"
dpkg-deb --extract "$archive" "$unpacked"
cp "$unpacked/usr/include/jpeglib.h" "$unpacked/usr/include/jerror.h" \
    "$unpacked/usr/include/jmorecfg.h" "$unpacked/usr/include/x86_64-linux-gnu/jconfig.h" \
    "$prefix/include/"
cp "$unpacked/usr/lib/x86_64-linux-gnu/libjpeg.a" "$prefix/lib/"
cp "$unpacked/usr/share/doc/libjpeg62-turbo-dev/copyright" \
    "$prefix/share/licenses/libjpeg-turbo/copyright"
cat > "$prefix/lib/pkgconfig/libjpeg.pc" <<EOF
prefix=$prefix
libdir=\${prefix}/lib
includedir=\${prefix}/include

Name: libjpeg
Description: Pinned local libjpeg-turbo static library
Version: 2.1.5
Libs: -L\${libdir} -ljpeg
Cflags: -I\${includedir}
EOF
printf 'Local libjpeg-turbo %s ready in %s\n' "$version" "$prefix"
