# Whitecube Command Center

A Whitecube Wayland desktop command center for Ubuntu and Debian. Version 2 adds storage
inspection, installed-package search, system diagnostics, command activity, and
portable library backups to the existing monitoring and shell tools.

Source lives at `~/whitecube/command-center`; it retains its own Git repository.
See [IMPROVEMENTS.txt](IMPROVEMENTS.txt) for 48 proposed improvements with priorities
and completion criteria. These are a backlog, not implemented features.

The 2.0.1 rebrand preserves application data, storage keys, and JSON backup format.
The Debian package replaces the former `ubuntu-command-center` package, keeping
its command aliases. The user installer hides the old desktop-menu entry while
leaving the old system package installed until root authentication is available.

## Install the Debian package

The package bundles Electron; Node.js and npm are **not** required on the target.
It installs the application under `/opt/whitecube-command-center`, adds a desktop
entry, and provides `whitecube-command-center`, plus the `command-center` and
`ubuntu-lab` compatibility commands.

```sh
sudo apt install ./release/whitecube-command-center_2.0.1_amd64.deb
whitecube-command-center
whitecube-command-center --version
```

Run the desktop app as your normal user in a Wayland session. Do not run it with
sudo. The package retains the existing Electron user-data directory (`~/.config/Command Center`), so upgrades
preserve commands and preferences. Removing the package with
`sudo apt remove whitecube-command-center` leaves your user data in place.

When root authentication is unavailable, install the same package payload for the
current account with `node scripts/install-user.cjs`. It creates
`~/.local/bin/whitecube-command-center`, `command-center` and `ubuntu-lab` aliases, and a desktop menu entry.
Versions are kept under `~/.local/opt/whitecube-command-center/versions` with an atomic
`current` link. This is a user installation, **not** a registration in the system
dpkg database. It uses Chromium's namespace sandbox when user namespaces work. Otherwise it
requires a matching root-owned setuid sandbox helper from an existing system
installation, checks its owner, permissions, and Electron version, and uses it
without disabling the sandbox. It never uses `--no-sandbox`.

## Build from source

Requires Linux, Node.js 24+, npm, and `dpkg-deb` for packaging. The target
architecture follows the build machine (`amd64` or `arm64`).

```sh
npm ci
npm run setup:electron
npm run verify
npm run package:deb
npm run install:deb
```

`setup:electron` explicitly downloads the pinned Electron runtime and checks its
upstream checksum. The packaging script copies only runtime files, compiled UI,
icons, and main-process modules. Source tests, development dependencies, the
legacy daemon, and the boot theme are not installed. A `.sha256` file is written
beside the `.deb` in `release/`.

Development: `npm run dev`, then in another terminal:

```sh
WHITECUBE_COMMAND_CENTER_DEV_URL=http://127.0.0.1:3000 ./whitecube-command-center.sh
```

Only localhost HTTP development servers are accepted. To run the compiled source
checkout, use `npm start`.

## Views

| View | What it does |
| --- | --- |
| Overview | Live CPU, memory, swap, network rates, temperatures, battery, disk usage, and quick navigation |
| Processes | Search and sort processes; signal, pause, and terminate with force-kill confirmation |
| Storage | Filesystem space, available bytes, inode usage, block-device hierarchy, and mount points |
| Network | Interfaces, addresses, Wi-Fi signal, gateway and external probes, listening sockets |
| Services | System/user units, filtering, and confirmed start/stop/restart/enable/disable |
| Journal | Priority, unit, and scope filters; live/pause, text search, refresh, and text export |
| Updates | Local apt index status, security updates, and confirmed refresh/upgrade commands |
| Installed Packages | Search names/descriptions, sort by installed size, and browse paginated results |
| Diagnostics | Failed units, filesystem and inode pressure, dpkg consistency, reboot marker, and system state |
| Command Activity | Last 200 commands, directories, timestamps, durations, results, and confirmed replay |
| Settings & Backups | JSON library export, validated import preview/merge, startup and online preferences |
| Display Session | Wayland sockets, connected outputs, and compositor information |
| Kernel Settings | Live sysctl editing, optional persistence, and recovery backups |
| Command Library | Saved commands, working directories, confirmations, editing, and search |
| Shortcut Groups | In-app keyboard groups linked to saved commands |
| File Manager Menus | Saved action definitions; exportable with the library, not installed into Nautilus |

## Execution, privacy, and recovery

Commands run in a real, noninteractive `bash`, with streamed stdout/stderr and
working-directory tracking. Interactive programs requiring a TTY are not
supported. One command runs at a time. Ctrl+C stops its process group; output is
capped at 4 MiB so noisy commands cannot overwhelm the UI. Use file redirection for
large output. Commands are real operations on your account and machine.

Privileged operations use noninteractive sudo and require an existing sudo grant;
passwords are never collected by this app. Failed authorization reports the
command to run in an external terminal. Service actions, privileged commands,
imported commands, and activity replay require confirmation. Kernel recovery data
is saved **before** writes because a failed multi-key change may partially apply.
Restoring a backup restores live values only; it does not revert persistent files.

History and terminal logs can contain sensitive command text. Activity stores only
command metadata, not stdout/stderr, and retains 200 records. Clear history from
the Activity view; clear output separately in the terminal. Library backups omit
terminal logs and command history. Import merges without replacing existing items
and forces confirmation on imported commands. Diagnostic exports include host,
user, home path, OS, service names, and mounts; review before sharing.

Online wallpaper and IP-based weather are off by default. Enable them in Settings.
Wallpapers use Picsum; weather uses ipwho.is and Open-Meteo. A manual new-wallpaper
palette action also fetches Picsum. Routine inspection uses local system tools.
Diagnostic checks marked **unknown** were unavailable and should not be interpreted
as healthy. In particular, systemd views require a working system or user bus.

## Wayland and desktop integration

The launcher and main process require a usable Wayland session. Electron uses the
Wayland ozone backend, hides `DISPLAY`, and passes Wayland-only toolkit settings to
child processes. The Debian package installs Chromium's standard root-owned
sandbox helper. The normal launcher does not disable Chromium sandboxing.

The whitecube palette and canvas are preserved. See `boot/README.md` for the
optional Plymouth theme and session boot chain; package installation does not
modify your bootloader, boot theme, session, or system services.

## Verification

```sh
npm run verify             # type check, unit tests, Python boot tests, production build
npm run e2e                # isolated headless Wayland session with a scratch HOME
npm audit                  # dependency advisories
```

The end-to-end test needs `~/whitecube/build/whitecube` (or `CC_COMPOSITOR`) and
Node 24+. Screenshots can be retained with `scripts/e2e.sh test-artifacts/e2e`.
`CC_APP_LAUNCHER` selects an extracted/installed package launcher for the same
suite. The test compositor never touches the live desktop. It deliberately uses
`--no-sandbox` only in its isolated test profile, because user namespaces may be
restricted in build environments.
Set `CC_TEST_SANDBOX=1` to test with the namespace sandbox enabled, or
`CC_TEST_SANDBOX=system` to test with the installed root-owned helper.
Use `CC_APP_LAUNCHER=~/.local/bin/whitecube-command-center` for the user-installed app.

Keyboard shortcuts: Ctrl+K opens the command palette; Alt+C opens Core Actions;
Ctrl+C stops a terminal command; Ctrl+L clears output; Escape dismisses startup
checks or a confirmation.

## Legacy components

`daemon/` (Go D-Bus helper) and `relay/` belong to the earlier sysctl architecture.
The desktop app uses Electron IPC instead. These components remain in the source
checkout but are not started or included in the package.
