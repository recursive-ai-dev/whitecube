#!/bin/sh
# Inspect the complete dynamic-loader closure, including transitive Pango deps.
set -eu
if [ "$#" -eq 0 ]; then
    echo 'Usage: scripts/check-links.sh ELF [ELF ...]' >&2
    exit 2
fi
status=0
for binary in "$@"; do
    if [ ! -f "$binary" ]; then
        printf 'Missing binary: %s\n' "$binary" >&2
        status=1
        continue
    fi
    closure=$(ldd "$binary" 2>&1) || {
        printf 'Could not inspect %s:\n%s\n' "$binary" "$closure" >&2
        status=1
        continue
    }
    if printf '%s\n' "$closure" | grep -Eiq 'lib(X11|Xext|Xrender|Xfixes|Xi\.|Xrandr|Xcursor|Xinerama|Xau|Xdmcp|Xt\.|Xtst|Xss|Xv\.|xcb|GLX|SDL)|not found'; then
        printf 'Forbidden or unresolved link dependency in %s:\n%s\n' "$binary" "$closure" >&2
        status=1
    else
        printf 'X-free link closure: %s\n' "$binary"
    fi
done
exit "$status"
