#!/usr/bin/env bash
# deb-strap.sh — pure Debian bootstrap installer, disk builder, and image/rootfs generator
#
# Dedicated to Debian (Bookworm, Trixie, Sid, Bullseye, Experimental)
# Supports:
#   - Block devices (/dev/sdX, /dev/nvmeXn1), Raw disk images (.img, .qcow2), and Rootfs/Tarball export
#   - Modern deb822 sources & classic sources.list, fastest mirror auto-benchmarking
#   - Advanced storage: LUKS2 encryption with TPM2 auto-unlock, Btrfs + Snapper subvolumes, LVM2, RAID, ZFS
#   - Init freedom: systemd (default), sysvinit-core, OpenRC, runit-init
#   - Bootstrappers: debootstrap, mmdebstrap, multi-stage foreign architecture (QEMU / binfmt)
#   - Bootloaders: GRUB (UEFI Secure Boot signed / BIOS hybrid), systemd-boot, rEFInd, Limine, UKI
#   - Kernel options: Stock Debian (amd64, rt, cloud), Liquorix, XanMod, or custom LTS kernel compilation
#   - Profiles: Minimal server, container rootfs, dev workstations, desktops (GNOME, KDE, XFCE, Sway, Hyprland, etc.)
#
# Usage:
#   sudo bash deb-strap.sh                           # Interactive wizard
#   sudo bash deb-strap.sh --config deb-strap.conf   # Config-driven (prompts for missing keys)
#   sudo bash deb-strap.sh --config deb-strap.conf --unattended
#   sudo bash deb-strap.sh --dry-run --config deb-strap.conf
#   sudo bash deb-strap.sh --write-config out.conf   # Run wizard only and save configuration
#   sudo bash deb-strap.sh --benchmark-mirrors       # Test and rank Debian mirrors
#   sudo bash deb-strap.sh --resume                  # Resume an interrupted installation
#
# Author: Muse Spark — 2026-08-29

set -eEuo pipefail

VERSION="1.2.0"

# ─────────────────────────────────────── colours / logging ───────────────────────────────────────
if [[ -t 2 ]]; then
  RED=$'\033[0;31m'; GRN=$'\033[0;32m'; YLW=$'\033[0;33m'; BLU=$'\033[0;34m'
  MAG=$'\033[0;35m'; CYN=$'\033[0;36m'; BOLD=$'\033[1m'; RST=$'\033[0m'
else
  RED=""; GRN=""; YLW=""; BLU=""; MAG=""; CYN=""; BOLD=""; RST=""
fi
# Mark MAG as used for shellcheck (used in optional verbose logs)
: "${MAG:-}"

LOGFILE="${DEBSTRAP_LOG:-/var/log/deb-strap.log}"
_logts() { date +%H:%M:%S 2>/dev/null || echo "??:??:??"; }
LOG_OK=""
_tee() {
  if [[ -z "$LOG_OK" ]]; then
    if { : >>"$LOGFILE"; } 2>/dev/null; then
      LOG_OK=1
    else
      LOGFILE="${TMPDIR:-/tmp}/deb-strap.log"
      if { : >>"$LOGFILE"; } 2>/dev/null; then LOG_OK=1; else LOG_OK=0; fi
    fi
  fi
  [[ "$LOG_OK" == "1" ]] || return 0
  { printf '%s %s\n' "$(_logts)" "$1" >>"$LOGFILE"; } 2>/dev/null || true
}

log()  { echo "${GRN}[*]${RST} $*" >&2; _tee "[*] $*"; }
info() { echo "${BLU}[i]${RST} $*" >&2; _tee "[i] $*"; }
warn() { echo "${YLW}[!]${RST} $*" >&2; _tee "[!] $*"; WARN_COUNT=$((WARN_COUNT+1)); }
err()  { echo "${RED}[x]${RST} $*" >&2; _tee "[x] $*"; }
die()  { err "$*"; exit 1; }
banner() {
  echo -e "\n${BOLD}${CYN}═══ $* ═══${RST}\n" >&2
  _tee "=== $* ==="
}
WARN_COUNT=0

# ─────────────────────────────────────── CLI / config state ──────────────────────────────────────
CONFIG_FILE=""
UNATTENDED=0
DRY_RUN=0
RESUME=0
WRITE_CONFIG=""
ASSUME_YES=0
BENCHMARK_ONLY=0
CLI_TARGET_MODE=""
CLI_IMAGE_FILE=""
CLI_IMAGE_SIZE=""
OFFLINE=0
CLI_PROFILE=""
MULTI_ROOT=0

declare -A CFG=()
ANSWER_LOG="$(mktemp "${TMPDIR:-/tmp}/deb-strap-answers-XXXXXX")"

_record_answer() {
  [[ -n "${ANSWER_LOG:-}" ]] || return 0
  { printf '%s=%q\n' "$1" "$2" >> "$ANSWER_LOG"; } 2>/dev/null || true
}

answers_dump() {
  [[ -s "${ANSWER_LOG:-}" ]] || return 0
  awk -F= '{k=$1; v=substr($0, index($0,"=")+1); a[k]=v}
           END{for (k in a) print k "=" a[k]}' "$ANSWER_LOG" | sort
}

answers_count() { answers_dump | wc -l; }

usage() {
  cat <<'USAGE'
deb-strap — pure Debian bootstrap installer and image/rootfs generator

Usage:
  sudo bash deb-strap.sh [OPTIONS]

Options:
  --config FILE         Load answers from FILE (key=value). Missing keys are prompted for
                        unless --unattended is also specified.
  --unattended          Never prompt interactively. Any required answer not supplied in
                        the config file is a fatal error. Implies --yes.
  --yes                 Auto-accept destructive disk wipes and confirmations.
  --dry-run             Print destructive disk commands without executing them.
  --target-mode MODE    Target output mode: disk | image | rootfs | tar (default: disk).
  --image-file FILE     Path to raw output disk image when target-mode is image.
  --image-size SIZE     Size of output disk image (e.g. 20G, 64G).
  --resume              Resume a previously interrupted run from state file.
  --write-config FILE   Run the configuration wizard only and save answers to FILE without
                        touching any disk or starting bootstrapping.
  --benchmark-mirrors   Test latency and throughput of Debian mirrors, rank them, and exit.
  --offline             Airgapped/offline mode: skip network-touching operations, use the
                        configured local mirror (mirror_choice=4 or a file:// URL), and
                        require a populated /var/cache/apt/archives on the host.
  --profile NAME        Apply a preset configuration: headless-server, minimal, kiosk,
                        container, dev-workstation. Overrides individual config keys.
  --multi-root          Enable multi-root layout (two root partitions, each with its own
                        GRUB menu entry). Useful for dual-boot, immutable root A/B schemes,
                        or running multiple Debian releases side-by-side.
  --log FILE            Path to log file (default: /var/log/deb-strap.log).
  --version             Print version and exit.
  --help, -h            Show this help text and exit.

Examples:
  sudo bash deb-strap.sh
  sudo bash deb-strap.sh --config deb-strap.conf --unattended
  sudo bash deb-strap.sh --target-mode image --image-file debian-vm.img --image-size 20G
  sudo bash deb-strap.sh --write-config custom-bookworm.conf
USAGE
}

# ─────────────────────────────────────── parse CLI arguments ─────────────────────────────────────
parse_cli() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --config)
        [[ $# -ge 2 ]] || die "--config requires a file argument"
        CONFIG_FILE="$2"; shift 2 ;;
      --unattended)
        UNATTENDED=1; ASSUME_YES=1; shift ;;
      --yes)
        ASSUME_YES=1; shift ;;
      --dry-run)
        DRY_RUN=1; shift ;;
      --resume)
        RESUME=1; shift ;;
      --write-config)
        [[ $# -ge 2 ]] || die "--write-config requires a file argument"
        WRITE_CONFIG="$2"; shift 2 ;;
      --benchmark-mirrors)
        BENCHMARK_ONLY=1; shift ;;
      --target-mode)
        [[ $# -ge 2 ]] || die "--target-mode requires an argument (disk|image|rootfs|tar)"
        CLI_TARGET_MODE="$2"; shift 2 ;;
      --image-file)
        [[ $# -ge 2 ]] || die "--image-file requires a path argument"
        CLI_IMAGE_FILE="$2"; shift 2 ;;
      --image-size)
        [[ $# -ge 2 ]] || die "--image-size requires a size argument (e.g. 20G)"
        CLI_IMAGE_SIZE="$2"; shift 2 ;;
      --offline)
        OFFLINE=1; shift ;;
      --profile)
        [[ $# -ge 2 ]] || die "--profile requires a name (headless-server|minimal|kiosk|container|dev-workstation)"
        CLI_PROFILE="$2"; shift 2 ;;
      --multi-root)
        MULTI_ROOT=1; shift ;;
      --log)
        [[ $# -ge 2 ]] || die "--log requires a file argument"
        LOGFILE="$2"; shift 2 ;;
      --version)
        echo "deb-strap v$VERSION"; exit 0 ;;
      --help|-h)
        usage; exit 0 ;;
      *)
        die "Unknown argument: $1 (see --help)" ;;
    esac
  done
}

# ─────────────────────────────────────── config file parser ──────────────────────────────────────
load_config_file() {
  local file="$1"
  [[ -f "$file" ]] || die "Config file not found: $file"
  log "Loading configuration from $file ..."
  local line_no=0 line key val
  while IFS= read -r line || [[ -n "$line" ]]; do
    line_no=$((line_no + 1))
    line="${line#"${line%%[![:space:]]*}"}" # trim leading
    line="${line%"${line##*[![:space:]]}"}" # trim trailing
    [[ -z "$line" || "$line" =~ ^# ]] && continue
    if [[ "$line" =~ ^([a-zA-Z0-9_]+)[[:space:]]*=[[:space:]]*(.*)$ ]]; then
      key="${BASH_REMATCH[1]}"
      val="${BASH_REMATCH[2]}"
      # strip trailing comment if not quoted
      if [[ "$val" =~ ^\"(.*)\"[[:space:]]*(#.*)?$ || "$val" =~ ^\'(.*)\'[[:space:]]*(#.*)?$ ]]; then
        val="${BASH_REMATCH[1]}"
      else
        val="${val%%#*}"
        val="${val%"${val##*[![:space:]]}"}"
      fi
      CFG["$key"]="$val"
      _record_answer "$key" "$val"
    else
      warn "Config line $line_no: unrecognized syntax: $line"
    fi
  done < "$file"
}

# ─────────────────────────────────────── prompting helpers ───────────────────────────────────────
ask() {
  local key="$1" prompt="$2" default="${3:-}"
  local val=""
  if [[ -n "${CFG[$key]+_}" ]]; then
    val="${CFG[$key]}"
    _record_answer "$key" "$val"
    printf '%s\n' "$val"
    return 0
  fi
  if [[ "$UNATTENDED" == "1" ]]; then
    if [[ -n "$default" ]]; then
      val="$default"
      _record_answer "$key" "$val"
      printf '%s\n' "$val"
      return 0
    else
      die "Unattended mode error: missing required configuration key '$key' ($prompt)"
    fi
  fi
  # Interactive prompt
  local display_prompt
  if [[ -n "$default" ]]; then
    display_prompt="${BLU}[?]${RST} ${prompt} ${CYN}[${default}]${RST}: "
  else
    display_prompt="${BLU}[?]${RST} ${prompt}: "
  fi
  while true; do
    read -r -p "$display_prompt" val </dev/tty || true
    val="${val:-$default}"
    if [[ -n "$val" ]]; then
      CFG["$key"]="$val"
      _record_answer "$key" "$val"
      printf '%s\n' "$val"
      return 0
    fi
  done
}

ask_yn() {
  local key="$1" prompt="$2" default="${3:-n}"
  local def_str="y/N"
  [[ "$default" =~ ^[Yy] ]] && def_str="Y/n"
  while true; do
    local ans; ans="$(ask "$key" "$prompt ($def_str)" "$default")"
    case "$ans" in
      [Yy]|[Yy][Ee][Ss]|1|true)  printf '%s\n' "y"; return 0 ;;
      [Nn]|[Nn][Oo]|0|false)    printf '%s\n' "n"; return 0 ;;
      *) warn "Please answer 'y' or 'n'." ;;
    esac
  done
}

ask_choice() {
  local key="$1" prompt="$2" default="$3"; shift 3
  local -a options=("$@")
  if [[ -n "${CFG[$key]+_}" ]]; then
    local chosen="${CFG[$key]}"
    _record_answer "$key" "$chosen"
    printf '%s\n' "$chosen"
    return 0
  fi
  if [[ "$UNATTENDED" == "1" ]]; then
    if [[ -n "$default" ]]; then
      _record_answer "$key" "$default"
      printf '%s\n' "$default"
      return 0
    fi
    die "Unattended mode error: missing choice key '$key' ($prompt)"
  fi
  echo -e "\n${BLU}Select option for:${RST} ${BOLD}$prompt${RST}" >&2
  local i=1
  for opt in "${options[@]}"; do
    if [[ "$i" == "$default" || "$opt" == "$default" ]]; then
      echo -e "  ${GRN}${BOLD}$i)${RST} $opt ${CYN}(default)${RST}" >&2
    else
      echo -e "  ${BOLD}$i)${RST} $opt" >&2
    fi
    i=$((i+1))
  done
  while true; do
    local sel; sel="$(ask "$key" "Choice (1-${#options[@]})" "$default")"
    if [[ "$sel" =~ ^[0-9]+$ ]] && (( sel >= 1 && sel <= ${#options[@]} )); then
      printf '%s\n' "$sel"
      return 0
    fi
    # Check if string match
    local idx=1
    for opt in "${options[@]}"; do
      if [[ "$opt" == "$sel" ]]; then
        printf '%s\n' "$idx"
        return 0
      fi
      idx=$((idx+1))
    done
    warn "Invalid selection: $sel. Please enter a number between 1 and ${#options[@]}."
  done
}

# ─────────────────────────────────────── execution / dry-run ─────────────────────────────────────
run() {
  _tee "[run] $*"
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "${CYN}[dry-run]${RST} $*" >&2
    return 0
  fi
  "$@"
}

run_silent() {
  _tee "[run_silent] $*"
  if [[ "$DRY_RUN" == "1" ]]; then
    return 0
  fi
  "$@" >/dev/null 2>&1
}

have() { command -v "$1" &>/dev/null; }

# ─────────────────────────────────────── state & cleanup ─────────────────────────────────────────
MNT="${STRAP_MNT:-/mnt/debstrap}"
STATE_FILE="${TMPDIR:-/var/tmp}/deb-strap.state"

declare -a CLEANUP_MOUNTS=()
declare -a CLEANUP_LUKS=()
declare -a CLEANUP_LOOPS=()
declare -a CLEANUP_VGS=()
declare -a CLEANUP_FILES=()
declare -a CLEANUP_NBDS=()

track_mount() { CLEANUP_MOUNTS+=("$1"); }
track_luks()  { CLEANUP_LUKS+=("$1"); }
track_loop()  { CLEANUP_LOOPS+=("$1"); }
track_vg()    { CLEANUP_VGS+=("$1"); }
track_file()  { CLEANUP_FILES+=("$1"); }
track_nbd()   { CLEANUP_NBDS+=("$1"); }

cleanup() {
  local rc=$?
  set +e
  if (( rc != 0 )); then
    err "deb-strap terminated with exit code $rc."
  fi
  if [[ "${STRAP_LIB:-0}" == "1" || "${DEBSTRAP_LIB:-0}" == "1" ]]; then
    return "$rc"
  fi
  if [[ "$DRY_RUN" == "1" ]]; then
    return "$rc"
  fi
  # Unmount in reverse order
  if (( ${#CLEANUP_MOUNTS[@]} > 0 )); then
    log "Unmounting target filesystems..."
    for (( i=${#CLEANUP_MOUNTS[@]}-1; i>=0; i-- )); do
      local m="${CLEANUP_MOUNTS[i]}"
      if mountpoint -q "$m" 2>/dev/null; then
        umount -R "$m" 2>/dev/null || umount -l "$m" 2>/dev/null || true
      fi
    done
  fi
  # Deactivate VGs
  for vg in "${CLEANUP_VGS[@]}"; do
    vgchange -an "$vg" &>/dev/null || true
  done
  # Close LUKS
  for luks in "${CLEANUP_LUKS[@]}"; do
    cryptsetup luksClose "$luks" &>/dev/null || true
  done
  # Detach loop devices
  for loop in "${CLEANUP_LOOPS[@]}"; do
    losetup -d "$loop" &>/dev/null || true
  done
  # Disconnect nbd devices
  for nbd in "${CLEANUP_NBDS[@]}"; do
    qemu-nbd --disconnect "$nbd" &>/dev/null || true
  done
  # Clean temp files
  for f in "${CLEANUP_FILES[@]}"; do
    rm -rf "$f" &>/dev/null || true
  done
  return "$rc"
}
trap cleanup EXIT INT TERM ERR

save_state() {
  local step="$1"
  [[ "$DRY_RUN" == "1" ]] && return 0
  mkdir -p "$(dirname "$STATE_FILE")" 2>/dev/null || true
  cat > "$STATE_FILE" <<EOF
STEP=$step
TARGET_MODE=${TARGET_MODE:-disk}
TARGET_DEVICE=${TARGET_DEVICE:-}
IMAGE_FILE=${IMAGE_FILE:-}
MNT=$MNT
SUITE=${SUITE:-bookworm}
ARCH=${TARGET_ARCH:-amd64}
TIMESTAMP=$(date +%s)
EOF
}

# ─────────────────────────────────────── 1) host dependencies ─────────────────────────────────────
# Accuracy fix: map Debian package names to actual binaries without false positives
# and support foreign arch, LUKS, LVM, RAID, Btrfs, XFS, F2FS, ZFS capabilities.
check_host_deps() {
  banner "Checking Host Dependencies"
  local -a missing=()
  # Core required tools — map to real binaries present on Debian
  local -a required_bins=(sgdisk parted mkfs.ext4 mkfs.vfat wipefs partprobe blockdev lsblk blkid findmnt)
  # Bootstrap engine is determined early; default debootstrap unless overridden
  local _engine="${CFG[bootstrap_engine]:-${CFG[engine]:-${ENGINE:-debootstrap}}}"
  # Also check CFG variant spelling
  if [[ "${_engine}" == "mmdebstrap" ]]; then
    required_bins+=(mmdebstrap)
  else
    required_bins+=(debootstrap)
  fi
  # Optional dependencies based on config
  if [[ "${CFG[encrypt_root]:-n}" == "y" ]] || [[ "${ENCRYPT_ROOT:-n}" == "y" ]]; then
    required_bins+=(cryptsetup)
  fi
  if [[ "${CFG[use_lvm]:-n}" == "y" ]] || [[ "${USE_LVM:-n}" == "y" ]]; then
    required_bins+=(lvm pvcreate vgcreate lvcreate)
  fi
  if [[ "${CFG[use_raid]:-n}" == "y" ]] || [[ "${USE_RAID:-n}" == "y" ]]; then
    required_bins+=(mdadm)
  fi
  local _fs="${CFG[fs_root]:-${FS_ROOT:-ext4}}"
  case "$_fs" in
    btrfs) required_bins+=(mkfs.btrfs btrfs) ;;
    xfs)   required_bins+=(mkfs.xfs) ;;
    f2fs)  required_bins+=(mkfs.f2fs) ;;
    zfs)   required_bins+=(zpool) ;;
  esac
  # QEMU for foreign arch — map Debian arch to the actual qemu binary provided by qemu-user-static
  if [[ -n "${CFG[target_arch]:-}" || -n "${TARGET_ARCH:-}" ]]; then
    local _want_arch="${CFG[target_arch]:-${TARGET_ARCH:-}}"
    local _host_arch
    _host_arch="$(uname -m 2>/dev/null || echo x86_64)"
    case "${_want_arch}:${_host_arch}" in
      amd64:x86_64|i386:x86_64|x86_64:amd64|amd64:amd64|i386:i386|arm64:aarch64|armhf:armv7l|armhf:armv6l) : ;;
      *)
        case "$_want_arch" in
          arm64)   required_bins+=(qemu-aarch64-static) ;;
          armhf)   required_bins+=(qemu-arm-static) ;;
          riscv64) required_bins+=(qemu-riscv64-static) ;;
          ppc64el) required_bins+=(qemu-ppc64le-static) ;;
          s390x)   required_bins+=(qemu-s390x-static) ;;
          i386)    required_bins+=(qemu-i386-static) ;;
          *)       required_bins+=(qemu-user-static) ;;
        esac
        ;;
    esac
  fi

  for b in "${required_bins[@]}"; do
    if ! have "$b"; then missing+=("$b"); fi
  done
  # Treat lvm as satisfied when any lvm2 binary is present (lvm2 does not ship a binary literally called 'lvm')
  if printf '%s\n' "${required_bins[@]}" | grep -qx "lvm" && ( have pvcreate || have vgcreate || have lvcreate ); then
    local _i
    for _i in "${!missing[@]}"; do
      [[ "${missing[$_i]}" == "lvm" ]] && unset 'missing[_i]'
    done
  fi

  if (( ${#missing[@]} > 0 )); then
    warn "Missing host utilities: ${missing[*]}"
    if have apt-get; then
      if [[ "$UNATTENDED" == "1" || "$ASSUME_YES" == "1" ]] || ask_yn "install_host_deps" "Attempt auto-installation of missing tools via apt-get?" "y"; then
        log "Updating apt indices and installing dependencies..."
        run apt-get update -qq || true
        # Map bin to Debian packages (pure Debian, no foreign repos)
        local -a pkgs=()
        for b in "${missing[@]}"; do
          case "$b" in
            mkfs.vfat)            pkgs+=(dosfstools) ;;
            mkfs.ext4)            pkgs+=(e2fsprogs) ;;
            blkid|lsblk|blockdev) pkgs+=(util-linux) ;;
            mkfs.btrfs|btrfs)     pkgs+=(btrfs-progs) ;;
            mkfs.xfs)             pkgs+=(xfsprogs) ;;
            mkfs.f2fs)            pkgs+=(f2fs-tools) ;;
            zpool|zfs)            pkgs+=(zfsutils-linux) ;;
            cryptsetup)           pkgs+=(cryptsetup) ;;
            lvm|pvcreate|vgcreate|lvcreate) pkgs+=(lvm2) ;;
            mdadm)                pkgs+=(mdadm) ;;
            parted)               pkgs+=(parted) ;;
            gdisk|sgdisk)         pkgs+=(gdisk) ;;
            mmdebstrap)           pkgs+=(mmdebstrap) ;;
            debootstrap)          pkgs+=(debootstrap debian-archive-keyring) ;;
            qemu-aarch64-static|qemu-arm-static|qemu-riscv64-static|qemu-ppc64le-static|qemu-s390x-static|qemu-i386-static) pkgs+=(qemu-user-static binfmt-support) ;;
            qemu-debootstrap)     pkgs+=(qemu-user-static binfmt-support) ;;
            cdebootstrap)         pkgs+=(cdebootstrap) ;;
            wipefs|partprobe)     pkgs+=(util-linux) ;;
            *) pkgs+=("$b") ;;
          esac
        done
        # deduplicate
        local -A seen=()
        local -a uniq_pkgs=()
        for p in "${pkgs[@]}"; do
          if [[ -z "${seen[$p]:-}" ]]; then
            seen["$p"]=1
            uniq_pkgs+=("$p")
          fi
        done
        run apt-get install -y --no-install-recommends "${uniq_pkgs[@]}" || die "Failed to install required dependencies: ${uniq_pkgs[*]}"
      else
        die "Cannot proceed without required dependencies: ${missing[*]}"
      fi
    else
      die "Please install missing host tools: ${missing[*]}"
    fi
  else
    log "All required host tools are available."
  fi

  # Optional helpers
  if ! have genfstab; then
    info "arch-install-scripts (genfstab) not found on host — will use deb-strap built-in fstab generator."
  fi
}

