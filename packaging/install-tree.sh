#!/bin/bash
# install-tree.sh DESTDIR : lay the built Whitecube desktop out as a Debian-style filesystem tree.
#
# This is the single definition of "what lands where". build-deb.sh runs it into a staging
# directory; running it by hand into an empty directory shows exactly what a package would install.
# It never touches the running system and needs no root. Run ./BUILD.sh first.
#
#   /usr/bin/                       commands (compositor, bar, settings, wallpaper, helpers, apps)
#   /usr/libexec/whitecube/         root helpers (cube-settings-helper, cube-net-dns)
#   /usr/lib/whitecube/             private libraries (X-free wlroots, X-free cairo) - added by build-deb.sh
#   /usr/lib/systemd/system/        whitecube-network.service
#   /usr/share/whitecube/           keys, schemas, wallpapers, workspace suite, bundled apps, boot theme
#   /usr/share/{applications,icons,wayland-sessions}/   desktop entries, icons, the login-manager session
#   /usr/share/doc/whitecube-desktop/   docs and the sudoers example (not installed into /etc/sudoers.d)
#   /etc/whitecube/                 system defaults (conffiles); per-user settings stay in ~/.config/whitecube
#   /etc/skel/                      dotfiles for NEW accounts (foot, fuzzel, fnott)
set -euo pipefail

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
dest=${1:?usage: install-tree.sh DESTDIR}
[[ -d $dest && -z $(ls -A "$dest") ]] || { echo "install-tree.sh: $dest must be an existing, empty directory" >&2; exit 1; }
cd "$root"

for b in whitecube cube-status cube-settings cube-ca; do
  [[ -x build/$b ]] || { echo "install-tree.sh: build/$b is missing; run ./BUILD.sh first" >&2; exit 1; }
done
[[ -x build/native-apps-root/usr/bin/cube-player && -x build/native-apps-root/usr/libexec/whitecube/cube-browser/cube-browser ]] || {
  echo "install-tree.sh: build/native-apps-root is missing; run ./BUILD.sh first" >&2; exit 1; }
[[ -f build/schemas/gschemas.compiled ]] || { echo "install-tree.sh: build/schemas/gschemas.compiled is missing; run ./BUILD.sh first" >&2; exit 1; }

bin=$dest/usr/bin lib=$dest/usr/libexec/whitecube share=$dest/usr/share/whitecube doc=$dest/usr/share/doc/whitecube-desktop
install -d -m 0755 "$bin" "$lib" "$share" "$doc" "$dest/etc/whitecube/network" "$dest/usr/lib/systemd/system"

# --- compiled programs
for b in cube-status cube-settings cube-ca; do install -m 0755 "build/$b" "$bin/$b"; done
# not /usr/bin/whitecube: the "whitecube" package (image tool) already owns that name
install -m 0755 build/whitecube "$bin/whitecube-compositor"

# Cube Browser and Cube Player (meson, built by apps/build-native-apps.sh). The browser starts its
# renderer and network workers from its own directory, so the three stay together in libexec.
cp -a build/native-apps-root/usr/. "$dest/usr/"
ln -s ../libexec/whitecube/cube-browser/cube-browser "$bin/cube-browser"

