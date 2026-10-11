# Cube Strap

Builds Debian whitecubes from a GTK4 window in the Whitecube look:

- **Chroot workspace**: deb-strap exports a rootfs tarball (no kernel or bootloader), and
  `chrootable-manager --import` turns it into a workspace. It then shows in Chrootables, and
  the Shell button opens it in a terminal.
- **Virtual machine**: deb-strap builds a raw UEFI disk (EFI + ext4 root, GRUB, the cloud kernel).
  `whitecube import` adds it to the image store, and `whitecube run NAME` boots it. The Server
  recipe forwards host port 2222 to the VM's SSH.

Recipes: Minimal, Everyday tools and Developer for both kinds. Server and Desktop (XFCE) are VM-only.

## How a build runs

1. The window writes `cube.conf` (mode 0600; it holds the cube user's password) into
   `~/.cache/cube-strap/build-*`.
2. `sudo -n cube-strap-build` runs the bundled `deb-strap.sh` unattended. It bootstraps under
   `/var/lib/cube-strap/build-*`, because debootstrap needs a filesystem that allows device nodes.
   It always deletes the config, unmounts anything left behind, removes that directory, and hands
   the tarball or image back to you.
3. The window imports the result (chroot: through sudo; VM: as you) and removes the build directory.
   Logs are kept in `~/.local/state/cube-strap/logs/`.

The password for sudo is asked once, through the Chrootables backend (`sudo -S -v`), and is never stored.

## deb-strap

`deb-strap.sh` is deb-strap 1.2.0 from github.com/recursive-ai-dev/deb-strap, with these changes:

- tar/rootfs mode gives the disk-layout variables default values (it used to die with `ENCRYPT_ROOT: unbound variable`);
- the tarball is packed after the chroot's /dev, /proc, /sys, /run and /tmp are unmounted, with `--one-file-system`;
- `kernel = 7` / `none` skips the kernel, firmware, microcode and initramfs, for chroot rootfs.

## Tests

    python3 -I -m unittest discover -s apps/cube-strap/tests -v
    python3 -I tests/test-cube-strap-ui.py        # needs ./BUILD.sh's build/whitecube
