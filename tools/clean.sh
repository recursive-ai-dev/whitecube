#!/bin/bash
# Removes regenerable clutter from the whitecube tree. Dry run unless --apply is given.
# It deletes only what it can regenerate: __pycache__, *.pyc, build-asan/, build-cairo/, and the exact
# outputs BUILD.sh writes into build/. Everything else in build/ (rollback archives such as
# privacy-install-before-* and privacy-before-*.tar.gz, patches, screenshots) is left alone, as are
# sources, ~/.config, /usr/local and the whitecube.bak-* directories next to the tree.
set -euo pipefail
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
apply=0; [[ ${1:-} == --apply ]] && apply=1
products=(whitecube cube-ca cube-settings cube-status workspace-test workspace-test.c desktop-state-test compositor-test hotplug-test
          status-theme-test settings-test cube-space.ppm bar.png menu.png menu-search.png quick.png chat.png power.png toolchain)
mapfile -t targets < <(find "$here" -path "$here/.git" -prune -o \( -name __pycache__ -type d -o -name '*.pyc' -type f \) -print
  for d in build-asan build-cairo; do [[ -d $here/$d ]] && echo "$here/$d"; done
  for p in "${products[@]}"; do [[ -e $here/build/$p ]] && echo "$here/build/$p"; done)
(( ${#targets[@]} )) || { echo "nothing to clean"; exit 0; }
for t in "${targets[@]}"; do
  if (( apply )); then rm -rf -- "$t"; echo "removed ${t#"$here"/}"; else echo "would remove ${t#"$here"/}"; fi
done
(( apply )) || echo "Dry run. Remove with: $0 --apply"
for b in "$here"/../whitecube.bak-*; do [[ -e $b ]] && echo "kept backup (delete yourself if unwanted): $b"; done
exit 0