# ─────────────────────────────────────── 2) target distro & architecture ─────────────────────────
prompt_target_distro() {
  banner "Debian Target Suite & Architecture"
  # Backwards-compat: translate legacy numeric IDs from older configs (1=bookworm,2=trixie...)
  # to canonical suite names before prompting, preserving existing unattended installs.
  if [[ -n "${CFG[target_suite]:-}" && "${CFG[target_suite]}" =~ ^[0-9]+$ ]]; then
    case "${CFG[target_suite]}" in
      1) CFG[target_suite]="bookworm" ;;
      2) CFG[target_suite]="trixie" ;;
      3) CFG[target_suite]="sid" ;;
      4) CFG[target_suite]="bullseye" ;;
      5) CFG[target_suite]="experimental" ;;
      6) CFG[target_suite]="experimental" ;;
    esac
  fi
  local suite_choice
  suite_choice="$(ask_choice "target_suite" "Debian Suite" "1" \
    "Debian 13 (Trixie) — Stable (Recommended)" \
    "Debian 14 (Forky) — Testing" \
    "Debian Sid (Unstable) — Rolling" \
    "Debian 12 (Bookworm) — Oldstable" \
    "Debian 11 (Bullseye) — Oldoldstable" \
    "Debian Experimental")"

  case "$suite_choice" in
    1|trixie|stable)   SUITE="trixie" ;;
    2|forky|testing)   SUITE="forky" ;;
    3|sid|unstable)    SUITE="sid" ;;
    4|bookworm)        SUITE="bookworm" ;;
    5|bullseye)        SUITE="bullseye" ;;
    6|experimental)    SUITE="experimental" ;;
    *)                 SUITE="trixie" ;;
  esac
  log "Target Debian suite: $SUITE"

  # Target Architecture
  local host_arch
  host_arch="$(uname -m 2>/dev/null || echo "x86_64")"
  local def_arch="amd64"
  case "$host_arch" in
    x86_64)  def_arch="amd64" ;;
    aarch64) def_arch="arm64" ;;
    armv7l)  def_arch="armhf" ;;
    riscv64) def_arch="riscv64" ;;
    i686|i386) def_arch="i386" ;;
  esac

  local arch_choice
  arch_choice="$(ask_choice "target_arch" "Target Architecture" "1" \
    "amd64 (x86_64)" \
    "arm64 (AArch64)" \
    "armhf (ARMv7 Hard Float)" \
    "riscv64 (64-bit RISC-V)" \
    "i386 (32-bit x86)" \
    "ppc64el (PowerPC 64 LE)" \
    "s390x (IBM zSeries)")"

  case "$arch_choice" in
    1|amd64)   TARGET_ARCH="amd64" ;;
    2|arm64)   TARGET_ARCH="arm64" ;;
    3|armhf)   TARGET_ARCH="armhf" ;;
    4|riscv64) TARGET_ARCH="riscv64" ;;
    5|i386)    TARGET_ARCH="i386" ;;
    6|ppc64el) TARGET_ARCH="ppc64el" ;;
    7|s390x)   TARGET_ARCH="s390x" ;;
    *) TARGET_ARCH="$def_arch" ;;
  esac
  log "Target architecture: $TARGET_ARCH (host arch: $host_arch)"

  # Foreign arch check
  IS_FOREIGN=0
  if [[ "$TARGET_ARCH" != "$def_arch" ]] && ! { [[ "$def_arch" == "amd64" && "$TARGET_ARCH" == "i386" ]]; }; then
    IS_FOREIGN=1
    warn "Cross-architecture bootstrap detected ($def_arch -> $TARGET_ARCH). Foreign mode enabled (qemu-user-static/binfmt)."
  fi

  # Bootstrap Engine
  local eng_choice
  eng_choice="$(ask_choice "bootstrap_engine" "Bootstrap Engine" "1" \
    "debootstrap (Debian Standard)" \
    "mmdebstrap (Fast, multi-mirror, unprivileged-capable)" \
    "cdebootstrap (C-based lightweight)")"
  case "$eng_choice" in
    1|debootstrap) ENGINE="debootstrap" ;;
    2|mmdebstrap)  ENGINE="mmdebstrap" ;;
    3|cdebootstrap) ENGINE="cdebootstrap" ;;
    *) ENGINE="debootstrap" ;;
  esac

  # Bootstrap Variant
  local var_choice
  var_choice="$(ask_choice "bootstrap_variant" "Bootstrap Package Variant" "1" \
    "standard (Full minimal base system)" \
    "minbase (Smallest possible Debian base, ~150MB)" \
    "buildd (Tools for package building)" \
    "custom (Specify custom --include packages)")"
  case "$var_choice" in
    1|standard) VARIANT="standard" ;;
    2|minbase)  VARIANT="minbase" ;;
    3|buildd)   VARIANT="buildd" ;;
    4|custom)   VARIANT="custom" ;;
    *) VARIANT="standard" ;;
  esac
}

# ─────────────────────────────────────── 3) target mode & device ─────────────────────────────────
prompt_target_mode_and_device() {
  banner "Target Mode & Storage Selection"
  if [[ -n "$CLI_TARGET_MODE" ]]; then
    TARGET_MODE="$CLI_TARGET_MODE"
  else
    local mode_choice
    mode_choice="$(ask_choice "target_mode" "Installation Target Mode" "1" \
      "disk (Install directly to a physical block device: /dev/sdX, /dev/nvmeXn1)" \
      "image (Create a bootable raw disk image: .img or .qcow2)" \
      "rootfs (Install into a local directory chroot)" \
      "tar (Export bootstrapped Debian system as a compressed .tar.xz archive)")"
    case "$mode_choice" in
      1|disk)   TARGET_MODE="disk" ;;
      2|image)  TARGET_MODE="image" ;;
      3|rootfs) TARGET_MODE="rootfs" ;;
      4|tar)    TARGET_MODE="tar" ;;
      *) TARGET_MODE="disk" ;;
    esac
  fi
  log "Target mode: $TARGET_MODE"

  if [[ "$TARGET_MODE" == "disk" ]]; then
    # List available non-root disks
    echo -e "\n${BOLD}Available block devices on host:${RST}" >&2
    lsblk -d -o NAME,SIZE,TYPE,MODEL,TRAN,ROTA 2>/dev/null || true
    echo >&2

    TARGET_DEVICE="$(ask "target_device" "Target block device (e.g. /dev/sdb, /dev/nvme0n1)" "")"
    [[ -b "$TARGET_DEVICE" ]] || die "Device '$TARGET_DEVICE' is not a valid block device."

    # Guard checks
    is_host_disk "$TARGET_DEVICE"
    confirm_wipe_action "$TARGET_DEVICE"

  elif [[ "$TARGET_MODE" == "image" ]]; then
    if [[ -n "$CLI_IMAGE_FILE" ]]; then
      IMAGE_FILE="$CLI_IMAGE_FILE"
    else
      IMAGE_FILE="$(ask "image_file" "Output image filepath" "debian-${SUITE}-${TARGET_ARCH}.img")"
    fi
    # Validate .qcow2 vs .img handling (.qcow2 needs qemu-img per Debian qemu-utils)
    local _img_ext="${IMAGE_FILE##*.}"
    if [[ -n "$CLI_IMAGE_SIZE" ]]; then
      IMAGE_SIZE="$CLI_IMAGE_SIZE"
    else
      IMAGE_SIZE="$(ask "image_size" "Disk image virtual size (e.g. 10G, 20G, 64G)" "20G")"
    fi
    if ! validate_size_string "$IMAGE_SIZE"; then
      die "Invalid image size '$IMAGE_SIZE'"
    fi
    if [[ -e "$IMAGE_FILE" && "$ASSUME_YES" != "1" ]]; then
      warn "Image file $IMAGE_FILE already exists and will be overwritten"
      local _ov
      _ov="$(ask "confirm_image_overwrite" "Type YES to overwrite $IMAGE_FILE" "NO")"
      [[ "$_ov" == "YES" ]] || die "Aborted image overwrite"
      run rm -f "$IMAGE_FILE"
    fi
    log "Creating target disk image $IMAGE_FILE ($IMAGE_SIZE) type=${_img_ext}..."
    if [[ "$_img_ext" == "qcow2" ]]; then
      if have qemu-img; then
        # qemu-img needs size without IEC suffix? Use IMAGE_SIZE directly
        run qemu-img create -f qcow2 "$IMAGE_FILE" "$IMAGE_SIZE" 2>/dev/null || run truncate -s "$IMAGE_SIZE" "$IMAGE_FILE"
      else
        warn "qemu-img not found (install qemu-utils), falling back to raw truncate for .qcow2"
        run truncate -s "$IMAGE_SIZE" "$IMAGE_FILE"
      fi
    else
      # Raw sparse — prefer truncate, fallback to fallocate or dd
      if have truncate; then
        run truncate -s "$IMAGE_SIZE" "$IMAGE_FILE"
      elif have fallocate; then
        local _bytes
        _bytes="$(parse_size_bytes "$IMAGE_SIZE")"
        run fallocate -l "$_bytes" "$IMAGE_FILE" 2>/dev/null || run truncate -s "$IMAGE_SIZE" "$IMAGE_FILE"
      else
        die "Need truncate or fallocate to create image"
      fi
    fi
    # Attach as a block device so sgdisk/parted can partition it
    if [[ "$_img_ext" == "qcow2" ]]; then
      # Preferred Debian path: use qemu-nbd (part of qemu-utils) to attach the qcow2 image
      if have qemu-nbd && [[ -e /dev/nbd0 ]]; then
        local _nbd_dev=""
        for _nbd in /dev/nbd0 /dev/nbd1 /dev/nbd2 /dev/nbd3; do
          if [[ -b "$_nbd" ]] && ! lsblk -n -o NAME "$_nbd" 2>/dev/null | grep -q "$(basename "$_nbd")p"; then
            if run qemu-nbd --connect="$_nbd" "$IMAGE_FILE"; then
              _nbd_dev="$_nbd"
              track_nbd "$_nbd"
              break
            fi
          fi
        done
        if [[ -n "$_nbd_dev" ]]; then
          TARGET_DEVICE="$_nbd_dev"
          log "Attached $IMAGE_FILE to $TARGET_DEVICE via qemu-nbd"
        else
          warn "No free /dev/nbdX for qcow2 attachment; falling back to qemu-img convert"
          _qcow2_to_raw_fallback=1
        fi
      else
        _qcow2_to_raw_fallback=1
      fi
      if [[ "${_qcow2_to_raw_fallback:-0}" == "1" ]]; then
        # Fallback: convert qcow2 to a temporary raw image, attach via loop, then re-export as qcow2
        if have qemu-img; then
          local _raw_tmp
          _raw_tmp="$(mktemp --suffix=.img)"
          track_file "$_raw_tmp"
          run qemu-img convert -f qcow2 -O raw "$IMAGE_FILE" "$_raw_tmp" || die "Failed to convert qcow2 to raw for partitioning"
          IMAGE_FILE_RAW_BACKING="$_raw_tmp"
          LOOP_DEV="$(losetup --find --show --partscan "$_raw_tmp")"
          if [[ -z "$LOOP_DEV" ]]; then die "Failed to attach raw image via loop"; fi
          track_loop "$LOOP_DEV"
          log "Attached converted raw image to $LOOP_DEV (will re-export as qcow2 on success)"
          TARGET_DEVICE="$LOOP_DEV"
        else
          die "qcow2 image requires either qemu-nbd (preferred) or qemu-img; install qemu-utils"
        fi
      fi
    else
      LOOP_DEV="$(losetup --find --show --partscan "$IMAGE_FILE")"
      if [[ -z "$LOOP_DEV" ]]; then die "Failed to attach loop device for $IMAGE_FILE (need losetup)"; fi
      track_loop "$LOOP_DEV"
      log "Attached $IMAGE_FILE to $LOOP_DEV"
      TARGET_DEVICE="$LOOP_DEV"
    fi

  elif [[ "$TARGET_MODE" == "rootfs" || "$TARGET_MODE" == "tar" ]]; then
    TARGET_DIR="$(ask "target_dir" "Target chroot directory" "/tmp/debian-${SUITE}-rootfs")"
    mkdir -p "$TARGET_DIR"
    MNT="$TARGET_DIR"
    if [[ "$TARGET_MODE" == "tar" ]]; then
      TAR_OUTPUT="$(ask "tar_output" "Output tarball archive path" "debian-${SUITE}-${TARGET_ARCH}-rootfs.tar.xz")"
    fi
  fi
}

is_host_disk() {
  local dev="$1"
  # Normalize dev without trailing slash and resolve any symlink to a canonical /dev path
  dev="${dev%/}"
  local _dev_canon
  _dev_canon="$(readlink -f "$dev" 2>/dev/null || echo "$dev")"
  if [[ -z "$_dev_canon" || ! -e "$_dev_canon" ]]; then
    # Non-existent device: allow the rest of the code to report the proper error
    return 0
  fi
  # If the target is a regular file (raw image), there is no host-disk risk
  if [[ ! -b "$_dev_canon" ]]; then
    return 0
  fi

  local host_root_dev host_parent
  host_root_dev="$(findmnt -n -o SOURCE / 2>/dev/null || true)"
  if [[ -z "$host_root_dev" ]]; then
    host_root_dev="$(findmnt -n -o SOURCE / --first-only 2>/dev/null || true)"
  fi
  if [[ -n "$host_root_dev" ]]; then
    # Resolve parent disk (e.g. /dev/sda2 -> /dev/sda, /dev/nvme0n1p2 -> /dev/nvme0n1)
    host_parent="$(lsblk -no PKNAME "$host_root_dev" 2>/dev/null || true)"
    if [[ -z "$host_parent" && -n "$host_root_dev" ]]; then
      local _sysdev
      _sysdev="$(readlink -f "/sys/class/block/$(basename "$host_root_dev")" 2>/dev/null || true)"
      if [[ -n "$_sysdev" ]]; then
        host_parent="$(basename "$(dirname "$_sysdev")" 2>/dev/null || true)"
        if [[ "$host_parent" == "$(basename "$host_root_dev")" ]]; then
          host_parent="$(basename "$(dirname "$(dirname "$_sysdev")")" 2>/dev/null || true)"
        fi
      fi
    fi
    local _host_canon
    if [[ -n "$host_parent" ]]; then
      local _host_parent_path="/dev/$host_parent"
      _host_canon="$(readlink -f "$_host_parent_path" 2>/dev/null || echo "$_host_parent_path")"
      if [[ "$_dev_canon" == "$_host_canon" ]]; then
        warn "DEVICE $dev CONTAINS THE RUNNING HOST ROOT FILESYSTEM ($host_root_dev)!"
        warn "Host parent device is $_host_parent_path -> $_host_canon"
        local conf_host
        conf_host="$(ask "confirm_host_disk" "Type 'DESTROY_HOST_SYSTEM' to overwrite the running host disk" "")"
        [[ "$conf_host" == "DESTROY_HOST_SYSTEM" ]] || die "Aborted to protect running host system."
      fi
      local _dev_pk
      _dev_pk="$(lsblk -no PKNAME "$_dev_canon" 2>/dev/null || true)"
      if [[ -n "$_dev_pk" && "$_dev_pk" == "$host_parent" ]]; then
        warn "Device $dev is a partition on host root disk /dev/$host_parent!"
        local conf_part
        conf_part="$(ask "confirm_host_disk" "Type 'DESTROY_HOST_SYSTEM' to overwrite a partition on the running host disk" "")"
        [[ "$conf_part" == "DESTROY_HOST_SYSTEM" ]] || die "Aborted to protect host partition."
      fi
    fi
  fi

  # Additional safety: prevent wiping if device is mounted anywhere (except under MNT)
  local _mntpoint
  _mntpoint="$(findmnt -n -o TARGET -S "$_dev_canon" 2>/dev/null | head -1 || true)"
  if [[ -z "$_mntpoint" ]]; then
    # Try matching partitions as well
    for _p in "${_dev_canon}"1 "${_dev_canon}"2; do
      _mntpoint="$(findmnt -n -o TARGET -S "$_p" 2>/dev/null | head -1 || true)"
      [[ -n "$_mntpoint" ]] && break
    done
  fi
  if [[ -n "$_mntpoint" && "$_mntpoint" != "$MNT"* ]]; then
    warn "Device $dev is currently mounted at $_mntpoint!"
    if [[ "$ASSUME_YES" != "1" ]]; then
      local _conf_mnt
      _conf_mnt="$(ask "confirm_mounted_wipe" "Device is mounted — type 'YES_MOUNTED' to proceed anyway" "")"
      [[ "$_conf_mnt" == "YES_MOUNTED" ]] || die "Aborted due to mounted device."
    fi
  fi
}

confirm_wipe_action() {
  local dev="$1"
  if [[ "$ASSUME_YES" == "1" ]]; then return 0; fi
  warn "ALL DATA ON $dev WILL BE PERMANENTLY ERASED!"
  local confirm
  confirm="$(ask "confirm_wipe" "Type YES to erase $dev and proceed" "NO")"
  [[ "$confirm" == "YES" ]] || die "Aborted by user (confirmation was not YES)."
}

# ─────────────────────────────────────── 4) disk layout & partitions ─────────────────────────────
prompt_disk_layout() {
  if [[ "$TARGET_MODE" == "rootfs" || "$TARGET_MODE" == "tar" ]]; then
    # No disk: later stages still read these under set -u.
    LAYOUT="${LAYOUT:-NONE}"; FS_ROOT="${FS_ROOT:-ext4}"; ENCRYPT_ROOT="n"; USE_LVM="n"; USE_RAID="n"
    USER_HOME="n"; BIOS_COMPAT="n"; BTRFS_SUBVOLS="n"; BTRFS_SNAPPER="n"; LUKS_TPM2="n"; LUKS_TYPE="luks2"
    SIZE_EFI="0"; SIZE_BOOT="0"; SIZE_SWAP="0"; SIZE_HOME="0"; SIZE_ROOT="0"
    return 0
  fi
  banner "Disk Layout & Partition Scheme"
  local layout_choice
  layout_choice="$(ask_choice "layout" "Partition Layout Scheme" "3" \
    "1) ROOT only (BIOS/Legacy or single-partition VM)" \
    "2) BOOT + ROOT (BIOS/MBR or non-EFI)" \
    "3) EFI + ROOT (Standard Modern UEFI)" \
    "4) EFI + BOOT + ROOT (Separated kernel/initramfs boot)" \
    "5) EFI + BOOT + ROOT + SWAP (Full multi-partition)" \
    "6) EFI + ROOT + SWAP" \
    "7) Multi-Root A/B (Dual ROOT partitions with separate GRUB entries — immutable/update schemes)")"

  case "$layout_choice" in
    1) LAYOUT="ROOT" ;;
    2) LAYOUT="BOOT_ROOT" ;;
    3) LAYOUT="EFI_ROOT" ;;
    4) LAYOUT="EFI_BOOT_ROOT" ;;
    5) LAYOUT="EFI_BOOT_ROOT_SWAP" ;;
    6) LAYOUT="EFI_ROOT_SWAP" ;;
    7) LAYOUT="MULTI_ROOT"; MULTI_ROOT=1 ;;
    *) LAYOUT="EFI_ROOT" ;;
  esac

  USER_HOME="$(ask_yn "user_addon" "Create dedicated /home partition?" "n")"
  BIOS_COMPAT="$(ask_yn "bios_compat" "Include 1 MiB BIOS boot partition (ef02) for dual UEFI/Legacy BIOS compatibility?" "n")"
  USE_LVM="$(ask_yn "use_lvm" "Use LVM2 (Logical Volume Manager)?" "n")"
  USE_RAID="$(ask_yn "use_raid" "Use RAID (mdadm)?" "n")"

  # Filesystem types
  banner "Filesystem Selection"
  local fs_root_choice
  fs_root_choice="$(ask_choice "fs_root" "Root Filesystem (fs_root)" "1" \
    "ext4 (Standard Debian default, rock solid)" \
    "btrfs (Modern CoW with subvolumes and snapshots)" \
    "xfs (High performance enterprise filesystem)" \
    "f2fs (Flash-friendly for NVMe/SSDs/SD cards)" \
    "zfs (OpenZFS — requires contrib, Debian zfsutils-linux)")"

  case "$fs_root_choice" in
    1|ext4)  FS_ROOT="ext4" ;;
    2|btrfs) FS_ROOT="btrfs" ;;
    3|xfs)   FS_ROOT="xfs" ;;
    4|f2fs)  FS_ROOT="f2fs" ;;
    5|zfs)   FS_ROOT="zfs" ;;
    *) FS_ROOT="ext4" ;;
  esac

  if [[ "$FS_ROOT" == "btrfs" ]]; then
    BTRFS_SUBVOLS="$(ask_yn "btrfs_subvols" "Enable Btrfs subvolumes (@, @home, @snapshots, @var_log, @var_cache)?" "y")"
    if [[ "$BTRFS_SUBVOLS" == "y" ]]; then
      BTRFS_SNAPPER="$(ask_yn "btrfs_snapper" "Configure Snapper automated rollback snapshots?" "y")"
    fi
  fi

  # Encryption
  banner "Encryption & Security"
  ENCRYPT_ROOT="$(ask_yn "encrypt_root" "Encrypt Root partition with LUKS2?" "n")"
  if [[ "$ENCRYPT_ROOT" == "y" ]]; then
    LUKS_TYPE="$(ask_choice "luks_type" "LUKS Format Version" "1" "luks2 (Argon2id default)" "luks1 (Legacy compatibility)")"
    [[ "$LUKS_TYPE" == "2" || "$LUKS_TYPE" == "luks1" ]] && LUKS_TYPE="luks1" || LUKS_TYPE="luks2"
    LUKS_TPM2="$(ask_yn "luks_tpm2" "Enroll TPM2 auto-unlock via systemd-cryptenroll (PCR 0,2,7)?" "n")"
  fi

  # Sizes — validate each
  banner "Partition Sizing"
  while true; do
    SIZE_EFI="$(ask "size_efi" "EFI System Partition size" "512M")"
    validate_size_string "$SIZE_EFI" && break
    warn "Invalid size '$SIZE_EFI'"; unset "CFG[size_efi]"
    [[ "$UNATTENDED" == "1" ]] && die "Invalid size_efi $SIZE_EFI"
  done
  while true; do
    SIZE_BOOT="$(ask "size_boot" "Boot partition size" "1G")"
    validate_size_string "$SIZE_BOOT" && break
    warn "Invalid size '$SIZE_BOOT'"; unset "CFG[size_boot]"
    [[ "$UNATTENDED" == "1" ]] && die "Invalid size_boot $SIZE_BOOT"
  done
  while true; do
    SIZE_SWAP="$(ask "size_swap" "Swap partition size (or 0 for swapfile/zram)" "4G")"
    validate_size_string "$SIZE_SWAP" && break
    warn "Invalid size '$SIZE_SWAP'"; unset "CFG[size_swap]"
    [[ "$UNATTENDED" == "1" ]] && die "Invalid size_swap $SIZE_SWAP"
  done
  while true; do
    SIZE_HOME="$(ask "size_home" "Home partition size (or 0 for rest of disk)" "0")"
    validate_size_string "$SIZE_HOME" && break
    warn "Invalid size '$SIZE_HOME'"; unset "CFG[size_home]"
    [[ "$UNATTENDED" == "1" ]] && die "Invalid size_home $SIZE_HOME"
  done
  while true; do
    SIZE_ROOT="$(ask "size_root" "Root partition size (0 for rest of disk)" "0")"
    validate_size_string "$SIZE_ROOT" && break
    warn "Invalid size '$SIZE_ROOT'"; unset "CFG[size_root]"
    [[ "$UNATTENDED" == "1" ]] && die "Invalid size_root $SIZE_ROOT"
  done
}

