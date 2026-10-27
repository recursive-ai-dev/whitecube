#!/bin/bash
# Build a standalone Debian package without installing or modifying the system.
set -euo pipefail
app_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
out_dir=$app_dir/dist
run_tests=1
while (($#)); do
  case $1 in
    --no-tests) run_tests=0; shift ;;
    --output-directory)
      [[ $# -ge 2 && -n $2 ]] || { echo 'build-deb: --output-directory requires a path' >&2; exit 2; }
      out_dir=$2; shift 2 ;;
    -h|--help)
      echo 'Usage: packaging/build-deb.sh [--no-tests] [--output-directory PATH]'
      echo 'Build dist/cube-prompts_VERSION_all.deb; package checks run by default.'
      echo 'This command installs nothing and requires no root permissions.'
      exit 0 ;;
    *) echo "build-deb: unknown option: $1" >&2; exit 2 ;;
  esac
done
for command in python3 dpkg dpkg-deb install gzip sha256sum flock; do
  command -v "$command" >/dev/null || { echo "build-deb: missing command: $command" >&2; exit 1; }
done
version=$(python3 -I - "$app_dir/prompt_manager/__init__.py" <<'PY'
import ast
from pathlib import Path
import sys
module = ast.parse(Path(sys.argv[1]).read_text())
print(next(ast.literal_eval(node.value) for node in module.body
           if isinstance(node, ast.Assign) and any(isinstance(name, ast.Name)
           and name.id == '__version__' for name in node.targets)))
PY
)
dpkg --validate-version "$version"
[[ $version =~ ^[0-9][A-Za-z0-9.+~:-]*$ ]] || { echo 'build-deb: invalid version' >&2; exit 2; }
# Validate before creating output directories, so invalid inputs leave no artifact.
epoch=${SOURCE_DATE_EPOCH:-$(git -C "$app_dir" log -1 --format=%ct 2>/dev/null || echo 0)}
[[ $epoch =~ ^[0-9]+$ ]] || { echo 'build-deb: SOURCE_DATE_EPOCH must be an integer' >&2; exit 2; }
if [[ $run_tests == 1 ]]; then
  python3 -I -m unittest discover -s "$app_dir/tests" -p test_standalone_package.py -v
fi
mkdir -p -- "$out_dir"
out_dir=$(cd -- "$out_dir" && pwd)
exec 9>"$out_dir/.build.lock"
flock -n 9 || { echo 'build-deb: another package build is running' >&2; exit 1; }
stage=$(mktemp -d "$out_dir/.stage-XXXXXX")
temporary_deb=$(mktemp "$out_dir/.package-XXXXXX.deb")
trap 'rm -rf -- "$stage"; rm -f -- "$temporary_deb"' EXIT
runtime=$stage/usr/lib/cube-prompts
install -d -m 0755 "$runtime" "$stage/usr/bin" "$stage/usr/share/doc/cube-prompts" \
  "$stage/usr/share/applications" "$stage/usr/share/icons/hicolor/scalable/apps" \
  "$stage/usr/share/metainfo" "$stage/usr/share/man/man1" "$stage/DEBIAN"
install -m 0644 "$app_dir/app.py" "$app_dir/style.css" "$runtime/"
# Include only source modules: never copy credentials, bytecode, tests or caches.
while IFS= read -r -d '' module; do
  relative=${module#"$app_dir/"}
  install -D -m 0644 "$module" "$runtime/$relative"
done < <(find "$app_dir/prompt_manager" -type f -name '*.py' -print0 | sort -z)
install -m 0755 "$app_dir/packaging/cube-prompts-studio" "$stage/usr/bin/"
install -m 0644 "$app_dir/packaging/org.whitecube.CubePrompts.Standalone.desktop" "$stage/usr/share/applications/"
install -m 0644 "$app_dir/packaging/org.whitecube.CubePrompts.Standalone.svg" "$stage/usr/share/icons/hicolor/scalable/apps/"
install -m 0644 "$app_dir/packaging/org.whitecube.CubePrompts.Standalone.metainfo.xml" "$stage/usr/share/metainfo/"
install -m 0644 "$app_dir/NOTICE" "$app_dir/packaging/copyright" "$stage/usr/share/doc/cube-prompts/"
gzip -n -9 -c "$app_dir/README.md" > "$stage/usr/share/doc/cube-prompts/README.md.gz"
gzip -n -9 -c "$app_dir/packaging/cube-prompts-studio.1" > "$stage/usr/share/man/man1/cube-prompts-studio.1.gz"
python3 -I - "$runtime" <<'PY'
from pathlib import Path
import sys
for path in Path(sys.argv[1]).rglob('*.py'):
    compile(path.read_bytes(), str(path), 'exec')
PY
if command -v desktop-file-validate >/dev/null; then
  desktop-file-validate "$stage/usr/share/applications/org.whitecube.CubePrompts.Standalone.desktop"
fi
installed_size=$(du -sk --exclude=DEBIAN "$stage" | cut -f1)
cat > "$stage/DEBIAN/control" <<CONTROL
Package: cube-prompts
Version: $version
Section: utils
Priority: optional
Architecture: all
Maintainer: Whitecube contributors <recursive-ai-dev@users.noreply.github.com>
Installed-Size: $installed_size
Depends: python3 (>= 3.10), python3-gi, gir1.2-gtk-4.0 (>= 4.10), libgtk-4-1 (>= 4.10), gir1.2-adw-1 (>= 1.5), libadwaita-1-0 (>= 1.5)
Recommends: python3-keyring, gir1.2-secret-1, gnome-keyring
Suggests: git
Homepage: https://github.com/recursive-ai-dev/whitecube
Description: Local prompt library and AI comparison workbench
 Organize prompts with folders, tags, favorites and reusable variable templates.
 Save revisions, compare model responses, and export libraries in open formats.
 Supports remote providers and local Ollama models. Library editing works offline.
 The standalone launcher is cube-prompts-studio and coexists with Whitecube.
CONTROL
cat > "$stage/DEBIAN/triggers" <<'TRIGGERS'
activate-noawait /usr/share/applications
activate-noawait /usr/share/icons/hicolor
TRIGGERS
(cd "$stage" && find usr -type f -print0 | sort -z | xargs -0 md5sum > DEBIAN/md5sums)
find "$stage" -type d -exec chmod 0755 {} +
find "$stage" -print0 | xargs -0 touch -h -d "@$epoch"
SOURCE_DATE_EPOCH=$epoch dpkg-deb --root-owner-group --threads-max=2 -Zxz -z6 --build "$stage" "$temporary_deb" >/dev/null
dpkg-deb --info "$temporary_deb" >/dev/null
chmod 0644 "$temporary_deb"
deb=$out_dir/cube-prompts_${version}_all.deb
mv -f -- "$temporary_deb" "$deb"
(cd "$out_dir" && sha256sum "$(basename -- "$deb")" > "$(basename -- "$deb").sha256")
echo "Built $deb"
echo "Install with: sudo apt install \"$deb\""
