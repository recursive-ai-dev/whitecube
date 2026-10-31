#!/bin/bash
# Builds a bootable, X-free Arch Linux Whitecube ISO in an isolated container.
#
#   packaging/arch/build-iso.sh          build (one password dialog; run it from a terminal in LXQt)
#   packaging/arch/build-iso.sh --test   boot the newest ISO in a QEMU window
#
# Downloads and building use a container under /var/lib/whitecube-arch-builder; delete that
# folder to reclaim the space. The ISO lands in ~/Documents/whitecube-arch-iso.
set -euo pipefail

BUILDER=/var/lib/whitecube-arch-builder
MIRROR=https://geo.mirror.pkgbuild.com/iso/latest
BOOTSTRAP=archlinux-bootstrap-x86_64.tar.zst
# Arch's release signing key, as published on archlinux.org/download. A mismatch stops the build.
SIGNING_KEY=3E80CA1A8B89F69CBA57D98A76A5EF9054449A5C

die() { printf '\nSTOPPED: %s\n' "$*" >&2; exit 1; }

root_stage() {
  local repo=$1 out=$2 pause=$3 user=$4
  export PATH=/usr/sbin:/usr/bin:/sbin:/bin
  if [[ $pause == pause ]]; then
    systemctl stop opensnitch
    trap 'systemctl start opensnitch && echo "OpenSnitch is back on." || echo "WARNING: OpenSnitch is still OFF. Run: pkexec systemctl start opensnitch" >&2' EXIT
    sleep 2
  fi
  install -d -m 0755 "$BUILDER/downloads" "$BUILDER/work/src" "$BUILDER/work/repo"
  cd "$BUILDER/downloads"
  curl -fL --proto '=https' --retry 3 -z "$BOOTSTRAP" -o "$BOOTSTRAP" "$MIRROR/$BOOTSTRAP"
  curl -fL --proto '=https' --retry 3 -o "$BOOTSTRAP.sig" "$MIRROR/$BOOTSTRAP.sig"
  local gnupg
  gnupg=$(mktemp -d)
  GNUPGHOME=$gnupg gpg --quiet --auto-key-locate clear,wkd --locate-external-key pierre@archlinux.org >/dev/null
  GNUPGHOME=$gnupg gpg --status-fd 1 --verify "$BOOTSTRAP.sig" "$BOOTSTRAP" 2>/dev/null \
    | grep -qE "^\[GNUPG:\] VALIDSIG .* $SIGNING_KEY\$" || { rm -rf "$gnupg"; die 'The Arch download is not signed by the expected key.'; }
  rm -rf "$gnupg"
  echo 'Arch download signature verified.'
  if [[ ! -x $BUILDER/root.x86_64/usr/bin/pacman ]]; then
    tar --numeric-owner --xattrs --xattrs-include='*' --zstd -xf "$BOOTSTRAP" -C "$BUILDER"
  fi
  # A plain copy of the checkout: Debian build output must not mix with this one.
  tar -C "$repo" -cf - --exclude=./build --exclude=./packaging/out --exclude=./.git --exclude='.deps' \
    --exclude=node_modules --exclude=__pycache__ . | tar -C "$BUILDER/work/src" -xf -
  rm -rf "$BUILDER/work/src/build/wlroots-source"
  install -d "$BUILDER/work/src/build"
  cp -a "$repo/build/native-deps/src/wlroots-0.18.2" "$BUILDER/work/src/build/wlroots-source"
  chown -R "$user" "$out"
  systemd-nspawn -q --register=no --resolv-conf=replace-uplink --capability=CAP_SYS_ADMIN,CAP_MKNOD \
    -D "$BUILDER/root.x86_64" --bind="$BUILDER/work:/work" --bind="$out:/out" \
    /bin/bash /work/src/packaging/arch/build-in-container.sh
}

if [[ ${1:-} == --root-stage ]]; then
  shift
  root_stage "$@"
  exit 0
fi

self=$(readlink -f "$0")
repo=$(cd -- "$(dirname -- "$self")/../.." && pwd)
out=$HOME/Documents/whitecube-arch-iso
mkdir -p "$out"

if [[ ${1:-} == --test ]]; then
  iso=$(ls -t "$out"/whitecube-*.iso 2>/dev/null | head -1 || true)
  [[ -n $iso ]] || die "No ISO in $out yet."
  (cd "$out" && sha256sum --check "$(basename "$iso").sha256") || die 'The ISO checksum does not match.'
  firmware=/usr/share/ovmf/OVMF.fd
  [[ -f $firmware ]] || die 'UEFI firmware not found. Install the ovmf package.'
  exec qemu-system-x86_64 -enable-kvm -cpu host -smp 2 -m 4096 -bios "$firmware" -device virtio-vga \
    -device qemu-xhci -device usb-tablet -cdrom "$iso" -boot d
fi
[[ -z ${1:-} ]] || { sed -n 2,8p "$self"; exit 2; }

[[ $EUID != 0 ]] || die 'Run this as yourself, not root.'
[[ -t 0 ]] || die 'Run this from a terminal, because it may need to ask you a question.'
[[ -f $repo/build/native-deps/src/wlroots-0.18.2/meson.build ]] || die 'The verified wlroots source is missing. Run tools/build-debian.sh once to fetch it.'

pause=keep
if systemctl is-active --quiet opensnitch; then
  cat <<'EOF'
OpenSnitch is running. This build downloads from many sites, and its tests use 127.0.0.1,
which is currently timing out. To build, OpenSnitch has to be paused until the build ends;
it comes back on afterwards, even if the build fails. Nothing filters connections meanwhile.
The build can take an hour or more.
EOF
  read -r -p 'Pause OpenSnitch for the build? [y/N] ' answer
  [[ $answer == [yY]* ]] || die 'Nothing was changed.'
  pause=pause
fi
log=$out/build-$(date +%Y%m%d-%H%M%S).log
echo "Log: $log"
pkexec /bin/bash "$self" --root-stage "$repo" "$out" "$pause" "$(id -un)" 2>&1 | tee "$log" || die "The build failed. Log: $log"
ls -lh "$out"/whitecube-*.iso | tail -1
echo "Done. Boot it in a window with: $self --test"
