#!/usr/bin/env bash
set -euo pipefail
player_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
mkdir -p "$player_root/.deps/downloads"
cd "$player_root/.deps/downloads"
apt-get download libmpv2=0.40.0-3+deb13u1 libmpv-dev=0.40.0-3+deb13u1
for package in libmpv*.deb; do dpkg-deb -x "$package" "$player_root/.deps"; done
