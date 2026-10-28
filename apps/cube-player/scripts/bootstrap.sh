#!/usr/bin/env bash
# Extract the current signed-repository mpv development/runtime packages locally.
# The package manager resolves versions from this build environment's metadata.
set -euo pipefail
player_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
downloads=$player_root/.deps/downloads
mkdir -p "$downloads"
work=$(mktemp -d "$downloads/mpv-XXXXXX")
trap 'rm -rf -- "$work"' EXIT
(
  cd "$work"
  apt-get download libmpv2 libmpv-dev
)
for package in "$work"/libmpv*.deb; do
  dpkg-deb --extract "$package" "$player_root/.deps"
done
# Retain the exact downloaded inputs for repeatable offline inspection.
for package in "$work"/*.deb; do
  mv -f -- "$package" "$downloads/"
done
