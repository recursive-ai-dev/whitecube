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

## Editing, recovery and release tools

The workbench now includes these sixteen workflow improvements:

1. **Storage preflight.** Source and Build pages offer conservative estimates.
   Workers check free space before extraction, compression and restore copies.
   Requirements on the same filesystem are added together. Estimates include
   staging space and a reserve; actual expansion and package downloads vary.
2. **Interrupted-project recovery.** Source reports active operations, interrupted
   states, staging files and guest environment backups. Recover project retains
   partial extraction and build files, restores guest DNS/service policy, and
   removes journaled temporary boot-layer deletion markers.
3. **Restore points.** Save a point explicitly or enable the default automatic
   point before recipes and manual terminals. It copies both `rootfs/` and `iso/`
   with reflinks when supported. Restore stages both copies before replacing
   current trees and rolls back promotion errors. Previous points and replaced
   trees remain under `recovery/`, so review them before removing them to reclaim
   space. Recovery artifacts on a separate output filesystem remain in its
   `.cube-editor-recovery/` folder. Some retained files need sudo to inspect.
4. **Complete operation previews.** Extraction, customization and build previews
   describe mounts, payloads, installer adaptation, metadata, temporary deletion
   markers, export promotion and cleanup in addition to the major argument arrays.
   `payload-copy` in a preview describes an internal copy operation, not a shell
   executable. Installer steps are conditional on recognized configurations.
5. **Portable recipes.** Import/export uses versioned recipe JSON, independent of
   the project directory. Local file paths are retained; import lists missing
   paths to update in Files & desktop before applying. Import replaces the recipe.
6. **Multiline Bash scripts.** The command editor preserves indentation, loops,
   functions and heredocs in a complete script. Existing command arrays retain
   their separate execution semantics until the script is edited.
7. **Package previews.** Simulate installation/purge and print required download
   URLs/sizes without applying the recipe. Results appear in Activity and
   `package-preview.txt`. Previews use existing guest apt indexes; use the image
   terminal to update those indexes when needed. Actual package availability can
   change before Apply recipe refreshes the indexes.
8. **Source integrity.** Supply a publisher SHA256 or checksum-file line, or record
   a digest without an expected value. Extraction hashes the source and checks
   any recorded digest. Recording a digest alone does not authenticate the source.
9. **Release history.** Every successful build appends its path, hash, size, recipe
   and build settings to project history. Test & release can select old releases
   for checksum verification and VM booting while their files remain available.
10. **Tool progress.** Extraction/compression percentages come from tool output.
    Activity shows elapsed time and an estimate for the current tool phase when
    percentage output is available; other steps retain stage progress.
11. **Activity controls.** Search, errors-only filtering, next-error selection,
    export and optional following are available. New visible entries append to
    the bounded display buffer. Export retains the complete worker JSONL log.
12. **Inline validation.** Invalid fields and recipe/resource settings show their
    errors near the relevant controls. Action tooltips explain unavailable
    operations. Invalid autosaves do not repeatedly open toasts or allow closing
    with unsaved changes.
13. **Payload editing.** Edit existing mappings, preview up to 200 directory
    entries, reject duplicate destinations, and choose replace, skip or fail
    when an existing file is encountered. Directories merge recursively; symlink
    destinations remain rejected for every policy.
14. **Build resources.** Choose processors, compression cache memory and gzip/zstd
    compression level. Zero selects the compressor default; xz/lz4 use their
    defaults. Memory is the squashfs cache setting, not a hard limit for the
    entire build process.
15. **Installer VM tests.** Create a sparse disposable 32 GiB qcow2 disk, boot the
    installer against it, close QEMU, then reboot the installed system. UEFI
    variables persist beside the disk. Open an existing disk from the project's
    `vm-tests/` folder to continue a test after reopening the editor. Discard
    removes the selected disk and its variables after confirmation. Live-only
    boots continue to use temporary disk changes.
16. **Recorded release checks.** Checksum outcomes are saved automatically. Record
    BIOS/UEFI live or installer outcomes and notes manually after testing. Every
    check includes the actual ISO SHA256, so replacing an ISO does not transfer
    an earlier passing result to its new contents. Launching QEMU does not mark
    a boot or installation test as passed.

Restore points, recovery and package previews use the same authenticated private
worker as other filesystem operations. A retained recovery copy is not an
off-device backup. The installer VM workflow still needs a person to exercise
the guest and assess the result; automated tests use small synthetic images and
do not certify a particular distribution or installer.

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
python3 apps/cube-editor/engine.py verify-source /path/to/project --expected '<SHA256>'
python3 apps/cube-editor/engine.py preflight /path/to/project build
python3 apps/cube-editor/engine.py recovery /path/to/project
python3 apps/cube-editor/engine.py run /path/to/project snapshot
python3 apps/cube-editor/engine.py run /path/to/project restore
python3 apps/cube-editor/engine.py run /path/to/project recover
python3 apps/cube-editor/engine.py run /path/to/project packages
python3 apps/cube-editor/engine.py export-recipe /path/to/project /path/to/recipe.json
python3 apps/cube-editor/engine.py import-recipe /path/to/project /path/to/recipe.json
python3 apps/cube-editor/engine.py releases /path/to/project
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

cube-editor is licensed under the Apache License 2.0 (`COPYING`). `NOTICE`
credits Cubic, whose workflow it follows; cube-editor contains no Cubic code.
`upstream/extraction.json` records the SHA256 and extraction offset of a Cubic
AppImage kept locally for reference. The extraction itself
(`upstream/cubic-2022.01.70/`) is neither committed nor installed and keeps
Cubic’s GPL-3.0 and CC-BY-NC-SA licenses; cube-editor uses fresh Whitecube cube
branding.
