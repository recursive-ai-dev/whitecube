#!/bin/bash
# build-deb.sh [--no-build] : build packaging/out/whitecube-desktop_<version>_amd64.deb
#
#   ./BUILD.sh                     (compile + every test)   <- run by this script unless --no-build
#   packaging/install-tree.sh      (lay the files out)
#   bundle X-free wlroots, cairo and display-info into /usr/lib/whitecube; derive Depends
#   packaging/check-tree.py        (every path the package mentions must exist in it)
#   dpkg-deb --root-owner-group    (no root; fakeroot is used for the unpack smoke test)
#
# It builds a file and nothing else: it installs nothing, touches no service, needs no sudo.
# Install the specific package path printed by this build with sudo apt install.
set -euo pipefail

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$root"
build=1
for a in "$@"; do
  case $a in
    --no-build) build=0 ;;
    -h|--help) sed -n '2,13p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "build-deb.sh: unknown option $a" >&2; exit 2 ;;
  esac
done

for command in dpkg dpkg-deb dpkg-shlibdeps dpkg-query flock ldd strip objcopy patchelf fakeroot python3; do
  command -v "$command" >/dev/null || { echo "build-deb.sh: required tool is missing: $command" >&2; exit 1; }
done
# Stable archive timestamps; callers can select an explicit source release epoch.
export SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH:-$(git log -1 --format=%ct 2>/dev/null || date +%s)}
[[ $SOURCE_DATE_EPOCH =~ ^[0-9]+$ ]] || { echo 'build-deb.sh: SOURCE_DATE_EPOCH must be a nonnegative integer' >&2; exit 2; }
export LC_ALL=C TZ=UTC

version=$(tr -d '[:space:]' < packaging/VERSION)
dpkg --validate-version "$version" >/dev/null
[[ $version =~ ^[0-9][A-Za-z0-9.+:~-]*$ ]] || { echo 'build-deb.sh: invalid package version' >&2; exit 2; }
[[ $(dpkg --print-architecture) == amd64 ]] || { echo 'build-deb.sh: this library bundle requires amd64' >&2; exit 2; }
out=packaging/out
mkdir -p "$out"
exec 9>"$out/.build.lock"
flock -n 9 || { echo 'build-deb.sh: another package build is running' >&2; exit 1; }
stage=$(mktemp -d "$out/.stage-${version}-XXXXXX")
trap 'rm -rf -- "$stage"' EXIT
deb=$out/whitecube-desktop_${version}_amd64.deb

if [[ $build = 1 ]]; then ./BUILD.sh > "$out.build.log" 2>&1 || { tail -30 "$out.build.log" >&2; echo "build-deb.sh: BUILD.sh failed (log: $out.build.log)" >&2; exit 1; }; fi
bash packaging/install-tree.sh "$stage"
# Source builds may embed checkout-local library directories to run their tests.
# Installed core programs must load only the system-wide private library root.
for native in whitecube-compositor cube-status cube-settings cube-ca; do
  patchelf --set-rpath /usr/lib/whitecube "$stage/usr/bin/$native"
done

# --- bundle the X-free libraries the binaries were linked against
priv=$stage/usr/lib/whitecube
install -d -m 0755 "$priv"
priv=$(realpath -- "$priv")
bundled=()
wlroots=$(ldd build/whitecube | awk '/libwlroots/ {print $3}')
[[ -f $wlroots ]] || { echo "build-deb.sh: cannot resolve the wlroots used by build/whitecube" >&2; exit 1; }
bundle_library() {
  local source=$1 name real
  name=$(basename "$source")
  real=$(readlink -f "$source")
  install -m 0644 "$real" "$priv/$(basename "$real")"
  [[ $name == "$(basename "$real")" ]] || ln -s "$(basename "$real")" "$priv/$name"
  bundled+=("$name")
}
# A previous desktop installation can provide the exact library the new build
# resolves. Include that file too: the new package must own its complete runtime.
bundle_library "$wlroots"
# Debian 13 wlroots uses display-info.so.2; newer Ubuntu distributions carry
# .so.3. Ship this small private ABI alongside wlroots rather than requiring a
# removed distribution package on the installation host.
display_info=$(ldd "$wlroots" | awk '$1 ~ /^libdisplay-info\.so\./ {print $3}')
[[ -f $display_info ]] || { echo 'build-deb.sh: cannot resolve wlroots display-info library' >&2; exit 1; }
bundle_library "$display_info"
display_info_package=$(dpkg-query -S "$(realpath "$display_info")" | head -1 | cut -d: -f1)
install -m 0644 "/usr/share/doc/$display_info_package/copyright" \
  "$stage/usr/share/doc/whitecube-desktop/libdisplay-info-copyright"
