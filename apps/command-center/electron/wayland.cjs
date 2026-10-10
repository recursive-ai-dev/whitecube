'use strict';
const fs = require('node:fs');
const path = require('node:path');

// Returns null when a Wayland session is usable, otherwise a human-readable reason.
function waylandProblem(env = process.env, exists = fs.existsSync) {
  const display = env.WAYLAND_DISPLAY;
  if (!display) return 'WAYLAND_DISPLAY is not set. Whitecube Command Center only runs inside a Wayland session.';
  const sock = path.isAbsolute(display) ? display : path.join(env.XDG_RUNTIME_DIR || '', display);
  if (!exists(sock)) return `Wayland socket ${sock} does not exist.`;
  if (env.XDG_SESSION_TYPE && env.XDG_SESSION_TYPE !== 'wayland') return `XDG_SESSION_TYPE is "${env.XDG_SESSION_TYPE}", expected "wayland".`;
  return null;
}

// Must run before app.whenReady(). Forces ozone to Wayland (no X11 fallback) and hides $DISPLAY so
// that neither Chromium nor child GUI apps started from here can quietly fall back to Xwayland.
function enforce(app, env = process.env) {
  app.commandLine.appendSwitch('ozone-platform', 'wayland');
  app.commandLine.appendSwitch('enable-features', 'UseOzonePlatform,WaylandWindowDecorations');
  app.commandLine.appendSwitch('disable-features', 'X11');
  delete env.DISPLAY;
  env.GDK_BACKEND = 'wayland';
  env.QT_QPA_PLATFORM = 'wayland';
  env.SDL_VIDEODRIVER = 'wayland';
  env.MOZ_ENABLE_WAYLAND = '1';
  env.ELECTRON_OZONE_PLATFORM_HINT = 'wayland';
}

module.exports = { waylandProblem, enforce };