# --- commands (scripts)
for f in bin/*; do
  [[ -f $f ]] && install -m 0755 "$f" "$bin/$(basename "$f")"
done
install -m 0755 net/cube-net "$bin/cube-net"
install -m 0755 net/net-off net/net-on "$bin/"
install -m 0755 tools/whitecube-migrate "$bin/whitecube-migrate"

# --- root helpers
install -m 0755 libexec/cube-settings-helper "$lib/cube-settings-helper"
install -m 0755 net/cube-net-dns "$lib/cube-net-dns"
install -m 0755 net/whitecube-net-switch "$lib/whitecube-net-switch"

# --- network service and its default configuration
install -m 0644 net/whitecube-network.service "$dest/usr/lib/systemd/system/whitecube-network.service"
install -m 0644 net/dhcpcd.conf "$dest/etc/whitecube/network/dhcpcd.conf"
install -m 0644 net/net-switch.conf "$dest/etc/whitecube/net-switch.conf"
install -m 0644 net/whitecube-net-lockdown.service "$dest/usr/lib/systemd/system/whitecube-net-lockdown.service"
# Restore only an explicitly saved lockdown at boot. Installing the package
# does not start this service or change the live firewall/network connection.
install -d -m 0755 "$dest/usr/lib/systemd/system/multi-user.target.wants" "$dest/usr/lib/systemd/system/whitecube-network.service.d"
ln -s ../whitecube-net-lockdown.service "$dest/usr/lib/systemd/system/multi-user.target.wants/whitecube-net-lockdown.service"
install -m 0644 net/whitecube-network.net-lockdown.conf "$dest/usr/lib/systemd/system/whitecube-network.service.d/net-lockdown.conf"
install -m 0644 data/config.example "$dest/etc/whitecube/config"

# --- shared data
install -m 0644 data/keys.txt "$share/keys.txt"
install -d -m 0755 "$share/schemas" "$share/wallpapers" "$share/skel"
install -m 0644 build/schemas/org.whitecube.desktop.gschema.xml build/schemas/gschemas.compiled "$share/schemas/"
cp -a data/wallpapers/. "$share/wallpapers/"
for f in foot.ini foot-ca.ini fuzzel.ini fnott.ini; do install -m 0644 "data/$f" "$share/skel/$f"; done
cp -a boot "$share/boot-theme"
find "$share/boot-theme" -name __pycache__ -prune -exec rm -rf {} +

# --- workspace suite and bundled apps (python / javascript, run in place from /usr/share)
cp -a workspace "$share/workspace"
cp -a apps/desktop-chat "$share/desktop-chat"
cp -a apps/cube-apps "$share/cube-apps"
# Keep local training environments and standalone build artifacts out of the desktop.
install -d -m 0755 "$share/cube-train"
for f in apps/cube-train/*.py apps/cube-train/*.md; do
  install -m 0644 "$f" "$share/cube-train/$(basename "$f")"
done
cp -a apps/cube-train/pipeline "$share/cube-train/pipeline"
find "$share/cube-train/pipeline" -name __pycache__ -prune -exec rm -rf {} +
install -d -m 0755 "$share/cube-editor"
# Exclude the extracted AppImage and development-only directories before copying.
while IFS= read -r -d '' source; do
  relative=${source#apps/cube-editor/}
  if [[ -d $source ]]; then
    install -d -m 0755 "$share/cube-editor/$relative"
  else
    install -m 0644 "$source" "$share/cube-editor/$relative"
  fi
done < <(find apps/cube-editor -mindepth 1 \( -type d \( -name upstream -o -name tests -o -name packaging -o -name __pycache__ \) -prune \) -o \( -type d -o -type f \) -print0)
rm -rf "$share/desktop-chat/test" "$share/desktop-chat/node_modules" "$share/cube-apps/tests" "$share/cube-apps/shot.sh" "$share/cube-train/tests"
# Chrootables: the window runs from /usr/share; its manager is a command of its own (CLI and menu),
# which the window finds as /usr/bin/chrootable-manager.
install -d -m 0755 "$share/chrootables"
for f in app.py backend.py broker.py style.css README.md MANAGER.md; do install -m 0644 "apps/chrootables/$f" "$share/chrootables/$f"; done
install -m 0755 apps/chrootables/chrootable-manager "$bin/chrootable-manager"
# Cube Strap: the window runs from /usr/share and imports cube-apps and chrootables beside it;
# its one privileged step, cube-strap-build, lives with the other root helpers.
install -d -m 0755 "$share/cube-strap"
for f in app.py strap.py README.md; do install -m 0644 "apps/cube-strap/$f" "$share/cube-strap/$f"; done
install -m 0755 apps/cube-strap/deb-strap.sh "$share/cube-strap/deb-strap.sh"
install -m 0755 apps/cube-strap/cube-strap-build "$lib/cube-strap-build"
# Cube Prompts: the window and its bundled prompt_manager core run from /usr/share.
install -d -m 0755 "$share/cube-prompts"
for f in app.py style.css README.md NOTICE; do install -m 0644 "apps/cube-prompts/$f" "$share/cube-prompts/$f"; done
(cd apps/cube-prompts && find prompt_manager -name '*.py' -print0) | while IFS= read -r -d '' f; do
  install -D -m 0644 "apps/cube-prompts/$f" "$share/cube-prompts/$f"
done
# Electron is a runtime dependency of Chat, shipped privately so the desktop
# session never depends on npm, nvm, or another user's development environment.
chat_runtime=${ELECTRON_DIST:-$root/build/desktop-chat-runtime}
if [[ ! -x $chat_runtime/electron && -x /usr/lib/whitecube/desktop-chat-runtime/electron ]]; then
  chat_runtime=/usr/lib/whitecube/desktop-chat-runtime
fi
[[ -x $chat_runtime/electron && -f $chat_runtime/LICENSE && -f $chat_runtime/version ]] || {
  echo 'install-tree.sh: stage Electron dist in build/desktop-chat-runtime or set ELECTRON_DIST' >&2; exit 1;
}
install -d -m 0755 "$dest/usr/lib/whitecube"
cp -a --reflink=auto "$chat_runtime" "$dest/usr/lib/whitecube/desktop-chat-runtime"
# This host disables unprivileged user namespaces. Ship Chromium's standard
# sandbox helper; dpkg-deb --root-owner-group assigns root ownership on install.
chmod 4755 "$dest/usr/lib/whitecube/desktop-chat-runtime/chrome-sandbox"
find "$dest" -name __pycache__ -prune -exec rm -rf {} +
find "$dest" -type d -name .git -prune -exec rm -rf {} +
ln -s ../share/whitecube/desktop-chat/desktop-chat.sh "$bin/desktop-chat"

# --- desktop integration
install -d -m 0755 "$dest/usr/share/applications" "$dest/usr/share/wayland-sessions"
install -m 0644 data/applications/*.desktop "$dest/usr/share/applications/"
install -m 0644 data/wayland-sessions/whitecube.desktop "$dest/usr/share/wayland-sessions/whitecube.desktop"
install -d -m 0755 "$dest/usr/share/icons/hicolor/scalable/apps"
install -m 0644 data/icons/cube-editor.svg "$dest/usr/share/icons/hicolor/scalable/apps/cube-editor.svg"
install -m 0644 data/icons/chrootables.svg "$dest/usr/share/icons/hicolor/scalable/apps/chrootables.svg"
install -m 0644 data/icons/cube-train.svg "$dest/usr/share/icons/hicolor/scalable/apps/cube-train.svg"
install -m 0644 data/icons/cube-strap.svg "$dest/usr/share/icons/hicolor/scalable/apps/cube-strap.svg"
install -m 0644 data/icons/cube-prompts.svg "$dest/usr/share/icons/hicolor/scalable/apps/cube-prompts.svg"
install -m 0644 data/icons/cube-browser.svg "$dest/usr/share/icons/hicolor/scalable/apps/cube-browser.svg"
install -m 0644 data/icons/cube-player.svg "$dest/usr/share/icons/hicolor/scalable/apps/cube-player.svg"
python3 -I apps/cube-apps/install.py --destdir "$dest" > /dev/null   # cube-lock/pdf/hash/rename: launchers, entries, icons
python3 -I packaging/install-wc-suite.py "$dest" > /dev/null          # wc-* commands, /usr/lib/wc-suite, completions

# --- skeleton for new accounts: the same defaults the desktop ships, as plain dotfiles
install -d -m 0755 "$dest/etc/skel/.config/foot" "$dest/etc/skel/.config/fuzzel" "$dest/etc/skel/.config/fnott"
install -m 0644 data/foot.ini "$dest/etc/skel/.config/foot/foot.ini"
install -m 0644 data/fuzzel.ini "$dest/etc/skel/.config/fuzzel/fuzzel.ini"
install -m 0644 data/fnott.ini "$dest/etc/skel/.config/fnott/fnott.ini"

# --- documentation and examples
cp -a docs/. "$doc/"
install -m 0644 LICENSE NOTICE "$doc/"
install -m 0644 packaging/debian/copyright "$doc/copyright"
install -m 0644 data/sudoers/whitecube-settings "$doc/sudoers-whitecube-settings.example"
install -m 0644 data/sudoers/whitecube-settings.hardened "$doc/sudoers-whitecube-settings.hardened.example"
install -m 0644 net/NET-SWITCH.txt "$doc/NET-SWITCH.txt"
install -d -m 0755 "$doc/cube-browser/licenses" "$doc/cube-player"
install -m 0644 apps/cube-browser/README.md apps/cube-browser/COMPATIBILITY.md "$doc/cube-browser/"
install -m 0644 apps/cube-browser/.deps/licenses/* "$doc/cube-browser/licenses/"
install -m 0644 apps/cube-browser/.deps/prefix/share/licenses/libjpeg-turbo/copyright "$doc/cube-browser/licenses/libjpeg-turbo-copyright"
install -m 0644 apps/cube-browser/third_party/nanosvg/LICENSE.txt "$doc/cube-browser/licenses/nanosvg-LICENSE"
install -m 0644 apps/cube-player/README.md apps/cube-player/COPYING "$doc/cube-player/"
[[ -f README.md ]] && install -m 0644 README.md "$doc/README.md"

# Source workspaces can use umask 077. Installed shared apps, launchers, icons
# and docs must be readable by every account, regardless of source file modes.
find "$dest/usr" -type d -exec chmod 0755 {} +
find "$dest/usr" -type f -exec chmod a+r,go-w {} +
find "$dest/usr" -type f -perm /0111 -exec chmod a+rx,go-w {} +

echo "install tree written to $dest ($(find "$dest" -type f | wc -l) files, $(du -sh "$dest" | cut -f1))"
