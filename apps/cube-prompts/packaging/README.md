Build from the app directory:

```sh
./packaging/build-deb.sh
sudo apt install ./dist/cube-prompts_*_all.deb
cube-prompts-studio
```

The builder needs Bash, Python 3, `dpkg-deb`, `flock`, and standard GNU file
utilities. It runs standalone packaging tests by default. The tests build a
separate temporary archive, verify its contents, and exercise its extracted
launcher when the GTK runtime is available. `--no-tests` skips those tests;
`--output-directory PATH` chooses another artifact directory. Set
`SOURCE_DATE_EPOCH` for reproducible builds from the same source tree.

The architecture-independent package targets Debian 13+, Ubuntu 24.04+, or
compatible distributions with GTK 4.10+ and libadwaita 1.5+. Runtime dependencies
are resolved by apt; no Python package download or virtual environment is needed.
Native keyring integration is recommended. If unavailable, the existing private
credential file fallback is used.

Files are installed under `/usr/lib/cube-prompts` and the command is
`/usr/bin/cube-prompts-studio`. Its application-menu entry is **Cube Prompts
Studio**. These paths do not overlap Whitecube Desktop's launcher or application
files. Both editions share the existing XDG `cube-prompts` library and settings.
Removing the package does not delete those user files. No privileged maintainer
script runs application code, changes services, or modifies user data.

The `.deb.sha256` file next to each archive can be verified from its directory
with `sha256sum -c cube-prompts_VERSION_all.deb.sha256`.