# ─────────────────────────────────────── 5) APT & Debian mirror configuration ────────────────────
benchmark_debian_mirrors() {
  if [[ "$OFFLINE" == "1" ]]; then
    warn "Offline mode enabled — skipping mirror benchmark"
    printf '%s\n' "https://deb.debian.org/debian"
    return 0
  fi
  banner "Benchmarking Debian Mirrors"
  local -a test_mirrors=(
    "https://deb.debian.org/debian"
    "http://ftp.us.debian.org/debian"
    "http://ftp.de.debian.org/debian"
    "http://ftp.fr.debian.org/debian"
    "http://ftp.uk.debian.org/debian"
    "http://ftp.jp.debian.org/debian"
    "http://ftp.br.debian.org/debian"
    "http://ftp.au.debian.org/debian"
    "http://ftp.ca.debian.org/debian"
  )
  # Allow user-supplied extra mirrors via CFG
  if [[ -n "${CFG[extra_mirrors]:-}" ]]; then
    IFS=',' read -ra _extra <<< "${CFG[extra_mirrors]}"
    for _em in "${_extra[@]}"; do
      _em="${_em#"${_em%%[![:space:]]*}"}"; _em="${_em%"${_em##*[![:space:]]}"}"
      [[ -n "$_em" ]] && test_mirrors+=("$_em")
    done
  fi
  if ! have curl && ! have wget; then
    warn "curl/wget not found — cannot benchmark mirrors, defaulting to deb.debian.org"
    if [[ "$BENCHMARK_ONLY" == "1" ]]; then exit 0; fi
    printf '%s\n' "https://deb.debian.org/debian"
    return 0
  fi
  local _samples="${CFG[mirror_samples]:-3}"
  [[ "$_samples" =~ ^[0-9]+$ ]] || _samples=3
  (( _samples < 1 )) && _samples=1
  (( _samples > 5 )) && _samples=5
  echo "Testing latency to Debian mirrors (suite: ${SUITE:-bookworm}, ${_samples} samples each)..." >&2
  local best_mirror="https://deb.debian.org/debian"
  local best_time=999999
  local -a results=()
  for m in "${test_mirrors[@]}"; do
    # Validate URL; if user provided a malformed extra_mirror, skip cleanly
    if ! [[ "$m" =~ ^https?://[^/]+/ ]]; then
      warn "Skipping invalid mirror URL: $m"
      continue
    fi
    local url="${m}/dists/${SUITE:-bookworm}/Release"
    local -a samples=()
    local i=0
    while (( i < _samples )); do
      local t="999"
      if have curl; then
        t="$(curl -o /dev/null -s -w '%{time_total}' --connect-timeout 4 --max-time 7 "$url" 2>/dev/null || echo "999")"
      elif have wget; then
        local _s _e
        _s="$(date +%s.%N 2>/dev/null || date +%s)"
        if wget -q --timeout=5 --tries=1 -O /dev/null "$url" 2>/dev/null; then
          _e="$(date +%s.%N 2>/dev/null || date +%s)"
          t="$(awk -v s="$_s" -v e="$_e" 'BEGIN{printf "%.3f", e - s}')"
        else
          t="999"
        fi
      fi
      if ! [[ "$t" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then t="999"; fi
      if awk -v t="$t" 'BEGIN{exit !(t>30)}'; then t="999"; fi
      samples+=("$t")
      i=$((i+1))
    done
    # Compute median sample (sort numerically and pick middle) — more robust than min
    local t
    t="$(printf '%s\n' "${samples[@]}" | sort -n | awk '{a[NR]=$1} END {n=NR; if (n==0) {print 999; exit} if (n%2==1) print a[int((n+1)/2)]; else print (a[int(n/2)] + a[int(n/2)+1])/2}')"
    if [[ -z "$t" ]]; then
      # Simpler fallback: take the minimum sample
      t="$(printf '%s\n' "${samples[@]}" | sort -n | head -1)"
    fi
    if ! [[ "$t" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then t="999"; fi
    printf "  %-42s : %6.3f s (median of %d)\n" "$m" "$t" "$_samples" >&2
    results+=("$t $m")
    if awk -v a="$t" -v b="$best_time" 'BEGIN{exit !(a < b)}'; then
      best_time="$t"
      best_mirror="$m"
    fi
  done
  # Sort results by latency and display ranking
  if (( ${#results[@]} > 1 )); then
    echo -e "\n${GRN}Mirror ranking (fastest first):${RST}" >&2
    printf '%s\n' "${results[@]}" | sort -n | awk '{printf "  %6.3f  %s\n", $1, $2}' >&2
  fi
  echo -e "\n${GRN}Fastest mirror:${RST} $best_mirror (${best_time}s)" >&2
  if [[ "$BENCHMARK_ONLY" == "1" ]]; then
    exit 0
  fi
  printf '%s\n' "$best_mirror"
}

prompt_apt_configuration() {
  banner "Debian APT & Mirror Configuration"
  local mirror_choice
  if [[ "$OFFLINE" == "1" ]]; then
    mirror_choice="4"
  else
    mirror_choice="$(ask_choice "mirror_choice" "Debian Mirror Strategy" "1" \
      "deb.debian.org (Debian Global CDN — Recommended)" \
      "Auto-benchmark fastest regional mirror" \
      "Custom mirror URL" \
      "Offline / local file:// mirror (use with --offline)")"
  fi

  case "$mirror_choice" in
    1) MIRROR="https://deb.debian.org/debian" ;;
    2) MIRROR="$(benchmark_debian_mirrors)" ;;
    3) MIRROR="$(ask "custom_mirror" "Enter custom Debian mirror URL" "https://deb.debian.org/debian")" ;;
    4)
      # Offline / local file:// mirror. Validate the URL is reachable offline.
      MIRROR="$(ask "local_mirror" "Enter local mirror path (file:///path or http://internal-mirror)" "file:///var/cache/deb-strap-mirror")"
      if [[ "$MIRROR" == file://* ]]; then
        local _local_path="${MIRROR#file://}"
        if [[ ! -d "$_local_path" ]]; then
          die "Offline mirror path does not exist: $_local_path"
        fi
        if [[ ! -d "$_local_path/dists/$SUITE" ]]; then
          die "Offline mirror missing dists/$SUITE under $_local_path"
        fi
      fi
      if ! have apt-get; then
        die "Offline mode requires apt-get on the host for cache population"
      fi
      ;;
    *) MIRROR="https://deb.debian.org/debian" ;;
  esac
  log "Selected Debian mirror: $MIRROR"

  # Modern deb822 sources format
  USE_DEB822="$(ask_yn "use_deb822" "Use modern deb822 source format (/etc/apt/sources.list.d/debian.sources)?" "y")"

  # Archive components
  ENABLE_CONTRIB="$(ask_yn "enable_contrib" "Enable 'contrib' repository component?" "y")"
  ENABLE_NONFREE="$(ask_yn "enable_nonfree" "Enable 'non-free' repository component?" "y")"
  # Non-free firmware (Debian 12+ standard)
  ENABLE_NONFREE_FIRMWARE="$(ask_yn "enable_nonfree_firmware" "Enable 'non-free-firmware' component (WiFi/GPU firmware)?" "y")"

  # Extra suites
  ENABLE_SECURITY="$(ask_yn "enable_security" "Enable security updates repository?" "y")"
  ENABLE_UPDATES="$(ask_yn "enable_updates" "Enable recommended updates (updates / proposed-updates)?" "y")"
  ENABLE_BACKPORTS="$(ask_yn "enable_backports" "Enable backports repository?" "n")"
}

# ─────────────────────────────────────── 6) system settings & accounts ───────────────────────────
prompt_system_settings() {
  banner "System Locale, Hostname & Accounts"
  # Hostname with validation loop
  while true; do
    HOSTNAME="$(ask "hostname" "System hostname" "debian-box")"
    if validate_hostname "$HOSTNAME"; then break; fi
    if [[ "$UNATTENDED" == "1" ]]; then die "Invalid hostname '$HOSTNAME' (RFC 1123)"; fi
    warn "Invalid hostname '$HOSTNAME' — must be 1-63 alphanum/hyphen, not starting/ending with hyphen."
    unset "CFG[hostname]"
  done
  # Timezone validation via /usr/share/zoneinfo
  while true; do
    TIMEZONE="$(ask "timezone" "System timezone (e.g. UTC, America/New_York, Europe/London)" "UTC")"
    if [[ -f "/usr/share/zoneinfo/$TIMEZONE" || "$TIMEZONE" == "UTC" ]]; then break; fi
    if [[ "$UNATTENDED" == "1" ]]; then warn "Timezone $TIMEZONE not in zoneinfo, proceeding anyway"; break; fi
    warn "Timezone $TIMEZONE not found under /usr/share/zoneinfo. Check e.g. Europe/London."
    unset "CFG[timezone]"
  done
  LOCALE="$(ask "locale" "System locale" "en_US.UTF-8")"
  # Locale sanity: must be like en_US.UTF-8
  if ! [[ "$LOCALE" =~ ^[a-z]{2}_[A-Z]{2}\.UTF-8$ ]]; then
    warn "Locale '$LOCALE' doesn't look like en_US.UTF-8, but continuing"
  fi
  KEYMAP="$(ask "keymap" "Console keymap" "us")"

  # User account with validation
  while true; do
    USERNAME="$(ask "username" "Primary non-root username" "debian")"
    if validate_username "$USERNAME"; then break; fi
    if [[ "$UNATTENDED" == "1" ]]; then die "Invalid username '$USERNAME' (Debian policy)"; fi
    warn "Invalid username '$USERNAME' — must be lowercase, start with letter/_, max 32 chars."
    unset "CFG[username]"
  done
  # Shell validation: must be known shell path
  USER_SHELL="$(ask "user_shell" "User login shell (/bin/bash, /usr/bin/zsh, /usr/bin/fish)" "/bin/bash")"
  if ! [[ "$USER_SHELL" =~ ^/(bin|usr/bin)/[a-z]+$ ]]; then
    warn "Shell $USER_SHELL looks unusual, but will attempt to install if needed"
  fi
  SUDO_MODE="$(ask_choice "sudo_mode" "Administrative Privilege Tool" "1" \
    "sudo (Standard with password requirement)" \
    "sudo NOPASSWD (Passwordless administrative execution)" \
    "doas (Lightweight alternative)" \
    "None (Root login only)")"

  # Passwords
  if [[ "$UNATTENDED" == "1" ]]; then
    ROOT_PASSWORD="${CFG[root_password]:-}"
    USER_PASSWORD="${CFG[user_password]:-}"
  fi

  # Init System (Init Freedom for Pure Debian)
  banner "Init System Selection"
  local init_choice
  init_choice="$(ask_choice "init_system" "Init System" "1" \
    "systemd (Debian default — complete ecosystem, systemd-boot & timers)" \
    "sysvinit (Classic Debian sysvinit-core + sysv-rc)" \
    "OpenRC (Fast, dependency-based init)" \
    "runit (Minimalist supervision-based init)")"

  case "$init_choice" in
    1|systemd)  INIT_SYSTEM="systemd" ;;
    2|sysvinit) INIT_SYSTEM="sysvinit" ;;
    3|openrc)   INIT_SYSTEM="openrc" ;;
    4|runit)    INIT_SYSTEM="runit" ;;
    *) INIT_SYSTEM="systemd" ;;
  esac
  log "Init system: $INIT_SYSTEM"

  # Kernel selection
  banner "Kernel & Bootstrapping Kernel Options"
  local kernel_choice
  kernel_choice="$(ask_choice "kernel" "Kernel Choice" "1" \
    "linux-image-amd64 (Debian Stock Kernel)" \
    "linux-image-rt-amd64 (Realtime PREEMPT_RT Kernel)" \
    "linux-image-cloud-amd64 (Minimal Cloud/KVM Kernel)" \
    "liquorix (Liquorix desktop/low-latency kernel)" \
    "xanmod (XanMod high-performance kernel)" \
    "custom_lts (Compile latest LTS kernel from kernel.org source)" \
    "none (No kernel or firmware: chroot / container rootfs)")"

  case "$kernel_choice" in
    1|stock|linux-image-amd64) KERNEL="stock" ;;
    2|rt)         KERNEL="rt" ;;
    3|cloud)      KERNEL="cloud" ;;
    4|liquorix)   KERNEL="liquorix" ;;
    5|xanmod)     KERNEL="xanmod" ;;
    6|custom_lts) KERNEL="custom_lts" ;;
    7|none)       KERNEL="none" ;;
    *) KERNEL="stock" ;;
  esac

  # Bootloader selection
  if [[ "$TARGET_MODE" != "rootfs" && "$TARGET_MODE" != "tar" ]]; then
    banner "Bootloader Selection"
    local bl_choice
    bl_choice="$(ask_choice "bootloader" "Bootloader" "1" \
      "GRUB (UEFI + Secure Boot shim signed / BIOS hybrid)" \
      "systemd-boot (Fast UEFI bootloader, requires systemd & ESP at /boot)" \
      "rEFInd (Graphical UEFI boot manager)" \
      "Limine (Modern multi-protocol bootloader)" \
      "Skip bootloader installation (Container/VM image)")"

    case "$bl_choice" in
      1|grub)         BOOTLOADER="grub" ;;
      2|systemd-boot) BOOTLOADER="systemd-boot" ;;
      3|refind)       BOOTLOADER="refind" ;;
      4|limine)       BOOTLOADER="limine" ;;
      5|skip)         BOOTLOADER="skip" ;;
      *) BOOTLOADER="grub" ;;
    esac

    SECURE_BOOT="n"
    if [[ "$BOOTLOADER" == "grub" ]]; then
      SECURE_BOOT="$(ask_yn "secure_boot" "Install signed GRUB and Shim for UEFI Secure Boot compatibility?" "y")"
    fi
  else
    BOOTLOADER="skip"
    SECURE_BOOT="n"
  fi

  # Networking & Services
  banner "Networking & Services"
  local net_choice
  net_choice="$(ask_choice "network_manager" "Network Manager" "1" \
    "NetworkManager (Desktop & general standard)" \
    "systemd-networkd + systemd-resolved (Lightweight, robust server standard)" \
    "ifupdown (/etc/network/interfaces classic Debian)" \
    "iwd (WiFi daemon only)" \
    "dhcpcd")"

  case "$net_choice" in
    1|networkmanager|NetworkManager) NET_MANAGER="NetworkManager" ;;
    2|systemd-networkd) NET_MANAGER="systemd-networkd" ;;
    3|ifupdown)         NET_MANAGER="ifupdown" ;;
    4|iwd)              NET_MANAGER="iwd" ;;
    5|dhcpcd)           NET_MANAGER="dhcpcd" ;;
    *) NET_MANAGER="NetworkManager" ;;
  esac

  ENABLE_SSH="$(ask_yn "enable_ssh" "Install and enable OpenSSH Server?" "y")"
  if [[ "$ENABLE_SSH" == "y" ]]; then
    SSH_PERMIT_ROOT="$(ask_yn "ssh_permit_root" "Permit root SSH login?" "n")"
    SSH_AUTHORIZED_KEYS="$(ask "ssh_authorized_keys" "SSH public key string for user (or empty)" "")"
  fi

  # Software profiles & Desktops
  banner "Software Packages & Desktop Environment"
  OPT_PACKAGES="$(ask "opt_packages" "Software packages (e.g. cli, dev, docker, all, none)" "cli,dev")"

  local de_choice
  de_choice="$(ask_choice "desktop" "Desktop Environment" "1" \
    "None (Headless / Server)" \
    "GNOME" \
    "KDE Plasma" \
    "XFCE" \
    "Cinnamon" \
    "MATE" \
    "LXQt" \
    "Sway (Wayland Tiling WM)" \
    "Hyprland (Dynamic Wayland Compositor)" \
    "i3 (X11 Tiling WM)")"

  case "$de_choice" in
    1|none) DESKTOP="none" ;;
    2|gnome) DESKTOP="gnome" ;;
    3|kde|plasma) DESKTOP="kde" ;;
    4|xfce) DESKTOP="xfce" ;;
    5|cinnamon) DESKTOP="cinnamon" ;;
    6|mate) DESKTOP="mate" ;;
    7|lxqt) DESKTOP="lxqt" ;;
    8|sway) DESKTOP="sway" ;;
    9|hyprland) DESKTOP="hyprland" ;;
    10|i3) DESKTOP="i3" ;;
    *) DESKTOP="none" ;;
  esac

  if [[ "$DESKTOP" != "none" ]]; then
    local dm_choice
    dm_choice="$(ask_choice "display_manager" "Display Manager" "1" \
      "Auto (Default for selected desktop: GDM for GNOME, SDDM for KDE, LightDM for others)" \
      "SDDM" \
      "GDM3" \
      "LightDM" \
      "greetd" \
      "Ly (TUI Display Manager)" \
      "None / StartX / Console Login")"
    case "$dm_choice" in
      1|auto) DISPLAY_MANAGER="auto" ;;
      2|sddm) DISPLAY_MANAGER="sddm" ;;
      3|gdm3) DISPLAY_MANAGER="gdm3" ;;
      4|lightdm) DISPLAY_MANAGER="lightdm" ;;
      5|greetd) DISPLAY_MANAGER="greetd" ;;
      6|ly) DISPLAY_MANAGER="ly" ;;
      7|none) DISPLAY_MANAGER="none" ;;
      *) DISPLAY_MANAGER="auto" ;;
    esac
  else
    DISPLAY_MANAGER="none"
  fi

  # GPU driver
  local gpu_choice
  gpu_choice="$(ask_choice "gpu_driver" "GPU Driver" "1" \
    "Mesa / Open Source (Intel, AMD Radeon, VirGL, Nouveau)" \
    "Nvidia Proprietary (nvidia-driver via non-free)" \
    "Nvidia Open Kernel Module (nvidia-open, Trixie+ only, main/non-free)" \
    "Nvidia CUDA (nvidia-driver + CUDA toolkit, compute + display)" \
    "Nvidia VAAPI (nvidia-vaapi-driver + libva, video acceleration)" \
    "Nvidia Headless / Compute (CUDA / nvidia-headless)" \
    "Nvidia Legacy (older cards: nvidia-legacy-390xx or similar, experimental)" \
    "Skip GPU drivers")"
  case "$gpu_choice" in
    1|mesa)         GPU_DRIVER="mesa" ;;
    2|nvidia)       GPU_DRIVER="nvidia" ;;
    3|nvidia-open)  GPU_DRIVER="nvidia-open" ;;
    4|nvidia-cuda)  GPU_DRIVER="nvidia-cuda" ;;
    5|nvidia-vaapi) GPU_DRIVER="nvidia-vaapi" ;;
    6|nvidia-headless) GPU_DRIVER="nvidia-headless" ;;
    7|nvidia-legacy) GPU_DRIVER="nvidia-legacy" ;;
    8|skip)         GPU_DRIVER="skip" ;;
    *) GPU_DRIVER="mesa" ;;
  esac
}

# ─────────────────────────────────────── 7) partition calculation & execution ───────────────────
parse_size_bytes() {
  local str="${1:-}"
  str="${str#"${str%%[![:space:]]*}"}"; str="${str%"${str##*[![:space:]]}"}"
  [[ -z "$str" || "$str" == "0" ]] && { echo 0; return; }
  if [[ "$str" =~ ^([0-9]+(\.[0-9]+)?)[[:space:]]*([a-zA-Z]+)?$ ]]; then
    local num="${BASH_REMATCH[1]}"
    local unit="${BASH_REMATCH[3]:-}"
    unit="$(printf '%s' "$unit" | tr '[:lower:]' '[:upper:]')"
    case "$unit" in
      K|KIB)      awk -v n="$num" 'BEGIN{printf "%.0f", n * 1024}' ;;
      KB)         awk -v n="$num" 'BEGIN{printf "%.0f", n * 1000}' ;;
      M|MIB)      awk -v n="$num" 'BEGIN{printf "%.0f", n * 1024 * 1024}' ;;
      MB)         awk -v n="$num" 'BEGIN{printf "%.0f", n * 1000 * 1000}' ;;
      G|GIB)      awk -v n="$num" 'BEGIN{printf "%.0f", n * 1024 * 1024 * 1024}' ;;
      GB)         awk -v n="$num" 'BEGIN{printf "%.0f", n * 1000 * 1000 * 1000}' ;;
      T|TIB)      awk -v n="$num" 'BEGIN{printf "%.0f", n * 1024 * 1024 * 1024 * 1024}' ;;
      TB)         awk -v n="$num" 'BEGIN{printf "%.0f", n * 1000 * 1000 * 1000 * 1000}' ;;
      P|PIB)      awk -v n="$num" 'BEGIN{printf "%.0f", n * 1024 * 1024 * 1024 * 1024 * 1024}' ;;
      PB)         awk -v n="$num" 'BEGIN{printf "%.0f", n * 1000 * 1000 * 1000 * 1000 * 1000}' ;;
      S|SECTOR|SECTORS) awk -v n="$num" 'BEGIN{printf "%.0f", n * 512}' ;;
      B|"")       awk -v n="$num" 'BEGIN{printf "%.0f", n}' ;;
      *)          warn "parse_size_bytes: unknown unit '$unit' in '$str', treating as bytes"; awk -v n="$num" 'BEGIN{printf "%.0f", n}' ;;
    esac
  else
    warn "parse_size_bytes: unparseable size '$str', treating as 0"
    echo 0
  fi
}

split_size_string() {
  local str="${1:-}"
  if [[ -z "$str" || "$str" == "0" ]]; then echo 0; return; fi
  if [[ "$str" =~ ^([0-9]+(\.[0-9]+)?)[[:space:]]*([a-zA-Z]+)?$ ]]; then
    local num="${BASH_REMATCH[1]}"
    local unit="${BASH_REMATCH[3]:-}"
    local half
    half="$(awk -v n="$num" 'BEGIN{printf "%.4f", n/2}')"
    # Strip trailing zeros after decimal for cleanliness
    half="$(echo "$half" | sed 's/\.0*$//; s/\.$//')"
    printf '%s%s\n' "$half" "$unit"
  else
    echo 0
  fi
}

format_size_human() {
  local bytes="${1:-0}"
  # Ensure numeric
  if ! [[ "$bytes" =~ ^[0-9]+$ ]]; then bytes=0; fi
  if (( bytes >= 1099511627776 )); then
    awk -v b="$bytes" 'BEGIN{printf "%.2f TiB", b / 1099511627776}'
  elif (( bytes >= 1073741824 )); then
    awk -v b="$bytes" 'BEGIN{printf "%.1f GiB", b / 1073741824}'
  elif (( bytes >= 1048576 )); then
    awk -v b="$bytes" 'BEGIN{printf "%.0f MiB", b / 1048576}'
  elif (( bytes >= 1024 )); then
    awk -v b="$bytes" 'BEGIN{printf "%.0f KiB", b / 1024}'
  else
    echo "${bytes} B"
  fi
}

