#!/bin/bash
# First live run of the built-in Wi-Fi station with a dead-man's switch. Run it on the machine's own
# console (not over the Wi-Fi it is about to restart):   sudo tools/wifi-live-test.sh
#
# 1. installs the staged network files through net/apply.sh (it makes its own backup),
# 2. restarts whitecube-network.service,
# 3. waits up to TIMEOUT seconds for: association COMPLETED, an IPv4 address, and the router answering,
# 4. on failure restores the previous files from that backup and restarts the old service, then checks
#    that the OLD controller got you back online, and prints the journal so the failure can be read.
# It never removes wpa_supplicant (the old controller needs it for the rollback to work).
set -u
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
APPLY=${APPLY:-$here/net/apply.sh}
SYSTEMCTL=${SYSTEMCTL:-/usr/bin/systemctl}
JOURNALCTL=${JOURNALCTL:-/usr/bin/journalctl}
PING=${PING:-/usr/bin/ping}
STATUS=${STATUS:-/run/whitecube-net/status.json}
UNIT=whitecube-network.service
TIMEOUT=${TIMEOUT:-120}
ROLLBACK_TIMEOUT=${ROLLBACK_TIMEOUT:-90}
if [[ ${LIVE_TEST_NO_ROOT_CHECK:-} != 1 && $EUID -ne 0 ]]; then echo "run as root: sudo $0" >&2; exit 2; fi

field() { python3 -I -c 'import json,sys
try: print(json.load(open(sys.argv[1])).get(sys.argv[2]) or "")
except Exception: print("")' "$STATUS" "$1"; }

# online <seconds>: associated, has an address, and the default gateway answers a ping
online() {
  local deadline=$((SECONDS + $1)) state ip gw
  while (( SECONDS < deadline )); do
    state=$(field association_state); ip=$(field ipv4_address); gw=$(field default_route)
    if [[ $state == COMPLETED && -n $ip && -n $gw && $gw != direct ]] && "$PING" -c1 -W2 "$gw" >/dev/null 2>&1; then return 0; fi
    sleep "${POLL:-2}"
  done
  return 1
}

echo "== installing the new network files (a backup is made first)"
out=$("$APPLY" --apply 2>&1) || { printf '%s\n' "$out"; echo "install failed; nothing was restarted"; exit 1; }
printf '%s\n' "$out"
backup=$(printf '%s\n' "$out" | sed -n 's/^installed .*Backup: //p' | head -1)
[[ -n $backup ]] || { echo "could not find the backup path in apply.sh output; not restarting anything" >&2; exit 1; }

echo "== restarting $UNIT; Wi-Fi drops for a moment"
"$SYSTEMCTL" daemon-reload
"$SYSTEMCTL" restart "$UNIT"
if online "$TIMEOUT"; then
  echo
  echo "SUCCESS: associated with a lease, router reachable (ssid: $(field ssid), signal: $(field signal_dbm) dBm)."
  echo "Look at it for a while:  journalctl -u $UNIT -f        whitecube-doctor"
  echo "When you are satisfied:  sudo cube-net migrate && sudo apt purge wpasupplicant && sudo apt autoremove"
  exit 0
fi

echo
echo "FAILED to come online within ${TIMEOUT}s. Rolling back to the previous controller."
"$JOURNALCTL" -u "$UNIT" -n 40 --no-pager 2>&1 | sed 's/^/  | /'
"$APPLY" --root / --rollback "$backup"
"$SYSTEMCTL" daemon-reload
"$SYSTEMCTL" restart "$UNIT"
if online "$ROLLBACK_TIMEOUT"; then
  echo "ROLLED BACK: the previous controller is online again. The journal above says why the new one failed."
  exit 3
fi
echo "ROLLBACK DID NOT RESTORE THE NETWORK within ${ROLLBACK_TIMEOUT}s." >&2
echo "Manual recovery: sudo $APPLY --rollback $backup ; sudo systemctl daemon-reload ; sudo systemctl restart $UNIT" >&2
exit 4
