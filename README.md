# Whitecube

A pure-Wayland tabbed "cube" desktop: compositor, top bar, settings, living-cube wallpaper, a native Wi-Fi
controller, a workspace suite and a few small apps. No X server or D-Bus session required. Installed system-wide as a Debian package;
each account keeps only its own settings in `~/.config/whitecube` and the usual dotfiles.

## Layout

| Path | What |
|---|---|
| `src/` | C sources: compositor `whitecube.c`, bar `cube-status.c`, `cube-settings.c`, `cube-ca.c`, `tab-panel.h`; `src/protocols/`, `src/cube-audio/` |
| `bin/` | commands: session, terminal, web, doctor, scripts, lab, privacy, preferences ... |
| `libexec/` | root helper `cube-settings-helper` |
| `net/` | native Wi-Fi/DHCP controller `cube-net`, reversible `net-off`/`net-on`, units, tests |
| `data/` | config example, key list, schemas, wallpapers, `.desktop` entries, sudoers examples, skel dotfiles |
| `workspace/` | Python workspace suite (served by `whitecube-web`) |
| `apps/` | `desktop-chat`, `cube-apps`, `cube-editor`, `cube-train`, `chrootables` (packaged); `command-center` (own product, not packaged) |
| `boot/` | GRUB / Plymouth / tty1 black cube theme |
| `packaging/` | `.deb` build: `install-tree.sh`, `build-deb.sh`, `check-tree.py`, `debian/` |
| `tools/` | `whitecube-migrate`, dev installers, hooks; `tools/os-build/` legacy ISO tooling (not packaged) |
| `tests/` | every C and Python test; `docs/` documentation |

## Build and test

    ./BUILD.sh        # compiles everything and runs every test; must stay green

`/tmp` may be mounted noexec: `export TMPDIR=$HOME/.cache/wc-tmp` first.

## Package

    bash packaging/build-deb.sh            # -> packaging/out/whitecube-desktop_<version>_amd64.deb
    sudo apt install ./packaging/out/whitecube-desktop_*.deb

Files land in `/usr/bin` (the compositor is `whitecube-compositor`, because the separate `whitecube` image
tool owns `/usr/bin/whitecube`), `/usr/libexec/whitecube`, `/usr/share/whitecube`, `/usr/lib/whitecube`
(bundled X-free wlroots and cairo), `/etc/whitecube` (conffiles) and `/etc/skel`. The package starts no
service and edits no home directory. The sudoers rule is **not** installed; examples are in
`/usr/share/doc/whitecube-desktop/`.

## Network switch

`sudo net-off` blocks IPv4/IPv6 on every interface while retaining localhost for local apps.
`sudo net-on` removes that block and clears its saved boot setting. Both commands preserve the existing
firewall, native Wi-Fi controller and credentials. Preview with `net-off --dry-run`; inspect actual
kernel state with `sudo net-off --status`. Use `sudo net-off --persist` to restore the block at boot,
or `--no-loopback` to also block localhost. Installation leaves the current connection running.
Defaults in `/etc/whitecube/net-switch.conf` describe this machine's `cube-net`/`wlan0` setup.
See [net/NET-SWITCH.txt](net/NET-SWITCH.txt) for recovery, scope and persistence details.

## Moving a machine from the old per-user install

    sudo whitecube-migrate                 # dry run: lists every step
    sudo whitecube-migrate --apply         # [--user NAME] [--restart-network]
    sudo whitecube-migrate --rollback /var/backups/whitecube-migrate/<stamp>

It backs up everything it moves, keeps unrelated files, and rollback restores the exact tree. Then log out and
back in. `whitecube-doctor` warns while older copies still shadow the package.

New session entry: choose **Whitecube** in a login manager, or `exec whitecube-session` from tty1.

`cube-editor` opens the native Linux ISO studio: persistent projects, layered live filesystem extraction,
package recipes, file overlays, Whitecube desktop injection, boot-preserving export, checksums and QEMU
testing. See [the editor guide](apps/cube-editor/README.md). The original extracted Cubic AppImage is
retained locally under `apps/cube-editor/upstream/` and excluded from the Debian package.

The panel groups windows under one **N tabs** button (Alt+G opens its list), and **Chat** sits beside networking.
New windows open visibly on the active workspace, keeping their saved size and position.
Previously hidden windows and placements from other workspaces cannot hide a fresh launch.
Desktop Chat includes a private Electron runtime, its sandbox helper and required library dependencies.
**Whitecube Trainer** (`cube-train`) is a native GTK4 app for local 1M, 2M, 5M and 10M models.
It selects datasets, saves named runs in `~/train-out`, monitors progress/loss/temperature,
supports Stop/Resume, previews checkpoints, and opens them directly in Chat.
Optional timed cooling breaks, lower training priority, and CPU/GPU temperature
limits help manage heat. Runs or individual checkpoints can be moved to Trash
after confirmation; restoring Latest restores the ability to resume.
The complete model pipeline lives in `apps/cube-train/pipeline` and ships with the deb.
`whitecube-train --5m` uses the same pipeline from the command line. Training uses the
configured PyTorch Python environment; system GTK remains independent of that environment.
See `docs/TRAINER-IMPROVEMENTS.txt` for the twenty improvements and shortcuts.
The **Talk to** picker discovers `~/train-out/*/ckpt_*.pt`, including Best and Latest.
Choose a checkpoint to start a new chat; **Rename** saves its display name without changing training files.
Local inference starts automatically on CPU using Trainer's configured environment (or the
Python environment in `~/.local/bin/tg-serve`) and the bundled model pipeline.
Local checkpoints need no API key; API settings are preserved.
These byte models use a small context and continue prose. The picker shows the context size from training metadata.
**Refresh** finds new checkpoints. A checkpoint replaced by training is reloaded on the next message.
Advanced overrides: `WHITECUBE_TRAIN_OUT`, `WHITECUBE_TENSEGRITY_DIR`, and `WHITECUBE_MODEL_PYTHON`.
Local server diagnostics are in `~/.local/state/whitecube/local-model-server.log`.
For source builds, stage Electron's `dist/` as `build/desktop-chat-runtime` or set `ELECTRON_DIST`;
set `NODE` to a Node executable when it is outside PATH. See `docs/PANEL-CHAT-IMPROVEMENTS.txt` for the
22 changes and validation. Log out and back in after installing to activate compositor changes.

`chrootables` manages Debian chroot workspaces: debootstrap images with profiles, persistent or disposable
sessions, snapshots, clones, exports and imports. The window runs as you and reaches root through sudo (a
password typed into it goes to `sudo -S -v` once and is never stored); shells open in `whitecube-terminal`.
The same engine is the `chrootable-manager` command, with its own menu and JSON output. See
[the app notes](apps/chrootables/README.md).

The `wc-*` command suite (`apps/wc-suite`; start with `wc-help`) is installed as `wc-*` commands in `/usr/bin`,
with its module in `/usr/lib/wc-suite` and completions for bash, zsh and fish. `python3 apps/wc-suite/install.py`
still installs a per-user copy into `~/.local`, which takes precedence on PATH.

## License

Apache License 2.0; see [LICENSE](LICENSE). [NOTICE](NOTICE) lists the third-party files that keep their own
licenses (xterm.js, the photographic wallpapers) and the libraries the package bundles.