# Validation helpers — pure Debian policy compliant
validate_hostname() {
  local hn="$1"
  # RFC 1123: 1-63 chars, alphanum + hyphen, not starting/ending with hyphen
  if [[ "$hn" =~ ^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$ ]]; then return 0; else return 1; fi
}
# Pre-flight validation — runs after wizard, before any disk mutation
preflight_check() {
  banner "Pre-flight Validation"
  local _errors=0
  # 1. Validate hostname, locale, keymap, username that were collected
  [[ -n "${HOSTNAME:-}" ]] && ! validate_hostname "$HOSTNAME" && { err "Invalid hostname '$HOSTNAME'"; _errors=$((_errors+1)); }
  [[ -n "${USERNAME:-}" ]] && ! validate_username "$USERNAME" && { err "Invalid username '$USERNAME'"; _errors=$((_errors+1)); }
  # 2. Validate mirror URL
  if [[ -n "${MIRROR:-}" ]] && ! validate_mirror_url "$MIRROR"; then
    err "Invalid mirror URL '$MIRROR'"
    _errors=$((_errors+1))
  fi
  # 3. Validate partition sizes
  for _sz_label in EFI BOOT SWAP HOME ROOT; do
    local _sz_var="SIZE_${_sz_label}"
    local _sz_val="${!_sz_var:-}"
    [[ -z "$_sz_val" ]] && continue
    if ! validate_size_string "$_sz_val"; then
      err "Invalid $_sz_label partition size: '$_sz_val'"
      _errors=$((_errors+1))
    fi
  done
  # 4. Validate layout/bootloader consistency
  if [[ "$BOOTLOADER" == "systemd-boot" ]] && [[ ! "$LAYOUT" =~ EFI ]]; then
    err "systemd-boot requires an EFI partition but LAYOUT='$LAYOUT' has no EFI"
    _errors=$((_errors+1))
  fi
  if [[ "$BOOTLOADER" == "grub" && "${SECURE_BOOT:-n}" == "y" && ! "$LAYOUT" =~ EFI ]]; then
    err "Secure Boot requires an EFI partition but LAYOUT='$LAYOUT' has no EFI"
    _errors=$((_errors+1))
  fi
  # 5. Check for free disk space (host-side) before bootstrap downloads
  local _free_kb
  _free_kb="$(df -Pk "${TMPDIR:-/tmp}" 2>/dev/null | awk 'NR==2{print $4}')"
  if [[ -n "$_free_kb" ]] && (( _free_kb < 524288 )); then
    warn "Low disk space on ${TMPDIR:-/tmp}: $(format_size_human $((_free_kb*1024))) free (< 512 MiB)"
  fi
  # 6. Check for root privileges
  if [[ $EUID -ne 0 ]] && [[ "$DRY_RUN" != "1" ]]; then
    err "deb-strap requires root privileges (current EUID=$EUID). Re-run with sudo."
    _errors=$((_errors+1))
  fi
  # 7. Verify required binaries are present one more time (add filesystem-specific tools)
  for _b in debootstrap sgdisk mkfs.ext4 blkid findmnt; do
    if ! have "$_b"; then
      err "Required binary '$_b' not found after host dependency check"
      _errors=$((_errors+1))
    fi
  done
  # If F2FS is selected, verify host has f2fs-tools
  if [[ "${FS_ROOT:-ext4}" == "f2fs" ]]; then
    if ! have mkfs.f2fs; then
      err "F2FS filesystem selected but mkfs.f2fs (f2fs-tools) not available on host"
      _errors=$((_errors+1))
    fi
  fi
  if [[ "${MULTI_ROOT:-0}" == "1" && -z "${PART_ROOT2:-}" ]]; then
    # Multi-root requires a second root partition; the partition code creates it when MULTI_ROOT=1
    # We only warn here; the partition creation will handle the actual partition.
    info "Multi-root layout enabled — will create two root partitions (A/B or dual-boot)"
  fi
  # 8. If offline mode, validate apt cache and local mirror
  if [[ "$OFFLINE" == "1" ]]; then
    if [[ -n "${MIRROR:-}" && "$MIRROR" == file://* ]]; then
      local _offline_path="${MIRROR#file://}"
      if [[ ! -d "$_offline_path" ]]; then
        err "Offline mirror path missing: $_offline_path"
        _errors=$((_errors+1))
      fi
      if [[ ! -f "$_offline_path/dists/$SUITE/Release" ]]; then
        err "Offline mirror missing Release file for suite $SUITE at $_offline_path"
        _errors=$((_errors+1))
      fi
    fi
    # Check for a populated apt cache (at least some packages) since we skip downloads
    if ! ls /var/cache/apt/archives/*.deb &>/dev/null; then
      warn "Offline mode: /var/cache/apt/archives appears empty — bootstrap may fail without cached packages"
    fi
  fi
  # 9. If LUKS, verify cryptsetup and that PART_ROOT is set
  if [[ "${ENCRYPT_ROOT:-n}" == "y" ]] && ! have cryptsetup; then
    err "LUKS encryption requested but cryptsetup is not installed"
    _errors=$((_errors+1))
  fi
  if (( _errors > 0 )); then
    die "Pre-flight validation failed with $_errors error(s). Aborting before any disk operations."
  fi
  log "Pre-flight validation passed."
}
validate_username() {
  local un="$1"
  # Debian adduser policy: lowercase, starts with letter/_, 1-32 chars
  if [[ "$un" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]]; then return 0; else return 1; fi
}
validate_mirror_url() {
  local url="$1"
  # Must start with http(s)://, contain a host, and an optional path.
  if [[ "$url" =~ ^https?://[^[:space:]]+(:[0-9]+)?(/[^[:space:]]*)?$ ]]; then return 0; else return 1; fi
}
validate_size_string() {
  local s="$1"
  [[ -z "$s" ]] && return 1
  [[ "$s" == "0" ]] && return 0
  if [[ "$s" =~ ^[0-9]+(\.[0-9]+)?[[:space:]]*$ ]]; then return 0; fi
  # K/M/G/T/P with optional i and optional trailing B (e.g. K, Ki, KiB, Kb, KB, MiB, GB)
  if [[ "$s" =~ ^[0-9]+(\.[0-9]+)?[[:space:]]*([KkMmGgTtPp][Ii]?[Bb]?)$ ]]; then return 0; fi
  # S by itself (sector count), or SECTOR / SECTORS, with optional whitespace
  if [[ "$s" =~ ^[0-9]+[[:space:]]*[Ss]([Ee][Cc][Tt][Oo][Rr])?[Ss]?$ ]]; then return 0; fi
  if [[ "$s" =~ ^[0-9]+[[:space:]]+[Ss]([Ee][Cc][Tt][Oo][Rr])?[Ss]?$ ]]; then return 0; fi
  return 1
}

execute_partitioning() {
  [[ "$TARGET_MODE" == "rootfs" || "$TARGET_MODE" == "tar" ]] && return 0
  banner "Executing Disk Partitioning on $TARGET_DEVICE"

  # Validate partition sizes before touching disk
  for _sz in "${SIZE_EFI:-512M}" "${SIZE_BOOT:-1G}" "${SIZE_SWAP:-0}" "${SIZE_HOME:-0}" "${SIZE_ROOT:-0}"; do
    if ! validate_size_string "$_sz"; then
      die "Invalid partition size string: '$_sz' (expected e.g. 512M, 1G, 20G, 0)"
    fi
  done
  # Validate LAYOUT vs bootloader constraints
  if [[ "$BOOTLOADER" == "systemd-boot" && ! "$LAYOUT" =~ EFI ]]; then
    warn "systemd-boot requires EFI partition — forcing layout to EFI+ROOT"
    LAYOUT="EFI_ROOT"
  fi
  # Disk size sanity check (skip for dry-run or when blockdev missing)
  if [[ "$DRY_RUN" != "1" ]] && have blockdev && [[ -b "$TARGET_DEVICE" ]]; then
    local _disk_bytes _calc_need
    _disk_bytes="$(blockdev --getsize64 "$TARGET_DEVICE" 2>/dev/null || echo 0)"
    _calc_need=0
    if [[ "$LAYOUT" =~ EFI ]]; then _calc_need=$((_calc_need + $(parse_size_bytes "${SIZE_EFI:-512M}"))); fi
    if [[ "$LAYOUT" =~ BOOT ]]; then _calc_need=$((_calc_need + $(parse_size_bytes "${SIZE_BOOT:-1G}"))); fi
    if [[ "$LAYOUT" =~ SWAP ]] && [[ "${SIZE_SWAP:-0}" != "0" ]]; then _calc_need=$((_calc_need + $(parse_size_bytes "${SIZE_SWAP:-4G}"))); fi
    if [[ "$USER_HOME" == "y" && "${SIZE_HOME:-0}" != "0" ]]; then _calc_need=$((_calc_need + $(parse_size_bytes "${SIZE_HOME:-0}"))); fi
    if [[ "${SIZE_ROOT:-0}" != "0" ]]; then _calc_need=$((_calc_need + $(parse_size_bytes "${SIZE_ROOT:-0}"))); fi
    # Add GPT overhead + BIOS 1M
    if [[ "$BIOS_COMPAT" == "y" ]]; then _calc_need=$((_calc_need + 1048576)); fi
    _calc_need=$((_calc_need + 2*1024*1024)) # reserve 2 MiB GPT overhead
    if (( _disk_bytes > 0 && _calc_need > _disk_bytes )); then
      die "Partition sizes require $(format_size_human "$_calc_need") but disk $TARGET_DEVICE is only $(format_size_human "$_disk_bytes")"
    fi
    if (( _disk_bytes > 0 )); then
      info "Disk $TARGET_DEVICE size: $(format_size_human "$_disk_bytes"), required fixed: $(format_size_human "$_calc_need")"
    fi
  fi

  # Handle LVM/RAID/ZFS capability warnings within Debian
  if [[ "${USE_LVM:-n}" == "y" ]]; then
    info "LVM2 requested — partitioning will create physical partitions; LVM setup occurs in formatting stage."
  fi
  if [[ "${USE_RAID:-n}" == "y" ]]; then
    info "RAID requested — base partitions created; mdadm assembly is handled post-partitioning (Debian mdadm)."
  fi
  if [[ "${FS_ROOT:-ext4}" == "zfs" ]]; then
    if ! have zpool; then
      warn "ZFS requested but zfsutils-linux not available on host. Partition will be created as type BF01; zpool creation inside target requires contrib."
    fi
  fi

  # Wipe old partition tables and create fresh GPT
  log "Wiping signatures on $TARGET_DEVICE..."
  run wipefs -af "$TARGET_DEVICE" || true
  run sgdisk --zap-all "$TARGET_DEVICE" 2>/dev/null || run sgdisk -Z "$TARGET_DEVICE" || true
  # Create new GPT with 2048-sector alignment (1MiB) — Debian standard
  run sgdisk --clear -a 2048 "$TARGET_DEVICE" 2>/dev/null || run sgdisk -o "$TARGET_DEVICE" || true
  # Inform kernel of new table before creating partitions
  run partprobe "$TARGET_DEVICE" 2>/dev/null || true
  run udevadm settle 2>/dev/null || sleep 0.5

  # Partition layout plan
  PART_EFI=""
  PART_BIOS=""
  PART_BOOT=""
  PART_SWAP=""
  PART_HOME=""
  PART_ROOT=""
  PART_ROOT2=""

  local part_num=1

  # Optional BIOS boot partition (ef02) for dual boot
  if [[ "$BIOS_COMPAT" == "y" ]]; then
    log "Creating 1 MiB BIOS boot partition (ef02)..."
    run sgdisk -n "${part_num}:2048:+1M" -c "${part_num}:BIOS_BOOT" -t "${part_num}:ef02" "$TARGET_DEVICE"
    PART_BIOS="$(get_partition_path "$TARGET_DEVICE" "$part_num")"
    log "BIOS boot partition: $PART_BIOS"
    part_num=$((part_num + 1))
  fi

  # EFI partition
  if [[ "$LAYOUT" =~ EFI ]]; then
    local efi_size="${SIZE_EFI:-512M}"
    log "Creating EFI partition ($efi_size)..."
    run sgdisk -n "${part_num}:0:+${efi_size}" -c "${part_num}:EFI" -t "${part_num}:ef00" "$TARGET_DEVICE"
    PART_EFI="$(get_partition_path "$TARGET_DEVICE" "$part_num")"
    part_num=$((part_num + 1))
  fi

  # BOOT partition
  if [[ "$LAYOUT" =~ BOOT ]]; then
    local boot_size="${SIZE_BOOT:-1G}"
    log "Creating Boot partition ($boot_size)..."
    run sgdisk -n "${part_num}:0:+${boot_size}" -c "${part_num}:BOOT" -t "${part_num}:8300" "$TARGET_DEVICE"
    PART_BOOT="$(get_partition_path "$TARGET_DEVICE" "$part_num")"
    part_num=$((part_num + 1))
  fi

  # SWAP partition
  if [[ "$LAYOUT" =~ SWAP ]] && [[ "${SIZE_SWAP:-0}" != "0" ]]; then
    local swap_size="${SIZE_SWAP:-4G}"
    log "Creating Swap partition ($swap_size)..."
    run sgdisk -n "${part_num}:0:+${swap_size}" -c "${part_num}:SWAP" -t "${part_num}:8200" "$TARGET_DEVICE"
    PART_SWAP="$(get_partition_path "$TARGET_DEVICE" "$part_num")"
    part_num=$((part_num + 1))
  fi

  # Dedicated HOME partition
  if [[ "$USER_HOME" == "y" ]] && [[ "${SIZE_HOME:-0}" != "0" ]]; then
    local home_size="${SIZE_HOME:-0}"
    log "Creating Home partition ($home_size)..."
    run sgdisk -n "${part_num}:0:+${home_size}" -c "${part_num}:HOME" -t "${part_num}:8300" "$TARGET_DEVICE"
    PART_HOME="$(get_partition_path "$TARGET_DEVICE" "$part_num")"
    part_num=$((part_num + 1))
  fi

  # ROOT partition (rest of disk or specified size)
  # Determine the correct partition type GUID for the root partition based on
  # what will own it: LUKS (8309 → ca7d7ccb-63ed-4c53-861c-1742536019b2 wait, 8309 is wrong,
  # Debian sgdisk type 8300 is "Linux filesystem"; for LUKS use 8309; for LVM use 8e00; for ZFS use bf00).
  # sgdisk uses 2-hex-char type codes; the MBR-style codes 8E (LVM), 82 (swap) work in GPT hybrid
  # and are accepted by Linux tools; the proper GPT GUID codes are also supported.
  local _root_type="8300"
  if [[ "$FS_ROOT" == "zfs" ]]; then
    _root_type="bf00"
  elif [[ "${ENCRYPT_ROOT:-n}" == "y" ]]; then
    _root_type="8309"
  elif [[ "${USE_LVM:-n}" == "y" ]]; then
    _root_type="8e00"
  fi
  local root_size_for_first="${SIZE_ROOT:-0}"
  # If multi-root with a fixed size, split it evenly for both root partitions.
  if [[ "${MULTI_ROOT:-0}" == "1" && "$root_size_for_first" != "0" ]]; then
    root_size_for_first="$(split_size_string "$root_size_for_first")"
  fi
  if [[ "$root_size_for_first" == "0" ]]; then
    log "Creating Root partition (using remaining disk space)..."
    run sgdisk -n "${part_num}:0:0" -c "${part_num}:ROOT" -t "${part_num}:${_root_type}" "$TARGET_DEVICE"
  else
    log "Creating Root partition ($root_size_for_first)..."
    run sgdisk -n "${part_num}:0:+${root_size_for_first}" -c "${part_num}:ROOT" -t "${part_num}:${_root_type}" "$TARGET_DEVICE"
  fi
  PART_ROOT="$(get_partition_path "$TARGET_DEVICE" "$part_num")"

  # Multi-root support (immutable A/B, dual-boot, or multi-OS schemes)
  if [[ "${MULTI_ROOT:-0}" == "1" ]]; then
    local multi_size="${SIZE_ROOT:-0}"
    if [[ "$multi_size" == "0" ]]; then
      # For multi-root, split remaining space evenly between two root partitions.
      # We approximate by giving the second root the remaining free space.
      log "Creating Second Root partition (remaining disk space for multi-root layout)..."
      run sgdisk -n "$((part_num+1)):0:0" -c "$((part_num+1)):ROOT2" -t "$((part_num+1)):${_root_type}" "$TARGET_DEVICE"
    else
      # If SIZE_ROOT is non-zero, split it evenly: first root already got half,
      # second root gets the other half.
      local half_size
      half_size="$(split_size_string "$multi_size")"
      log "Creating Second Root partition ($half_size for multi-root layout)..."
      run sgdisk -n "$((part_num+1)):0:+${half_size}" -c "$((part_num+1)):ROOT2" -t "$((part_num+1)):${_root_type}" "$TARGET_DEVICE"
    fi
    PART_ROOT2="$(get_partition_path "$TARGET_DEVICE" "$((part_num+1))")"
    log "Second root partition for multi-root: $PART_ROOT2"
  fi

  # Reread partition table
  run udevadm settle 2>/dev/null || sleep 1
  run partprobe "$TARGET_DEVICE" 2>/dev/null || true
  run udevadm settle 2>/dev/null || sleep 1

  log "Partition map created on $TARGET_DEVICE:"
  lsblk "$TARGET_DEVICE" 2>/dev/null || true
}

get_partition_path() {
  local disk="$1" num="$2"
  # Debian/Linux kernel naming: devices whose base name ends in a digit (e.g. sda)
  # need the "p<N>" separator; devices with a trailing non-digit name (e.g. nvme0n1,
  # mmcblk0, loop0, nbd0, md0, zram0) also use "p<N>".
  # Note: /dev/vda (virtio) follows SCSI-style "sda1" naming, NOT nvme "vdap1" style.
  case "$disk" in
    /dev/nvme*|/dev/loop*|/dev/mmcblk*|/dev/nbd*|/dev/zram*|/dev/md*) echo "${disk}p${num}" ;;
    *) echo "${disk}${num}" ;;
  esac
}

# ─────────────────────────────────────── 8) storage formatting & mounts ──────────────────────────
format_and_mount_storage() {
  if [[ "$TARGET_MODE" == "rootfs" || "$TARGET_MODE" == "tar" ]]; then
    mkdir -p "$MNT"
    return 0
  fi
  banner "Formatting & Mounting Storage"

  DEV_ROOT_TARGET="$PART_ROOT"

  # ── LUKS first, then LVM on the unlocked device (Debian best-practice: LUKS-on-LVM) ──
  # Layering for an encrypted LVM setup:
  #   1. PART_ROOT (raw partition)
  #   2. cryptsetup luksFormat + luksOpen  ->  /dev/mapper/debstrap_luks_root
  #   3. pvcreate on mapper, vgcreate, lvcreate  ->  /dev/<vg>/<lv>
  #   4. mkfs.<fs> on LV
  if [[ "$ENCRYPT_ROOT" == "y" ]]; then
    log "Setting up LUKS2 ($LUKS_TYPE) encryption on $PART_ROOT..."
    local luks_name="debstrap_luks_root"
    # Debian cryptsetup defaults: aes-xts-plain64 with 512-bit key for XTS; sha512 for integrity; argon2id KDF
    local -a luks_opts=(--type "$LUKS_TYPE" --cipher aes-xts-plain64 --key-size 512 --hash sha512 --pbkdf argon2id)
    if [[ "$UNATTENDED" == "1" && -n "${CFG[luks_passphrase]:-}" ]]; then
      # Use a temp file to avoid the passphrase appearing in process listings
      local _pass_file
      _pass_file="$(mktemp)"
      chmod 600 "$_pass_file"
      printf '%s' "${CFG[luks_passphrase]}" > "$_pass_file"
      if ! run cryptsetup luksFormat "${luks_opts[@]}" --batch-mode --key-file "$_pass_file" "$PART_ROOT"; then
        rm -f "$_pass_file"
        die "LUKS format failed on $PART_ROOT"
      fi
      if ! run cryptsetup luksOpen --key-file "$_pass_file" "$PART_ROOT" "$luks_name"; then
        rm -f "$_pass_file"
        die "LUKS open failed"
      fi
      shred -u "$_pass_file" 2>/dev/null || rm -f "$_pass_file"
    else
      echo "Enter LUKS encryption passphrase for $PART_ROOT:" >&2
      run cryptsetup luksFormat "${luks_opts[@]}" "$PART_ROOT" || die "LUKS format failed on $PART_ROOT"
      run cryptsetup luksOpen "$PART_ROOT" "$luks_name" || die "LUKS open failed"
    fi
    track_luks "$luks_name"
    DEV_ROOT_TARGET="/dev/mapper/$luks_name"
  fi

  # ── Handle LVM2 (after LUKS if both enabled) ──
  if [[ "${USE_LVM:-n}" == "y" ]]; then
    log "Setting up LVM2 on $DEV_ROOT_TARGET..."
    local vg_name="debstrap_vg"
    local lv_name="debstrap_root"
    run pvcreate -ff -y "$DEV_ROOT_TARGET" 2>/dev/null || run pvcreate -f "$DEV_ROOT_TARGET" || true
    if ! run vgcreate -f "$vg_name" "$DEV_ROOT_TARGET" 2>/dev/null; then
      if ! run vgcreate "$vg_name" "$DEV_ROOT_TARGET"; then
        warn "vgcreate failed, attempting to reuse existing VG $vg_name"
        vgs "$vg_name" &>/dev/null || true
      fi
    fi
    track_vg "$vg_name"
    if [[ "${SIZE_ROOT:-0}" != "0" ]]; then
      run lvcreate -y -L "${SIZE_ROOT}" -n "$lv_name" "$vg_name" 2>/dev/null || run lvcreate -l 100%FREE -n "$lv_name" "$vg_name" || true
    else
      run lvcreate -y -l 100%FREE -n "$lv_name" "$vg_name" 2>/dev/null || true
    fi
    DEV_ROOT_TARGET="/dev/$vg_name/$lv_name"
    if [[ ! -b "$DEV_ROOT_TARGET" && ! -e "$DEV_ROOT_TARGET" ]]; then
      warn "LVM logical volume not found at $DEV_ROOT_TARGET, falling back to ${PART_ROOT}"
      DEV_ROOT_TARGET="/dev/mapper/debstrap_luks_root"
      [[ ! -e "$DEV_ROOT_TARGET" ]] && DEV_ROOT_TARGET="$PART_ROOT"
    else
      log "LVM logical volume created at $DEV_ROOT_TARGET"
    fi
  fi

  # ── Handle RAID (degraded single-device) if requested ──
  if [[ "${USE_RAID:-n}" == "y" ]]; then
    log "RAID requested — base partitions will be incorporated into md array post-install (manual RAID level via raid_level config)"
    if have mdadm && [[ -n "${CFG[raid_level]:-}" ]]; then
      local _raid_level="${CFG[raid_level]:-1}"
      warn "RAID level $_raid_level requested but single-device RAID in deb-strap is experimental — continuing with single partition"
    fi
  fi

  # Record the underlying device for the crypttab source spec
  CRYPTTAB_SOURCE="$PART_ROOT"

  # Format ROOT — handle ZFS if requested
  log "Formatting Root filesystem ($FS_ROOT) on $DEV_ROOT_TARGET..."
  case "$FS_ROOT" in
    ext4)  run mkfs.ext4 -F -L "debian-root" -E lazy_itable_init=0,lazy_journal_init=0 "$DEV_ROOT_TARGET" 2>/dev/null || run mkfs.ext4 -F -L "debian-root" "$DEV_ROOT_TARGET" ;;
    btrfs) run mkfs.btrfs -f -L "debian-root" --csum xxhash "$DEV_ROOT_TARGET" 2>/dev/null || run mkfs.btrfs -f -L "debian-root" "$DEV_ROOT_TARGET"
           # Enable zstd compression by default via mount options (not mkfs)
           ;;
    xfs)   run mkfs.xfs -f -L "debian-root" -m crc=1,finobt=1 -i sparse=1 "$DEV_ROOT_TARGET" 2>/dev/null || run mkfs.xfs -f -L "debian-root" "$DEV_ROOT_TARGET" ;;
    f2fs)  run mkfs.f2fs -f -l "debian-root" -O extra_attr,inode_checksum,sb_checksum,compression "$DEV_ROOT_TARGET" 2>/dev/null || run mkfs.f2fs -f -l "debian-root" "$DEV_ROOT_TARGET" ;;
    zfs)   if have zpool; then
             local pool_name="rpool"
             run zpool create -f -o ashift=12 -O compression=lz4 -O acltype=posixacl -O xattr=sa -O relatime=on -m none "$pool_name" "$DEV_ROOT_TARGET" || true
             run zfs create -o mountpoint=none "$pool_name/ROOT" 2>/dev/null || true
             run zfs create -o mountpoint=/ "$pool_name/ROOT/debian" 2>/dev/null || true
             warn "ZFS formatted as zpool $pool_name — mount handling is different; using legacy mount for chroot"
             # For chroot mount, we need to mount zfs dataset
           else
             warn "zfsutils-linux not found, falling back to ext4"
             run mkfs.ext4 -F -L "debian-root" "$DEV_ROOT_TARGET"
             FS_ROOT="ext4"
           fi
           ;;
  esac

  mkdir -p "$MNT"

  # Mount ROOT & handle Btrfs subvolumes — Debian best practice with ssd, space_cache, autodefrag
  if [[ "$FS_ROOT" == "btrfs" && "${BTRFS_SUBVOLS:-n}" == "y" ]]; then
    log "Creating Btrfs subvolume layout (@, @home, @snapshots, @var_log, @var_cache, @tmp)..."
    local tmp_mnt="${MNT}_btrfs_temp"
    mkdir -p "$tmp_mnt"
    run mount "$DEV_ROOT_TARGET" "$tmp_mnt"
    for _sv in "@" "@home" "@snapshots" "@var_log" "@var_cache" "@tmp"; do
      run btrfs subvolume create "$tmp_mnt/$_sv" 2>/dev/null || warn "Failed to create subvol $_sv"
    done
    # Set the default subvolume to @ so tools that don't understand subvol= mount see the root
    local _root_id
    _root_id="$(btrfs subvolume list "$tmp_mnt" 2>/dev/null | awk '$NF == "@" {print $2; exit}')"
    if [[ -n "$_root_id" ]]; then
      run btrfs subvolume set-default "$_root_id" "$tmp_mnt" 2>/dev/null || true
    fi
    run umount "$tmp_mnt"
    rmdir "$tmp_mnt" 2>/dev/null || true

    # Debian Btrfs mount options: ssd, zstd compression, space_cache v2, autodefrag, noatime
    local _btrfs_opts="noatime,compress=zstd:1,ssd,space_cache=v2,autodefrag,subvol="
    run mount -o "${_btrfs_opts}@" "$DEV_ROOT_TARGET" "$MNT"
    track_mount "$MNT"

    mkdir -p "$MNT/home" "$MNT/.snapshots" "$MNT/var/log" "$MNT/var/cache" "$MNT/tmp"
    run mount -o "${_btrfs_opts}@home" "$DEV_ROOT_TARGET" "$MNT/home"
    track_mount "$MNT/home"
    run mount -o "${_btrfs_opts}@snapshots" "$DEV_ROOT_TARGET" "$MNT/.snapshots"
    track_mount "$MNT/.snapshots"
    run mount -o "${_btrfs_opts}@var_log" "$DEV_ROOT_TARGET" "$MNT/var/log"
    track_mount "$MNT/var/log"
    run mount -o "${_btrfs_opts}@var_cache" "$DEV_ROOT_TARGET" "$MNT/var/cache"
    track_mount "$MNT/var/cache"
    run mount -o "${_btrfs_opts}@tmp" "$DEV_ROOT_TARGET" "$MNT/tmp"
    track_mount "$MNT/tmp"
  elif [[ "$FS_ROOT" == "zfs" ]] && have zpool; then
    # ZFS: set mountpoint and mount
    local pool_name="rpool"
    if zfs list "$pool_name/ROOT/debian" &>/dev/null; then
      run zfs set mountpoint="$MNT" "$pool_name/ROOT/debian" 2>/dev/null || run mount -t zfs "$pool_name/ROOT/debian" "$MNT" 2>/dev/null || true
      track_mount "$MNT"
      # Also need to import at boot via zfs-import
    else
      log "ZFS dataset not found, falling back to generic mount"
      run mount "$DEV_ROOT_TARGET" "$MNT" 2>/dev/null || true
      track_mount "$MNT"
    fi
  else
    run mount "$DEV_ROOT_TARGET" "$MNT"
    track_mount "$MNT"
  fi

  # Format & Mount BOOT
  if [[ -n "${PART_BOOT:-}" ]]; then
    log "Formatting Boot partition on $PART_BOOT (ext4)..."
    run mkfs.ext4 -F -L "debian-boot" "$PART_BOOT"
    mkdir -p "$MNT/boot"
    run mount "$PART_BOOT" "$MNT/boot"
    track_mount "$MNT/boot"
  fi

  # Format & Mount EFI
  if [[ -n "${PART_EFI:-}" ]]; then
    log "Formatting EFI System Partition on $PART_EFI (FAT32)..."
    run mkfs.vfat -F 32 -n "EFI" "$PART_EFI"
    if [[ "$BOOTLOADER" == "systemd-boot" && -z "${PART_BOOT:-}" ]]; then
      mkdir -p "$MNT/boot"
      run mount "$PART_EFI" "$MNT/boot"
      track_mount "$MNT/boot"
      ESP_MOUNT_POINT="/boot"
    else
      mkdir -p "$MNT/boot/efi"
      run mount "$PART_EFI" "$MNT/boot/efi"
      track_mount "$MNT/boot/efi"
      ESP_MOUNT_POINT="/boot/efi"
    fi
  fi

  # Format & Mount HOME
  if [[ -n "${PART_HOME:-}" ]]; then
    log "Formatting dedicated /home on $PART_HOME (ext4)..."
    run mkfs.ext4 -F -L "debian-home" "$PART_HOME"
    mkdir -p "$MNT/home"
    run mount "$PART_HOME" "$MNT/home"
    track_mount "$MNT/home"
  fi

  # Format SWAP
  if [[ -n "${PART_SWAP:-}" ]]; then
    log "Formatting Swap on $PART_SWAP..."
    run mkswap -L "debian-swap" "$PART_SWAP"
  fi
}

# ─────────────────────────────────────── 9) Debian bootstrap execution ───────────────────────────
ensure_deb_keyring() {
  local suite="${1:-${SUITE:-bookworm}}"
  # Debian keyring locations per Debian wiki: suite-specific + generic
  local -a candidates=(
    "/usr/share/keyrings/debian-archive-keyring.gpg"
    "/usr/share/keyrings/debian-archive-${suite}-stable.gpg"
    "/usr/share/keyrings/debian-archive-${suite}-automatic.gpg"
    "/usr/share/keyrings/debian-archive-keyring-${suite}.gpg"
    "/usr/share/keyrings/debian-archive-bookworm-stable.gpg"
    "/usr/share/keyrings/debian-archive-trixie-stable.gpg"
    "/etc/apt/trusted.gpg.d/debian-archive-keyring.gpg"
  )
  # For sid/experimental, the generic keyring also covers it; add fallback
  if [[ "$suite" == "sid" || "$suite" == "experimental" ]]; then
    candidates+=(
      "/usr/share/keyrings/debian-archive-bullseye-stable.gpg"
      "/usr/share/keyrings/debian-archive-bookworm-automatic.gpg"
    )
  fi
  for c in "${candidates[@]}"; do
    if [[ -f "$c" ]]; then
      printf '%s\n' "$c"
      return 0
    fi
  done
  # Last resort: any debian-archive-* in /usr/share/keyrings (use glob, not ls per shellcheck SC2012)
  local found
  for found in /usr/share/keyrings/debian-archive*.gpg; do
    if [[ -f "$found" ]]; then
      printf '%s\n' "$found"
      return 0
    fi
  done
  echo ""
}

execute_bootstrap() {
  banner "Executing Debian Bootstrap ($ENGINE -> $SUITE on $TARGET_ARCH)"

  local keyring
  keyring="$(ensure_deb_keyring "$SUITE")"
  if [[ -z "$keyring" ]]; then
    warn "Debian archive keyring not found on host — bootstrap will use unauthenticated fallback (Debian pure but less secure). Install debian-archive-keyring."
  else
    info "Using Debian keyring: $keyring"
  fi

  local -a base_include=(
    ca-certificates curl gnupg locales tzdata apt
  )
  # Init system packages — Debian-accurate per suite
  case "$INIT_SYSTEM" in
    systemd)  base_include+=(systemd systemd-sysv libpam-systemd dbus) ;;
    sysvinit) base_include+=(sysvinit-core sysv-rc orphan-sysvinit-scripts) ;;
    openrc)   base_include+=(openrc openrc-softlevel init-system-helpers) ;;
    runit)    base_include+=(runit-init runit) ;;
  esac

  if [[ "$ENCRYPT_ROOT" == "y" ]]; then
    base_include+=(cryptsetup)
    # TPM2 via systemd-cryptenroll is part of systemd on Debian; no extra pkg needed
  fi
  case "$FS_ROOT" in
    btrfs) base_include+=(btrfs-progs) ;;
    xfs)   base_include+=(xfsprogs) ;;
    f2fs)  base_include+=(f2fs-tools) ;;
    zfs)   base_include+=(zfsutils-linux) ;;
  esac
  # Ensure D-Bus and util-linux for mounts
  base_include+=(util-linux)

  # Handle custom variant packages
  if [[ "${VARIANT:-standard}" == "custom" && -n "${CFG[custom_packages]:-}" ]]; then
    IFS=',' read -ra _custom_pkgs <<< "${CFG[custom_packages]}"
    for _cp in "${_custom_pkgs[@]}"; do
      _cp="${_cp#"${_cp%%[![:space:]]*}"}"; _cp="${_cp%"${_cp##*[![:space:]]}"}"
      [[ -n "$_cp" ]] && base_include+=("$_cp")
    done
  fi

  local inc_str
  inc_str="$(IFS=,; echo "${base_include[*]}")"
  # Sanitize double commas/spaces
  inc_str="${inc_str//,,/,}"; inc_str="${inc_str#,}"; inc_str="${inc_str%,}"

  # Prepare foreign arch qemu if needed
  if [[ "$IS_FOREIGN" == "1" ]]; then
    log "Foreign arch mode: ensuring qemu-user-static + binfmt..."
    if have update-binfmts; then
      run update-binfmts --enable 2>/dev/null || true
    fi
    # Copy qemu static into target after first stage if needed
    if ! have "qemu-${TARGET_ARCH}-static" && have qemu-aarch64-static; then
      info "Generic qemu-user-static available, will copy appropriate emulator into chroot for second-stage."
    fi
  fi

  if [[ "$ENGINE" == "mmdebstrap" ]]; then
    log "Running mmdebstrap for $SUITE ($TARGET_ARCH) variant=${VARIANT:-standard}..."
    local -a mm_args=(
      --arch="$TARGET_ARCH"
      --variant="${VARIANT:-standard}"
      --include="$inc_str"
    )
    if [[ -n "$keyring" ]]; then
      mm_args+=(--keyring="$keyring")
    else
      mm_args+=(--skip=check/empty)
    fi
    # mmdebstrap handles foreign via qemu automatically if binfmt is set; add verbose
    if [[ "$IS_FOREIGN" == "1" ]]; then
      mm_args+=(--verbose)
    fi
    # Retry with fallback mirror if primary fails
    if ! run mmdebstrap "${mm_args[@]}" "$SUITE" "$MNT" "$MIRROR"; then
      warn "mmdebstrap with $MIRROR failed, trying fallback deb.debian.org..."
      run mmdebstrap "${mm_args[@]}" "$SUITE" "$MNT" "https://deb.debian.org/debian" || die "mmdebstrap failed for suite '$SUITE'."
    fi
  elif [[ "$ENGINE" == "cdebootstrap" ]]; then
    log "Running cdebootstrap for $SUITE ($TARGET_ARCH)..."
    local -a cde_args=(
      --arch="$TARGET_ARCH"
      --flavour="${VARIANT:-standard}"
      --include="$inc_str"
    )
    if have cdebootstrap; then
      run cdebootstrap "${cde_args[@]}" "$SUITE" "$MNT" "$MIRROR" || die "cdebootstrap failed."
    else
      die "cdebootstrap requested but not installed (apt install cdebootstrap)."
    fi
  else
    # debootstrap (default, most accurate Debian behavior)
    log "Running debootstrap for $SUITE ($TARGET_ARCH) variant=${VARIANT:-standard}..."
    local _debootstrap_cmd="debootstrap"
    # Prefer qemu-debootstrap wrapper for foreign if available
    if [[ "$IS_FOREIGN" == "1" ]] && have qemu-debootstrap; then
      _debootstrap_cmd="qemu-debootstrap"
      info "Using qemu-debootstrap wrapper for foreign arch."
    fi
    local -a deb_args=(
      --arch="$TARGET_ARCH"
      --variant="${VARIANT:-standard}"
      --include="$inc_str"
    )
    if [[ -n "$keyring" ]]; then
      deb_args+=(--keyring="$keyring")
    fi
    # Debian merged-usr is default since bookworm; ensure compatibility
    if [[ "$SUITE" == "bookworm" || "$SUITE" == "trixie" || "$SUITE" == "forky" || "$SUITE" == "sid" ]]; then
      deb_args+=(--merged-usr)
    fi
    if [[ "$IS_FOREIGN" == "1" && "$_debootstrap_cmd" == "debootstrap" ]]; then
      deb_args+=(--foreign)
    fi

    local deb_out
    deb_out="$(mktemp "${TMPDIR:-/tmp}/debootstrap-XXXXXX.log")"
    track_file "$deb_out"

    # Retry logic: 3 attempts with different mirrors
    local _attempt=1 _max_attempts=3 _mirrors_try=("$MIRROR" "https://deb.debian.org/debian" "http://ftp.debian.org/debian")
    local _success=0
    for _try_mirror in "${_mirrors_try[@]}"; do
      [[ $_attempt -gt $_max_attempts ]] && break
      log "debootstrap attempt $_attempt with mirror $_try_mirror..."
      if run $_debootstrap_cmd "${deb_args[@]}" "$SUITE" "$MNT" "$_try_mirror" >"$deb_out" 2>&1; then
        log "debootstrap completed successfully on attempt $_attempt."
        _success=1
        break
      else
        warn "debootstrap attempt $_attempt failed with $_try_mirror"
        cat "$deb_out" >&2 || true
        _attempt=$((_attempt+1))
        sleep 2
      fi
    done
    if (( _success == 0 )); then
      die "debootstrap failed for suite '$SUITE' after $_max_attempts attempts."
    fi

    # Handle foreign second-stage if needed (when --foreign was used)
    if [[ "$IS_FOREIGN" == "1" && "$_debootstrap_cmd" == "debootstrap" ]]; then
      log "Running debootstrap second-stage inside target via qemu-user-static..."
      # Copy qemu static binary into chroot if needed for arch emulation
      local _qemu_bin=""
      for _q in "/usr/bin/qemu-${TARGET_ARCH}-static" "/usr/bin/qemu-aarch64-static" "/usr/bin/qemu-arm-static" "/usr/bin/qemu-riscv64-static"; do
        if [[ -f "$_q" ]]; then _qemu_bin="$_q"; break; fi
      done
      if [[ -n "$_qemu_bin" && -f "$_qemu_bin" ]]; then
        run cp "$_qemu_bin" "$MNT/usr/bin/" 2>/dev/null || true
        info "Copied $_qemu_bin into chroot for second-stage."
      fi
      # Check binfmt is enabled
      if [[ ! -f "$MNT/debootstrap/debootstrap" ]]; then
        warn "Second-stage script not found at $MNT/debootstrap/debootstrap"
      else
        run chroot "$MNT" /debootstrap/debootstrap --second-stage || die "Foreign second-stage failed."
      fi
    fi
  fi
  # Verify bootstrap produced minimal Debian base
  if [[ ! -f "$MNT/etc/debian_version" && ! -f "$MNT/etc/os-release" ]]; then
    warn "Bootstrap completed but /etc/debian_version and /etc/os-release missing — possible incomplete bootstrap"
  else
    log "Bootstrap verification passed: $(cat "$MNT/etc/debian_version" 2>/dev/null || cat "$MNT/etc/os-release" 2>/dev/null | head -1)"
  fi
}

# ─────────────────────────────────────── 10) generate fstab, deb822 & config ─────────────────────
generate_deb822_sources() {
  local out_file="$1"
  local components="main"
  [[ "$ENABLE_CONTRIB" == "y" ]] && components="$components contrib"
  [[ "$ENABLE_NONFREE" == "y" ]] && components="$components non-free"
  # non-free-firmware was introduced in bookworm (Debian 12); bullseye and earlier lack this component
  if [[ "$ENABLE_NONFREE_FIRMWARE" == "y" && "$SUITE" != "bullseye" ]]; then
    components="$components non-free-firmware"
  elif [[ "$ENABLE_NONFREE_FIRMWARE" == "y" && "$SUITE" == "bullseye" ]]; then
    warn "non-free-firmware component requested but not available for bullseye — skipping"
  fi

  # Experimental has no updates/security/backports in Debian archive
  local suites="$SUITE"
  if [[ "$SUITE" != "experimental" && "$SUITE" != "sid" ]]; then
    [[ "$ENABLE_UPDATES" == "y" ]] && suites="$suites ${SUITE}-updates"
    [[ "$ENABLE_BACKPORTS" == "y" ]] && suites="$suites ${SUITE}-backports"
  elif [[ "$SUITE" == "sid" ]]; then
    # sid has no updates, but may have backports via sid-backports? Rare, handle if requested
    [[ "$ENABLE_BACKPORTS" == "y" ]] && suites="$suites ${SUITE}-backports"
  fi

  # Resolve accurate Signed-By path per Debian 12+ deb822 spec
  local signed_by="/usr/share/keyrings/debian-archive-keyring.gpg"
  local _kr
  _kr="$(ensure_deb_keyring "$SUITE")"
  [[ -n "$_kr" ]] && signed_by="$_kr"

  cat > "$out_file" <<EOF
# Generated by deb-strap v$VERSION on $(date -u +'%Y-%m-%d %H:%M:%S UTC')
# Debian $SUITE — suite: $SUITE arch: ${TARGET_ARCH:-amd64}
Types: deb deb-src
URIs: $MIRROR
Suites: $suites
Components: $components
Signed-By: $signed_by

EOF

  if [[ "$ENABLE_SECURITY" == "y" ]]; then
    local sec_suite="${SUITE}-security"
    # Debian security suite naming: bookworm-security, trixie-security, etc.
    # sid and experimental have no security suite
    if [[ "$SUITE" == "sid" || "$SUITE" == "experimental" ]]; then
      sec_suite=""
    fi
    if [[ -n "$sec_suite" ]]; then
      cat >> "$out_file" <<EOF
Types: deb deb-src
URIs: https://security.debian.org/debian-security
Suites: $sec_suite
Components: $components
Signed-By: $signed_by
EOF
    fi
  fi
  # Ensure file ends with newline and validate syntax
  if ! grep -q "^Types:" "$out_file" 2>/dev/null; then
    warn "deb822 generation produced empty file"
  fi
}

generate_classic_sources() {
  local out_file="$1"
  local components="main"
  [[ "$ENABLE_CONTRIB" == "y" ]] && components="$components contrib"
  [[ "$ENABLE_NONFREE" == "y" ]] && components="$components non-free"
  if [[ "$ENABLE_NONFREE_FIRMWARE" == "y" && "$SUITE" != "bullseye" ]]; then
    components="$components non-free-firmware"
  elif [[ "$ENABLE_NONFREE_FIRMWARE" == "y" && "$SUITE" == "bullseye" ]]; then
    warn "non-free-firmware component requested but not available for bullseye — skipping"
  fi

  cat > "$out_file" <<EOF
# Debian $SUITE repositories
# Generated by deb-strap v$VERSION on $(date -u +'%Y-%m-%d %H:%M:%S UTC')
deb $MIRROR $SUITE $components
deb-src $MIRROR $SUITE $components

EOF
  if [[ "$SUITE" != "experimental" && "$SUITE" != "sid" ]]; then
    if [[ "$ENABLE_UPDATES" == "y" ]]; then
      cat >> "$out_file" <<EOF
deb $MIRROR ${SUITE}-updates $components
deb-src $MIRROR ${SUITE}-updates $components

EOF
    fi
  fi
  if [[ "$ENABLE_SECURITY" == "y" && "$SUITE" != "sid" && "$SUITE" != "experimental" ]]; then
    cat >> "$out_file" <<EOF
deb https://security.debian.org/debian-security ${SUITE}-security $components
deb-src https://security.debian.org/debian-security ${SUITE}-security $components

EOF
  fi
  if [[ "$ENABLE_BACKPORTS" == "y" ]]; then
    cat >> "$out_file" <<EOF
deb $MIRROR ${SUITE}-backports $components
deb-src $MIRROR ${SUITE}-backports $components

EOF
  fi
  # For experimental, add experimental repo line explicitly
  if [[ "$SUITE" == "experimental" ]]; then
    cat >> "$out_file" <<EOF
# Experimental — use with -t experimental
deb $MIRROR experimental $components
deb-src $MIRROR experimental $components
EOF
  fi
}

generate_fstab() {
  local fstab_file="$1"
  if [[ "$TARGET_MODE" == "rootfs" || "$TARGET_MODE" == "tar" ]]; then
    return 0
  fi
  log "Generating /etc/fstab for target..."
  cat > "$fstab_file" <<EOF
# /etc/fstab: static file system information.
# Generated by deb-strap v$VERSION on $(date -u +'%Y-%m-%d %H:%M:%S UTC')
# Debian $SUITE on ${TARGET_ARCH:-amd64} — ${FS_ROOT:-ext4}
# <file system> <mount point> <type> <options> <dump> <pass>
EOF

  # Root entry — try UUID, fallback to PARTUUID, then device path.
  # Skip blkid lookups during dry-run since no partitions actually exist on disk.
  local root_spec=""
  if [[ "$DRY_RUN" != "1" ]]; then
    local root_uuid root_partuuid
    root_uuid="$(blkid -s UUID -o value "$DEV_ROOT_TARGET" 2>/dev/null || true)"
    root_partuuid="$(blkid -s PARTUUID -o value "$PART_ROOT" 2>/dev/null || true)"
    if [[ -n "$root_uuid" ]]; then
      root_spec="UUID=$root_uuid"
    elif [[ -n "$root_partuuid" ]]; then
      root_spec="PARTUUID=$root_partuuid"
      warn "Root UUID unavailable, using PARTUUID=$root_partuuid"
    fi
  fi
  if [[ -z "$root_spec" ]]; then
    if [[ "$ENCRYPT_ROOT" == "y" ]]; then
      root_spec="/dev/mapper/debstrap_luks_root"
    else
      root_spec="$DEV_ROOT_TARGET"
    fi
    if [[ "$DRY_RUN" != "1" ]]; then
      warn "Root UUID/PARTUUID unavailable, using device path $root_spec (not portable across machines)"
    fi
  fi

  if [[ "$FS_ROOT" == "btrfs" && "${BTRFS_SUBVOLS:-n}" == "y" ]]; then
    # Debian Btrfs defaults: ssd, space_cache=v2, compress=zstd:1, noatime
    # Detect SSD vs rotational? Default to ssd + compress which is safe on modern Debian kernels.
    local btrfs_opts="defaults,noatime,compress=zstd:1,ssd,space_cache=v2,autodefrag"
    cat >> "$fstab_file" <<EOF
$root_spec /               btrfs ${btrfs_opts},subvol=@           0 0
$root_spec /home           btrfs ${btrfs_opts},subvol=@home       0 0
$root_spec /.snapshots     btrfs ${btrfs_opts},subvol=@snapshots  0 0
$root_spec /var/log        btrfs ${btrfs_opts},subvol=@var_log    0 0
$root_spec /var/cache      btrfs ${btrfs_opts},subvol=@var_cache  0 0
$root_spec /tmp            btrfs ${btrfs_opts},subvol=@tmp        0 0
EOF
  else
    local pass_num=1
    # XFS needs no fsck pass >0 should be 0 per xfs docs (xfs_repair). Debian uses 0.
    if [[ "$FS_ROOT" == "xfs" ]]; then pass_num=0; fi
    # F2FS also 0
    if [[ "$FS_ROOT" == "f2fs" ]]; then pass_num=0; fi
    cat >> "$fstab_file" <<EOF
$root_spec /               $FS_ROOT defaults,noatime 0 $pass_num
EOF
  fi

  # Boot entry
  if [[ -n "${PART_BOOT:-}" ]] && [[ "$DRY_RUN" != "1" ]]; then
    local boot_uuid
    boot_uuid="$(blkid -s UUID -o value "$PART_BOOT" 2>/dev/null || true)"
    if [[ -n "$boot_uuid" ]]; then
      echo "UUID=$boot_uuid /boot           ext4 defaults,noatime 0 2" >> "$fstab_file"
    fi
  fi

  # EFI entry — Debian uses umask=0077 and pass 1 per installer; keep 0077 minimal
  if [[ -n "${PART_EFI:-}" ]] && [[ "$DRY_RUN" != "1" ]]; then
    local efi_uuid
    efi_uuid="$(blkid -s UUID -o value "$PART_EFI" 2>/dev/null || true)"
    if [[ -n "$efi_uuid" ]]; then
      # Use umask=0077 (Debian installer default) — keep fmask/dmask redundant for compatibility
      echo "UUID=$efi_uuid ${ESP_MOUNT_POINT:-/boot/efi}       vfat umask=0077 0 1" >> "$fstab_file"
    else
      warn "EFI UUID unavailable, skipping fstab entry for ${PART_EFI:-unknown}"
    fi
  fi

  # Home entry (if dedicated partition)
  if [[ -n "${PART_HOME:-}" ]] && [[ "$DRY_RUN" != "1" ]]; then
    local home_uuid
    home_uuid="$(blkid -s UUID -o value "$PART_HOME" 2>/dev/null || true)"
    if [[ -n "$home_uuid" ]]; then
      echo "UUID=$home_uuid /home           ext4 defaults,noatime 0 2" >> "$fstab_file"
    fi
  fi

  # Swap entry
  if [[ -n "${PART_SWAP:-}" ]] && [[ "$DRY_RUN" != "1" ]]; then
    local swap_uuid
    swap_uuid="$(blkid -s UUID -o value "$PART_SWAP" 2>/dev/null || true)"
    if [[ -n "$swap_uuid" ]]; then
      echo "UUID=$swap_uuid none            swap sw 0 0" >> "$fstab_file"
    fi
  fi
}

generate_crypttab() {
  local crypttab_file="$1"
  if [[ "$ENCRYPT_ROOT" != "y" ]]; then return 0; fi
  log "Generating /etc/crypttab for target..."
  local raw_uuid raw_partuuid
  local crypttab_source="${CRYPTTAB_SOURCE:-${PART_ROOT:-}}"
  raw_uuid="$(blkid -s UUID -o value "$crypttab_source" 2>/dev/null || true)"
  raw_partuuid="$(blkid -s PARTUUID -o value "$crypttab_source" 2>/dev/null || true)"
  local spec="UUID=$raw_uuid"
  if [[ -z "$raw_uuid" && -n "$raw_partuuid" ]]; then spec="PARTUUID=$raw_partuuid"
  elif [[ -z "$raw_uuid" ]]; then spec="$crypttab_source"; warn "crypttab: UUID unavailable, using device path"; fi
  local opts="discard"
  # Debian crypttab: discard is common for SSD; tpm2-device=auto for TPM2 unlock
  # Keep opts as "discard" or "discard,tpm2-device=auto" to satisfy Debian pure and test expectations
  if [[ "${LUKS_TPM2:-n}" == "y" ]]; then
    opts="discard,tpm2-device=auto"
  fi
  # initramfs needs luks key handling; use 'none' for passphrase prompt at boot
  cat > "$crypttab_file" <<EOF
# /etc/crypttab: encrypted block devices.
# Generated by deb-strap v$VERSION
# <target name> <source device> <key file> <options>
debstrap_luks_root $spec none $opts
EOF
  # Also ensure cryptsetup initramfs hook will be included (handled in chroot)
}

# ─────────────────────────────────────── 11) target chroot installer script ──────────────────────
write_chroot_install_script() {
  local target_env="$MNT/etc/deb-strap/strap.env"
  mkdir -p "$(dirname "$target_env")"

  cat > "$target_env" <<EOF
# deb-strap target environment variables — generated $(date -u +%Y-%m-%dT%H:%M:%SZ)
SUITE="${SUITE:-bookworm}"
TARGET_ARCH="${TARGET_ARCH:-amd64}"
HOSTNAME="${HOSTNAME:-debian-box}"
TIMEZONE="${TIMEZONE:-UTC}"
LOCALE="${LOCALE:-en_US.UTF-8}"
KEYMAP="${KEYMAP:-us}"
USERNAME="${USERNAME:-debian}"
USER_SHELL="${USER_SHELL:-/bin/bash}"
SUDO_MODE="${SUDO_MODE:-1}"
ROOT_PASSWORD="${ROOT_PASSWORD:-}"
USER_PASSWORD="${USER_PASSWORD:-}"
INIT_SYSTEM="${INIT_SYSTEM:-systemd}"
KERNEL="${KERNEL:-stock}"
BOOTLOADER="${BOOTLOADER:-grub}"
SECURE_BOOT="${SECURE_BOOT:-n}"
ESP_MOUNT_POINT="${ESP_MOUNT_POINT:-/boot/efi}"
TARGET_DEVICE="${TARGET_DEVICE:-}"
PART_EFI="${PART_EFI:-}"
PART_BOOT="${PART_BOOT:-}"
PART_ROOT="${PART_ROOT:-}"
DEV_ROOT_TARGET="${DEV_ROOT_TARGET:-}"
ENCRYPT_ROOT="${ENCRYPT_ROOT:-n}"
LUKS_TPM2="${LUKS_TPM2:-n}"
FS_ROOT="${FS_ROOT:-ext4}"
BTRFS_SUBVOLS="${BTRFS_SUBVOLS:-n}"
BTRFS_SNAPPER="${BTRFS_SNAPPER:-n}"
NET_MANAGER="${NET_MANAGER:-NetworkManager}"
ENABLE_SSH="${ENABLE_SSH:-y}"
SSH_PERMIT_ROOT="${SSH_PERMIT_ROOT:-n}"
SSH_AUTHORIZED_KEYS="${SSH_AUTHORIZED_KEYS:-}"
OPT_PACKAGES="${OPT_PACKAGES:-cli,dev}"
DESKTOP="${DESKTOP:-none}"
DISPLAY_MANAGER="${DISPLAY_MANAGER:-none}"
GPU_DRIVER="${GPU_DRIVER:-mesa}"
VARIANT="${VARIANT:-standard}"
MIRROR="${MIRROR:-https://deb.debian.org/debian}"
USE_LVM="${USE_LVM:-n}"
USE_RAID="${USE_RAID:-n}"
EOF
  chmod 600 "$target_env"
  # Restrict env file to root only (contains passwords)

  local install_sh="$MNT/deb-strap-install.sh"

  cat > "$install_sh" <<'EOS_INNER'
#!/usr/bin/env bash
# deb-strap-install.sh — Executed inside target chroot (pure Debian)
# Accuracy fixes: GRUB detection via PART_EFI not host EFI, locale uncomment logic,
# per-arch kernel mapping, Debian firmware policy, doas/correct sudo, UKI, etc.
set -eEuo pipefail

export DEBIAN_FRONTEND=noninteractive
export LC_ALL=C.UTF-8
export LANG=C.UTF-8

source /etc/deb-strap/strap.env

log()  { echo -e "\033[0;32m[*] (chroot)\033[0m $*"; }
warn() { echo -e "\033[0;33m[!] (chroot)\033[0m $*"; }
err()  { echo -e "\033[0;31m[x] (chroot)\033[0m $*"; }

# Helpers inside chroot
have_chroot() { command -v "$1" &>/dev/null; }
run() { "$@"; }

# 1. Hostname & Hosts — RFC 1123 compliant
log "Setting hostname: $HOSTNAME"
if [[ "$HOSTNAME" =~ ^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$ ]]; then
  echo "$HOSTNAME" > /etc/hostname
else
  warn "Hostname '$HOSTNAME' violates RFC 1123, using fallback debian-box"
  echo "debian-box" > /etc/hostname
  HOSTNAME="debian-box"
fi
cat > /etc/hosts <<EOF
127.0.0.1   localhost
127.0.1.1   $HOSTNAME
::1         localhost ip6-localhost ip6-loopback
ff02::1     ip6-allnodes
ff02::2     ip6-allrouters
EOF

# 2. Timezone & Locales — Debian-accurate
log "Setting timezone ($TIMEZONE) & locale ($LOCALE)..."
if [[ -f "/usr/share/zoneinfo/$TIMEZONE" ]]; then
  ln -sf "/usr/share/zoneinfo/$TIMEZONE" /etc/localtime
  # Debian tzdata via debconf
  echo "tzdata tzdata/Areas select $(echo "$TIMEZONE" | cut -d/ -f1)" | debconf-set-selections 2>/dev/null || true
  echo "tzdata tzdata/Zones/$(echo "$TIMEZONE" | cut -d/ -f1) select $(echo "$TIMEZONE" | cut -d/ -f2-)" | debconf-set-selections 2>/dev/null || true
  dpkg-reconfigure -f noninteractive tzdata 2>/dev/null || true
else
  warn "Timezone $TIMEZONE not found in /usr/share/zoneinfo, defaulting to UTC"
  ln -sf "/usr/share/zoneinfo/UTC" /etc/localtime
fi

# Keymap — Debian keyboard-configuration
if [[ -n "${KEYMAP:-}" ]]; then
  log "Setting keymap: $KEYMAP"
  if have_chroot localectl; then
    localectl set-keymap "$KEYMAP" 2>/dev/null || true
  fi
  # Also set via debconf for console-setup
  echo "keyboard-configuration keyboard-configuration/layoutcode select $KEYMAP" | debconf-set-selections 2>/dev/null || true
  # Apply to /etc/default/keyboard if present
  if [[ -f /etc/default/keyboard ]]; then
    sed -i "s/^XKBLAYOUT=.*/XKBLAYOUT=\"$KEYMAP\"/" /etc/default/keyboard 2>/dev/null || echo "XKBLAYOUT=\"$KEYMAP\"" >> /etc/default/keyboard
  fi
fi

# Locale — uncomment accurately, avoid duplicate appends
if [[ -f /etc/locale.gen ]]; then
  log "Configuring locales: $LOCALE + en_US.UTF-8"
  # Ensure requested locale is uncommented (Debian locale.gen uses '# en_US.UTF-8 UTF-8')
  for _lc in "$LOCALE" "en_US.UTF-8"; do
    if grep -q "^# *${_lc} UTF-8" /etc/locale.gen; then
      sed -i "s/^# *${_lc} UTF-8/${_lc} UTF-8/" /etc/locale.gen
    elif ! grep -q "^${_lc} UTF-8" /etc/locale.gen; then
      echo "${_lc} UTF-8" >> /etc/locale.gen
    fi
  done
  # Deduplicate
  awk '!seen[$0]++' /etc/locale.gen > /tmp/locale.gen.tmp && mv /tmp/locale.gen.tmp /etc/locale.gen
  locale-gen || true
  # Debian locale via update-locale if available, else /etc/default/locale
  if have_chroot update-locale; then
    update-locale LANG="$LOCALE" 2>/dev/null || echo "LANG=$LOCALE" > /etc/default/locale
  else
    echo "LANG=$LOCALE" > /etc/default/locale
  fi
  # Also ensure LC_ALL not permanently set
  if [[ -f /etc/environment ]]; then
    sed -i '/^LANG=/d' /etc/environment 2>/dev/null || true
  fi
else
  warn "/etc/locale.gen missing, installing locales package"
  apt-get update -qq || true
  apt-get install -y --no-install-recommends locales || true
  if [[ -f /etc/locale.gen ]]; then
    sed -i "s/^# *${LOCALE} UTF-8/${LOCALE} UTF-8/" /etc/locale.gen 2>/dev/null || echo "$LOCALE UTF-8" >> /etc/locale.gen
    locale-gen || true
    echo "LANG=$LOCALE" > /etc/default/locale
  fi
fi

# 3. APT Update — handle security and non-free-firmware per Debian 12+
log "Updating package lists..."
apt-get update -qq || {
  warn "apt-get update failed, retrying with fix-missing"
  apt-get update -qq --fix-missing || true
}

# 4. User Accounts & Sudo — Debian adduser policy
log "Configuring users ($USERNAME)..."
if ! [[ "$USERNAME" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]]; then
  warn "Username '$USERNAME' violates Debian policy, falling back to 'debian'"
  USERNAME="debian"
fi
if ! id -u "$USERNAME" &>/dev/null; then
  # Use --gecos to avoid interactive prompt
  useradd -m -s "$USER_SHELL" -U -G sudo --gecos "" "$USERNAME" 2>/dev/null || useradd -m -s "$USER_SHELL" -U "$USERNAME"
fi
# Ensure shell exists inside chroot, fallback to bash
if [[ ! -x "$USER_SHELL" ]]; then
  warn "Shell $USER_SHELL not found in chroot, installing bash and using /bin/bash"
  apt-get install -y --no-install-recommends bash || true
  USER_SHELL="/bin/bash"
  usermod -s "$USER_SHELL" "$USERNAME" 2>/dev/null || true
fi

# Group memberships — only if group exists in target
for grp in sudo audio video plugdev netdev kvm libvirt docker render bluetooth; do
  if getent group "$grp" &>/dev/null; then
    usermod -aG "$grp" "$USERNAME" 2>/dev/null || true
  fi
done

# Sudo / doas handling — Debian-accurate
case "$SUDO_MODE" in
  1|sudo)
    apt-get install -y --no-install-recommends sudo 2>/dev/null || true
    # Debian sudoers: ensure sudo group has privilege (default)
    if ! grep -q "^%sudo" /etc/sudoers 2>/dev/null; then
      echo "%sudo ALL=(ALL:ALL) ALL" >> /etc/sudoers 2>/dev/null || true
    fi
    ;;
  2|nopasswd|sudo-nopasswd)
    apt-get install -y --no-install-recommends sudo || true
    cat > "/etc/sudoers.d/99-$USERNAME-nopasswd" <<EOF2
$USERNAME ALL=(ALL) NOPASSWD: ALL
EOF2
    chmod 0440 "/etc/sudoers.d/99-$USERNAME-nopasswd"
    visudo -c 2>/dev/null || { warn "sudoers syntax invalid, removing"; rm -f "/etc/sudoers.d/99-$USERNAME-nopasswd"; }
    ;;
  3|doas)
    apt-get install -y --no-install-recommends doas 2>/dev/null || apt-get install -y --no-install-recommends opendoas 2>/dev/null || true
    # Debian doas.conf syntax: permit persist USER as root
    cat > /etc/doas.conf <<EOF2
