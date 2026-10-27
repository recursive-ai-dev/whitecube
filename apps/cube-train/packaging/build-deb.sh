#!/bin/sh
# Build a standalone .deb; installs nothing and requires no root access.
set -eu
exec /usr/bin/python3 -I "$(dirname -- "$0")/build-deb.py" "$@"
