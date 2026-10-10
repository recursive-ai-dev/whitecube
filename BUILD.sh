#!/bin/bash
# Builds only into this staging directory. Never installs packages or modules.
# From a live USB: ./BUILD.sh /mnt/nvme_root ; on the target: ./BUILD.sh
set -euo pipefail
native_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
sysroot=${1:-}
cd "$native_dir"
# SANITIZE=1 ./BUILD.sh builds with AddressSanitizer + UndefinedBehaviorSanitizer into build-asan/ and runs
# the same C tests under them (leak detection is off by default: wlroots/pango keep process-lifetime caches;
# set ASAN_OPTIONS=detect_leaks=1 to look anyway). The normal build/ is untouched.
out=build
cc=${CC:-cc}
flags=(-std=c11 -O2 -g -Wall -Wextra -Wno-unused-parameter -Isrc -Isrc/protocols -Wl,-rpath,/usr/lib/whitecube)
if [[ -n ${SANITIZE:-} ]]; then
  out=build-asan
  flags+=(-fsanitize=address,undefined -fno-sanitize-recover=undefined -fno-omit-frame-pointer)
  export ASAN_OPTIONS=${ASAN_OPTIONS:-detect_leaks=0:abort_on_error=0}
  export UBSAN_OPTIONS=${UBSAN_OPTIONS:-print_stacktrace=1}
fi
mkdir -p "$out"
if [[ -n $sysroot ]]; then
  sysroot=$(realpath -- "$sysroot")
  cc=${CC:-$sysroot/usr/bin/gcc}
  loader=$sysroot/usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2
  libpath=$sysroot/usr/local/lib/x86_64-linux-gnu:$sysroot/usr/lib/x86_64-linux-gnu:$sysroot/usr/lib/mesa-diverted/x86_64-linux-gnu
  mkdir -p "$out/toolchain"
  for tool in as ld; do
    {
      printf '#!/bin/bash\nexec %q --library-path %q %q "$@"\n' "$loader" "$libpath" "$sysroot/usr/bin/$tool"
    } > "$out/toolchain/$tool"
    chmod +x "$out/toolchain/$tool"
  done
  export PKG_CONFIG_SYSROOT_DIR=$sysroot
  export PKG_CONFIG_LIBDIR=$sysroot/usr/local/lib/x86_64-linux-gnu/pkgconfig:$sysroot/usr/lib/x86_64-linux-gnu/pkgconfig:$sysroot/usr/share/pkgconfig
  pkg() { "$loader" --library-path "$libpath" "$sysroot/usr/bin/pkg-config" "$@"; }
  flags+=("--sysroot=$sysroot" "-B$native_dir/$out/toolchain/" "-Wl,-rpath-link,$sysroot/usr/local/lib/x86_64-linux-gnu" "-Wl,-rpath-link,$sysroot/usr/lib/x86_64-linux-gnu" "-Wl,-rpath-link,$sysroot/usr/lib/mesa-diverted/x86_64-linux-gnu")
else
  export PKG_CONFIG_PATH=/usr/local/lib/x86_64-linux-gnu/pkgconfig:${PKG_CONFIG_PATH:-}
  pkg() { pkg-config "$@"; }
