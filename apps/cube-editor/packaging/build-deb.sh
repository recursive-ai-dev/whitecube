#!/bin/bash
# Build a standalone package without installing anything or requiring root.
set -euo pipefail
app_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
out_dir=$app_dir/packaging/out
run_tests=1
while (($#)); do
  case $1 in
    --no-tests) run_tests=0; shift ;;
    --output-directory)
      [[ $# -ge 2 && -n $2 ]] || { echo 'build-deb: --output-directory requires a path' >&2; exit 2; }
      out_dir=$2; shift 2 ;;
    -h|--help)
      echo 'Usage: packaging/build-deb.sh [--no-tests] [--output-directory PATH]'
      echo 'Build cube-editor_VERSION_all.deb. Tests run by default; nothing is installed.'
      exit 0 ;;
    *) echo "build-deb: unknown option: $1" >&2; exit 2 ;;
  esac
done
for command in python3 dpkg dpkg-deb install gzip sha256sum flock; do
  command -v "$command" >/dev/null || { echo "build-deb: missing command: $command" >&2; exit 1; }
done
[[ -x /usr/bin/python3 ]] || { echo 'build-deb: distribution Python /usr/bin/python3 is required' >&2; exit 1; }
version=$(tr -d '\n' < "$app_dir/packaging/VERSION")
dpkg --validate-version "$version"
[[ $version =~ ^[0-9][A-Za-z0-9.+~:-]*$ ]] || { echo 'build-deb: invalid version' >&2; exit 2; }
mkdir -p -- "$out_dir"
out_dir=$(cd -- "$out_dir" && pwd)
exec 9>"$out_dir/.build.lock"
flock -n 9 || { echo 'build-deb: another package build is running' >&2; exit 1; }
# Avoid changing an active desktop session: no privileged operations are used.
if [[ $run_tests == 1 ]]; then
  if command -v gtk4-broadwayd >/dev/null && [[ -f $app_dir/tools/check.py ]]; then
    /usr/bin/python3 -I "$app_dir/tools/check.py"
  else
    /usr/bin/python3 -I -m unittest discover -s "$app_dir/tests" -v
  fi
fi
stage=$(mktemp -d "$out_dir/.stage-XXXXXX")
temporary_deb=$(mktemp "$out_dir/.package-XXXXXX.deb")
trap 'rm -rf -- "$stage"; rm -f -- "$temporary_deb"' EXIT
runtime=$stage/usr/lib/cube-editor
install -d -m 0755 "$runtime" "$stage/usr/bin" "$stage/usr/share/doc/cube-editor" \
  "$stage/usr/share/applications" "$stage/usr/share/icons/hicolor/scalable/apps" \
  "$stage/usr/share/metainfo" "$stage/usr/share/mime/packages" "$stage/usr/share/man/man1" "$stage/DEBIAN"
