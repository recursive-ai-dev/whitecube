# Cube Chrootables changes

## 1.3.0 — 2026-10-31

The Whitecube workspace manager now uses the standalone Chrootables Qt desktop,
including recovery points, operation diagnostics, shared host folders and the
embedded terminal. The public Whitecube command is `cube-chrootables`.

- Administrator authentication uses `sudo` in a terminal. Passwords are entered
  directly into sudo and never passed through the graphical application.
- Privileged workflows reject nonregular exports, isolate child input and
  preserve cancellation and completion state across Cube Strap builds/imports.
- Protocol and terminal resource limits keep malformed or excessive output from
  exhausting the window; helper retirement and terminal reaping keep it responsive.
- Concurrent inventory changes and history-write failures have defined outcomes.
  Store paths and screenshot results are checked at their application boundaries.
- Cube Chrootables Studio provides coexisting DEB, RPM, Arch and host-runtime
  AppImage distributions with normalized payloads, checksums and a release manifest.

The bundled engine identifies as Chrootables 1.2.1. Existing workspace images
remain in their original store; this release does not convert image formats.
