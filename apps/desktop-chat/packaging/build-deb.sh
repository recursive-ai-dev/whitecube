#!/bin/sh
# No root or installation required. Use npm ci first to obtain the pinned runtime.
set -eu
app_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec node "$app_dir/packaging/build-deb.js" "$@"