cairo_stage=build-cairo/stage/usr/local/lib/x86_64-linux-gnu
cairo=$(ldd build/cube-settings | awk '$1 == "libcairo.so.2" {print $3}')
if [[ -f $cairo ]] && ! ldd "$cairo" | grep -qiE 'libX11|libxcb'; then
  bundle_library "$cairo"
elif [[ -f $cairo_stage/libcairo.so.2 ]] && ! ldd "$cairo_stage/libcairo.so.2" | grep -qiE 'libX11|libxcb'; then
  bundle_library "$cairo_stage/libcairo.so.2"
else
  echo "build-deb.sh: no X-free cairo available; run tools/build-xfree-cairo.sh first" >&2; exit 1
fi
echo "bundled libraries: ${bundled[*]:-none}"
# DT_RUNPATH on an executable is not inherited by its transitive dependencies.
# Give each private library its own sibling search path before stripping it.
while IFS= read -r -d '' library; do
  patchelf --set-rpath '$ORIGIN' "$library"
done < <(find "$priv" -maxdepth 1 -type f -name '*.so*' -print0)
display_resolution=$(env -u LD_LIBRARY_PATH ldd "$priv/$(basename "$(readlink -f "$wlroots")")" | awk '$1 ~ /^libdisplay-info\.so\./ {print $3}')
[[ $display_resolution == "$priv/"* ]] || { echo 'build-deb.sh: bundled wlroots cannot resolve its private display-info library' >&2; exit 1; }

