#!/bin/sh
# Wayland-native launcher for desktop-chat (whitecube has no X).
# Usage: desktop-chat [--toggle|--show|--hide|--settings|--quit] [--checkpoint RUN/ckpt_best.pt]
# The system package owns its runtime; source checkouts can use local Electron.
script_path=$(readlink -f -- "$0") || exit 1
app_dir=${script_path%/*}
cd -- "$app_dir" || exit 1
export MOZ_ENABLE_WAYLAND=1 ELECTRON_OZONE_PLATFORM_HINT=wayland
unset DISPLAY
unset ELECTRON_RUN_AS_NODE
runtime=${DESKTOP_CHAT_RUNTIME:-"$app_dir/../../../lib/whitecube/desktop-chat-runtime/electron"}
if [ -x "$runtime" ]; then
  exec "$runtime" --ozone-platform=wayland "$app_dir" "$@"
elif [ -x "$app_dir/node_modules/.bin/electron" ]; then
  exec "$app_dir/node_modules/.bin/electron" --ozone-platform=wayland "$app_dir" "$@"
elif command -v electron >/dev/null 2>&1; then
  exec electron --ozone-platform=wayland "$app_dir" "$@"
fi
printf '%s\n' 'Desktop Chat: Electron is not installed or is missing from PATH.' >&2
if [ -n "${WAYLAND_DISPLAY:-}" ] && command -v foot >/dev/null 2>&1; then
  foot -T 'Desktop Chat could not start' /bin/sh -c 'printf "%s\n" "Desktop Chat runtime is missing. Reinstall whitecube-desktop to restore it." "Press Enter to close."; read -r reply' &
fi
exit 127
