# cube-editor

cube-editor is Whitecube’s native desktop workspace for customizing Ubuntu and
Debian live ISO images. It adapts Cubic’s extract → customize → rebuild workflow
into persistent projects, recipes, operation logs, release checksums, and virtual
machine boot tests. The extracted Cubic AppImage is kept as a local reference;
its old Python runtime and GTK wizard are not runtime dependencies.

## Run and install

From the Whitecube repository:

```sh
./bin/cube-editor
```

Build the updated desktop package and install the specific resulting file:

```sh
bash packaging/build-deb.sh
sudo apt install ./packaging/out/whitecube-desktop_<version>_amd64.deb
cube-editor
```

The package build runs the repository’s required checks. Building does not
install the package or restart the desktop. The packaged editor lives under
`/usr/share/whitecube/cube-editor`; its launcher is `/usr/bin/cube-editor`.

## Project workflow

1. Create a project from a live ISO. Inspection identifies its live filesystem,
   layered squashfs chain, volume label, and BIOS/UEFI boot records.
2. Extract the ISO into the project. The original ISO remains the input; edits
   are made to the extracted filesystem and copied ISO tree.
3. Configure a recipe: install or purge packages, run guest shell commands,
   copy payload files to absolute guest destinations, or install a locally built
   Whitecube desktop `.deb` inside the image. Open the guest terminal for manual
   configuration when needed.
4. Choose the output name, volume label, and squashfs compression, then build.
5. Verify the resulting ISO against its SHA256 sidecar. Launch a QEMU boot test
   before installing or distributing the customized image.

Projects default to `~/cubic/work`, finished ISOs to `~/cubic/complete`, and the
recent-project index to `~/.config/cube-editor/recent.json`. A project contains
`cube-editor.json`, the copied `iso/` tree, the editable `rootfs/`, and operation
logs. Layered images also keep the extracted source layers. Leave space for the
copied ISO, uncompressed layers, editable root, and exported image. Each project
allows one filesystem operation at a time.

For modern layered Ubuntu images, the editor composes the source layers using
OverlayFS, including deletion markers, before editing. Export preserves the
source’s lower layers and replaces the selected active layer with the complete
customized root plus deletion markers. This favors complete, editable projects
and can produce a larger ISO than the original.

Recognized layered Calamares installers receive a separate clean image at
`/cube-editor/install.squashfs`, outside Casper’s boot-layer discovery. Their
known root mappings are updated to `/cdrom/cube-editor/install.squashfs` so
custom packages, files, and desktop changes also reach the installed system.
This makes the source installer’s minimal and regular choices use the complete
customized root. The clean installation image contains no added boot-layer
deletion markers. Installer jobs use the root’s available initramfs tools,
remove live-session helpers from the installed target, and regenerate its
initramfs; the live ISO retains its boot tools. Original installer configurations
are backed up in the project. The additional image increases output size and
storage needs. Custom installer mappings require explicit adaptation.

Extract, customize, terminal, and build operations open a Foot terminal and use
`sudo` authentication for filesystem work. Close the guest shell normally to
finish a manual editing session. Follow the operation log if extraction or
package installation fails; an incomplete operation is not a successful build.

QEMU is optional. BIOS boot tests need `qemu-system-x86_64`; UEFI tests additionally
need OVMF firmware. KVM acceleration is used when the current account can access
`/dev/kvm`. The ISO’s original boot records are replayed during export; no claim
is made that every third-party installer or distribution supports customization.

## Developer commands

The process/project API can run without starting the desktop interface:

```sh
python3 apps/cube-editor/engine.py dependencies
python3 apps/cube-editor/engine.py inspect ~/cubic/iso/lubuntu-26.04.1-desktop-amd64.iso
python3 apps/cube-editor/engine.py create /path/to/source.iso --name 'My Cube'
python3 apps/cube-editor/engine.py plan /path/to/project extract
python3 apps/cube-editor/engine.py run /path/to/project extract
python3 apps/cube-editor/engine.py run /path/to/project customize
python3 apps/cube-editor/engine.py run /path/to/project build
python3 apps/cube-editor/engine.py verify /path/to/project
```

`inspect` reads the ISO. `plan` prints argument arrays and the worker launch
command without changing an image. `run` opens the authenticated worker in Foot.
The project JSON records package lists, custom commands, payload mappings, and
output settings; retain its schema and use absolute paths for local files.

Run the focused editor tests and then the required repository build:

```sh
python3 -m unittest discover -s apps/cube-editor/tests -v
./BUILD.sh
```

The core external tools are `xorriso`, `unsquashfs`, `mksquashfs`, `sudo`,
`unshare`, `mount`, `chroot`, and `foot`. The UI uses Python GI bindings for GTK 4
and libadwaita. Layered Calamares integration uses `python3-yaml` to preserve and
validate configuration structure. Privileged mount operations use a private
mount namespace.

## Source and licensing

`COPYING` contains the GNU GPL version 3. `NOTICE` identifies Cubic and the
workflow adaptation. `upstream/extraction.json` records the original AppImage’s
SHA256 and extraction offset; `upstream/cubic-2022.01.70/` contains the preserved
AppDir. The upstream extraction and tests are excluded from the installed
runtime. Cubic’s original artwork has a separate CC-BY-NC-SA license; cube-editor
uses fresh Whitecube cube branding.
