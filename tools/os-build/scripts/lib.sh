#!/usr/bin/env bash
# whitecube script helpers — colors, prompts, status, and error handling
# Source this from other scripts:  source "$(dirname "$0")/lib.sh"

set -euo pipefail

# ── colors ────────────────────────────────────────────────────────────────────
RST="\033[0m"; BLD="\033[1m"; DIM="\033[2m"
RED="\033[91m"; GRN="\033[92m"; YLW="\033[93m"; BLU="\033[94m"; CYN="\033[96m"

info()  { echo -e " ${BLU}→${RST} $*"; }
ok()    { echo -e " ${GRN}✓${RST} $*"; }
warn()  { echo -e " ${YLW}⚠${RST} $*"; }
err()   { echo -e " ${RED}✗${RST} $*" >&2; }
die()   { err "$@"; exit 1; }
header(){ echo -e "\n${BLD}${CYN}━━━ $* ━━━${RST}\n"; }
sub()   { echo -e " ${DIM}·${RST} $*"; }
done_msg()  { echo -e " ${GRN}✔${RST} Done.\n"; }

# ── prompts ───────────────────────────────────────────────────────────────────

prompt() {
    # Usage: val=$(prompt "Question" "default")
    local msg="$1" default="$2"
    local p
    if [[ -n "$default" ]]; then
        read -r -p "$(echo -e " ${BLU}?${RST} ${msg} [${default}]: ")" p
    else
        read -r -p "$(echo -e " ${BLU}?${RST} ${msg}: ")" p
    fi
    echo "${p:-$default}"
}

confirm() {
    # Usage: confirm "Question" "y/N"  → returns true/false
    local msg="$1" default="${2:-N}"
    local y n
    if [[ "$default" =~ ^[Yy] ]]; then y="Y"; n="n"; else y="y"; n="N"; fi
    local p
    read -r -p "$(echo -e " ${BLU}?${RST} ${msg} (${y}/${n}): ")" p
    p="${p:-$default}"
    [[ "$p" =~ ^[Yy] ]]
}

select_one() {
    # Usage: val=$(select_one "Choose" "opt1" "opt2" "opt3")
    # Displays a numbered menu, returns the selected value.
    #
    # The menu is drawn on stderr so that the caller's $(...) captures only the
    # chosen value; writing it to stdout made the prompt part of the result.
    local msg="$1"; shift
    local opts=("$@")
    [[ ${#opts[@]} -gt 0 ]] || die "select_one called with no options"

    local sel
    while true; do
        echo -e " ${BLU}?${RST} ${msg}:" >&2
        local i
        for i in "${!opts[@]}"; do
            echo "    $((i+1))) ${opts[$i]}" >&2
        done
        read -r -p "$(echo -e "   ${DIM}enter number [1-${#opts[@]}]:${RST} ")" sel
        sel="${sel:-1}"
        # Unvalidated input indexed the array directly: "0" selected the LAST
        # option (bash negative indexing) and anything out of range expanded to
        # the empty string under `set -u`, aborting the wizard.
        if [[ "$sel" =~ ^[0-9]+$ ]] && (( sel >= 1 && sel <= ${#opts[@]} )); then
            echo "${opts[$((sel-1))]}"
            return 0
        fi
        warn "Please enter a number between 1 and ${#opts[@]}." >&2
    done
}

# ── status bar ────────────────────────────────────────────────────────────────

run_with_spinner() {
    # Runs a command, showing a one-line status.  Errors are captured and
    # replayed on failure: discarding stderr outright (the previous behaviour)
    # meant a failed step reported nothing but a red cross.
    local label="$1"; shift
    local errlog
    errlog=$(mktemp)
    echo -ne " ${CYN}⠋${RST} ${label}..."
    if "$@" 2>"$errlog"; then
        echo -e "\r ${GRN}✓${RST} ${label}  "
        rm -f "$errlog"
        return 0
    fi
    echo -e "\r ${RED}✗${RST} ${label}  "
    if [[ -s "$errlog" ]]; then
        sed 's/^/     /' "$errlog" >&2
    fi
    rm -f "$errlog"
    return 1
}

# ── whitecube wrapper ─────────────────────────────────────────────────────────────

WHITECUBE="${WHITECUBE:-$(dirname "$0")/../whitecube.py}"

whitecube() {
    python3 "$WHITECUBE" "$@"
}

list_systems() {
    local out
    if ! out=$(whitecube list --json 2>/dev/null); then
        echo "  (could not list systems)"
        return 0
    fi
    printf '%s' "$out" | python3 -c "
import json, sys
try:
    systems = json.load(sys.stdin)
except Exception:
    systems = []
if not systems:
    print('  (no systems)')
for m in systems:
    badges = ''
    if m.get('mounted'): badges += 'M'
    if m.get('encrypted'): badges += 'E'
    if m.get('frugal'): badges += 'F'
    print(f\"  {m['name']:<22} [{m.get('type','?'):>8}]  {m.get('size') or '?':<8}  {badges}\")
"
}

require_whitecube() {
    if [[ ! -f "$WHITECUBE" ]]; then
        die "whitecube.py not found at $WHITECUBE"
    fi
}