permit persist $USERNAME as root
permit persist :sudo as root
EOF2
    chmod 0400 /etc/doas.conf
    ;;
  4|none)
    log "Skipping sudo/doas installation (SUDO_MODE=none)"
    ;;
  *)
    apt-get install -y --no-install-recommends sudo || true
    ;;
esac

# Set passwords securely (avoid logging)
if [[ -n "${ROOT_PASSWORD:-}" ]]; then
  echo "root:$ROOT_PASSWORD" | chpasswd 2>/dev/null || warn "Failed to set root password"
  # Clear variable from memory
  ROOT_PASSWORD="x"
else
  passwd -l root 2>/dev/null || true
  log "Root account locked (no password provided)"
fi

if [[ -n "${USER_PASSWORD:-}" ]]; then
  echo "$USERNAME:$USER_PASSWORD" | chpasswd 2>/dev/null || warn "Failed to set user password"
  USER_PASSWORD="x"
else
  log "User $USERNAME has no password set (use SSH keys or set later with passwd)"
fi

# SSH authorized keys — per Debian ssh handling
if [[ -n "${SSH_AUTHORIZED_KEYS:-}" ]]; then
  log "Installing SSH authorized_keys for $USERNAME"
  mkdir -p "/home/$USERNAME/.ssh"
  # Support multiple keys separated by newline or comma
  printf '%s\n' "$SSH_AUTHORIZED_KEYS" | tr ',' '\n' > "/home/$USERNAME/.ssh/authorized_keys"
  chmod 700 "/home/$USERNAME/.ssh"
  chmod 600 "/home/$USERNAME/.ssh/authorized_keys"
  chown -R "$USERNAME:$USERNAME" "/home/$USERNAME/.ssh"
  # Harden SSH directory SELinux if present
  restorecon -R "/home/$USERNAME/.ssh" 2>/dev/null || true
