# Testing the installed runtime

After a successful package build, run the installed runtime smoke test on the
host (Bubblewrap and the cached Debian builder are required):

```sh
python3 -I packaging/smoke-installed.py \
  --rootfs build/debian-builder/rootfs \
  --deb packaging/out/whitecube-desktop_0.8.1_amd64.deb \
  --output build/installed-smoke
```

The output directory must be absent or empty. A finished staging directory can
be supplied with `--stage packaging/out/stage` instead of `--deb`. Testing the
archive itself gives stronger evidence that the delivered payload works.

The runner copies Debian's system runtime paths and overlays the package in a
temporary root. It makes that root read-only inside Bubblewrap, creates fresh
home/runtime/cache directories, and isolates PID, device and network access. It
mounts neither the checkout nor the account's actual home and passes no inherited
library or Python search paths. It changes no host packages, services or desktop
session, and does not modify the cached builder.

It exercises installed absolute paths for session prerequisite checks, status
selftests, package/library diagnostics, real dconf profile import/export, the
headless compositor, panel, wallpaper, live preferences and Settings window.
It also renders a local page through Browser's installed workers, plays a WAV
fixture through Player's real Wayland window with null audio, and waits for an
actual Chat window buffer from the bundled Electron runtime.

Logs, PNG captures, player diagnostics and `report.json` remain in the requested
output directory. Success requires every check; failure reports the error and
retains completed checks and logs for diagnosis.

The test uses a headless Pixman output and does not validate physical display,
GPU, input or sound hardware. Extracting rootlessly cannot provide the sandbox
helper's real root ownership, so Chat receives `--no-sandbox` inside this already
isolated smoke namespace. Normal application launchers retain their standard
sandbox behavior. Debian's builder includes development dependencies, so this
test does not prove that the package's minimal dependency list is complete.
Package maintainer scripts, login-manager integration, system services and model
or API requests require separate installation testing.
