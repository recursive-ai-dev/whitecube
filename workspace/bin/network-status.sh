#!/bin/bash
set -u
base=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
while :; do
  printf '\033[H\033[2JWHITECUBE / WI-FI NETWORK\n\n'
  if [[ -x /usr/bin/cube-net ]]; then
    /usr/bin/cube-net status
  elif [[ -f "$base/network/cube-net" ]]; then
    python3 "$base/network/cube-net" status
  else
    printf 'cube-net is not installed.\n'
  fi
  printf '\nRefresh 3s · Ctrl+C returns to Bash\n'
  sleep 3
done