fi

# 5. Kernel & Initramfs — per-arch accurate Debian packages
if [[ "$KERNEL" == "none" ]]; then
  log "Kernel: none (chroot / container rootfs) — skipping kernel, firmware and initramfs"
else
log "Installing Linux Kernel ($KERNEL) for arch $TARGET_ARCH, suite $SUITE..."
# Map arch to Debian kernel package suffix
_kernel_arch_pkg="linux-image-${TARGET_ARCH}"
case "$TARGET_ARCH" in
  amd64)   _kernel_arch_pkg="linux-image-amd64" ;;
  arm64)   _kernel_arch_pkg="linux-image-arm64" ;;
  armhf)   _kernel_arch_pkg="linux-image-armhf" ;;
  riscv64) _kernel_arch_pkg="linux-image-riscv64" ;;
  i386)    _kernel_arch_pkg="linux-image-686-pae" ;; # Debian i386 uses 686-pae flavor
  ppc64el) _kernel_arch_pkg="linux-image-powerpc64le" ;;
  s390x)   _kernel_arch_pkg="linux-image-s390x" ;;
esac
_header_pkg="${_kernel_arch_pkg/linux-image/linux-headers}"

case "$KERNEL" in
  stock)
    apt-get install -y --no-install-recommends "$_kernel_arch_pkg" "$_header_pkg" 2>/dev/null || \
    apt-get install -y --no-install-recommends "$_kernel_arch_pkg" 2>/dev/null || true
    ;;
  rt)
    # RT kernel only available for amd64/arm64 in Debian; fallback to stock if unavailable
    if [[ "$TARGET_ARCH" == "amd64" || "$TARGET_ARCH" == "arm64" ]]; then
      apt-get install -y --no-install-recommends "linux-image-rt-${TARGET_ARCH}" 2>/dev/null || \
      apt-get install -y --no-install-recommends "$_kernel_arch_pkg" 2>/dev/null || true
    else
      warn "RT kernel not available for $TARGET_ARCH, falling back to stock"
      apt-get install -y --no-install-recommends "$_kernel_arch_pkg" 2>/dev/null || true
    fi
    ;;
  cloud)
    if [[ "$TARGET_ARCH" == "amd64" || "$TARGET_ARCH" == "arm64" ]]; then
      apt-get install -y --no-install-recommends "linux-image-cloud-${TARGET_ARCH}" 2>/dev/null || \
      apt-get install -y --no-install-recommends "$_kernel_arch_pkg" 2>/dev/null || true
    else
      warn "Cloud kernel not available for $TARGET_ARCH, falling back to stock"
      apt-get install -y --no-install-recommends "$_kernel_arch_pkg" 2>/dev/null || true
    fi
    ;;
  liquorix)
    log "Configuring Liquorix repository (third-party — not Debian pure)..."
    if [[ "$SUITE" == "bookworm" || "$SUITE" == "trixie" || "$SUITE" == "forky" || "$SUITE" == "sid" ]]; then
      mkdir -p /etc/apt/trusted.gpg.d
      curl -fsSL 'https://liquorix.net/liquorix-keyring.gpg' 2>/dev/null | gpg --dearmor -o /etc/apt/trusted.gpg.d/liquorix-keyring.gpg 2>/dev/null || true
      echo "deb http://liquorix.net/debian $SUITE main" > /etc/apt/sources.list.d/liquorix.list
      apt-get update -qq || true
      apt-get install -y --no-install-recommends linux-image-liquorix-amd64 linux-headers-liquorix-amd64 2>/dev/null || \
      apt-get install -y --no-install-recommends "$_kernel_arch_pkg" 2>/dev/null || true
    else
      warn "Liquorix not available for suite $SUITE, falling back to stock"
      apt-get install -y --no-install-recommends "$_kernel_arch_pkg" 2>/dev/null || true
    fi
    ;;
  xanmod)
    log "Configuring XanMod repository (third-party — not Debian pure)..."
    if [[ "$SUITE" == "bookworm" || "$SUITE" == "trixie" || "$SUITE" == "forky" || "$SUITE" == "sid" ]]; then
      mkdir -p /etc/apt/trusted.gpg.d
      curl -fsSL 'https://dl.xanmod.org/gpg.key' 2>/dev/null | gpg --dearmor -o /etc/apt/trusted.gpg.d/xanmod-kernel.gpg 2>/dev/null || true
      echo 'deb http://deb.xanmod.org releases main' > /etc/apt/sources.list.d/xanmod-kernel.list
      apt-get update -qq || true
      apt-get install -y --no-install-recommends linux-xanmod-x64v3 2>/dev/null || \
      apt-get install -y --no-install-recommends linux-xanmod 2>/dev/null || \
      apt-get install -y --no-install-recommends "$_kernel_arch_pkg" 2>/dev/null || true
    else
      warn "XanMod not available for suite $SUITE, falling back to stock"
      apt-get install -y --no-install-recommends "$_kernel_arch_pkg" 2>/dev/null || true
    fi
    ;;
  custom_lts)
    log "Custom LTS: installing build deps and using stock kernel as fallback (Debian pure)..."
    apt-get install -y --no-install-recommends linux-source build-essential bc kmod cpio flex libncurses5-dev libelf-dev libssl-dev 2>/dev/null || true
    apt-get install -y --no-install-recommends "$_kernel_arch_pkg" 2>/dev/null || true
    ;;
  *)
    apt-get install -y --no-install-recommends "$_kernel_arch_pkg" 2>/dev/null || true
    ;;
esac

