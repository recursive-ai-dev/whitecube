#!/bin/bash
# Installs the staged Whitecube network files into a root filesystem, with a checksummed backup
# and a rollback. Dry run unless --apply is given.
#
#   net/apply.sh [--root DIR]                  show what would change (default root: /)
#   sudo net/apply.sh [--root DIR] --apply     back up, then install
#   sudo net/apply.sh [--root DIR] --rollback /var/backups/whitecube/net-YYYYmmdd-HHMMSS [--force]
#
# Installs exactly the five files below. It never touches sudoers, ufw, systemd masks, sysctl,
# kernel modules or packages, never reads or rewrites saved networks (networks.json, wpa.conf)
# or profiles, and never starts, stops or restarts a service: restarting Wi-Fi is yours to do
# (the commands are printed).
set -euo pipefail

here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
project=$(dirname -- "$here")
root=/
apply=0
rollback=
force=0

# source (relative to the project)   destination (relative to the root)               mode
FILES=(
  "net/cube-net|usr/local/bin/cube-net|0755"
  "net/cube-net-dns|usr/local/libexec/cube-net-dns|0755"
  "net/dhcpcd.conf|etc/whitecube/network/dhcpcd.conf|0644"
  "net/whitecube-network.service|etc/systemd/system/whitecube-network.service|0644"
  "cube-settings-helper|usr/local/libexec/cube-settings-helper|0755"
)

die() { printf 'apply.sh: %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case $1 in
    --root) root=${2:?--root needs a directory}; shift 2 ;;
    --apply) apply=1; shift ;;
    --rollback) rollback=${2:?--rollback needs a backup directory}; shift 2 ;;
    --force) force=1; shift ;;
    -h|--help) sed -n '2,12p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done
root=$(realpath -- "$root")
[[ -d $root/usr/local/bin && -d $root/etc/systemd/system ]] || die "$root does not look like an installed system root"

sha() { sha256sum -- "$1" | cut -d' ' -f1; }
parse() { IFS='|' read -r src dst mode <<<"$1"; }

check_sources() {
  local entry src dst mode
  for entry in "${FILES[@]}"; do
    parse "$entry"
    [[ -f $project/$src ]] || die "missing $project/$src"
  done
  for entry in net/cube-net net/cube-net-dns cube-settings-helper; do
    python3 -I -c 'import ast,sys; ast.parse(open(sys.argv[1]).read(), feature_version=(3, 13))' "$project/$entry" \
      || die "$entry does not parse as Python 3.13"
  done
  bash -n "${BASH_SOURCE[0]}"
}

report() {  # prints the plan; sets $changes
  local entry src dst mode state
  changes=0
  printf 'root: %s\n' "$root"
  for entry in "${FILES[@]}"; do
    parse "$entry"
    if [[ ! -e $root/$dst ]]; then state=new
    elif [[ $(sha "$project/$src") == "$(sha "$root/$dst")" ]]; then state=same
    else state=changed; fi
    [[ $state == same ]] || changes=$((changes + 1))
    printf '  %-8s %s -> /%s (%s root:root)\n' "$state" "$src" "$dst" "$mode"
  done
}

do_apply() {
  [[ $EUID -eq 0 ]] || die "--apply needs root (files are installed root:root)"
  local stamp backup entry src dst mode tmp
  stamp=$(date +%Y%m%d-%H%M%S)
  backup=$root/var/backups/whitecube/net-$stamp
  mkdir -p -m 0700 "$backup" 2>/dev/null || die "cannot create $backup (is $root mounted read-only? remount it read-write first)"
  : >"$backup/BEFORE.sha256"; : >"$backup/INSTALLED.sha256"
  for entry in "${FILES[@]}"; do
    parse "$entry"
    if [[ -e $root/$dst ]]; then
      mkdir -p -m 0700 "$backup/files/$(dirname -- "$dst")"
      cp -p -- "$root/$dst" "$backup/files/$dst"
      printf '%s  %s\n' "$(sha "$root/$dst")" "$dst" >>"$backup/BEFORE.sha256"
    else
      printf '%s  %s\n' "-" "$dst" >>"$backup/BEFORE.sha256"       # did not exist: rollback removes it
    fi
  done
  for entry in "${FILES[@]}"; do
    parse "$entry"
    tmp=$root/$dst.new-$$
    install -o root -g root -m "$mode" -- "$project/$src" "$tmp"
    mv -f -- "$tmp" "$root/$dst"
    [[ $(sha "$root/$dst") == "$(sha "$project/$src")" ]] || die "verification failed for /$dst (backup kept in $backup)"
    printf '%s  %s\n' "$(sha "$root/$dst")" "$dst" >>"$backup/INSTALLED.sha256"
  done
  printf 'installed %d file(s). Backup: %s\n' "${#FILES[@]}" "$backup"
  printf 'Roll back with: sudo %s --root %s --rollback %s\n' "$here/apply.sh" "$root" "${backup#"$root"}"
  next_steps
}

do_rollback() {
  [[ $EUID -eq 0 ]] || die "--rollback needs root"
  local backup=$rollback entry src dst mode before installed current
  [[ $backup == /* && -d $root$backup ]] && backup=$root$backup
  [[ -f $backup/BEFORE.sha256 && -f $backup/INSTALLED.sha256 ]] || die "$backup is not an apply.sh backup"
  # Refuse to erase edits made after installation unless --force.
  while read -r installed dst; do
    current=-; [[ -e $root/$dst ]] && current=$(sha "$root/$dst")
    if [[ $current != "$installed" && $force -eq 0 ]]; then
      die "/$dst changed since it was installed; inspect it, or add --force to roll back anyway"
    fi
  done <"$backup/INSTALLED.sha256"
  while read -r before dst; do
    if [[ $before == - ]]; then
      rm -f -- "$root/$dst"; printf '  removed  /%s (it did not exist before)\n' "$dst"
    else
      tmp=$root/$dst.new-$$
      cp -p -- "$backup/files/$dst" "$tmp"; mv -f -- "$tmp" "$root/$dst"
      [[ $(sha "$root/$dst") == "$before" ]] || die "restore check failed for /$dst"
      printf '  restored /%s\n' "$dst"
    fi
  done <"$backup/BEFORE.sha256"
  next_steps
}

next_steps() {
  if [[ $root == / ]]; then
    cat <<'EOF'

Next (restarting drops Wi-Fi for about 20 seconds, so do it when that is fine):
  sudo systemctl daemon-reload
  sudo systemctl restart whitecube-network.service
  journalctl -u whitecube-network.service -f      # expect "wifi_state ...", "lease ...", "checks: ..." lines
  cube-net status
EOF
  else
    cat <<EOF

$root is not the running system, so nothing was reloaded or restarted. After booting it:
  sudo systemctl daemon-reload
  sudo systemctl restart whitecube-network.service   # or simply reboot
  journalctl -u whitecube-network.service -b
  cube-net status
EOF
  fi
}

check_sources
if [[ -n $rollback ]]; then
  do_rollback
elif [[ $apply -eq 1 ]]; then
  report
  if [[ $changes -eq 0 ]]; then echo "nothing to do: every file is already installed"; exit 0; fi
  do_apply
else
  report
  if [[ $changes -eq 0 ]]; then echo "nothing to do: every file is already installed"
  else printf '\nDry run. Install with: sudo %s --root %s --apply\n' "$here/apply.sh" "$root"; fi
fi
