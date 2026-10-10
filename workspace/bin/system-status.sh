#!/bin/bash
set -u
trap 'printf "\033[?25h\033[0m\n"; exit 0' INT TERM
printf '\033[?25l'
while :; do
  printf '\033[H\033[2J\033[1;37mWHITECUBE / SYSTEM STREAM\033[0m\n\n'
  date '+%A %d %B %Y  %H:%M:%S'
  printf '\n'
  uptime
  printf '\n'
  free -h
  printf '\nPROCESSES\n'
  ps -eo pid,comm,%cpu,%mem --sort=-%cpu | head -n 12
  printf '\nRefresh 2s · Ctrl+C returns to Bash\n'
  sleep 2
done