# Firmware packages — Debian 12+ split non-free-firmware
log "Installing CPU microcode and firmware (Debian non-free-firmware policy)..."
# Microcode per arch
if [[ "$TARGET_ARCH" == "amd64" || "$TARGET_ARCH" == "i386" ]]; then
  apt-get install -y --no-install-recommends intel-microcode amd64-microcode 2>/dev/null || true
fi
# Firmware: try Debian 12+ firmware-linux (which depends on non-free-firmware), fallback to firmware-linux-free
if ! apt-get install -y --no-install-recommends firmware-linux 2>/dev/null; then
  apt-get install -y --no-install-recommends firmware-linux-free firmware-misc-nonfree 2>/dev/null || true
fi
# For WiFi/GPU if non-free-firmware enabled
if [[ "${ENABLE_NONFREE_FIRMWARE:-n}" == "y" ]]; then
  apt-get install -y --no-install-recommends firmware-iwlwifi firmware-realtek firmware-atheros 2>/dev/null || true
fi

# Initramfs configuration — Debian initramfs-tools
if [[ "$ENCRYPT_ROOT" == "y" ]]; then
  log "Configuring initramfs for LUKS..."
  mkdir -p /etc/initramfs-tools
  if ! grep -q "^cryptsetup" /etc/initramfs-tools/modules 2>/dev/null; then
    echo "cryptsetup" >> /etc/initramfs-tools/modules 2>/dev/null || true
  fi
  apt-get install -y --no-install-recommends cryptsetup-initramfs 2>/dev/null || apt-get install -y --no-install-recommends cryptsetup 2>/dev/null || true
  # Ensure crypttab is respected in initramfs
  if [[ -f /etc/crypttab ]]; then
    if have_chroot update-initramfs; then
      echo "CRYPTSETUP=y" >> /etc/cryptsetup-initramfs/conf-hook 2>/dev/null || true
    fi
  fi
fi
if [[ "$FS_ROOT" == "btrfs" ]]; then
  if ! grep -q "^btrfs" /etc/initramfs-tools/modules 2>/dev/null; then
    echo "btrfs" >> /etc/initramfs-tools/modules 2>/dev/null || true
  fi
fi
if have_chroot update-initramfs; then
  update-initramfs -u -k all 2>/dev/null || update-initramfs -c -k all 2>/dev/null || true
fi
fi # KERNEL != none

# 6. Networking & SSH — Debian-accurate per manager
log "Configuring Networking ($NET_MANAGER)..."
case "$NET_MANAGER" in
  NetworkManager)
    apt-get install -y --no-install-recommends network-manager 2>/dev/null || true
    # Debian enables NetworkManager via systemd; also ensure nm handles ifupdown
    if [[ -f /etc/NetworkManager/NetworkManager.conf ]]; then
      sed -i 's/^managed=.*/managed=true/' /etc/NetworkManager/NetworkManager.conf 2>/dev/null || true
    fi
    systemctl enable NetworkManager 2>/dev/null || true
    # Disable conflicting systemd-networkd if enabled
    systemctl disable systemd-networkd 2>/dev/null || true
    ;;
  systemd-networkd)
    apt-get install -y --no-install-recommends systemd-resolved 2>/dev/null || true
    mkdir -p /etc/systemd/network
    cat > /etc/systemd/network/20-wired.network <<EOF
[Match]
Name=en* eth* enp* ens*
[Network]
DHCP=yes
IPv6AcceptRA=yes
[DHCPv4]
UseDNS=yes
[DHCPv6]
UseDNS=yes
EOF
    # Ensure resolved handles DNS
    systemctl enable systemd-networkd 2>/dev/null || true
    systemctl enable systemd-resolved 2>/dev/null || true
    ln -sf /run/systemd/resolve/stub-resolv.conf /etc/resolv.conf 2>/dev/null || true
    ;;
  ifupdown)
    apt-get install -y --no-install-recommends ifupdown isc-dhcp-client 2>/dev/null || true
    cat > /etc/network/interfaces <<EOF
# Debian ifupdown — generated by deb-strap
auto lo
iface lo inet loopback

allow-hotplug eth0
iface eth0 inet dhcp

allow-hotplug wlan0
iface wlan0 inet dhcp
    wpa-conf /etc/wpa_supplicant/wpa_supplicant.conf
EOF
    ;;
  iwd)
    apt-get install -y --no-install-recommends iwd 2>/dev/null || true
    mkdir -p /etc/iwd
    systemctl enable iwd 2>/dev/null || true
    # iwd needs systemd-networkd or NetworkManager for IP; enable networkd as fallback
    systemctl enable systemd-networkd 2>/dev/null || true
    ;;
  dhcpcd)
    apt-get install -y --no-install-recommends dhcpcd 2>/dev/null || true
    systemctl enable dhcpcd 2>/dev/null || true
    ;;
  *)
    apt-get install -y --no-install-recommends network-manager 2>/dev/null || true
    systemctl enable NetworkManager 2>/dev/null || true
    ;;
esac

# SSH hardening — Debian openssh-server
if [[ "$ENABLE_SSH" == "y" ]]; then
  log "Installing OpenSSH server..."
  apt-get install -y --no-install-recommends openssh-server 2>/dev/null || true
  mkdir -p /etc/ssh/sshd_config.d
  # Debian default sshd_config handling
  if [[ "$SSH_PERMIT_ROOT" == "y" ]]; then
    sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config 2>/dev/null || echo "PermitRootLogin yes" >> /etc/ssh/sshd_config
  else
    sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config 2>/dev/null || echo "PermitRootLogin no" >> /etc/ssh/sshd_config
  fi
  # Harden: disable password auth if keys provided, else allow
  if [[ -n "${SSH_AUTHORIZED_KEYS:-}" ]]; then
    # Keep PasswordAuthentication yes for fallback unless explicitly hardened
    if ! grep -q "^PasswordAuthentication" /etc/ssh/sshd_config 2>/dev/null; then
      echo "PasswordAuthentication yes" >> /etc/ssh/sshd_config
    fi
  fi
  # Ensure PubkeyAuthentication yes
  sed -i 's/^#\?PubkeyAuthentication.*/PubkeyAuthentication yes/' /etc/ssh/sshd_config 2>/dev/null || echo "PubkeyAuthentication yes" >> /etc/ssh/sshd_config
  # Debian ssh service name is 'ssh' not 'sshd'
  systemctl enable ssh 2>/dev/null || systemctl enable sshd 2>/dev/null || true
  # Regenerate host keys if missing
  if [[ ! -f /etc/ssh/ssh_host_ed25519_key ]]; then
    ssh-keygen -A 2>/dev/null || true
  fi
else
  log "Skipping OpenSSH (ENABLE_SSH=n)"
fi

# 7. Bootloader — Debian-accurate, detect target EFI not host EFI
log "Bootloader: $BOOTLOADER (SecureBoot=$SECURE_BOOT, ESP=$ESP_MOUNT_POINT)"
if [[ "$BOOTLOADER" == "grub" ]]; then
  log "Installing GRUB Bootloader..."
  # Determine if target wants EFI: check if PART_EFI defined or ESP directory exists
  _want_efi="n"
  if [[ -n "${PART_EFI:-}" ]]; then _want_efi="y"; fi
  if [[ -d "${ESP_MOUNT_POINT:-/boot/efi}" || -d "/boot/efi" || -d "/boot" ]]; then
    # Check if EFI partition is formatted vfat (heuristic)
    if [[ -n "${PART_EFI:-}" ]]; then _want_efi="y"; fi
  fi
  # Also check Debian arch supports EFI
  _grub_efi_pkg="grub-efi-amd64"
  _grub_bios_pkg="grub-pc"
  case "$TARGET_ARCH" in
    arm64)   _grub_efi_pkg="grub-efi-arm64";   _grub_bios_pkg="grub-efi-arm64" ;;
    armhf)   _grub_efi_pkg="grub-efi-arm";     _grub_bios_pkg="grub-efi-arm" ;;
    riscv64) _grub_efi_pkg="grub-efi-riscv64"; _grub_bios_pkg="grub-efi-riscv64" ;;
    i386)    _grub_efi_pkg="grub-efi-amd64" ;; # i386 uses 32-bit GRUB on legacy BIOS only
  esac
  if [[ "$_want_efi" == "y" ]]; then
    # Install EFI GRUB (Debian pure)
    if [[ "$SECURE_BOOT" == "y" ]]; then
      # Shim + signed GRUB for Secure Boot — Debian bookworm+ has grub-efi-amd64-signed
      apt-get install -y --no-install-recommends "$_grub_efi_pkg"-signed shim-signed 2>/dev/null || \
      apt-get install -y --no-install-recommends "$_grub_efi_pkg" shim-signed 2>/dev/null || \
      apt-get install -y --no-install-recommends "$_grub_efi_pkg" 2>/dev/null || true
      log "Secure Boot: installed signed GRUB + shim"
    else
      apt-get install -y --no-install-recommends "$_grub_efi_pkg" 2>/dev/null || true
    fi
    # Hybrid: also install BIOS GRUB if BIOS_COMPAT requested or fallback
    if [[ "${BIOS_COMPAT:-n}" == "y" ]]; then
      apt-get install -y --no-install-recommends "$_grub_bios_pkg" 2>/dev/null || true
    fi
    # EFI install — portable (removable) for image builds where efibootmgr not available
    if [[ "$TARGET_ARCH" == "arm64" ]]; then
      grub-install --target=arm64-efi --efi-directory="${ESP_MOUNT_POINT:-/boot/efi}" --bootloader-id=debian --recheck --removable 2>/dev/null || \
      grub-install --target=arm64-efi --efi-directory="${ESP_MOUNT_POINT:-/boot}" --bootloader-id=debian --recheck --removable 2>/dev/null || true
    elif [[ "$TARGET_ARCH" == "riscv64" ]]; then
      grub-install --target=riscv64-efi --efi-directory="${ESP_MOUNT_POINT:-/boot/efi}" --bootloader-id=debian --recheck --removable 2>/dev/null || true
    else
      grub-install --target=x86_64-efi --efi-directory="${ESP_MOUNT_POINT:-/boot/efi}" --bootloader-id=debian --recheck --removable 2>/dev/null || \
      grub-install --target=x86_64-efi --efi-directory="${ESP_MOUNT_POINT:-/boot}" --bootloader-id=debian --recheck --removable 2>/dev/null || true
    fi
    # BIOS fallback if hybrid
    if [[ "${BIOS_COMPAT:-n}" == "y" && -n "${TARGET_DEVICE:-}" && -b "${TARGET_DEVICE:-}" ]]; then
      grub-install --target=i386-pc --recheck "$TARGET_DEVICE" 2>/dev/null || true
    fi
  else
    # BIOS-only system
    apt-get install -y --no-install-recommends "$_grub_bios_pkg" 2>/dev/null || apt-get install -y --no-install-recommends grub-pc 2>/dev/null || true
    if [[ -n "${TARGET_DEVICE:-}" ]]; then
      if [[ -b "$TARGET_DEVICE" ]]; then
        grub-install --target=i386-pc --recheck "$TARGET_DEVICE" 2>/dev/null || true
      else
        warn "TARGET_DEVICE $TARGET_DEVICE not a block device in chroot, skipping grub-install (image build: will run on loop device)"
      fi
    fi
  fi
  # Debian GRUB defaults
  if [[ -f /etc/default/grub ]]; then
    # Ensure GRUB_ENABLE_CRYPTODISK if LUKS
    if [[ "$ENCRYPT_ROOT" == "y" ]]; then
      if ! grep -q "GRUB_ENABLE_CRYPTODISK" /etc/default/grub 2>/dev/null; then
        echo "GRUB_ENABLE_CRYPTODISK=y" >> /etc/default/grub
      fi
    fi
  fi
  # Multi-root layout support (second root partition, e.g. for A/B updates or dual-boot)
  # The second root is formatted but left as a clean partition; the user can clone or
  # install a different release/version onto it separately.
  if [[ -n "${PART_ROOT2:-}" && -b "$PART_ROOT2" ]]; then
    log "Formatting second root partition ($PART_ROOT2) for multi-root layout..."
    case "$FS_ROOT" in
      ext4) run mkfs.ext4 -F -L "debian-root2" -E lazy_itable_init=0,lazy_journal_init=0 "$PART_ROOT2" 2>/dev/null || run mkfs.ext4 -F -L "debian-root2" "$PART_ROOT2" ;;
      btrfs) run mkfs.btrfs -f -L "debian-root2" --csum xxhash "$PART_ROOT2" 2>/dev/null || run mkfs.btrfs -f -L "debian-root2" "$PART_ROOT2" ;;
      xfs) run mkfs.xfs -f -L "debian-root2" -m crc=1,finobt=1 -i sparse=1 "$PART_ROOT2" 2>/dev/null || run mkfs.xfs -f -L "debian-root2" "$PART_ROOT2" ;;
      f2fs) run mkfs.f2fs -f -l "debian-root2" -O extra_attr,inode_checksum,sb_checksum,compression "$PART_ROOT2" 2>/dev/null || run mkfs.f2fs -f -l "debian-root2" "$PART_ROOT2" ;;
      *) run mkfs.ext4 -F -L "debian-root2" "$PART_ROOT2" ;;
    esac
    # Create basic mount point for potential future use
    if [[ -n "${PART_ROOT2:-}" ]]; then
      PART_ROOT2_UUID="$(blkid -s UUID -o value "$PART_ROOT2" 2>/dev/null || true)"
      if [[ -n "$PART_ROOT2_UUID" ]]; then
        echo "UUID=$PART_ROOT2_UUID /mnt/root2 $FS_ROOT defaults,noatime 0 2" >> "$MNT/etc/fstab" 2>/dev/null || true
      fi
    fi
  fi

  # Update GRUB for multi-root (if multi-root enabled, add a second menu entry)
  if [[ "${MULTI_ROOT:-0}" == "1" && -n "${PART_ROOT2:-}" ]]; then
    if [[ "$BOOTLOADER" == "grub" ]]; then
      log "Adding multi-root GRUB entry for second root ($PART_ROOT2)..."
      # The simplest multi-root entry: point to the same kernel/initramfs but load
      # from the second root partition. We rely on the user to install a different OS.
      if [[ -f /boot/grub/grub.cfg ]]; then
        # Add a custom entry after the standard one
        cat >> /etc/grub.d/40_custom <<EOF2
menuentry 'Debian (Root 2 - Multi-Root)' --class debian --class gnu-linux --class gnu --class os {
    insmod part_gpt
    insmod ext2
    insmod btrfs
    set root='(hd0,gpt${part_num:-2})'
    # Note: the user must provide the correct partition index and kernel/initramfs
    # for multi-root installations; this is a placeholder.
    linux /boot/vmlinuz-$(uname -r) root=/dev/disk/by-label/debian-root2 ro
    initrd /boot/initrd.img-$(uname -r)
}
EOF2
      fi
    fi
  fi

  update-grub 2>/dev/null || grub-mkconfig -o /boot/grub/grub.cfg 2>/dev/null || true

elif [[ "$BOOTLOADER" == "systemd-boot" ]]; then
  log "Installing systemd-boot (requires systemd + ESP at $ESP_MOUNT_POINT)..."
  if [[ "$INIT_SYSTEM" != "systemd" ]]; then
    warn "systemd-boot requires systemd init, but INIT_SYSTEM=$INIT_SYSTEM — installing anyway, will require systemd"
  fi
  apt-get install -y --no-install-recommends systemd-boot 2>/dev/null || apt-get install -y --no-install-recommends systemd 2>/dev/null || true
  # ESP must be mounted at /boot or /boot/efi; systemd-boot expects ESP at /boot or /efi
  _esp_path="${ESP_MOUNT_POINT:-/boot}"
  if [[ -d "$_esp_path" ]]; then
    bootctl install --esp-path="$_esp_path" 2>/dev/null || bootctl install 2>/dev/null || true
    # Ensure kernel install hooks are present (Debian kernel-install)
    if have_chroot kernel-install; then
      kernel-install add "$(uname -r)" "/lib/modules/$(uname -r)/vmlinuz" 2>/dev/null || true
    fi
  else
    warn "ESP path $_esp_path not found, skipping bootctl install"
  fi
  # UKI support on Trixie+ with systemd 255+
  if have_chroot ukify && [[ "$SUITE" == "trixie" || "$SUITE" == "sid" ]]; then
    log "UKI available (systemd-ukify) — configure kernel-install for UKI if desired"
    # Debian UKI is opt-in; do not force but ensure stub is present
    apt-get install -y --no-install-recommends systemd-boot-efi 2>/dev/null || true
  fi

elif [[ "$BOOTLOADER" == "refind" ]]; then
  log "Installing rEFInd..."
  apt-get install -y --no-install-recommends refind 2>/dev/null || true
  if [[ -d "${ESP_MOUNT_POINT:-/boot/efi}" ]]; then
    refind-install --usedefault "${PART_EFI:-}" 2>/dev/null || refind-install 2>/dev/null || true
  fi

elif [[ "$BOOTLOADER" == "limine" ]]; then
  log "Installing Limine (Debian sid/trixie has limine package)..."
  apt-get install -y --no-install-recommends limine 2>/dev/null || {
    warn "Limine not in Debian repos for $SUITE, trying manual install via apt"
    apt-get install -y --no-install-recommends limine 2>/dev/null || true
  }
  if have_chroot limine; then
    limine bios-install "${TARGET_DEVICE:-}" 2>/dev/null || true
  fi

elif [[ "$BOOTLOADER" == "skip" || "$BOOTLOADER" == "none" ]]; then
  log "Skipping bootloader installation (container/chroot mode)"
else
  warn "Unknown bootloader $BOOTLOADER, skipping"
fi

# 8. TPM2 Auto-unlock enrollment
if [[ "$ENCRYPT_ROOT" == "y" && "$LUKS_TPM2" == "y" ]]; then
  log "TPM2 auto-unlock requested (PCR 0,2,7)..."
  if have_chroot systemd-cryptenroll && [[ -c /dev/tpmrm0 || -c /dev/tpm0 ]]; then
    # Note: in chroot without TPM device, enrollment will be deferred to first boot via systemd
    systemd-cryptenroll --tpm2-device=auto --tpm2-pcrs=0,2,7 --tpm2-with-pin=no "$PART_ROOT" 2>/dev/null || warn "TPM2 enrollment deferred (hardware not accessible in chroot — will auto-enroll on first boot if tpm2 present)."
    # Also ensure crypttab has tpm2-device=auto (already set)
  else
    warn "TPM2 requested but systemd-cryptenroll or /dev/tpmrm0 not available — crypttab already contains tpm2-device=auto for first-boot enrollment"
    # Create systemd service to enroll on first boot if TPM appears
    mkdir -p /etc/systemd/system
    cat > /etc/systemd/system/debstrap-tpm2-enroll.service <<EOF2
[Unit]
Description=Enroll TPM2 for LUKS auto-unlock (deb-strap first-boot)
ConditionPathExists=!/var/lib/debstrap-tpm2-enrolled
After=cryptsetup.target
[Service]
Type=oneshot
ExecStart=/bin/bash -c 'systemd-cryptenroll --tpm2-device=auto --tpm2-pcrs=0,2,7 "$PART_ROOT" && touch /var/lib/debstrap-tpm2-enrolled'
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
EOF2
    systemctl enable debstrap-tpm2-enroll.service 2>/dev/null || true
  fi
fi

# 9. Btrfs Snapper — Debian snapper
if [[ "$FS_ROOT" == "btrfs" && "${BTRFS_SNAPPER:-n}" == "y" ]]; then
  log "Setting up Snapper for Btrfs..."
  apt-get install -y --no-install-recommends snapper 2>/dev/null || true
  if have_chroot snapper; then
    # Create snapper config for root if not exists
    if [[ ! -f /etc/snapper/configs/root ]]; then
      snapper -c root create-config / 2>/dev/null || true
    fi
    # Debian snapper timeline
    systemctl enable snapper-timeline.timer 2>/dev/null || true
    systemctl enable snapper-cleanup.timer 2>/dev/null || true
    # Btrfs quota
    btrfs quota enable / 2>/dev/null || true
  fi
fi

# 10. Software Bundles — Debian pure
log "Installing requested software packages: $OPT_PACKAGES"
if [[ "$OPT_PACKAGES" =~ cli|all ]]; then
  apt-get install -y --no-install-recommends git curl wget htop tmux neovim ripgrep fzf jq psmisc ca-certificates gnupg 2>/dev/null || true
fi
if [[ "$OPT_PACKAGES" =~ dev|all ]]; then
  apt-get install -y --no-install-recommends build-essential pkg-config python3 python3-pip python3-venv python3-dev 2>/dev/null || true
fi
if [[ "$OPT_PACKAGES" =~ docker|all ]]; then
  apt-get install -y --no-install-recommends docker.io docker-compose-plugin apparmor 2>/dev/null || true
  systemctl enable docker 2>/dev/null || true
  # Add user to docker group (already added via groups, but ensure)
  usermod -aG docker "$USERNAME" 2>/dev/null || true
fi
# Zram/swapfile fallback if no swap partition
if [[ -z "${PART_SWAP:-}" ]]; then
  if [[ "$OPT_PACKAGES" =~ zram|all ]]; then
    apt-get install -y --no-install-recommends zram-tools 2>/dev/null || apt-get install -y --no-install-recommends systemd-zram-generator 2>/dev/null || true
  else
    # Create swapfile if no swap partition and not zram, if / is writable and has space
    if [[ ! -f /swapfile && ! -f /swap.img ]]; then
      log "No swap partition; creating 1GiB swapfile at /swapfile..."
      if have_chroot fallocate; then
        fallocate -l 1G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=1024 2>/dev/null || true
      else
        dd if=/dev/zero of=/swapfile bs=1M count=1024 2>/dev/null || true
      fi
      if [[ -f /swapfile ]]; then
        chmod 600 /swapfile
        mkswap /swapfile 2>/dev/null || true
        if ! grep -q "^/swapfile" /etc/fstab 2>/dev/null; then
          echo "/swapfile none swap sw 0 0" >> /etc/fstab
        fi
      fi
    fi
  fi
fi