# --- strip (also removes the build directory recorded in the debug info)
strip --strip-unneeded "$stage"/usr/bin/{whitecube-compositor,cube-status,cube-settings,cube-ca,cube-player} \
  "$stage"/usr/libexec/whitecube/cube-browser/cube-{browser,renderd,netd} "$priv"/*.so* 2>/dev/null || true
python3 -I packaging/check-libraries.py "$stage"

# --- Depends: whatever package owns each shared library the programs still need
declare -A pkgs=()
library_packages=$(python3 -I packaging/library-dependencies.py "$stage")
while read -r package; do [[ -z $package ]] || pkgs[$package]=1; done <<< "$library_packages"
# the point of bundling cairo and wlroots: nothing the programs load may reach X11 any more
if [[ ${bundled[*]:-} == *libcairo* ]] && printf '%s\n' "${!pkgs[@]}" | grep -qE '^(libx11-6|libxcb)'; then
  echo "build-deb.sh: X11 libraries are still reachable even with the bundled cairo: ${!pkgs[*]}" >&2; exit 1
fi
# Chromium ships with its distribution GTK/X compatibility libraries even
# though Chat is launched with Ozone Wayland. Native desktop binaries stay X-free.
chat_packages=$(python3 -I packaging/library-dependencies.py "$stage" --chat-runtime)
while read -r package; do [[ -z $package ]] || pkgs[$package]=1; done <<< "$chat_packages"
runtime=(python3-numpy apt less procps systemd foot fuzzel swaylock swaybg dbus dconf-cli python3 python3-gi python3-cairo python3-gi-cairo python3-yaml python3-requests python3-pikepdf
  gir1.2-gtk-4.0 gir1.2-adw-1 xorriso squashfs-tools sudo util-linux mount coreutils debootstrap e2fsprogs
  gdisk parted dosfstools xz-utils qt6-wayland
  debian-archive-keyring ca-certificates dpkg findutils gpgv gzip hostname mawk tar wget whiptail
  dhcpcd-base iproute2 nftables seatd
  libmpv2 libwayland-egl1)   # cube-player: libmpv is not bundled (it reaches X11 and gets security updates from Debian)
for p in "${runtime[@]}"; do pkgs[$p]=1; done
# Cube Chrootables uses the distribution Qt runtime in both the window and
# terminal pane. Qt's native Wayland plugin is needed by the Whitecube session.
pkgs['python3-pyside6.qtcore (>= 6.8)']=1
pkgs['python3-pyside6.qtgui (>= 6.8)']=1
pkgs['python3-pyside6.qtwidgets (>= 6.8)']=1
# Debian13 uses the private CP313 decoder; newer Python distros can provide
# their own PyArrow without requiring an unavailable older interpreter.
pkgs['python3.13 | python3-pyarrow']=1
# Cube Editor uses Adw.Dialog (introduced in libadwaita 1.5). Constrain both
# the typelib and its runtime, replacing any unconstrained scanner entries.
unset 'pkgs[gir1.2-adw-1]' 'pkgs[libadwaita-1-0]'
pkgs['gir1.2-adw-1 (>= 1.5)']=1
pkgs['libadwaita-1-0 (>= 1.5)']=1
# Record ABI minima for the actual native programs, bundled libraries and Chat
# runtime. Ownership alone does not prevent installation on an older libc.
abi_dependencies=$(python3 -I packaging/dependency-versions.py "$stage" --exclude-package "$display_info_package")
while IFS= read -r dependency; do
  [[ -n $dependency ]] || continue
  package=${dependency%% *}
  unset 'pkgs['"$package"']'
  pkgs[$dependency]=1
done <<< "$abi_dependencies"
depends=$(printf '%s\n' "${!pkgs[@]}" | sort | paste -sd',' | sed 's/,/, /g')
[[ -z ${pkgs[whitecube-desktop]:-} ]] || { echo "build-deb.sh: dependency scanner found an unbundled library owned by whitecube-desktop" >&2; exit 1; }

# Record the actual source, dependency recipes and selected runtime in the payload.
python3 -I tools/build-provenance.py "$stage"

# --- DEBIAN/
install -d -m 0755 "$stage/DEBIAN"
size=$(du -sk --exclude=DEBIAN "$stage" | cut -f1)
sed -e "s/@VERSION@/$version/" -e "s/@SIZE@/$size/" -e "s/@DEPENDS@/$depends/" packaging/debian/control.in > "$stage/DEBIAN/control"
install -m 0755 packaging/debian/postinst packaging/debian/postrm "$stage/DEBIAN/"
install -m 0644 packaging/debian/conffiles "$stage/DEBIAN/conffiles"
( cd "$stage" && find . -type f ! -path './DEBIAN/*' -printf '%P\n' | sort | xargs -d '\n' md5sum > DEBIAN/md5sums )
find "$stage" -type d -exec chmod 0755 {} +
find "$stage" -exec touch -h -d "@$SOURCE_DATE_EPOCH" {} +

python3 -I packaging/check-tree.py "$stage" --sources src
python3 -I packaging/check-installed-conflicts.py "$stage"
python3 -I packaging/check-libraries.py "$stage"
python3 -I packaging/check-chat-runtime.py "$stage"
python3 -I apps/cube-train/packaging/parquet-runtime.py check "$stage/usr/lib/whitecube/trainer-parquet-runtime"
temporary_deb=$out/.whitecube-desktop_${version}_amd64.deb
trap 'rm -rf -- "$stage"; rm -f -- "$temporary_deb" "$temporary_deb.json"' EXIT
dpkg-deb --root-owner-group --threads-max="${DPKG_DEB_THREADS_MAX:-2}" -Zxz -z"${DPKG_DEB_COMPRESSION_LEVEL:-3}" --build "$stage" "$temporary_deb" > /dev/null
python3 -I packaging/verify-deb.py "$temporary_deb" --unpack --json "$temporary_deb.json"
mv -f -- "$temporary_deb" "$deb"
mv -f -- "$temporary_deb.json" "$deb.json"
( cd "$out" && sha256sum "$(basename "$deb")" > "$(basename "$deb").sha256" )
chmod 0644 "$deb" "$deb.json" "$deb.sha256"
rm -rf -- "$out/stage"
mv -- "$stage" "$out/stage"
echo
dpkg-deb -I "$deb" | sed -n '1,/^ Description/p' | grep -v '^ new Debian'
echo "files: $(dpkg-deb -c "$deb" | wc -l)   size: $(du -h "$deb" | cut -f1)   sha256: $(sha256sum "$deb" | cut -d' ' -f1)"
echo "built $deb"
