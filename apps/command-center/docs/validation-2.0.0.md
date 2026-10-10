# 2.0.0 validation

Validated on Debian 13 amd64 with the whitecube Wayland compositor, Node 24.18.0,
and bundled Electron 44.7.0.

- `npm run verify`: TypeScript check, 34 JavaScript tests, four Python boot-image tests, and production build passed.
- `npm audit`: zero reported vulnerabilities, including development dependencies.
- Headless Wayland end-to-end suite passed against the source build and extracted Debian payload. The package suite used Chromium namespace sandboxing (`CC_TEST_SANDBOX=1`).
- Covered live metrics, real shell output and exit codes, directory tracking, cancellation, all original and new views, storage and package data, diagnostics, activity persistence, confirmation, searching, kernel reads, CSP, IPC input rejection, and absence of uncaught renderer exceptions.
- Installed-copy smoke checks passed: packaged runtime, live overview, fonts, styling, Wayland, renderer isolation, CSP, and startup behavior.
- Desktop entries validate. Debian archive permissions and the single setuid sandbox helper were inspected.
- `apt-get --simulate install` accepted the package with one new package, no removals, and no additional dependency installs required on this machine.
- The installed application files and package SHA-256 were compared with the release artifact.

## Installation result

System installation was attempted with `sudo -n apt-get install` and `pkexec`.
Sudo requires a password; polkit cannot connect to its authority service in this
session. The system dpkg database has **not** registered the package.

The tested Debian payload was installed for the current account using
`scripts/install-user.cjs`. It has a desktop launcher and commands at
`~/.local/bin/command-center` and `~/.local/bin/ubuntu-lab`, with versioned runtime
files under `~/.local/opt/ubuntu-command-center`. Its launcher uses Chromium's
namespace sandbox and disables only the setuid helper.

The `.deb` and SHA-256 checksum are in `release/`. To register it system-wide:

```sh
sudo apt install /home/woez/Projects/ubuntu-lab/release/ubuntu-command-center_2.0.0_amd64.deb
```

## Test boundaries

Systemd's system bus is unavailable in this execution environment. Its panels
show that limitation, and diagnostics marks those checks unknown. Tests did not
upgrade the operating system, alter live kernel settings, terminate user
processes, install file-manager actions, or modify boot configuration. The new
sysctl recovery behavior was tested with mocked privileged calls. Native save/open
dialogs require interactive use; backup parsing and merging have automated tests.

Screenshots are retained under `test-artifacts/` (ignored by git).
