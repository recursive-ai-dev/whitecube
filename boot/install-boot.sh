#!/bin/sh
# Install or inspect the whitecube boot theme (GRUB + Plymouth + optional tty1 banner).
# Backs up originals to /var/backups/whitecube-boot. Undo with: sudo ./install-boot.sh --revert
set -eu
VERSION="0.12.0"
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
root=${WC_BOOT_ROOT:-}
bk="$root/var/backups/whitecube-boot"
pd="$root/usr/share/plymouth/themes/whitecube"
gd="$root/boot/grub/themes/whitecube"
grub_default="$root/etc/default/grub"
issue_file="$root/etc/issue"
font_dir="$root/usr/share/fonts/truetype/dejavu"

mode=install
dry_run=0
json_out=0
width=1920
height=1080
scale=1
timeout=""
update_issue=1
purge=0

usage() {
  cat <<'EOF'
Usage: install-boot.sh [OPTIONS]

Options:
  --status            Inspect installed boot theme, GRUB setting, and backups (no root needed)
  --json              Emit JSON with --status
  -n, --dry-run       Show planned changes without modifying files
  --revert            Restore backed-up GRUB, /etc/issue, and Plymouth settings
  --purge             With --revert, also remove installed whitecube GRUB/Plymouth theme dirs
  --resolution WxH    Background resolution (default: 1920x1080)
  --scale N           Plymouth HiDPI sprite scale 1..4 (default: 1)
  --timeout SEC       Set GRUB_TIMEOUT (default: preserve existing value or set 5 if missing)
  --no-issue          Do not modify /etc/issue banner
  -V, --version       Print version and exit
  -h, --help          Show this help and exit
EOF
}

die() {
  echo "install-boot: $*" >&2
  exit 1
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --revert) mode=revert ;;
    --status) mode=status ;;
    --json) json_out=1 ;;
    -n|--dry-run) dry_run=1 ;;
    --purge) purge=1 ;;
    --no-issue) update_issue=0 ;;
    --resolution)
      [ "$#" -ge 2 ] || die "--resolution requires WxH"
      case "$2" in
        *x*) width=${2%x*}; height=${2#*x} ;;
        *) die "invalid resolution '$2' (expected WxH)" ;;
      esac
      shift
      ;;
    --scale)
      [ "$#" -ge 2 ] || die "--scale requires 1..4"
      scale=$2
      shift
      ;;
    --timeout)
      [ "$#" -ge 2 ] || die "--timeout requires seconds"
      timeout=$2
      shift
      ;;
    -V|--version) echo "whitecube-install-boot $VERSION"; exit 0 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
  shift
done

case "$width$height" in *[!0-9]*|"") die "resolution must be numeric WxH" ;; esac
[ "$width" -ge 320 ] && [ "$width" -le 7680 ] || die "width out of bounds: $width"
[ "$height" -ge 240 ] && [ "$height" -le 4320 ] || die "height out of bounds: $height"
case "$scale" in 1|2|3|4) ;; *) die "scale must be 1..4, got '$scale'" ;; esac
if [ -n "$timeout" ]; then
  case "$timeout" in *[!0-9]*|"") die "--timeout must be a non-negative integer" ;; esac
fi

if [ "$mode" = status ]; then
  ply_present=no; ply_bool=false; [ -f "$pd/whitecube.plymouth" ] && { ply_present=yes; ply_bool=true; }
  grub_present=no; grub_bool=false; [ -f "$gd/theme.txt" ] && { grub_present=yes; grub_bool=true; }
  backup_present=no; backup_bool=false; [ -f "$bk/grub.default" ] && { backup_present=yes; backup_bool=true; }
  grub_line="(missing)"
  if [ -f "$grub_default" ]; then
    grub_line=$(grep -m1 '^GRUB_THEME=' "$grub_default" || echo "(unset)")
  fi
  ply_active="unknown"
  if command -v plymouth-set-default-theme >/dev/null 2>&1; then
    ply_active=$(plymouth-set-default-theme 2>/dev/null || echo "unknown")
  fi
  if [ "$json_out" -eq 1 ]; then
    WC_GL="$grub_line" WC_PA="$ply_active" python3 -I -c '
import json, os, sys
print(json.dumps({
    "plymouth_theme_installed": sys.argv[1] == "true",
    "grub_theme_installed": sys.argv[2] == "true",
    "backup_present": sys.argv[3] == "true",
    "grub_theme_config": os.environ["WC_GL"],
    "plymouth_active": os.environ["WC_PA"],
}, indent=2))' "$ply_bool" "$grub_bool" "$backup_bool"
  else
    printf 'plymouth_theme_installed=%s\ngrub_theme_installed=%s\ngrub_theme_config=%s\nplymouth_active=%s\nbackup_present=%s\n' \
      "$ply_present" "$grub_present" "$grub_line" "$ply_active" "$backup_present"
  fi
  exit 0
fi

set_var() {
  key=$1 val=$2 file=$3
  tmp=$(mktemp "$(dirname -- "$file")/.grub.XXXXXX")
  trap 'rm -f "$tmp"' EXIT
  WC_VAR_KEY="$key" WC_VAR_VAL="$val" awk '
    BEGIN { k = ENVIRON["WC_VAR_KEY"]; v = ENVIRON["WC_VAR_VAL"]; done = 0 }
    index($0, k "=") == 1 {
      if (!done) { print k "=" v; done = 1 }
      next
    }
    { print }
    END { if (!done) print k "=" v }
  ' "$file" > "$tmp"
  chmod 0644 "$tmp"
  mv -f "$tmp" "$file"
  trap - EXIT
}

