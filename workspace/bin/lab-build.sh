#!/bin/bash
set -eu
if [[ ! -x /usr/bin/cube-lab ]]; then
  printf 'Install the Whitecube upgrade first. Then provision your four labs with:\n  sudo cube-lab provision --install-tools\n'
  exit 1
fi
exec sudo /usr/bin/cube-lab enter build
