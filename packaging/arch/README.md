# Arch Linux ISO

`packaging/arch/build-iso.sh` builds a bootable Arch Linux image that logs in to Whitecube on the
first console. The image has no X server or XWayland; `audit-no-x.sh` fails the build if it finds one.

Whitecube and its private X-free libraries are built by `BUILD.sh` in an Arch container, packaged by
`PKGBUILD`, and installed from a local repository. archiso's baseline profile supplies boot
configuration; only `packages.extra` and `airootfs/` are kept here.

The live user `cube` has no password and passwordless sudo. Don't use the live image as an
installed system without changing that.

`cube-lab` still expects apt, so Debian lab images aren't available on this image.