if [ "$dry_run" -eq 1 ]; then
  if [ "$mode" = revert ]; then
    echo "[dry-run] would restore backups from $bk, set previous Plymouth theme, run update-grub, purge=$purge"
  else
    echo "[dry-run] would render ${width}x${height} (scale=$scale), install Plymouth ($pd) & GRUB ($gd), update $grub_default (timeout=${timeout:-preserve}), issue=$update_issue"
  fi
  exit 0
fi

[ "$(id -u)" -eq 0 ] || [ -n "$root" ] || die "run with sudo"

if [ "$mode" = revert ]; then
  [ -f "$bk/grub.default" ] && cp -f "$bk/grub.default" "$grub_default"
  if [ -f "$bk/issue" ]; then
    cp -f "$bk/issue" "$issue_file"
  elif [ -f "$bk/issue.absent" ]; then
    rm -f "$issue_file"
  fi
  if [ -f "$bk/plymouth-theme" ] && command -v plymouth-set-default-theme >/dev/null 2>&1; then
    plymouth-set-default-theme -R "$(cat "$bk/plymouth-theme")"
  fi
  if command -v update-grub >/dev/null 2>&1; then
    update-grub
  fi
  if [ "$purge" -eq 1 ]; then
    rm -rf "$gd" "$pd"
  fi
  echo "reverted"
  exit 0
fi

for cmd in python3 grub-mkfont update-grub plymouth-set-default-theme; do
  command -v "$cmd" >/dev/null 2>&1 || die "required tool not found: $cmd"
done
python3 -I -c "import PIL.Image, PIL.ImageDraw" 2>/dev/null || die "Pillow (python3-pil) is required"
[ -f "$font_dir/DejaVuSansMono.ttf" ] && [ -f "$font_dir/DejaVuSansMono-Bold.ttf" ] || die "DejaVu fonts missing in $font_dir"

stage=$(mktemp -d "${TMPDIR:-/tmp}/wc-boot.XXXXXX")
trap 'rm -rf "$stage"' EXIT
python3 -I "$here/gen-boot-theme.py" "$stage" "$width" "$height" --scale "$scale"

mkdir -p "$bk" "$(dirname -- "$grub_default")"
if [ -f "$grub_default" ] && [ ! -f "$bk/grub.default" ]; then
  cp -a "$grub_default" "$bk/grub.default"
elif [ ! -f "$grub_default" ]; then
  : > "$grub_default"
fi
if [ "$update_issue" -eq 1 ]; then
  if [ -f "$issue_file" ] && [ ! -f "$bk/issue" ] && [ ! -f "$bk/issue.absent" ]; then
    cp -a "$issue_file" "$bk/issue"
  elif [ ! -f "$issue_file" ] && [ ! -f "$bk/issue" ] && [ ! -f "$bk/issue.absent" ]; then
    : > "$bk/issue.absent"
  fi
fi
[ -f "$bk/plymouth-theme" ] || plymouth-set-default-theme > "$bk/plymouth-theme"

# --- Plymouth ---
install -d "$pd"
install -m644 "$stage"/plymouth/*.png "$here/whitecube.script" "$here/whitecube.plymouth" "$pd/"

# --- GRUB ---
install -d "$gd"
install -m644 "$stage"/grub/*.png "$here/theme.txt" "$gd/"
mk() { grub-mkfont -s "$1" -o "$gd/$2" "$3"; }
mk 12 dejavu-mono-12.pf2 "$font_dir/DejaVuSansMono.ttf"
mk 14 dejavu-mono-14.pf2 "$font_dir/DejaVuSansMono.ttf"
mk 14 dejavu-mono-bold-14.pf2 "$font_dir/DejaVuSansMono-Bold.ttf"
mk 18 dejavu-mono-bold-18.pf2 "$font_dir/DejaVuSansMono-Bold.ttf"
chmod 644 "$gd"/*.pf2

set_var GRUB_THEME '"/boot/grub/themes/whitecube/theme.txt"' "$grub_default"
set_var GRUB_GFXMODE "${width}x${height},auto" "$grub_default"
set_var GRUB_GFXPAYLOAD_LINUX keep "$grub_default"
if [ -n "$timeout" ]; then
  set_var GRUB_TIMEOUT "$timeout" "$grub_default"
elif ! grep -q '^GRUB_TIMEOUT=' "$grub_default"; then
  set_var GRUB_TIMEOUT 5 "$grub_default"
fi
update-grub

# --- tty1 banner (optional; original backed up) ---
if [ "$update_issue" -eq 1 ]; then
  tmp_issue=$(mktemp "$(dirname -- "$issue_file")/.issue.XXXXXX")
  printf 'whitecube\n\n' > "$tmp_issue"
  chmod 0644 "$tmp_issue"
  mv -f "$tmp_issue" "$issue_file"
fi

plymouth-set-default-theme -R whitecube
echo "installed. originals are in $bk; reboot to see it."
