#!/bin/bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
case "${1:-}" in
  ''|--check) [ "$#" -le 1 ] ;;
  --help|-h) printf '%s\n' 'Usage: scripts/install-deb.sh [--check]' 'Installs Whitecube Command Center system-wide, replacing ubuntu-command-center.'; exit 0 ;;
  *) printf '%s\n' 'Usage: scripts/install-deb.sh [--check]' >&2; exit 2 ;;
esac
version=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["version"])' "$root/package.json")
architecture=$(dpkg --print-architecture)
package="$root/release/whitecube-command-center_${version}_${architecture}.deb"
[ -f "$package" ] || { printf '%s\n' 'Build the package first: npm run package:deb' >&2; exit 1; }
(cd "$root/release" && sha256sum --check --status "$(basename "$package").sha256")
[ "$(dpkg-deb -f "$package" Package)" = whitecube-command-center ]
[ "$(dpkg-deb -f "$package" Version)" = "$version" ]
[ "$(dpkg-deb -f "$package" Architecture)" = "$architecture" ]
if [ "${1:-}" = --check ]; then
  printf 'Verified Whitecube Command Center %s (%s).\n' "$version" "$architecture"
  exit 0
fi
[ "$#" -eq 0 ] || { printf '%s\n' 'Usage: scripts/install-deb.sh [--check]' >&2; exit 2; }
# The new package explicitly replaces ubuntu-command-center, retaining its aliases.
if [ "$(id -u)" -eq 0 ]; then
  apt-get --yes install -- "$package"
else
  sudo apt-get --yes install -- "$package"
fi
[ "$(dpkg-query -W -f='${Status}' whitecube-command-center)" = 'install ok installed' ]
[ "$(dpkg-query -W -f='${Version}' whitecube-command-center)" = "$version" ]
/usr/bin/whitecube-command-center --version
printf '%s\n' 'Launch as your normal desktop user: whitecube-command-center'