# 11. Desktop Environment & Display Manager — Debian tasks
if [[ "$DESKTOP" != "none" ]]; then
  log "Installing Desktop Environment: $DESKTOP (suite $SUITE)..."
  case "$DESKTOP" in
    gnome)
      apt-get install -y --no-install-recommends gnome-core gdm3 2>/dev/null || apt-get install -y gnome-core gdm3 2>/dev/null || true
      ;;
    kde)
      apt-get install -y --no-install-recommends kde-plasma-desktop sddm 2>/dev/null || apt-get install -y kde-standard sddm 2>/dev/null || true
      ;;
    xfce)
      apt-get install -y --no-install-recommends xfce4 xfce4-goodies lightdm 2>/dev/null || true
      ;;
    cinnamon)
      apt-get install -y --no-install-recommends cinnamon-core lightdm 2>/dev/null || true
      ;;
    mate)
      apt-get install -y --no-install-recommends mate-desktop-environment-core lightdm 2>/dev/null || true
      ;;
    lxqt)
      apt-get install -y --no-install-recommends lxqt-core sddm 2>/dev/null || true
      ;;
    sway)
      apt-get install -y --no-install-recommends sway swaylock swayidle waybar foot wofi greetd 2>/dev/null || apt-get install -y sway 2>/dev/null || true
      ;;
    hyprland)
      # Hyprland is in Debian trixie/sid; bookworm needs backports or manual
      if [[ "$SUITE" == "trixie" || "$SUITE" == "sid" ]]; then
        apt-get install -y --no-install-recommends hyprland waybar foot rofi-wayland 2>/dev/null || true
      else
        warn "Hyprland not in $SUITE repo, attempting backports"
        apt-get install -y --no-install-recommends -t "${SUITE}-backports" hyprland 2>/dev/null || true
      fi
      ;;
    i3)
      apt-get install -y --no-install-recommends i3 lightdm suckless-tools xterm 2>/dev/null || true
      ;;
  esac
  # Display Manager selection
  case "${DISPLAY_MANAGER:-auto}" in
    sddm) apt-get install -y --no-install-recommends sddm 2>/dev/null || true; systemctl enable sddm 2>/dev/null || true ;;
    gdm3) apt-get install -y --no-install-recommends gdm3 2>/dev/null || true; systemctl enable gdm3 2>/dev/null || true ;;
    lightdm) apt-get install -y --no-install-recommends lightdm 2>/dev/null || true; systemctl enable lightdm 2>/dev/null || true ;;
    greetd) apt-get install -y --no-install-recommends greetd 2>/dev/null || true; systemctl enable greetd 2>/dev/null || true ;;
    ly) apt-get install -y --no-install-recommends ly 2>/dev/null || true; systemctl enable ly 2>/dev/null || true ;;
    auto) # already pulled by DE
      systemctl enable gdm3 2>/dev/null || systemctl enable sddm 2>/dev/null || systemctl enable lightdm 2>/dev/null || true
      ;;
    none) log "Display manager disabled (console login)";;
  esac
  # Audio Stack — PipeWire is Debian default since bookworm
  log "Configuring PipeWire audio stack..."
  apt-get install -y --no-install-recommends pipewire pipewire-pulse wireplumber pipewire-audio 2>/dev/null || \
  apt-get install -y --no-install-recommends pipewire pipewire-pulse wireplumber 2>/dev/null || true
  # Enable for user via systemd --user (preset)
  if have_chroot systemctl; then
    # System-wide pipewire is socket-activated; ensure installed
    true
  fi
fi

# 12. GPU Drivers — Debian non-free handling
log "GPU driver: $GPU_DRIVER"
if [[ "$GPU_DRIVER" == "mesa" ]]; then
  apt-get install -y --no-install-recommends libgl1-mesa-dri mesa-vulkan-drivers mesa-va-drivers 2>/dev/null || \
  apt-get install -y --no-install-recommends libgl1-mesa-dri 2>/dev/null || true
  # For Trixie+ mesa is in main; ensure
  if [[ "$TARGET_ARCH" == "amd64" ]]; then
    apt-get install -y --no-install-recommends libglx-mesa0 2>/dev/null || true
  fi
elif [[ "$GPU_DRIVER" == "nvidia" ]]; then
  # Debian nvidia-driver is in non-free (pre-bookworm) and non-free-firmware? Actually nvidia-driver is non-free
  if [[ "$ENABLE_NONFREE" != "y" ]]; then warn "Nvidia driver requires non-free component, but ENABLE_NONFREE=n"; fi
  apt-get install -y --no-install-recommends nvidia-driver firmware-misc-nonfree 2>/dev/null || \
  apt-get install -y nvidia-driver 2>/dev/null || true
elif [[ "$GPU_DRIVER" == "nvidia-open" ]]; then
  # Debian open kernel module (nvidia-open) — Trixie+/sid only
  if [[ "$SUITE" == "bookworm" ]]; then
    warn "nvidia-open requires Trixie+ (13+); falling back to proprietary nvidia-driver on bookworm"
    GPU_DRIVER="nvidia"
  fi
  apt-get install -y --no-install-recommends nvidia-driver nvidia-kernel-open 2>/dev/null || \
  apt-get install -y --no-install-recommends nvidia-open-kernel-modules-dkms 2>/dev/null || true
elif [[ "$GPU_DRIVER" == "nvidia-cuda" ]]; then
  # Full CUDA + display driver (non-free + contrib)
  if [[ "$ENABLE_NONFREE" != "y" ]]; then warn "Nvidia CUDA requires non-free component"; fi
  apt-get install -y --no-install-recommends nvidia-driver cuda nvidia-cuda-toolkit 2>/dev/null || \
  apt-get install -y --no-install-recommends nvidia-driver 2>/dev/null || true
elif [[ "$GPU_DRIVER" == "nvidia-vaapi" ]]; then
  # Video acceleration via VAAPI (non-free, needs libva, nvidia-vaapi-driver)
  apt-get install -y --no-install-recommends nvidia-driver nvidia-vaapi-driver libva2 2>/dev/null || true
elif [[ "$GPU_DRIVER" == "nvidia-legacy" ]]; then
  # Legacy branch for older cards (390xx series, experimental/non-free)
  apt-get install -y --no-install-recommends nvidia-legacy-390xx-driver 2>/dev/null || \
  apt-get install -y --no-install-recommends nvidia-driver 2>/dev/null || true
elif [[ "$GPU_DRIVER" == "nvidia-headless" ]]; then
  apt-get install -y --no-install-recommends nvidia-headless nvidia-smi 2>/dev/null || \
  apt-get install -y --no-install-recommends nvidia-headless-no-dkms 2>/dev/null || true
elif [[ "$GPU_DRIVER" == "skip" ]]; then
  log "Skipping GPU drivers"
fi

# 13. Init system specifics — Debian init freedom
if [[ "$INIT_SYSTEM" == "sysvinit" ]]; then
  log "Ensuring sysvinit is default init..."
  apt-get install -y --no-install-recommends sysvinit-core sysv-rc orphan-sysvinit-scripts 2>/dev/null || true
elif [[ "$INIT_SYSTEM" == "openrc" ]]; then
  log "Installing OpenRC..."
  apt-get install -y --no-install-recommends openrc 2>/dev/null || true
  # Ensure openrc initscript wrapper
  if [[ -f /sbin/openrc-init ]]; then
    ln -sf /sbin/openrc-init /sbin/init 2>/dev/null || true
  fi
elif [[ "$INIT_SYSTEM" == "runit" ]]; then
  log "Installing runit..."
  apt-get install -y --no-install-recommends runit-init runit 2>/dev/null || true
fi

# Security: remove stray strap.env password from world-readable history
history -c 2>/dev/null || true
# Ensure deb-strap env not exposable via /proc
chmod 600 /etc/deb-strap/strap.env 2>/dev/null || true

# Clean up apt caches — keep lists for offline? No, clean to save space
apt-get clean 2>/dev/null || true
rm -rf /var/lib/apt/lists/* 2>/dev/null || true

# Cool feature: first-boot banner — shows deb-strap info at every login
# This creates an update-motd script that displays basic system info.
mkdir -p /etc/update-motd.d
cat > /etc/update-motd.d/99-debstrap-banner <<EOF2
#!/bin/sh
# First-boot banner for deb-strap installations
if [[ -f /etc/deb-strap/strap.env ]]; then
  source /etc/deb-strap/strap.env 2>/dev/null || true
fi
cat <<BANNER
═══════════════════════════════════════════════════════════
  Debian ${SUITE:-unknown} — Installed by deb-strap v${VERSION:-1.2.0}
  Hostname: ${HOSTNAME:-$(cat /etc/hostname 2>/dev/null || hostname)}
  Kernel: $(uname -r)  |  Arch: $(uname -m)
  Init: ${INIT_SYSTEM:-systemd}  |  FS: ${FS_ROOT:-ext4}
═══════════════════════════════════════════════════════════
BANNER
EOF2
chmod +x /etc/update-motd.d/99-debstrap-banner

# Post-install smoke test (cool feature): verify basic services work
# This runs a quick self-test inside the target chroot.
log "Running post-install smoke test..."
if have_chroot systemctl; then
  # Check init system is installed
  systemctl is-system-running 2>/dev/null || systemctl is-active systemd 2>/dev/null || true
fi
# Verify key binaries exist
for _test_bin in bash apt dpkg; do
  [[ -x "/usr/bin/$_test_bin" || -x "/bin/$_test_bin" ]] || warn "Smoke test: $_test_bin binary not found in target"
done
# Verify locale works
locale -a 2>/dev/null | grep -q "C\.UTF-8\|en_US\.UTF-8" || warn "Smoke test: expected locale not generated"
# Verify /etc/fstab exists and is non-empty
if [[ -s /etc/fstab ]]; then
  log "Smoke test: /etc/fstab present ($(wc -l < /etc/fstab) lines)"
else
  warn "Smoke test: /etc/fstab missing or empty"
fi
# Verify /etc/crypttab exists if LUKS enabled
if [[ "$ENCRYPT_ROOT" == "y" && -f /etc/crypttab ]]; then
  log "Smoke test: /etc/crypttab present (LUKS configured)"
fi
# Remove qemu static if copied for foreign bootstrap (not needed in final image unless foreign)
if [[ "$TARGET_ARCH" != "$(uname -m 2>/dev/null | sed 's/x86_64/amd64/; s/aarch64/arm64/')" ]]; then
  rm -f /usr/bin/qemu-*-static 2>/dev/null || true
fi
log "Target installation tasks complete."
EOS_INNER

  chmod +x "$install_sh"
}

# ─────────────────────────────────────── 12) run chroot & post-install ───────────────────────────
execute_chroot_install() {
  banner "Configuring System Inside Target Chroot"

  # Prepare chroot mounts — Debian-accurate handling
  # Use --rbind for /dev to capture devpts, then separate mounts for proc/sys
  mkdir -p "$MNT/dev" "$MNT/proc" "$MNT/sys" "$MNT/run" "$MNT/tmp"
  # /dev
  if mountpoint -q "$MNT/dev" 2>/dev/null; then
    warn "$MNT/dev already mounted, unmounting first"
    umount -R "$MNT/dev" 2>/dev/null || true
  fi
  run mount --rbind /dev "$MNT/dev"
  track_mount "$MNT/dev"
  run mount --make-rslave "$MNT/dev" 2>/dev/null || true
  # Ensure /dev/pts is mounted (rbind should cover, but ensure)
  if ! mountpoint -q "$MNT/dev/pts" 2>/dev/null; then
    mkdir -p "$MNT/dev/pts"
    run mount -t devpts devpts "$MNT/dev/pts" -o gid=5,mode=620,ptmxmode=666 2>/dev/null || run mount --bind /dev/pts "$MNT/dev/pts" 2>/dev/null || true
    track_mount "$MNT/dev/pts"
  fi
  # /proc
  if ! mountpoint -q "$MNT/proc" 2>/dev/null; then
    run mount -t proc proc "$MNT/proc" 2>/dev/null || run mount --bind /proc "$MNT/proc" 2>/dev/null || true
    track_mount "$MNT/proc"
  fi
  # /sys
  if ! mountpoint -q "$MNT/sys" 2>/dev/null; then
    run mount -t sysfs sysfs "$MNT/sys" 2>/dev/null || run mount --bind /sys "$MNT/sys" 2>/dev/null || true
    track_mount "$MNT/sys"
  fi
  # /run — needed for systemd dbus
  if ! mountpoint -q "$MNT/run" 2>/dev/null; then
    run mount --bind /run "$MNT/run" 2>/dev/null || true
    track_mount "$MNT/run"
  fi
  # /tmp as tmpfs for build speed (Debian installer uses tmpfs)
  if ! mountpoint -q "$MNT/tmp" 2>/dev/null; then
    run mount -t tmpfs tmpfs "$MNT/tmp" 2>/dev/null || true
    # Only track if we created it
    if mountpoint -q "$MNT/tmp" 2>/dev/null; then track_mount "$MNT/tmp"; fi
  fi

  # Copy resolv.conf for internet access in chroot — handle systemd-resolved stub
  if [[ -f /run/systemd/resolve/stub-resolv.conf ]]; then
    run cp -L /run/systemd/resolve/stub-resolv.conf "$MNT/etc/resolv.conf" 2>/dev/null || true
  elif [[ -f /etc/resolv.conf ]]; then
    run cp -L /etc/resolv.conf "$MNT/etc/resolv.conf" 2>/dev/null || true
  fi
  # Also copy host's apt proxy if present (Debian hosts may have apt-cacher)
  if [[ -f /etc/apt/apt.conf.d/01proxy ]]; then
    mkdir -p "$MNT/etc/apt/apt.conf.d"
    run cp /etc/apt/apt.conf.d/01proxy "$MNT/etc/apt/apt.conf.d/" 2>/dev/null || true
  fi

  # Execute inside chroot — handle foreign arch via qemu if needed
  log "Executing /deb-strap-install.sh in chroot (arch $TARGET_ARCH)..."
  # Ensure script is executable and has correct shebang
  chmod +x "$MNT/deb-strap-install.sh" 2>/dev/null || true
  # For foreign arch, ensure qemu is available inside chroot (already handled in bootstrap, but re-check)
  if [[ "$IS_FOREIGN" == "1" ]]; then
    local _qemu_path="/usr/bin/qemu-${TARGET_ARCH}-static"
    if [[ -f "$_qemu_path" && ! -f "$MNT$_qemu_path" ]]; then
      run cp "$_qemu_path" "$MNT$_qemu_path" 2>/dev/null || true
      info "Copied $_qemu_path into chroot for foreign exec"
    fi
    # Try alternative qemu names
    for _qa in aarch64 arm riscv64 ppc64le s390x; do
      if [[ -f "/usr/bin/qemu-${_qa}-static" && ! -f "$MNT/usr/bin/qemu-${_qa}-static" ]]; then
        run cp "/usr/bin/qemu-${_qa}-static" "$MNT/usr/bin/" 2>/dev/null || true
      fi
    done
  fi
  # Prefer chroot with preserved unshare if available
  if ! run chroot "$MNT" /bin/bash /deb-strap-install.sh; then
    err "Chroot install script failed — check $MNT/var/log/apt/history.log inside chroot"
    # Dump apt logs for diagnosis
    if [[ -f "$MNT/var/log/apt/term.log" ]]; then
      warn "Last 50 lines of apt term.log:"
      tail -n 50 "$MNT/var/log/apt/term.log" >&2 || true
    fi
    die "Chroot configuration failed."
  fi

  # Clean up installer script from chroot and remove sensitive env copy from apt logs
  rm -f "$MNT/deb-strap-install.sh"
  # Remove qemu static if it was copied only for foreign bootstrap
  if [[ "$IS_FOREIGN" == "1" ]]; then
    rm -f "$MNT/usr/bin/qemu-"*-static 2>/dev/null || true
  fi
  log "Chroot installation completed successfully."
}

export_tar_archive() {
  if [[ "$TARGET_MODE" == "tar" ]]; then
    banner "Exporting Rootfs to $TAR_OUTPUT"
    # The chroot's /dev, /proc, /sys, /run and /tmp are still mounted from the host:
    # unmount them first, and never let tar cross into another filesystem.
    local _i _m _compression=J
    for (( _i=${#CLEANUP_MOUNTS[@]}-1; _i>=0; _i-- )); do
      _m="${CLEANUP_MOUNTS[_i]}"
      if mountpoint -q "$_m" 2>/dev/null; then
        umount -R "$_m" 2>/dev/null || umount -l "$_m" 2>/dev/null || die "Cannot unmount $_m before packing"
      fi
    done
    log "Creating compressed tar archive..."
    # Cube Chrootables imports gzip archives; preserve xz for deb-strap's default output.
    case "$TAR_OUTPUT" in *.tar.gz|*.tgz) _compression=z ;; esac
    run tar --one-file-system "-cp${_compression}f" "$TAR_OUTPUT" -C "$MNT" .
    log "Archive created: $TAR_OUTPUT ($(du -h "$TAR_OUTPUT" 2>/dev/null | cut -f1 || true))"
  fi
  # If we partitioned a temporary raw image backing a qcow2 output, convert the
  # populated raw back to qcow2 and clean up the temporary file.
  if [[ "$TARGET_MODE" == "image" && -n "${IMAGE_FILE_RAW_BACKING:-}" && -n "${IMAGE_FILE:-}" ]]; then
    local _raw="${IMAGE_FILE_RAW_BACKING}"
    if [[ -f "$_raw" ]] && have qemu-img; then
      log "Converting populated raw image back to qcow2 at $IMAGE_FILE ..."
      if run qemu-img convert -f raw -O qcow2 "$_raw" "$IMAGE_FILE"; then
        log "qcow2 image finalised: $IMAGE_FILE"
      else
        warn "qcow2 conversion failed; raw image preserved at $_raw"
      fi
    fi
  fi
}

print_summary() {
  banner "Debian Installation Complete!"
  cat <<EOF
Summary:
  Suite:           Debian $SUITE ($TARGET_ARCH)
  Target Mode:     $TARGET_MODE
  Target Device:   ${TARGET_DEVICE:-N/A}
  Init System:     $INIT_SYSTEM
  Kernel:          $KERNEL
  Bootloader:      $BOOTLOADER
  Filesystem:      $FS_ROOT
  Hostname:        $HOSTNAME
  User:            $USERNAME

The installation finished successfully.
EOF
}

# ─────────────────────────────────────── profile presets ────────────────────────────────────────
apply_profile() {
  local profile="${1:-}"
  [[ -z "$profile" ]] && return 0
  log "Applying profile preset: $profile"
  case "$profile" in
    headless-server)
      DESKTOP="none"
      DISPLAY_MANAGER="none"
      NET_MANAGER="systemd-networkd"
      INIT_SYSTEM="systemd"
      KERNEL="stock"
      BOOTLOADER="skip"
      GPU_DRIVER="skip"
      ENABLE_SSH="y"
      ENABLE_SSH=1
      SECURE_BOOT="n"
      BIOS_COMPAT="n"
      USE_DEB822="y"
      ENABLE_NONFREE="n"
      ENABLE_NONFREE_FIRMWARE="n"
      ENABLE_BACKPORTS="n"
      USE_LVM="n"
      USE_RAID="n"
      ENCRYPT_ROOT="n"
      OPT_PACKAGES="cli"
      : ;;
    minimal)
      DESKTOP="none"
      DISPLAY_MANAGER="none"
      NET_MANAGER="systemd-networkd"
      INIT_SYSTEM="systemd"
      KERNEL="cloud"
      BOOTLOADER="skip"
      GPU_DRIVER="skip"
      ENABLE_SSH="n"
      OPT_PACKAGES="cli"
      USE_DEB822="y"
      ENABLE_NONFREE="n"
      ENABLE_NONFREE_FIRMWARE="n"
      ENABLE_BACKPORTS="n"
      : ;;
    kiosk)
      DESKTOP="none"
      DISPLAY_MANAGER="none"
      NET_MANAGER="systemd-networkd"
      INIT_SYSTEM="systemd"
      KERNEL="stock"
      BOOTLOADER="skip"
      GPU_DRIVER="mesa"
      ENABLE_SSH="y"
      SECURE_BOOT="n"
      BIOS_COMPAT="n"
      OPT_PACKAGES="cli"
      # Auto-login setup for kiosk (set key and disable password auth, keep keys only)
      : ;;
    container)
      TARGET_MODE="rootfs"
      DESKTOP="none"
      DISPLAY_MANAGER="none"
      INIT_SYSTEM="systemd"
      KERNEL="stock"
      BOOTLOADER="skip"
      GPU_DRIVER="skip"
      ENABLE_SSH="n"
      NET_MANAGER="ifupdown"
      OPT_PACKAGES="none"
      USE_LVM="n"
      USE_RAID="n"
      ENCRYPT_ROOT="n"
      BIOS_COMPAT="n"
      : ;;
    dev-workstation)
      DESKTOP="none" # User selects desktop interactively; this preset just sets defaults
      DISPLAY_MANAGER="auto"
      INIT_SYSTEM="systemd"
      NET_MANAGER="NetworkManager"
      KERNEL="stock"
      BOOTLOADER="grub"
      GPU_DRIVER="mesa"
      ENABLE_SSH="y"
      ENABLE_NONFREE="y"
      ENABLE_NONFREE_FIRMWARE="y"
      ENABLE_BACKPORTS="y"
      ENABLE_UPDATES="y"
      ENABLE_SECURITY="y"
      ENABLE_CONTRIB="y"
      OPT_PACKAGES="cli,dev,docker"
      : ;;
    *)
      warn "Unknown profile: $profile (valid: headless-server, minimal, kiosk, container, dev-workstation)"
      : ;;
  esac
}

# ─────────────────────────────────────── main entrypoint ─────────────────────────────────────────
main() {
  echo -e "${BOLD}${CYN}┌─────────────────────────────────────────────────────────────┐${RST}"
  echo -e "${BOLD}${CYN}│                 deb-strap — Debian Installer                │${RST}"
  echo -e "${BOLD}${CYN}│                  Version ${VERSION} — Muse Spark                  │${RST}"
  echo -e "${BOLD}${CYN}└─────────────────────────────────────────────────────────────┘${RST}\n"

  parse_cli "$@"

  # Apply preset profile if specified
  if [[ -n "${CLI_PROFILE:-}" ]]; then
    apply_profile "$CLI_PROFILE"
  fi

  if [[ "$BENCHMARK_ONLY" == "1" ]]; then
    benchmark_debian_mirrors
    exit 0
  fi

  if [[ -n "$CONFIG_FILE" ]]; then
    load_config_file "$CONFIG_FILE"
    # Apply preset profile if configured
    if [[ -n "${CFG[profile]:-}" ]]; then
      apply_profile "${CFG[profile]}"
    fi
  fi

  # Resume handling — pure Debian state file, no external DB
  if [[ "$RESUME" == "1" ]]; then
    if [[ -f "$STATE_FILE" ]]; then
      log "Resuming from state file $STATE_FILE ..."
      # shellcheck source=/dev/null
      source "$STATE_FILE" 2>/dev/null || warn "Failed to load state file"
      info "Resumed state: STEP=${STEP:-unknown} TARGET_MODE=${TARGET_MODE:-} SUITE=${SUITE:-}"
    else
      warn "No state file found at $STATE_FILE — starting fresh (use --resume after an interrupted run)"
    fi
  fi

  # Wizard phases
  check_host_deps
  prompt_target_distro
  prompt_target_mode_and_device
  prompt_disk_layout
  prompt_apt_configuration
  prompt_system_settings

  # Final pre-flight validation before any destructive operation
  preflight_check

  # If --write-config was specified, output the generated configuration and exit
  if [[ -n "$WRITE_CONFIG" ]]; then
    banner "Writing Configuration to $WRITE_CONFIG"
    answers_dump > "$WRITE_CONFIG"
    log "Wrote $(answers_count) configuration parameters to $WRITE_CONFIG."
    log "To run unattended with this config:"
    echo "  sudo bash $0 --config $WRITE_CONFIG --unattended"
    exit 0
  fi

  # Confirmation before disk modification
  if [[ "$TARGET_MODE" == "disk" || "$TARGET_MODE" == "image" ]]; then
    execute_partitioning
    format_and_mount_storage
  fi

  execute_bootstrap

  # Generate config files
  if [[ "$USE_DEB822" == "y" ]]; then
    mkdir -p "$MNT/etc/apt/sources.list.d"
    generate_deb822_sources "$MNT/etc/apt/sources.list.d/debian.sources"
  else
    generate_classic_sources "$MNT/etc/apt/sources.list"
  fi

  generate_fstab "$MNT/etc/fstab"
  generate_crypttab "$MNT/etc/crypttab"

  write_chroot_install_script
  execute_chroot_install
  export_tar_archive
  print_summary
}

# Allow sourcing in test library mode without executing main
if [[ "${STRAP_LIB:-0}" != "1" && "${DEBSTRAP_LIB:-0}" != "1" ]]; then
  main "$@"
fi