# Only runtime modules and assets belong in the package; exclude tests, extracted
# upstream references, build output, local credentials and developer helpers.
install -m 0644 "$app_dir"/*.py "$app_dir/style.css" "$runtime/"
install -m 0644 "$app_dir/packaging/VERSION" "$runtime/VERSION"
install -m 0755 "$app_dir/packaging/cube-editor-studio" "$stage/usr/bin/"
ln -s cube-editor-studio "$stage/usr/bin/cube-editor-cli"
install -m 0644 "$app_dir/packaging/org.whitecube.CubeEditor.Standalone.desktop" "$stage/usr/share/applications/"
install -m 0644 "$app_dir/packaging/cube-editor.svg" "$stage/usr/share/icons/hicolor/scalable/apps/org.whitecube.CubeEditor.Standalone.svg"
install -m 0644 "$app_dir/packaging/org.whitecube.CubeEditor.Standalone.metainfo.xml" "$stage/usr/share/metainfo/"
install -m 0644 "$app_dir/packaging/cube-editor-project.xml" "$stage/usr/share/mime/packages/"
install -m 0644 "$app_dir/COPYING" "$app_dir/NOTICE" "$app_dir/packaging/copyright" "$stage/usr/share/doc/cube-editor/"
gzip -n -9 -c "$app_dir/README.md" > "$stage/usr/share/doc/cube-editor/README.md.gz"
gzip -n -9 -c "$app_dir/packaging/cube-editor.1" > "$stage/usr/share/man/man1/cube-editor-studio.1.gz"
ln -s cube-editor-studio.1.gz "$stage/usr/share/man/man1/cube-editor-cli.1.gz"
/usr/bin/python3 -I - "$runtime" "$version" <<'PY'
import ast
from pathlib import Path
import sys
runtime, expected = Path(sys.argv[1]), sys.argv[2]
for path in runtime.glob('*.py'):
    compile(path.read_bytes(), str(path), 'exec')
def version_node(module):
    assignments = ast.parse((runtime / module).read_text()).body
    return next(node.value for node in assignments if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == 'VERSION' for target in node.targets))
node = version_node('app.py')
if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == 'engine' and node.attr == 'VERSION':
    node = version_node('engine.py')
version = ast.literal_eval(node)
if version != expected:
    raise SystemExit(f'build-deb: app version {version} differs from package version {expected}')
PY
if command -v desktop-file-validate >/dev/null; then
  desktop-file-validate "$stage/usr/share/applications/org.whitecube.CubeEditor.Standalone.desktop"
fi
if command -v appstreamcli >/dev/null; then
  appstreamcli validate --no-net "$stage/usr/share/metainfo/org.whitecube.CubeEditor.Standalone.metainfo.xml"
fi
# Use logical file sizes with a 1 KiB allowance per directory/link. Filesystem
# allocation from du differs between tmpfs and disk, changing package bytes.
installed_size=$(/usr/bin/python3 -I - "$stage/usr" <<'PY'
from pathlib import Path
import sys
paths = [Path(sys.argv[1]), *Path(sys.argv[1]).rglob('*')]
print(sum(1 if path.is_dir() or path.is_symlink() else max(1, (path.stat().st_size + 1023) // 1024) for path in paths))
PY
)
cat > "$stage/DEBIAN/control" <<EOF
Package: cube-editor
Version: $version
Section: utils
Priority: optional
Architecture: all
Maintainer: Whitecube contributors <recursive-ai-dev@users.noreply.github.com>
Installed-Size: $installed_size
Depends: python3 (>= 3.10), python3-gi, python3-cairo, python3-gi-cairo, python3-yaml, gir1.2-gtk-4.0, gir1.2-adw-1 (>= 1.5), libadwaita-1-0 (>= 1.5), xorriso, squashfs-tools, sudo, util-linux, mount, coreutils, qterminal | gnome-terminal | konsole | xfce4-terminal | xterm | foot
Recommends: qemu-system-x86, qemu-utils, ovmf
Homepage: https://github.com/recursive-ai-dev/whitecube
Description: Desktop studio for customizing Debian and Ubuntu live images
 Create persistent ISO projects, apply package recipes and payloads, save
 restore points, recover interrupted work, export images with checksums and
 launch virtual machine tests. Includes an independent command-line API.
 The standalone launcher is cube-editor-studio and coexists with Whitecube.
EOF
# Standard desktop triggers refresh caches without running application code.
cat > "$stage/DEBIAN/triggers" <<'EOF'
activate-noawait /usr/share/applications
activate-noawait /usr/share/icons/hicolor
activate-noawait /usr/share/mime/packages
EOF
find "$stage" -type d -exec chmod 0755 {} +
find "$stage" -type f -exec chmod 0644 {} +
chmod 0755 "$stage/usr/bin/cube-editor-studio"
(cd "$stage" && find usr -type f -print0 | sort -z | xargs -0 md5sum > DEBIAN/md5sums)
chmod 0644 "$stage/DEBIAN/md5sums"
epoch=${SOURCE_DATE_EPOCH:-$(git -C "$app_dir" log -1 --format=%ct 2>/dev/null || echo 0)}
[[ $epoch =~ ^[0-9]+$ ]] || { echo 'build-deb: SOURCE_DATE_EPOCH must be an integer' >&2; exit 2; }
find "$stage" -print0 | xargs -0 touch -h -d "@$epoch"
SOURCE_DATE_EPOCH=$epoch dpkg-deb --root-owner-group --threads-max=2 -Zxz -z6 --build "$stage" "$temporary_deb" >/dev/null
dpkg-deb --info "$temporary_deb" >/dev/null
chmod 0644 "$temporary_deb"
deb=$out_dir/cube-editor_${version}_all.deb
mv -f -- "$temporary_deb" "$deb"
(cd "$out_dir" && sha256sum "$(basename -- "$deb")" > "$(basename -- "$deb").sha256")
chmod 0644 "$deb.sha256"
echo "Built $deb"
echo "Install with: sudo apt install \"$deb\""
