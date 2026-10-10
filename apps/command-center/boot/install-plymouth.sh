#!/usr/bin/env bash
# Installs the whitecube Plymouth theme. Needs root and rebuilds the initramfs, so read it first.
# It does NOT touch your disk encryption: it only changes the splash that draws the passphrase prompt.
# Usage: sudo boot/install-plymouth.sh [--size 1920x1080] [--no-fetch]
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
dest=/usr/share/plymouth/themes/whitecube
size=1920x1080; fetch=1
while [[ $# -gt 0 ]]; do case $1 in --size) size=$2; shift 2;; --no-fetch) fetch=0; shift;; *) echo "unknown $1" >&2; exit 2;; esac; done

install -d -m 0755 "$dest"
install -m 0644 "$here/plymouth/whitecube/whitecube.plymouth" "$here/plymouth/whitecube/whitecube.script" "$dest/"

# Assets are built as the invoking user's python: bg (free API + watermark/123% crop) and a 1px red bar.
python3 - "$dest" "$here" "$size" "$fetch" <<'PY'
import sys, importlib.util, os
from PIL import Image
dest, here, size, fetch = sys.argv[1], sys.argv[2], tuple(int(v) for v in sys.argv[3].split("x")), sys.argv[4] == "1"
Image.new("RGB", (4, 3), (255, 59, 59)).save(os.path.join(dest, "bar.png"))
spec = importlib.util.spec_from_file_location("mbg", os.path.join(here, "make-boot-bg.py")); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
try:
    if not fetch: raise RuntimeError("fetch disabled")
    img, label = m.fetch(size)
    out, flagged, side, _ = m.build(img, size)
    out.save(os.path.join(dest, "bg.png")); print("background:", label, "watermark:", side if flagged else "none")
except Exception as e:
    Image.new("RGB", size, m.BG).save(os.path.join(dest, "bg.png")); print("background: plain (", e, ")")
PY

if command -v plymouth-set-default-theme >/dev/null; then
  plymouth-set-default-theme -R whitecube     # -R rebuilds the initramfs
else
  echo "plymouth-set-default-theme not found; install plymouth first" >&2; exit 1
fi
echo "Installed. Preview without rebooting is not possible from a Wayland session; reboot to see it."
echo "Revert: sudo plymouth-set-default-theme -R <previous theme>   (see: plymouth-set-default-theme --list)"
