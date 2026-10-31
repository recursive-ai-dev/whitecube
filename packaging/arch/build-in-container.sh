#!/bin/bash
# Runs inside the Arch container as root: installs tools, builds Whitecube as a plain user, packages it, makes the ISO.
set -euo pipefail
src=/work/src out=/out jobs=${JOBS:-$(($(nproc) / 2 + 1))}
pacman-key --init
pacman-key --populate archlinux
echo 'Server = https://geo.mirror.pkgbuild.com/$repo/os/$arch' > /etc/pacman.d/mirrorlist
sed -i 's/^#\?DisableSandbox.*/DisableSandbox/' /etc/pacman.conf
grep -q '^DisableSandbox' /etc/pacman.conf || sed -i '/^\[options\]/a DisableSandbox' /etc/pacman.conf
pacman -Syu --noconfirm --needed base-devel git curl unzip pkgconf meson ninja patchelf archiso nodejs npm \
  python python-gobject python-cairo python-yaml python-requests python-pikepdf python-pillow python-numpy \
  wayland wayland-protocols libxkbcommon pango cairo libinput glib2 mpv libglvnd libpng expat fontconfig freetype2 \
  pixman libadwaita gtk4 gtk3 desktop-file-utils nss alsa-lib at-spi2-core libdrm mesa dbus dconf foot procps-ng \
  ttf-dejavu libdisplay-info libliftoff libseat vulkan-headers vulkan-icd-loader glslang hwdata sudo fuzzel swaylock \
  swaybg libisoburn squashfs-tools debootstrap e2fsprogs dosfstools gptfdisk parted nftables dhcpcd iproute2 seatd \
  libxcomposite libxdamage libxrandr libcups dpkg fakeroot

id builder >/dev/null 2>&1 || useradd -m builder
chown -R builder: /work/src /work/repo
run_builder() { runuser -u builder -- env -u TMPDIR HOME=/home/builder PATH=/usr/bin WHITECUBE_SRC="$src" JOBS="$jobs" REQUIRE_XFREE=1 bash -euc "$1"; }

run_builder 'cd "$WHITECUBE_SRC"
  python3 -I tools/build-inputs.py ensure-electron apps/desktop-chat/node_modules/electron/dist
  WLROOTS_SOURCE_DIR=$PWD/build/wlroots-source bash tools/prepare-native-deps.sh
  source build/native-deps/environment.sh
  ./BUILD.sh'
run_builder 'cd /work/repo && rm -f whitecube-desktop-*.pkg.tar.zst && cp "$WHITECUBE_SRC/packaging/arch/PKGBUILD" . &&
  source "$WHITECUBE_SRC/build/native-deps/environment.sh" && makepkg --force --nodeps --noconfirm &&
  repo-add whitecube.db.tar.zst whitecube-desktop-*.pkg.tar.zst'
cp /work/repo/whitecube-desktop-*.pkg.tar.zst "$out/"

# archiso's own baseline tracks its current boot options, so only the differences live in this repository.
profile=/work/profile iso_work=/work/iso-work
rm -rf "$profile" "$iso_work"
cp -a /usr/share/archiso/configs/baseline/. "$profile/"
cat "$src/packaging/arch/packages.extra" >> "$profile/packages.x86_64"
cp -a "$src/packaging/arch/airootfs/." "$profile/airootfs/"
install -d "$profile/airootfs/etc/sudoers.d"
install -m 0440 "$src/data/sudoers/whitecube-settings" "$profile/airootfs/etc/sudoers.d/whitecube-settings"
# Live image only: there is no password to type, so the live user is passwordless for sudo.
install -m 0440 /dev/stdin "$profile/airootfs/etc/sudoers.d/live-user" <<'CONF'
cube ALL=(ALL) NOPASSWD: ALL
CONF
sed -i "s/^iso_name=.*/iso_name=\"whitecube\"/; s/^iso_label=.*/iso_label=\"WHITECUBE_\$(date +%Y%m)\"/; s/^iso_publisher=.*/iso_publisher=\"Whitecube\"/; s/^iso_application=.*/iso_application=\"Whitecube live system\"/" "$profile/profiledef.sh"
cat >> "$profile/profiledef.sh" <<'CONF'
file_permissions+=(["/etc/sudoers.d/whitecube-settings"]="0:0:440" ["/etc/sudoers.d/live-user"]="0:0:440")
CONF
printf '\n[whitecube]\nSigLevel = Optional TrustAll\nServer = file:///work/repo\n' >> "$profile/pacman.conf"
mkarchiso -v -w "$iso_work" -o "$out" "$profile"
root=$(find "$iso_work" -maxdepth 2 -type d -name airootfs | head -1)
[[ -n $root ]] || { echo 'build: the image root was not kept, so it cannot be audited' >&2; exit 1; }
bash "$src/packaging/arch/audit-no-x.sh" "$root"
sha=$(ls -t "$out"/whitecube-*.iso | head -1)
(cd "$out" && sha256sum "$(basename "$sha")" > "$(basename "$sha").sha256")
chown -R --reference="$out" "$out" 2>/dev/null || true
echo "ISO: $(basename "$sha")"
