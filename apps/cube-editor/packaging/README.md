# Standalone Debian package

Build from the Cube Editor directory:

```sh
./packaging/build-deb.sh
sudo apt install ./packaging/out/cube-editor_0.5.0_all.deb
cube-editor-studio
```

The rootless build runs the editor's full focused test suite on a private GTK
display when `gtk4-broadwayd` is available (`libgtk-4-bin`), validates desktop
metadata when the validator is installed, checks Python syntax and version
consistency, then creates a package and SHA256 sidecar. It never installs or
starts the application. Verify the result with:

```sh
cd packaging/out
sha256sum --check cube-editor_0.5.0_all.deb.sha256
dpkg-deb --info cube-editor_0.5.0_all.deb
```

For another output location, use `--output-directory PATH`. Use `--no-tests`
only when the focused suite has already run. Set `SOURCE_DATE_EPOCH` to control
archive timestamps; by default the most recent Git commit's timestamp is used.
Identical sources and the same timestamp produce the same package bytes.

The package uses distribution Python, GTK 4, libadwaita 1.5 or newer, YAML and
ISO utilities. A supported terminal (QTerminal, GNOME Terminal, Konsole, Xfce
Terminal, xterm or Foot) is required. QEMU and
OVMF are recommended for optional VM tests. This is an `Architecture: all`
package; the image formats and VM firmware an individual host can use still
depend on the installed tools. Install using APT so dependencies are resolved.

The standalone application appears as **Cube Editor Studio** in the application
menu. Its commands are:

```sh
cube-editor-studio --version
cube-editor-studio /path/to/project
cube-editor-cli dependencies
cube-editor-cli --help
cube-editor-studio --cli dependencies
```

From a checkout, `./packaging/cube-editor-studio` provides the same GUI and
`--cli` behavior. Headless version and CLI commands do not import GTK.

Runtime modules are installed under `/usr/lib/cube-editor`, with documentation
under `/usr/share/doc/cube-editor`. The package has its own desktop file and
icon and does not replace the `cube-editor` launcher owned by
`whitecube-desktop`. Both packages can coexist; run `cube-editor-studio` to
select this standalone version. Uninstall with `sudo apt remove cube-editor`.
Projects, images and user settings are retained.

The package excludes test fixtures, build output, upstream AppImage references
and the Whitecube compositor. Source license and notices are included.
