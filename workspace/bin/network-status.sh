#!/bin/bash
set -u
base=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
while :; do
  printf '\033[H\033[2JWHITECUBE / USB NETWORK\n\n'
  if [[ -x /usr/bin/cube-net ]]; then
    /usr/bin/cube-net status
  elif [[ -f "$base/network/cube-net" ]]; then
    python3 "$base/network/cube-net" status
  else
    printf 'cube-net is not installed. Expected dongle: USB 2001:3329.\n'
  fi
  printf '\nRefresh 3s · Ctrl+C returns to Bash\n'
  sleep 3
done