fi
# pkg-config output consists of compiler options, expanded as individual words.
read -r -a compositor_flags <<< "$(pkg --cflags --libs wlroots-0.18 wayland-server xkbcommon pangocairo cairo gio-2.0 libinput)"
read -r -a client_flags <<< "$(pkg --cflags --libs wayland-client)"
read -r -a settings_flags <<< "$(pkg --cflags --libs wayland-client wayland-cursor xkbcommon pangocairo cairo gio-2.0)"
mkdir -p "$out/schemas"
cp data/schemas/org.whitecube.desktop.gschema.xml "$out/schemas/"
glib-compile-schemas --strict "$out/schemas"
"$cc" "${flags[@]}" -DWLR_USE_UNSTABLE src/whitecube.c -o $out/whitecube "${compositor_flags[@]}" -lm
"$cc" "${flags[@]}" src/cube-ca.c src/protocols/wlr-layer-shell-protocol.c src/protocols/xdg-shell-protocol.c -o $out/cube-ca "${client_flags[@]}" -lm
"$cc" "${flags[@]}" src/cube-settings.c src/protocols/xdg-shell-protocol.c src/protocols/wlr-output-management-protocol.c -o $out/cube-settings "${settings_flags[@]}" -lm
"$cc" "${flags[@]}" src/cube-status.c src/protocols/wlr-layer-shell-protocol.c src/protocols/xdg-shell-protocol.c -o $out/cube-status "${settings_flags[@]}" -lm
# X-free policy (MAINTENANCE.md): report libX11/libxcb reaching our binaries through the distro cairo.
# REQUIRE_XFREE=1 makes it fatal. Fix: tools/build-xfree-cairo.sh stages an X-free cairo (no root needed to build).
if [[ -z $sysroot ]]; then
  xlinked=()
  for bin in whitecube cube-ca cube-settings cube-status; do
    if ldd "$out/$bin" | grep -qi -E 'libX11|libxcb'; then xlinked+=("$bin"); fi
  done
  if (( ${#xlinked[@]} )); then
    echo "WARNING: X11/XCB libraries are linked into: ${xlinked[*]} (see tools/build-xfree-cairo.sh)" >&2
    [[ -z ${REQUIRE_XFREE:-} ]] || { echo "REQUIRE_XFREE is set: failing" >&2; exit 1; }
  else
    echo "X-free: no libX11/libxcb in our binaries"
  fi
fi
python3 tests/workspace-test.py > $out/workspace-test.c
"$cc" "${flags[@]}" $out/workspace-test.c -o $out/workspace-test
"$cc" "${flags[@]}" tests/desktop-state-test.c -o $out/desktop-state-test
"$cc" "${flags[@]}" -DWLR_USE_UNSTABLE tests/compositor-test.c -o $out/compositor-test "${compositor_flags[@]}" -lm
"$cc" "${flags[@]}" -DWLR_USE_UNSTABLE tests/hotplug-test.c -o $out/hotplug-test "${compositor_flags[@]}" -lm
"$cc" "${flags[@]}" tests/status-theme-test.c -o $out/status-theme-test -lm
"$cc" "${flags[@]}" tests/settings-test.c src/protocols/xdg-shell-protocol.c src/protocols/wlr-output-management-protocol.c -o $out/settings-test "${settings_flags[@]}" -lm
if [[ -n $sysroot ]]; then
	"$loader" --library-path "$libpath" $out/compositor-test
	WLR_RENDERER=pixman "$loader" --library-path "$libpath" $out/hotplug-test
	"$loader" --library-path "$libpath" $out/status-theme-test
	"$loader" --library-path "$libpath" $out/settings-test
	"$loader" --library-path "$libpath" $out/cube-status --selftest
  "$loader" --library-path "$libpath" $out/workspace-test
  "$loader" --library-path "$libpath" $out/desktop-state-test
  "$loader" --library-path "$libpath" $out/cube-ca --self-test
  "$loader" --library-path "$libpath" $out/cube-ca --preview $out/cube-space.ppm
  "$loader" --library-path "$libpath" $out/cube-status --render "$out" >/dev/null
else
	$out/compositor-test
	WLR_RENDERER=pixman $out/hotplug-test      # real wlroots headless backend: outputs added and removed at runtime
	$out/status-theme-test
	$out/settings-test
	$out/cube-status --selftest
  $out/workspace-test
  $out/desktop-state-test
  $out/cube-ca --self-test
  $out/cube-ca --preview $out/cube-space.ppm
  $out/cube-status --render "$out" >/dev/null
fi
# Network controller (cube-net, DHCP hook, unit, helper): pure Python, needs no root, radio or network.
python3 -I net/test_cube_net.py
python3 -I net/test_cube_wifi.py
python3 -I net/test_net_switch.py
python3 -I tests/test-privacy.py
python3 -I tests/test-doctor.py
python3 -I tests/test-sudoers.py
python3 -I tests/test-clean.py
python3 -I tests/test-whitecube-install.py
python3 -I tests/test-install.py
python3 -I tests/test-preferences.py
python3 -I tests/test-migrate.py
python3 -I tests/test-packaging.py
"${NODE:-node}" --test apps/desktop-chat/test/*.test.js
python3 -I apps/desktop-chat/test/local-server-test.py
python3 -I -m unittest discover -s apps/cube-train/tests -p 'test_engine.py' -v
python3 -I tests/test-cube-train-ui.py
python3 -I -m unittest discover -s apps/cube-editor/tests -v
python3 -I tests/test-cube-editor-ui.py
python3 -I tests/test-chrootables.py
python3 -I tests/test-app-launch.py
# The wc suite tests run wrappers from temporary homes, which a noexec /tmp would refuse.
TMPDIR="$native_dir/$out" python3 -I -m unittest discover -s apps/wc-suite -p test_suite.py
bash -n net/apply.sh
printf 'Built: %s/%s/{whitecube,cube-ca,cube-settings,cube-status}; previews: %s/cube-space.ppm %s/{bar,menu,menu-search}.png\n' "$native_dir" "$out" "$out" "$out"
