#!/bin/sh
# Official maintained 3.6 LTS release; no system packages or shared libraries.
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
deps="$root/.deps"
version=3.6.7
sha256=a7e8bcbec0e6f761b4af24f25677626b35f762f68eef79c08677a363212d11f6
archive="mbedtls-$version.tar.bz2"
mkdir -p "$deps/src" "$deps/include" "$deps/lib" "$deps/licenses"
cd "$deps/src"
if [ ! -f "$archive" ]; then
    curl --fail --location --proto '=https' --tlsv1.2 --max-time 120 \
        "https://github.com/Mbed-TLS/mbedtls/releases/download/mbedtls-$version/$archive" -o "$archive.part"
    mv "$archive.part" "$archive"
fi
printf '%s  %s\n' "$sha256" "$archive" | sha256sum --check --status
if [ ! -d "mbedtls-$version" ]; then tar -xf "$archive"; fi
make -C "mbedtls-$version" -j"${JOBS:-4}" lib SHARED=
cp -R "mbedtls-$version/include/mbedtls" "mbedtls-$version/include/psa" "$deps/include/"
cp "mbedtls-$version/library/libmbedtls.a" "mbedtls-$version/library/libmbedx509.a" \
    "mbedtls-$version/library/libmbedcrypto.a" "$deps/lib/"
cp "mbedtls-$version/LICENSE" "$deps/licenses/mbedtls-LICENSE"
printf 'Mbed TLS %s: static libraries ready in %s\n' "$version" "$deps/lib"
