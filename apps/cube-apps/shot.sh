#!/usr/bin/env bash
# Render an app window to a PNG inside a throwaway headless Whitecube
# compositor, so the live desktop session is never touched.
# usage: shot.sh <app.py> <out.png> [extra env, e.g. CUBE_TAB=1] [-- app args]
set -euo pipefail
app="$1"; out="$2"; shift 2
here="$(cd "$(dirname "$0")" && pwd)"
s="$(mktemp -d)"; mkdir -p "$s/home/state" "$s/xdg"; chmod 700 "$s/xdg"
extra=()
while [[ $# -gt 0 && "$1" != -- ]]; do extra+=("$1"); shift; done
[[ "${1:-}" == -- ]] && shift
env -i PATH="$PATH" HOME="$s/home" XDG_STATE_HOME="$s/home/state" XDG_RUNTIME_DIR="$s/xdg" \
  WLR_BACKENDS=headless WLR_HEADLESS_OUTPUTS=1 WLR_RENDERER=pixman \
  timeout 25 "$HOME/whitecube/build/whitecube" -s \
  "GSK_RENDERER=cairo GDK_BACKEND=wayland CUBE_SHOT='$out' ${extra[*]:-} python3 '$here/$app' $* 2>'$s/app.log'" \
  >"$s/wc.log" 2>&1 || true
[[ -s "$out" ]] && echo "wrote $out" || { echo "no screenshot"; tail -n 20 "$s/app.log"; tail -n 5 "$s/wc.log"; }
rm -rf "$s"
