#!/bin/sh
# Source and packaged launcher. Keep app arguments literal and runtime private.
set -eu
script_path=$(readlink -f -- "$0") || exit 1
app_dir=${script_path%/*}
cd -- "$app_dir" || exit 1
case ${1:-} in
  -h|--help)
    cat <<'HELP'
Usage: whitecube-chat [OPTION]
Open the Desktop Chat workspace. Repeated launches control the running app.

  --show                     Show the window
  --hide                     Start or leave the window hidden
  --toggle                   Toggle the window
  --settings, --configure     Open settings
  --checkpoint RUN/FILE.pt    Select a trained local checkpoint
  --quit                     Quit the running app
  --version                  Print application version
  --help                     Show this help

Configure an OpenAI-compatible endpoint in Settings. A localhost server needs
no key. Trained checkpoints additionally need the Whitecube training runtime.

DESKTOP_CHAT_RUNTIME overrides the Electron executable.
DESKTOP_CHAT_OZONE_PLATFORM selects auto, wayland or x11 (normally automatic).
HELP
    exit 0 ;;
  --version)
    if [ -f "$app_dir/VERSION" ]; then
      version=$(cat "$app_dir/VERSION")
    else
      version=$(sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' "$app_dir/package.json" | head -n 1)
    fi
    printf 'Desktop Chat %s\n' "$version"
    exit 0 ;;
esac
unset ELECTRON_RUN_AS_NODE
platform=${DESKTOP_CHAT_OZONE_PLATFORM:-${ELECTRON_OZONE_PLATFORM_HINT:-}}
if [ -z "$platform" ]; then
  if [ -n "${WAYLAND_DISPLAY:-}" ] || [ "${XDG_SESSION_TYPE:-}" = wayland ]; then
    platform=wayland
  elif [ -n "${DISPLAY:-}" ]; then
    platform=x11
  else
    platform=auto
  fi
fi
case $platform in auto|wayland|x11) ;; *) printf '%s\n' 'Desktop Chat: platform must be auto, wayland or x11.' >&2; exit 2 ;; esac
export ELECTRON_OZONE_PLATFORM_HINT=$platform
# Standalone Debian layout, existing Whitecube layout, source checkout, PATH.
if [ -n "${DESKTOP_CHAT_RUNTIME:-}" ]; then
  runtime=$DESKTOP_CHAT_RUNTIME
  if [ ! -x "$runtime" ]; then
    printf '%s\n' 'Desktop Chat: DESKTOP_CHAT_RUNTIME is not executable.' >&2
    exit 127
  fi
elif [ -x "$app_dir/../runtime/electron" ]; then
  runtime=$app_dir/../runtime/electron
elif [ -x "$app_dir/../../../lib/whitecube/desktop-chat-runtime/electron" ]; then
  runtime=$app_dir/../../../lib/whitecube/desktop-chat-runtime/electron
elif [ -x "$app_dir/node_modules/.bin/electron" ]; then
  runtime=$app_dir/node_modules/.bin/electron
elif command -v electron >/dev/null 2>&1; then
  runtime=electron
else
  printf '%s\n' 'Desktop Chat: Electron is missing. Reinstall whitecube-desktop-chat, or run npm ci in the source checkout.' >&2
  exit 127
fi
exec "$runtime" "--ozone-platform=$platform" "$app_dir" "$@"
