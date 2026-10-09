#!/bin/sh
# Install the whitecube boot theme (GRUB + Plymouth + tty1 banner). Run: sudo ./install-boot.sh
# Backs up everything it touches to /var/backups/whitecube-boot. Undo with: sudo ./install-boot.sh --revert
set -eu
[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
bk=/var/backups/whitecube-boot
mkdir -p "$bk"

if [ "${1:-}" = --revert ]; then
  [ -f "$bk/grub.default" ] && cp -f "$bk/grub.default" /etc/default/grub
  [ -f "$bk/issue" ] && cp -f "$bk/issue" /etc/issue
  [ -f "$bk/plymouth-theme" ] && plymouth-set-default-theme -R "$(cat "$bk/plymouth-theme")"
  update-grub
  echo "reverted"; exit 0
fi

stage=$(mktemp -d); trap 'rm -rf "$stage"' EXIT
python3 -I "$here/gen-boot-theme.py" "$stage" 1920 1080

# first run only: remember the originals
[ -f "$bk/grub.default" ] || cp -a /etc/default/grub "$bk/grub.default"
[ -f "$bk/issue" ] || cp -a /etc/issue "$bk/issue"
[ -f "$bk/plymouth-theme" ] || plymouth-set-default-theme > "$bk/plymouth-theme"

# --- Plymouth ---
pd=/usr/share/plymouth/themes/whitecube
install -d "$pd"
install -m644 "$stage"/plymouth/*.png "$here/whitecube.script" "$here/whitecube.plymouth" "$pd/"

# --- GRUB ---
gd=/boot/grub/themes/whitecube
install -d "$gd"
install -m644 "$stage"/grub/*.png "$here/theme.txt" "$gd/"
mk() { grub-mkfont -s "$1" -o "$gd/$2" "$3"; }
f=/usr/share/fonts/truetype/dejavu
mk 12 dejavu-mono-12.pf2 $f/DejaVuSansMono.ttf
mk 14 dejavu-mono-14.pf2 $f/DejaVuSansMono.ttf
mk 14 dejavu-mono-bold-14.pf2 $f/DejaVuSansMono-Bold.ttf
mk 18 dejavu-mono-bold-18.pf2 $f/DejaVuSansMono-Bold.ttf
chmod 644 "$gd"/*.pf2

set_var() { # set_var KEY VALUE in /etc/default/grub (replace or append)
  if grep -q "^$1=" /etc/default/grub; then sed -i "s|^$1=.*|$1=$2|" /etc/default/grub
  else echo "$1=$2" >> /etc/default/grub; fi
}
set_var GRUB_THEME '"/boot/grub/themes/whitecube/theme.txt"'
set_var GRUB_GFXMODE '1920x1080,auto'
set_var GRUB_GFXPAYLOAD_LINUX keep
set_var GRUB_TIMEOUT 5
update-grub

# --- tty1 banner (autologin prints it before the desktop starts) ---
printf 'whitecube\n\n' > /etc/issue

plymouth-set-default-theme -R whitecube
echo "installed. originals are in $bk; reboot to see it."
