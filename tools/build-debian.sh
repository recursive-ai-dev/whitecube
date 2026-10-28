#!/bin/bash
# Build Whitecube for Debian 13 without installing build dependencies on the host.
# Usage: tools/build-debian.sh [--refresh] [--prepare-only | --shell | -- COMMAND...]
# Requires host apt, dpkg, Perl, Python3, curl and bubblewrap. Apt downloads use
# Debian's archive keyring. Builder stays under build/; app dependencies in checkout.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
work=${WHITECUBE_BUILDER_DIR:-$root/build/debian-builder}
mkdir -p "$work"
work=$(realpath -- "$work")
rootfs=$work/rootfs
mode=build
refresh=0
while (( $# )); do
  case $1 in
    --refresh) refresh=1; shift ;;
    --prepare-only) mode=prepare; shift ;;
    --shell) mode=shell; shift ;;
    --) mode=command; shift; break ;;
    -h|--help) sed -n '2,5p' "$0"; exit 0 ;;
    *) echo "build-debian.sh: unknown option: $1" >&2; exit 2 ;;
  esac
done
if [[ $mode == command && $# == 0 ]]; then
  echo 'build-debian.sh: -- requires a command' >&2; exit 2
fi
electron_dist=$root/apps/desktop-chat/node_modules/electron/dist
extra_binds=()
if [[ $mode != prepare && -n ${ELECTRON_DIST:-} ]]; then
  [[ -x $ELECTRON_DIST/electron ]] || { echo 'build-debian.sh: ELECTRON_DIST must contain an executable electron' >&2; exit 1; }
  electron_dist=$(realpath -- "$ELECTRON_DIST")
  python3 -I "$root/tools/build-inputs.py" check-electron "$electron_dist" >/dev/null
  extra_binds=(--ro-bind "$electron_dist" "$electron_dist")
fi
for command in apt-get dpkg dpkg-deb bwrap perl python3 flock curl sha256sum tar; do
  command -v "$command" >/dev/null || { echo "build-debian.sh: required host tool: $command" >&2; exit 1; }
done
[[ $(dpkg --print-architecture) == amd64 ]] || { echo 'build-debian.sh: this release requires an amd64 host' >&2; exit 1; }
builder_key=$(python3 -I "$root/tools/build-inputs.py" builder-key)
exec 9>"$work/.prepare.lock"
flock 9
cached_key=$(cat "$rootfs/.whitecube-builder-ready" 2>/dev/null || true)
if [[ $refresh = 1 || $cached_key != "$builder_key" ]]; then
  host=$work/host
  mkdir -p "$host"
  if [[ ! -x $host/usr/bin/mmdebstrap || ! -f $host/usr/share/keyrings/debian-archive-keyring.pgp ]]; then
    (cd "$host" && apt-get download mmdebstrap debian-archive-keyring)
    for package in "$host"/mmdebstrap_*.deb "$host"/debian-archive-keyring_*.deb; do
      dpkg-deb --extract "$package" "$host"
    done
  fi
  packages='apt,build-essential,fakeroot,patchelf,ca-certificates,curl,git,unzip,pkg-config,meson,ninja-build,python3,python3-gi,python3-cairo,python3-gi-cairo,python3-yaml,python3-requests,python3-pikepdf,python3-pil,nodejs,npm,libwlroots-0.18-dev,libwayland-dev,wayland-protocols,libxkbcommon-dev,libpango1.0-dev,libcairo2-dev,libinput-dev,libglib2.0-dev,libmpv-dev,libegl-dev,libgles-dev,libpng-dev,libexpat1-dev,libfontconfig-dev,libfreetype-dev,libpixman-1-dev,libadwaita-1-0,gir1.2-adw-1,gir1.2-gtk-4.0,desktop-file-utils,libnss3,libasound2t64,libatk-bridge2.0-0,libdrm2,libgbm1,dbus,dconf-cli,foot,mc,procps,fonts-dejavu-core,libdisplay-info-dev,libliftoff-dev,libseat-dev,libdrm-dev,libgbm-dev,libudev-dev,libgles2-mesa-dev,libvulkan-dev,glslang-tools,hwdata,libgtk-3-0t64,libxcomposite1,libxdamage1,libxrandr2,sudo,fuzzel,swaylock,swaybg,xorriso,squashfs-tools,debootstrap,e2fsprogs,gdisk,parted,dosfstools,xz-utils,dhcpcd-base,iproute2,nftables,seatd,libwayland-egl1'
  next=$work/rootfs-extract
  extracted_key=$(cat "$next/.whitecube-extracted" 2>/dev/null || true)
  if [[ $refresh = 1 || $extracted_key != "$builder_key" ]]; then
    previous=
    if [[ -e $next ]]; then
      previous=$next.incomplete-$(date +%s)
      mv -- "$next" "$previous"
    fi
    export WHITECUBE_BUILDER_CACHE=${previous:+$previous/var/cache/apt/archives}
    echo 'Preparing an authenticated Debian 13 build environment (log: build/debian-builder/extract.log)...'
    "$host/usr/bin/mmdebstrap" --mode=chrootless --variant=extract --architectures=amd64 \
      --skip=cleanup/apt --keyring="$host/usr/share/keyrings/debian-archive-keyring.pgp" \
      --setup-hook='if [ -n "$WHITECUBE_BUILDER_CACHE" ] && [ -d "$WHITECUBE_BUILDER_CACHE" ]; then mkdir -p "$1/var/cache/apt/archives"; cp -an --reflink=auto "$WHITECUBE_BUILDER_CACHE/." "$1/var/cache/apt/archives/"; fi' \
      --include='?essential' --include="$packages" trixie "$next" https://deb.debian.org/debian > "$work/extract.log" 2>&1 || {
        tail -40 "$work/extract.log" >&2; exit 1;
      }
    printf '%s\n' "$builder_key" > "$next/.whitecube-extracted"
  fi
  printf '%s\n' 'deb https://deb.debian.org/debian trixie main' \
    'deb-src https://deb.debian.org/debian trixie main' \
    'deb https://security.debian.org/debian-security trixie-security main' > "$next/etc/apt/sources.list"
  cp --dereference /etc/resolv.conf "$next/etc/resolv.conf"
  mkdir -p "$next/usr/sbin" "$next/tmp" "$next/etc/ssl/certs"
  chmod 1777 "$next/tmp"
  printf '#!/bin/sh\nexit 101\n' > "$next/usr/sbin/policy-rc.d"
  chmod 755 "$next/usr/sbin/policy-rc.d"
  ln -sfn dash "$next/usr/bin/sh"
  for database in passwd group; do
    if [[ ! -f $next/etc/$database ]]; then
      install -m 0644 "$next/usr/share/base-passwd/$database.master" "$next/etc/$database"
    fi
  done
  # Plain extraction avoids both chroot(2) and privileged file ownership. Debian's
  # own fakeroot/dpkg then records ownership, symbols and maintainer state inside
  # a bubblewrap namespace, which uses pivot_root instead of the blocked chroot.
  bwrap --bind "$next" / --dev /dev --proc /proc --unshare-user --uid 0 --gid 0 \
    --setenv PATH /usr/sbin:/usr/bin:/sbin:/bin --setenv HOME /root --setenv DEBIAN_FRONTEND noninteractive \
    --setenv FAKEROOTDONTTRYCHOWN 1 --setenv WHITECUBE_BUILDER_PACKAGES "$packages" /bin/bash -euc '
      update-alternatives --install /usr/bin/awk awk /usr/bin/mawk 5
      /usr/bin/fakeroot-sysv /bin/bash -euc '\''
        dpkg --force-depends --force-confold --force-confdef --unpack /var/cache/apt/archives/*.deb
        dpkg --force-depends --configure -a
        apt-get -y -f install
        apt-get update
        IFS=, read -r -a package_names <<< "$WHITECUBE_BUILDER_PACKAGES"
        apt-get -y --no-install-recommends install "${package_names[@]}"
      '\''
      mkdir -p /tmp/whitecube-build-home
      chmod 0700 /tmp/whitecube-build-home
      dpkg-query -W libc6 dpkg gcc nodejs libwlroots-0.18-dev
    ' > "$work/configure.log" 2>&1 || { tail -60 "$work/configure.log" >&2; exit 1; }
  printf '%s\n' "$builder_key" > "$next/.whitecube-builder-ready"
  if [[ -e $rootfs ]]; then mv -- "$rootfs" "$rootfs.incomplete-$(date +%s)"; fi
  mv -- "$next" "$rootfs"
fi
node_version=22.23.4
node_sha256=1805bcd35e6815559bd2eb97db438d3fdb0eb3a5e6a977b7c1badca76337e9ce
if [[ ! -x $work/node/bin/node ]] || [[ $("$work/node/bin/node" --version) != v$node_version ]]; then
  mkdir -p "$work/node-downloads" "$work/node"
  archive=node-v${node_version}-linux-x64.tar.xz
  if [[ ! -f $work/node-downloads/$archive ]]; then
    curl -fL --retry 3 "https://nodejs.org/dist/v${node_version}/$archive" -o "$work/node-downloads/$archive.part"
    mv -- "$work/node-downloads/$archive.part" "$work/node-downloads/$archive"
  fi
  (cd "$work/node-downloads" && printf '%s  %s\n' "$node_sha256" "$archive" | sha256sum -c -)
  tar --no-same-owner -xJf "$work/node-downloads/$archive" --strip-components=1 -C "$work/node"
fi
# D-Bus and GTK resolve the namespace uid through passwd, even though the build
# process already owns its files. Keep this local account in cached roots too.
python3 - "$rootfs" "$(id -u)" "$(id -g)" <<'PY'
import os
from pathlib import Path
import sys
rootfs, uid, gid = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
home = rootfs / 'tmp/whitecube-build-home'
home.mkdir(parents=True, exist_ok=True)
home.chmod(0o700)
# Refresh host DNS after moves between networks. Replace rather than follow a
# cached absolute resolv.conf symlink into the host filesystem.
resolver = rootfs / 'etc/resolv.conf'
temporary = resolver.with_name(f'.resolv.conf.whitecube-{os.getpid()}')
temporary.write_bytes(Path('/etc/resolv.conf').read_bytes())
temporary.chmod(0o644)
temporary.replace(resolver)
for filename, identifier, entry in (
    ('group', gid, f'whitecube-builder:x:{gid}:'),
    ('passwd', uid, f'whitecube-builder:x:{uid}:{gid}:Whitecube builder:/tmp/whitecube-build-home:/bin/bash'),
):
    path = rootfs / 'etc' / filename
    text = path.read_text()
    if not any(line.split(':')[2:3] == [identifier] for line in text.splitlines()):
        temporary = path.with_name(f'.{filename}.whitecube-{os.getpid()}')
        temporary.write_text(text.rstrip('\n') + '\n' + entry + '\n')
        temporary.chmod(0o644)
        temporary.replace(path)
PY
flock -u 9
if [[ $mode == prepare ]]; then
  echo "Debian 13 builder ready: $rootfs"
  exit 0
fi
arguments=()
case $mode in
  shell) arguments=(/bin/bash) ;;
  command) [[ $# -gt 0 ]] || { echo 'build-debian.sh: -- requires a command' >&2; exit 2; }; arguments=("$@") ;;
  build) arguments=(/bin/bash -euc 'electron_args=(); [[ $WHITECUBE_EXTERNAL_ELECTRON = 0 ]] || electron_args+=(--external); [[ $WHITECUBE_REFRESH = 0 ]] || electron_args+=(--refresh); python3 -I tools/build-inputs.py ensure-electron "$ELECTRON_DIST" "${electron_args[@]}"; bash tools/prepare-native-deps.sh; source build/native-deps/environment.sh; bash packaging/build-deb.sh') ;;
esac
bwrap --unsetenv LD_LIBRARY_PATH --unsetenv PKG_CONFIG_PATH --unsetenv GI_TYPELIB_PATH \
  --unsetenv PYTHONPATH --unsetenv PYTHONHOME --unsetenv NODE_PATH --unsetenv TMPDIR \
  --unsetenv XDG_RUNTIME_DIR --unsetenv WAYLAND_DISPLAY --unsetenv DISPLAY --unsetenv DBUS_SESSION_BUS_ADDRESS \
  --unsetenv XDG_CONFIG_HOME --unsetenv XDG_DATA_HOME --unsetenv XDG_STATE_HOME --unsetenv XDG_CACHE_HOME \
  --bind "$rootfs" / --bind "$root" "$root" --ro-bind "$work/node" /opt/whitecube-node --dev /dev --proc /proc \
  --unshare-user --uid "$(id -u)" --gid "$(id -g)" --chdir "$root" --setenv HOME /tmp/whitecube-build-home \
  --setenv PATH /opt/whitecube-node/bin:/usr/sbin:/usr/bin:/sbin:/bin --setenv NODE /opt/whitecube-node/bin/node --setenv LANG C.UTF-8 \
  --setenv WHITECUBE_BUILDER_KEY "$builder_key" --setenv WHITECUBE_REFRESH "$refresh" \
  --setenv WHITECUBE_EXTERNAL_ELECTRON "$([[ -n ${ELECTRON_DIST:-} ]] && echo 1 || echo 0)" \
  --setenv ELECTRON_DIST "$electron_dist" "${extra_binds[@]}" "${arguments[@]}"
if [[ $mode == build ]]; then
  version=$(tr -d '[:space:]' < "$root/packaging/VERSION")
  # The builder's dpkg database cannot reveal file collisions on the host.
  # Check the delivered archive against that host too, without changing it.
  python3 -I "$root/packaging/check-installed-conflicts.py" "$root/packaging/out/whitecube-desktop_${version}_amd64.deb"
fi
