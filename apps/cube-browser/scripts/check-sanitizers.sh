#!/usr/bin/env bash
# Component checks run without the workers' address-space ceiling, which is
# incompatible with ASan shadow memory. Production sandbox tests run separately.
set -euo pipefail
browser_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$browser_root"
export PKG_CONFIG_PATH="$browser_root/.deps/prefix/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
quickjs_source="$browser_root/.deps/src/quickjs-2026-06-04"
quickjs_objects=()
for unit in quickjs dtoa libregexp libunicode cutils; do
    quickjs_objects+=(".obj/asan/ubsan/$unit.o")
done
make -C "$quickjs_source" -j"${JOBS:-4}" CONFIG_ASAN=y CONFIG_UBSAN=y "${quickjs_objects[@]}"
quickjs_paths=("${quickjs_objects[@]/#/$quickjs_source/}")
ar rcs .deps/lib/libquickjs-core-sanitize.a "${quickjs_paths[@]}"
if [[ -f build/sanitize/build.ninja ]]; then
    meson setup --reconfigure build/sanitize -Db_sanitize=address,undefined -Dbuildtype=debug
else
    meson setup build/sanitize -Db_sanitize=address,undefined -Dbuildtype=debug
fi
meson compile -C build/sanitize test-engine test-layout test-image test-storage test-core test-policy test-forms test-media test-session test-download test-dom test-fuzz test-js
# Fontconfig/Pango keep global caches. Address/UB checks remain enabled for all
# components; leak checks are retained for components that do not initialize fonts.
for suite in storage ipc-and-address web-policy forms media sessions downloads images javascript-runtime; do
    ASAN_OPTIONS=detect_leaks=1:abort_on_error=1 UBSAN_OPTIONS=halt_on_error=1:print_stacktrace=1 \
        meson test -C build/sanitize --no-rebuild --print-errorlogs "$suite"
done
ASAN_OPTIONS=detect_leaks=0:abort_on_error=1 UBSAN_OPTIONS=halt_on_error=1:print_stacktrace=1 \
    meson test -C build/sanitize --no-rebuild --print-errorlogs engine layout-compatibility dom-lifecycle mutation-smoke --num-processes 1 --timeout-multiplier 3
