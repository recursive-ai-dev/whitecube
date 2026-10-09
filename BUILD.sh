#!/bin/bash
# Builds only into this staging directory. Never installs packages or modules.
# From a live USB: ./BUILD.sh /mnt/nvme_root ; on the target: ./BUILD.sh
set -euo pipefail
native_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
sysroot=${1:-}
cd "$native_dir"
mkdir -p build
cc=${CC:-cc}
flags=(-std=c11 -O2 -g -Wall -Wextra -Wno-unused-parameter -I.)
if [[ -n $sysroot ]]; then
  sysroot=$(realpath -- "$sysroot")
  cc=${CC:-$sysroot/usr/bin/gcc}
  loader=$sysroot/usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2
  libpath=$sysroot/usr/local/lib/x86_64-linux-gnu:$sysroot/usr/lib/x86_64-linux-gnu:$sysroot/usr/lib/mesa-diverted/x86_64-linux-gnu
  mkdir -p build/toolchain
  for tool in as ld; do
    {
      printf '#!/bin/bash\nexec %q --library-path %q %q "$@"\n' "$loader" "$libpath" "$sysroot/usr/bin/$tool"
    } > "build/toolchain/$tool"
    chmod +x "build/toolchain/$tool"
  done
  export PKG_CONFIG_SYSROOT_DIR=$sysroot
  export PKG_CONFIG_LIBDIR=$sysroot/usr/local/lib/x86_64-linux-gnu/pkgconfig:$sysroot/usr/lib/x86_64-linux-gnu/pkgconfig:$sysroot/usr/share/pkgconfig
  pkg() { "$loader" --library-path "$libpath" "$sysroot/usr/bin/pkg-config" "$@"; }
  flags+=("--sysroot=$sysroot" "-B$native_dir/build/toolchain/" "-Wl,-rpath-link,$sysroot/usr/local/lib/x86_64-linux-gnu" "-Wl,-rpath-link,$sysroot/usr/lib/x86_64-linux-gnu" "-Wl,-rpath-link,$sysroot/usr/lib/mesa-diverted/x86_64-linux-gnu")
else
  export PKG_CONFIG_PATH=/usr/local/lib/x86_64-linux-gnu/pkgconfig:${PKG_CONFIG_PATH:-}
  pkg() { pkg-config "$@"; }
fi
# pkg-config output consists of compiler options, expanded as individual words.
read -r -a compositor_flags <<< "$(pkg --cflags --libs wlroots-0.18 wayland-server xkbcommon pangocairo cairo)"
read -r -a client_flags <<< "$(pkg --cflags --libs wayland-client)"
read -r -a settings_flags <<< "$(pkg --cflags --libs wayland-client wayland-cursor xkbcommon pangocairo cairo)"
"$cc" "${flags[@]}" -DWLR_USE_UNSTABLE whitecube.c -o build/whitecube "${compositor_flags[@]}" -lm
"$cc" "${flags[@]}" cube-ca.c wlr-layer-shell-protocol.c xdg-shell-protocol.c -o build/cube-ca "${client_flags[@]}" -lm
"$cc" "${flags[@]}" cube-settings.c xdg-shell-protocol.c wlr-output-management-protocol.c -o build/cube-settings "${settings_flags[@]}" -lm
"$cc" "${flags[@]}" cube-status.c wlr-layer-shell-protocol.c xdg-shell-protocol.c -o build/cube-status "${settings_flags[@]}" -lm
python3 workspace-test.py > build/workspace-test.c
"$cc" "${flags[@]}" build/workspace-test.c -o build/workspace-test
"$cc" "${flags[@]}" desktop-state-test.c -o build/desktop-state-test
"$cc" "${flags[@]}" -DWLR_USE_UNSTABLE compositor-test.c -o build/compositor-test "${compositor_flags[@]}" -lm
"$cc" "${flags[@]}" status-theme-test.c -o build/status-theme-test -lm
"$cc" "${flags[@]}" settings-test.c xdg-shell-protocol.c wlr-output-management-protocol.c -o build/settings-test "${settings_flags[@]}" -lm
if [[ -n $sysroot ]]; then
	"$loader" --library-path "$libpath" build/compositor-test
	"$loader" --library-path "$libpath" build/status-theme-test
	"$loader" --library-path "$libpath" build/settings-test
	"$loader" --library-path "$libpath" build/cube-status --selftest
  "$loader" --library-path "$libpath" build/workspace-test
  "$loader" --library-path "$libpath" build/desktop-state-test
  "$loader" --library-path "$libpath" build/cube-ca --self-test
  "$loader" --library-path "$libpath" build/cube-ca --preview build/cube-space.ppm
  "$loader" --library-path "$libpath" build/cube-status --render build >/dev/null
else
	build/compositor-test
	build/status-theme-test
	build/settings-test
	build/cube-status --selftest
  build/workspace-test
  build/desktop-state-test
  build/cube-ca --self-test
  build/cube-ca --preview build/cube-space.ppm
  build/cube-status --render build >/dev/null
fi
# Network controller (cube-net, DHCP hook, unit, helper): pure Python, needs no root, radio or network.
python3 -I net/test_cube_net.py
python3 -I net/test_cube_wifi.py
python3 -I test-privacy.py
python3 -I test-doctor.py
python3 -I test-install.py
bash -n net/apply.sh
printf 'Built: %s/build/{whitecube,cube-ca,cube-settings,cube-status}; previews: build/cube-space.ppm build/{bar,menu,menu-search}.png\n' "$native_dir"
