#!/bin/bash
# Removes regenerable clutter from the whitecube tree. Dry run unless --apply is given.
# Touches only: __pycache__ directories, *.pyc, and build/ (rebuilt by BUILD.sh). Never touches
# sources, ~/.config, /usr/local, or the whitecube.bak-* backup directories next to the tree.
set -euo pipefail
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
apply=0; [[ ${1:-} == --apply ]] && apply=1
mapfile -t targets < <(find "$here" -path "$here/.git" -prune -o \( -name __pycache__ -type d -o -name '*.pyc' -type f \) -print; for d in build build-asan build-cairo; do [[ -d $here/$d ]] && echo "$here/$d"; done)
(( ${#targets[@]} )) || { echo "nothing to clean"; exit 0; }
for t in "${targets[@]}"; do
  if (( apply )); then rm -rf -- "$t"; echo "removed ${t#"$here"/}"; else echo "would remove ${t#"$here"/}"; fi
done
(( apply )) || echo "Dry run. Remove with: $0 --apply"
ls -d "$here"/../whitecube.bak-* 2>/dev/null | sed 's/^/kept backup (delete yourself if unwanted): /' || true
