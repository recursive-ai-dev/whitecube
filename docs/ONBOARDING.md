# Whitecube onboarding

Whitecube is a Wayland desktop with its own wlroots compositor, native panel,
settings, wallpaper engine, Wi-Fi controller and bundled application suite.
The supported binary release target is Debian 13 (Trixie), amd64. Read the
[README](../README.md), [build guide](BUILD-DEBIAN.md) and
[maintenance guide](MAINTENANCE.md) for the current workflow.
`REVIEW.md` records an older source review; its claims must be checked against
current code before becoming work items.

## Repository map

| Path | Responsibility |
|---|---|
| `src/whitecube.c` | Compositor, workspaces, scene stacking, focus, session lock, output management and input |
| `src/cube-status.c` | Layer-shell panel, launcher, quick settings, supervised helper jobs and visible action errors |
| `src/cube-settings.c` | Native settings UI and typed preference integration |
| `src/cube-ca.c` | Living wallpaper and private control socket |
| `src/native-input.h` | Shared locale-aware Compose/dead-key state |
| `bin/whitecube-session` | Private runtime/log setup, startup checks, single-session guard and safe mode |
| `bin/whitecube-suspend` | User-level lock acknowledgement followed by privileged suspend |
| `libexec/cube-settings-helper` | Validated privileged actions and staged system-file changes |
| `bin/cube-preferences`, `bin/cube-privacy` | Typed desktop preferences and user-only privacy actions |
| `net/` | Native Wi-Fi station, DHCP/DNS hooks and reversible network switch |
| `bin/`, `workspace/` | Packaged terminal/web wrappers, scripts, diagnostics and workspace services |
| `apps/` | Chat, Trainer, Editor, Browser, Player, file apps, Prompts, Strap, Chrootables and wc suite |
| `BUILD.sh`, `meson.build` | Full build/test entry point and authoritative core native definitions |
| `tools/` | Isolated Debian build, dependency preparation, release verification and legacy installers |
| `packaging/` | Install tree, Debian build, archive verifier and disposable runtime/lifecycle tests |
| `tests/` | Native/Python regression tests and headless lock/settings integrations |

`apps/command-center` is a separate product and does not ship in Whitecube.
Extracted upstream Editor artifacts and legacy OS image tooling are excluded
from the desktop package. `LICENSE` and `NOTICE` describe licensing.

## Installed product

The package uses `/usr/bin/whitecube-compositor`; the unrelated image program
owns the name `whitecube`. Other native programs are `/usr/bin/cube-status`,
`cube-settings` and `cube-ca`. Internal helpers live under
`/usr/libexec/whitecube`, private native libraries under `/usr/lib/whitecube`,
and app code/data under `/usr/share/whitecube`.

The bundled Browser is `whitecube-browser`; the bundled workspace manager CLI
is `whitecube-chrootable-manager`. These coexist with independently installed
Browser and Chrootables packages. The package provides a Whitecube login-manager
entry; it can also be started with `exec whitecube-session` from a suitable TTY.
Seat access and physical DRM/input hardware remain host prerequisites.

Defaults are conffiles under `/etc/whitecube`; `/etc/skel` files affect newly
created accounts. Each existing account retains its own
`~/.config/whitecube` settings and `~/.local/state/whitecube` diagnostics/state.
Installation starts no services, edits no existing homes, and installs no active
sudoers policy. Administrator examples ship under
`/usr/share/doc/whitecube-desktop/`.

## Configuration and desktop behavior

Typed dconf overrides take precedence over user config, then system config,
then built-in defaults. Settings imports validate all values before a single
commit; absent values remain inherited. See [settings profiles](SETTINGS-PROFILES.md).
The desktop requires no permanent session bus; the dconf writer starts its own
short-lived bus when needed.

Four workspaces retain window geometry and the most recently focused application.
Fresh windows open visibly; dialogs stay above their parent and fullscreen apps.
Tab order is independent of scene stacking. Use `data/keys.txt` for current keys
(installed at `/usr/share/whitecube/keys.txt`). Multiple-monitor window placement,
panels and mixed-scale rendering are still follow-up work.

Suspend from the native UIs first waits for screen-lock acknowledgement.
System changes use the privileged helper and require the configured sudo policy.
The network controller deliberately supports a narrower set of networks than
mainstream managers: one radio, WPA2 personal/CCMP and IPv4. Read `net/README.txt`
for capabilities and recovery; do not introduce disconnects merely because an
Internet/DNS observation fails.

## Build and validate

```sh
bash tools/build-debian.sh
```

This builds and tests inside a checkout-local Debian environment without changing
host packages or services. Current instructions and tool requirements are in
[BUILD-DEBIAN.md](BUILD-DEBIAN.md). `BUILD.sh` builds core native programs through
Meson and runs app/Python/Node checks. Optional training pipeline/hardware tests
require the explicit environment flags documented in README.

After a package build, use `tools/verify-release.py` as documented in
[MAINTENANCE.md](MAINTENANCE.md). It retains archive verification, protocol/settings
integration logs and installed runtime evidence; supply the previous `.deb` for
minimal-dependency install/upgrade/remove testing.

Use `whitecube-session --check`, `whitecube-session --safe-mode` and
`whitecube-doctor --check package --check libraries --check session` to diagnose a
real installation. Restarting the active compositor is never an installation
step: log out and back in after upgrading.
