#!/bin/sh
# Wayland-only launcher for Whitecube Command Center. Refuses to run without a Wayland session;
# the app itself pins Electron to the Wayland ozone backend and hides $DISPLAY.
script_path=$(readlink -f -- "$0") || exit 1
app_dir=${script_path%/*}
cd -- "$app_dir" || exit 1
if [ -z "$WAYLAND_DISPLAY" ]; then
  printf '%s\n' 'Whitecube Command Center: WAYLAND_DISPLAY is not set; this app only runs on Wayland.' >&2
  exit 1
fi
if [ ! -f dist/index.html ]; then
  printf '%s\n' 'Whitecube Command Center: dist/ is missing. Run "npm run build" first.' >&2
  exit 1
fi
unset ELECTRON_RUN_AS_NODE DISPLAY
export ELECTRON_OZONE_PLATFORM_HINT=wayland
if [ -x "$app_dir/node_modules/electron/dist/electron" ]; then
  exec "$app_dir/node_modules/electron/dist/electron" --ozone-platform=wayland "$app_dir" "$@"
elif command -v electron >/dev/null 2>&1; then
  exec electron --ozone-platform=wayland "$app_dir" "$@"
fi
printf '%s\n' 'Whitecube Command Center: Electron is not installed. Run "npm run setup:electron".' >&2
exit 127
