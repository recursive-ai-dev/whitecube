# Cube Chrootables source

The Qt desktop, embedded terminal, validated backend, stdio helper, demo data,
manager and regression tests are copied from Chrootables 1.2.1, revision
`8c0a816`, in https://github.com/recursive-ai-dev/chrootables.
The complete Apache-2.0 license is included in `LICENSE`.

Cube Chrootables has its own application version, currently 1.3.0; the engine
continues to report its upstream version 1.2.1. The desktop and engine share the
existing capability schema and image-store format.

Whitecube changes the application name to **Cube Chrootables**, exposes
`cube-chrootables`, uses the `cube-chrootables` desktop identity and
stores desktop preferences under `whitecube/cube-chrootables`. Administrator
access uses a terminal running `sudo`, with a private local connection to the
helper, instead of a graphical polkit prompt. External sessions prefer
Whitecube Terminal. The bundled manager stays private to Whitecube.

The 1.3.0 audit additionally changes protocol/resource limits, helper and terminal
lifecycle handling, inventory races, export source validation, store boundaries,
Cube Strap privileged workflows and distribution tooling. `TODO.md` and the
regression tests record these changes and their verification.

The top-level `backend.py` and `broker.py` are the former Whitecube adapter,
retained for Cube Strap. The replacement desktop uses the modules inside
`chrootables/`. The GTK Chrootables window and stylesheet have been removed.
