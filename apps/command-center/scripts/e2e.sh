#!/bin/bash
# An isolated headless compositor and profile; never connects to the live desktop.
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
S=$(mktemp -d)
out=${1:-$here/test-artifacts/e2e}; mkdir -p "$out" "$S/home/state" "$S/xdg"; chmod 700 "$S/xdg"
out=$(cd "$out" && pwd)
compositor=${CC_COMPOSITOR:-$HOME/whitecube/build/whitecube}
launcher=${CC_APP_LAUNCHER:-$here/whitecube-command-center.sh}
port=$(node -e 'const s=require("net").createServer().listen(0,"127.0.0.1",()=>{console.log(s.address().port);s.close()})')
cd "$here"
# The startup file avoids interpreting caller-provided paths as shell code.
sandbox_flag=--no-sandbox
if [ "${CC_TEST_SANDBOX:-0}" = 1 ]; then sandbox_flag=--disable-setuid-sandbox; fi
if [ "${CC_TEST_SANDBOX:-0}" = system ]; then sandbox_flag=; fi
printf '#!/bin/bash\nexec %q --remote-debugging-port=%q --user-data-dir=%q' "$launcher" "$port" "$S/ud" > "$S/start"
if [ -n "$sandbox_flag" ]; then printf ' %q' "$sandbox_flag" >> "$S/start"; fi
printf '\n' >> "$S/start"
chmod +x "$S/start"
startup_command=$(printf '/bin/bash %q' "$S/start")
env -i PATH="$PATH" HOME="$S/home" XDG_STATE_HOME="$S/home/state" XDG_RUNTIME_DIR="$S/xdg" XDG_SESSION_TYPE=wayland \
  WLR_BACKENDS=headless WLR_HEADLESS_OUTPUTS=1 WLR_RENDERER=pixman \
  timeout 150 "$compositor" -s "$startup_command" >"$S/app.log" 2>&1 &
wc=$!
cleanup() { kill "$wc" 2>/dev/null || true; wait "$wc" 2>/dev/null || true; cp "$S/app.log" "$out/app.log"; rm -rf -- "$S"; }
trap cleanup EXIT
node scripts/e2e-driver.cjs "$port" "$out"
echo "Screenshots: $out"
