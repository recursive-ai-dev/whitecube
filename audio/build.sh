#!/bin/sh
# Build cube-audio and run its hardware-free tests. Needs only gcc and the kernel
# headers (linux-libc-dev); nothing from alsa, pipewire or pulse.
set -eu
cd "$(dirname "$0")"
CFLAGS="${CFLAGS:--std=gnu11 -O2 -Wall -Wextra -Wno-unused-parameter}"
mkdir -p build
gcc $CFLAGS -o build/cube-audio cube-audio.c -lm
gcc $CFLAGS -Wno-unused-function -fsanitize=address,undefined -o build/test-cube-audio test-cube-audio.c -lm
(cd build && ./test-cube-audio)
echo "built audio/build/cube-audio"
