#!/bin/bash
# build-deb.sh [--no-build] : build packaging/out/whitecube-desktop_<version>_amd64.deb
#
#   ./BUILD.sh                     (compile + every test)   <- run by this script unless --no-build
#   packaging/install-tree.sh      (lay the files out)
#   bundle the X-free wlroots and cairo into /usr/lib/whitecube, strip, work out Depends
#   packaging/check-tree.py        (every path the package mentions must exist in it)
#   dpkg-deb --root-owner-group    (no root, no fakeroot needed)
#
# It builds a file and nothing else: it installs nothing, touches no service, needs no sudo.
# Install the result yourself:  sudo apt install ./packaging/out/whitecube-desktop_*.deb
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

version=$(tr -d '[:space:]' < packaging/VERSION)
out=packaging/out
stage=$out/stage
deb=$out/whitecube-desktop_${version}_amd64.deb

if [[ $build = 1 ]]; then ./BUILD.sh > "$out.build.log" 2>&1 || { tail -30 "$out.build.log" >&2; echo "build-deb.sh: BUILD.sh failed (log: $out.build.log)" >&2; exit 1; }; fi
rm -rf "$out"; mkdir -p "$stage"
bash packaging/install-tree.sh "$stage"

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

# --- strip (also removes the build directory recorded in the debug info)
strip --strip-unneeded "$stage"/usr/bin/{whitecube-compositor,cube-status,cube-settings,cube-ca} "$priv"/*.so* 2>/dev/null || true
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
runtime=(foot fuzzel swaylock swaybg python3 python3-gi python3-cairo python3-gi-cairo python3-yaml
  gir1.2-gtk-4.0 gir1.2-adw-1 xorriso squashfs-tools sudo util-linux mount coreutils debootstrap e2fsprogs
  dhcpcd-base iproute2 seatd)
for p in "${runtime[@]}"; do pkgs[$p]=1; done
depends=$(printf '%s\n' "${!pkgs[@]}" | sort | paste -sd',' | sed 's/,/, /g')
[[ -z ${pkgs[whitecube-desktop]:-} ]] || { echo "build-deb.sh: dependency scanner found an unbundled library owned by whitecube-desktop" >&2; exit 1; }

# --- DEBIAN/
install -d -m 0755 "$stage/DEBIAN"
size=$(du -sk --exclude=DEBIAN "$stage" | cut -f1)
sed -e "s/@VERSION@/$version/" -e "s/@SIZE@/$size/" -e "s/@DEPENDS@/$depends/" packaging/debian/control.in > "$stage/DEBIAN/control"
install -m 0755 packaging/debian/postinst packaging/debian/postrm "$stage/DEBIAN/"
install -m 0644 packaging/debian/conffiles "$stage/DEBIAN/conffiles"
( cd "$stage" && find . -type f ! -path './DEBIAN/*' -printf '%P\n' | sort | xargs -d '\n' md5sum > DEBIAN/md5sums )
find "$stage" -type d -exec chmod 0755 {} +

python3 -I packaging/check-tree.py "$stage" --sources src
python3 -I packaging/check-libraries.py "$stage"
python3 -I packaging/check-chat-runtime.py "$stage"
dpkg-deb --root-owner-group -Zxz --build "$stage" "$deb" > /dev/null
sha256sum "$deb" > "$deb.sha256"
echo
dpkg-deb -I "$deb" | sed -n '1,/^ Description/p' | grep -v '^ new Debian'
echo "files: $(dpkg-deb -c "$deb" | wc -l)   size: $(du -h "$deb" | cut -f1)   sha256: $(sha256sum "$deb" | cut -d' ' -f1)"
echo "built $deb"
