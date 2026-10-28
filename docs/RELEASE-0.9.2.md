# Whitecube Desktop 0.9.2

Whitecube 0.9.2 fixes a compositor frame-recovery bug that can make Firefox and
other applications appear frozen until switching windows. The supported binary
release is Debian 13 (Trixie), amd64.

## Display recovery

A failed display update, including a DRM page flip rejected with `Device or
resource busy`, could leave the compositor waiting for a frame event that would
never arrive. Applications waiting for their frame callback then stopped
redrawing; switching windows supplied fresh scene damage and woke the display.

The compositor now retries after 16 milliseconds, with repeated failures backing
off to a maximum interval of 250 milliseconds. A successful update cancels the
retry. Disconnecting an output removes its timer. Failed lock-screen frames are
retried without acknowledging the lock until presentation succeeds.

## Installation

Download `whitecube-desktop_0.9.2_amd64.deb` and its `.sha256` file from this release,
then run these commands in the download directory:

```sh
sha256sum --check whitecube-desktop_0.9.2_amd64.deb.sha256
sudo apt install ./whitecube-desktop_0.9.2_amd64.deb
```

Save your work, then log out and back in to load the updated compositor.
Installation does not restart the active desktop. This package includes the
desktop and application improvements from 0.9.1.

## Verification

Regression tests inject failed output commits into the real headless scene
renderer and check recovery without new input or scene damage, cancellation
after success, bounded retries during persistent failure, disconnecting an
output with a pending retry, and lock-frame recovery. The recovery regression
fails against the previous compositor implementation.

The release assets include the Debian package, checksums, build/archive manifest,
and verification reports. The release gate covers the full build/test suite,
archive integrity, session locking, live settings, clean installation, upgrade
from 0.9.1 with modified configuration, removal/purge, and installed application
smoke tests. See [maintenance](MAINTENANCE.md) for the commands.

Automated rendering checks use headless outputs. Confirmation on the affected
physical display with Firefox remains necessary; this fixes recovery after a
rejected frame, not the underlying graphics-driver rejection.
