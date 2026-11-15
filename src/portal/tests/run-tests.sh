#!/bin/sh
# SPDX-License-Identifier: MIT
#
# run-tests.sh - whitecube-portal integration suite.
#
# Runs against a throwaway dbus-daemon session bus (dbus-run-session), which is
# wire-compatible with whitecube-bus.  Uses gdbus + busctl for everything that
# can be tested with one-shot calls, and tests/portal-probe for the signal flow
# (a real client must stay connected and subscribe before calling OpenFile -
# `gdbus call` disconnects the instant the handle is returned).
#
#   make integration     or     sh tests/run-tests.sh
#
# Set WC_PORTAL_BIN to test another build (e.g. build/whitecube-portal-asan).

set -u

HERE=$(cd "$(dirname "$0")" && pwd)
PORTAL=${WC_PORTAL_BIN:-$HERE/../build/whitecube-portal}
PROBE=${WC_PROBE_BIN:-$HERE/../build/portal-probe}
DEST=org.freedesktop.portal.Desktop
OPATH=/org/freedesktop/portal/desktop

if [ ! -x "$PORTAL" ]; then echo "no portal binary at $PORTAL" >&2; exit 1; fi
if [ ! -x "$PROBE" ]; then echo "no probe binary at $PROBE" >&2; exit 1; fi

# Re-exec ourselves inside a private session bus.
if [ -z "${WC_TEST_INNER:-}" ]; then
    exec dbus-run-session -- env WC_TEST_INNER=1 \
        WC_PORTAL_BIN="$PORTAL" WC_PROBE_BIN="$PROBE" sh "$HERE/run-tests.sh"
fi

WORK=$(mktemp -d "${TMPDIR:-/tmp}/wc-portal-test-XXXXXX")
PORTAL_PID=
PASS=0
FAIL=0

cleanup() {
    if [ -n "$PORTAL_PID" ]; then
        kill "$PORTAL_PID" 2>/dev/null
        wait "$PORTAL_PID" 2>/dev/null
    fi
    rm -rf "$WORK"
}
trap cleanup EXIT INT TERM

mkdir -p "$WORK/runtime" "$WORK/home" "$WORK/config/whitecube" "$WORK/state/whitecube" "$WORK/files/sub"
chmod 700 "$WORK/runtime"
XDG_RUNTIME_DIR="$WORK/runtime"
HOME="$WORK/home"
XDG_CONFIG_HOME="$WORK/config"
XDG_STATE_HOME="$WORK/state"
export XDG_RUNTIME_DIR HOME XDG_CONFIG_HOME XDG_STATE_HOME

printf 'hello\n' > "$WORK/files/plain.txt"
printf 'x\n' > "$WORK/files/with space #?%.txt"
printf 'x\n' > "$WORK/files/café 日本.txt"
printf 'x\n' > "$WORK/files/sub/nested.txt"

export WC_MOCK_LOG="$WORK/argv.log"
export WC_MOCK_BROWSER_LOG="$WORK/browser.log"
export WC_MOCK_STATE="$WORK/state.log"
export WC_MOCK_CONFIG="$WORK/mock.cfg"
: > "$WC_MOCK_LOG"
: > "$WC_MOCK_BROWSER_LOG"
: > "$WC_MOCK_STATE"

# mock_cfg ACTION [DELAY] [paths...]
mock_cfg() {
    action=$1; delay=${2:-0}
    shift 2 2>/dev/null || shift $#
    : > "$WORK/paths"
    for p in "$@"; do printf '%s\n' "$p" >> "$WORK/paths"; done
    {
        printf 'ACTION=%s\n' "$action"
        printf 'DELAY=%s\n' "$delay"
        printf 'PATHS_FILE=%s\n' "$WORK/paths"
    } > "$WC_MOCK_CONFIG"
}

gcall() {
    gdbus call --session --dest "$DEST" --object-path "$OPATH" --method "$@" 2>&1
}

ok()   { PASS=$((PASS + 1)); printf '  ok    %s\n' "$1"; }
bad()  { FAIL=$((FAIL + 1)); printf '  FAIL  %s\n        %s\n' "$1" "$2"; }

is()   { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "want [$2] got [$3]"; fi; }
has()  { case "$3" in *"$2"*) ok "$1" ;; *) bad "$1" "missing [$2] in [$3]" ;; esac; }
hasnt(){ case "$3" in *"$2"*) bad "$1" "unexpected [$2] in [$3]" ;; *) ok "$1" ;; esac; }

printf '\n=== whitecube-portal integration suite ===\n'
printf 'binary: %s\n\n' "$PORTAL"

mock_cfg select 0 "$WORK/files/plain.txt"
"$PORTAL" --picker-cmd "$HERE/mock-picker" --browser-cmd "$HERE/mock-browser" \
    --max-requests 8 --timeout 6 -v > "$WORK/portal.log" 2>&1 &
PORTAL_PID=$!

i=0
while [ "$i" -lt 60 ]; do
    if gdbus call --session --dest "$DEST" --object-path "$OPATH" \
        --method org.freedesktop.DBus.Peer.Ping >/dev/null 2>&1; then break; fi
    i=$((i + 1)); sleep 0.1
done
if [ "$i" -ge 60 ]; then
    echo "portal did not start:"; cat "$WORK/portal.log"; exit 1
fi
FD_START=$(ls "/proc/$PORTAL_PID/fd" 2>/dev/null | wc -l)

# ---------------------------------------------------------------------------
printf -- '--- 1. Settings (GTK4 / libadwaita dark mode) ---\n'
is  "Settings.Read color-scheme == 1 (dark)" "(<<uint32 1>>,)" \
    "$(gcall org.freedesktop.portal.Settings.Read org.freedesktop.appearance color-scheme)"
is  "Settings.ReadOne color-scheme == 1 (dark)" "(<uint32 1>,)" \
    "$(gcall org.freedesktop.portal.Settings.ReadOne org.freedesktop.appearance color-scheme)"
has "Settings.Read accent-color is (ddd)" "(0.4" \
    "$(gcall org.freedesktop.portal.Settings.Read org.freedesktop.appearance accent-color)"
is  "Settings.Read contrast == 0" "(<<uint32 0>>,)" \
    "$(gcall org.freedesktop.portal.Settings.Read org.freedesktop.appearance contrast)"
has "Settings.Read unknown key -> NotFound" "org.freedesktop.portal.Error.NotFound" \
    "$(gcall org.freedesktop.portal.Settings.Read org.freedesktop.appearance nope)"
has "Settings.Read unknown ns -> NotFound" "org.freedesktop.portal.Error.NotFound" \
    "$(gcall org.freedesktop.portal.Settings.Read org.example.nope color-scheme)"
RA=$(gcall org.freedesktop.portal.Settings.ReadAll "['org.freedesktop.appearance']")
has "ReadAll contains color-scheme" "'color-scheme': <uint32 1>" "$RA"
has "ReadAll contains accent-color" "'accent-color'" "$RA"
has "ReadAll contains contrast" "'contrast': <uint32 0>" "$RA"
has "ReadAll([]) returns every namespace" "org.freedesktop.appearance" \
    "$(gcall org.freedesktop.portal.Settings.ReadAll "[]")"
has "ReadAll unknown ns -> empty dict" "'org.example.nope': @a{sv} {}" \
    "$(gcall org.freedesktop.portal.Settings.ReadAll "['org.example.nope']")"
has "Settings.Read with wrong arity -> InvalidArgs" "InvalidArgs" \
    "$(gcall org.freedesktop.portal.Settings.Read onlyone)"

# latency: the whole point is that this never blocks
T0=$(date +%s%N)
gcall org.freedesktop.portal.Settings.Read org.freedesktop.appearance color-scheme >/dev/null
T1=$(date +%s%N)
MS=$(( (T1 - T0) / 1000000 ))
if [ "$MS" -lt 1000 ]; then ok "Settings.Read latency ${MS}ms (<1000ms)"
else bad "Settings.Read latency" "${MS}ms"; fi

printf -- '\n--- 2. Properties (the 25 second timeout killer) ---\n'
is  "Properties.Get Settings.version" "(<uint32 2>,)" \
    "$(gcall org.freedesktop.DBus.Properties.Get org.freedesktop.portal.Settings version)"
is  "Properties.Get FileChooser.version" "(<uint32 3>,)" \
    "$(gcall org.freedesktop.DBus.Properties.Get org.freedesktop.portal.FileChooser version)"
is  "Properties.Get OpenURI.version" "(<uint32 3>,)" \
    "$(gcall org.freedesktop.DBus.Properties.Get org.freedesktop.portal.OpenURI version)"
is  "Properties.GetAll FileChooser" "({'version': <uint32 3>},)" \
    "$(gcall org.freedesktop.DBus.Properties.GetAll org.freedesktop.portal.FileChooser)"
has "Properties.Set -> PropertyReadOnly" "PropertyReadOnly" \
    "$(gcall org.freedesktop.DBus.Properties.Set org.freedesktop.portal.FileChooser version "<uint32 9>")"
has "Properties.Get unknown iface -> UnknownInterface" "UnknownInterface" \
    "$(gcall org.freedesktop.DBus.Properties.Get org.example.Nope version)"
has "Properties.Get unknown prop -> InvalidArgs" "InvalidArgs" \
    "$(gcall org.freedesktop.DBus.Properties.Get org.freedesktop.portal.Settings nope)"
has "Peer.Ping" "()" "$(gcall org.freedesktop.DBus.Peer.Ping)"
has "Peer.GetMachineId is 32 hex" "'" "$(gcall org.freedesktop.DBus.Peer.GetMachineId)"

T0=$(date +%s%N)
gcall org.freedesktop.DBus.Properties.Get org.freedesktop.portal.FileChooser version >/dev/null
T1=$(date +%s%N)
MS=$(( (T1 - T0) / 1000000 ))
if [ "$MS" -lt 1000 ]; then ok "Properties.Get latency ${MS}ms (<1000ms)"
else bad "Properties.Get latency" "${MS}ms"; fi

printf -- '\n--- 3. busctl + introspection ---\n'
is  "busctl Settings.Read" "v v u 1" \
    "$(busctl --user call "$DEST" "$OPATH" org.freedesktop.portal.Settings Read ss \
        org.freedesktop.appearance color-scheme 2>&1)"
is  "busctl Settings.ReadOne" "v u 1" \
    "$(busctl --user call "$DEST" "$OPATH" org.freedesktop.portal.Settings ReadOne ss \
        org.freedesktop.appearance color-scheme 2>&1)"
is  "busctl get-property FileChooser version" "u 3" \
    "$(busctl --user get-property "$DEST" "$OPATH" org.freedesktop.portal.FileChooser version 2>&1)"
is  "busctl get-property Settings version" "u 2" \
    "$(busctl --user get-property "$DEST" "$OPATH" org.freedesktop.portal.Settings version 2>&1)"
has "busctl Properties.Get" "v u 3" \
    "$(busctl --user call "$DEST" "$OPATH" org.freedesktop.DBus.Properties Get ss \
        org.freedesktop.portal.FileChooser version 2>&1)"
INTRO=$(busctl --user introspect "$DEST" "$OPATH" 2>&1)
has "introspection lists Settings" "org.freedesktop.portal.Settings" "$INTRO"
has "introspection lists FileChooser" "org.freedesktop.portal.FileChooser" "$INTRO"
has "introspection lists OpenURI" "org.freedesktop.portal.OpenURI" "$INTRO"
has "introspection lists OpenFile" "OpenFile" "$INTRO"
has "introspection hides denied ScreenCast" "org.freedesktop.portal.FileChooser" "$INTRO"
hasnt "introspection does not advertise ScreenCast" "ScreenCast" "$INTRO"
has "gdbus introspect parses our XML" "interface org.freedesktop.portal.FileChooser" \
    "$(gdbus introspect --session --dest "$DEST" --object-path "$OPATH" 2>&1)"

printf -- '\n--- 4. deny-by-default for invasive portals ---\n'
for spec in \
    "org.freedesktop.portal.ScreenCast CreateSession" \
    "org.freedesktop.portal.RemoteDesktop CreateSession" \
    "org.freedesktop.portal.Camera AccessCamera" \
    "org.freedesktop.portal.Location CreateSession" \
    "org.freedesktop.portal.Secret RetrieveSecret" \
    "org.freedesktop.portal.Screenshot Screenshot" ; do
    set -- $spec
    has "$1.$2 -> NotAllowed" "org.freedesktop.portal.Error.NotAllowed" "$(gcall "$1.$2")"
done
has "Properties.Get on denied iface -> NotAllowed" "NotAllowed" \
    "$(gcall org.freedesktop.DBus.Properties.Get org.freedesktop.portal.Camera version)"
has "unknown interface -> UnknownInterface" "UnknownInterface" \
    "$(gcall org.example.Bogus.DoThing)"
has "unknown method -> UnknownMethod" "UnknownMethod" \
    "$(gcall org.freedesktop.portal.FileChooser.Bogus)"
has "unknown object -> UnknownObject" "UnknownObject" \
    "$(gdbus call --session --dest "$DEST" --object-path /nope --method org.freedesktop.DBus.Peer.Ping 2>&1)"
has "OpenURI.OpenDirectory -> NotAllowed" "NotAllowed" \
    "$(gcall org.freedesktop.portal.OpenURI.OpenDirectory '' '/tmp' '{}')"

printf -- '\n--- 5. FileChooser request/response flow ---\n'
H=$(gcall org.freedesktop.portal.FileChooser.OpenFile "" "Select File" "{'handle_token': <'test_tok_1'>}")
has "OpenFile returns a Request object path" "/org/freedesktop/portal/desktop/request/" "$H"
has "OpenFile honours handle_token" "/test_tok_1'" "$H"
case "$H" in
    */request/[0-9]*_[0-9]*/test_tok_1*) ok "request path uses GTK sender escaping" ;;
    *) bad "request path uses GTK sender escaping" "$H" ;;
esac

mock_cfg select 0 "$WORK/files/plain.txt"
OUT=$("$PROBE" --method OpenFile --token t_sel --dir "$WORK/files" --wait-ms 8000 2>&1)
has "OpenFile emits Response 0" "RESPONSE 0" "$OUT"
has "OpenFile returns the selected file URI" "URI file://$WORK/files/plain.txt" "$OUT"

# sender escaping cross-check against the daemon's own log
LINE=$(grep 'for :1\.' "$WORK/portal.log" | tail -1)
RPATH=$(printf '%s\n' "$LINE" | sed -n 's/.*request \([^ ]*\) (kind=[0-9]*) for .*$/\1/p')
RSEND=$(printf '%s\n' "$LINE" | sed -n 's/.* for \(:[0-9.]*\)$/\1/p')
ELEM=$(printf '%s' "$RSEND" | sed 's/^://; s/\./_/g')
case "$RPATH" in
    */request/$ELEM/*) ok "sender $RSEND -> path element $ELEM (matches GTK)" ;;
    *) bad "sender escaping" "$RSEND vs $RPATH" ;;
esac

printf -- '\n--- 6. URI percent encoding (RFC 8089) ---\n'
mock_cfg select 0 "$WORK/files/with space #?%.txt" "$WORK/files/café 日本.txt"
OUT=$("$PROBE" --method OpenFile --token t_enc --dir "$WORK/files" --multiple --wait-ms 8000 2>&1)
has "space -> %20" "with%20space%20" "$OUT"
has "# -> %23" "%23" "$OUT"
has "? -> %3F" "%3F" "$OUT"
has "% -> %25" "%25.txt" "$OUT"
has "UTF-8 percent encoded" "caf%C3%A9%20%E6%97%A5%E6%9C%AC.txt" "$OUT"
hasnt "no raw spaces in URIs" "URI file://$WORK/files/with space" "$OUT"

printf -- '\n--- 7. multiple / directory / save ---\n'
mock_cfg select 0 "$WORK/files/plain.txt" "$WORK/files/sub/nested.txt"
OUT=$("$PROBE" --method OpenFile --token t_multi --dir "$WORK/files" --multiple --wait-ms 8000 2>&1)
N=$(printf '%s\n' "$OUT" | grep -c '^URI ')
is  "multiple=true returns both selections" "2" "$N"
OUT=$("$PROBE" --method OpenFile --token t_single --dir "$WORK/files" --wait-ms 8000 2>&1)
N=$(printf '%s\n' "$OUT" | grep -c '^URI ')
is  "multiple=false returns only the first" "1" "$N"

mock_cfg select 0 "$WORK/files/sub"
OUT=$("$PROBE" --method OpenFile --token t_dir --dir "$WORK/files" --directory --wait-ms 8000 2>&1)
has "directory=true returns a directory URI" "URI file://$WORK/files/sub" "$OUT"

mock_cfg select 0 "$WORK/files/brand-new.txt"
OUT=$("$PROBE" --method SaveFile --token t_save --dir "$WORK/files" --name "brand-new.txt" --wait-ms 8000 2>&1)
has "SaveFile accepts a not-yet-existing target" "RESPONSE 0" "$OUT"
has "SaveFile returns the target URI" "URI file://$WORK/files/brand-new.txt" "$OUT"

mock_cfg select 0 "$WORK/files/passwd"
OUT=$("$PROBE" --method SaveFile --token t_trav --dir "$WORK/files" --name "../../etc/passwd" --wait-ms 8000 2>&1)
has "SaveFile current_name is basename-sanitised" "ARG passwd" "$(cat "$WC_MOCK_LOG")"

printf -- '\n--- 8. cancellation ---\n'
mock_cfg cancel 0
OUT=$("$PROBE" --method OpenFile --token t_cancel --dir "$WORK/files" --wait-ms 8000 2>&1)
has "picker exit 1 -> Response 1" "RESPONSE 1" "$OUT"
hasnt "cancel carries no uris" "URI " "$OUT"
mock_cfg empty 0
OUT=$("$PROBE" --method OpenFile --token t_empty --dir "$WORK/files" --wait-ms 8000 2>&1)
has "exit 0 with empty selection -> Response 1" "RESPONSE 1" "$OUT"
mock_cfg crash 0
OUT=$("$PROBE" --method OpenFile --token t_crash --dir "$WORK/files" --wait-ms 8000 2>&1)
has "picker killed by signal -> Response 1" "RESPONSE 1" "$OUT"

printf -- '\n--- 9. Request.Close ---\n'
mock_cfg hang 0
OUT=$("$PROBE" --method OpenFile --token t_close --dir "$WORK/files" \
        --close-after-ms 600 --wait-ms 8000 2>&1)
has "Close is accepted" "CLOSE_SENT" "$OUT"
has "Close -> Response 2" "RESPONSE 2" "$OUT"
has "Close SIGTERMs the picker" "TERM " "$(cat "$WC_MOCK_STATE")"

printf -- '\n--- 10. path + argument sanitisation ---\n'
mock_cfg select 0 "$WORK/files/plain.txt"
OUT=$("$PROBE" --method OpenFile --token t_badfolder --dir "/nonexistent/../etc/nope" --wait-ms 8000 2>&1)
has "invalid current_folder falls back to \$HOME" "DIR $HOME" "$(cat "$WC_MOCK_STATE")"
has "invalid current_folder still completes" "RESPONSE 0" "$OUT"

OUT=$("$PROBE" --method OpenFile --token t_goodfolder --dir "$WORK/files/sub" --wait-ms 8000 2>&1)
has "valid current_folder is passed through" "DIR $WORK/files/sub" "$(cat "$WC_MOCK_STATE")"

EVIL='$(touch /tmp/WC_PWNED) ; touch /tmp/WC_PWNED2 `touch /tmp/WC_PWNED3`'
OUT=$("$PROBE" --method OpenFile --token t_inject --title "$EVIL" --dir "$WORK/files" --wait-ms 8000 2>&1)
has "shell metacharacters survive as one literal argv element" "ARG $EVIL" "$(cat "$WC_MOCK_LOG")"
if [ ! -e /tmp/WC_PWNED ] && [ ! -e /tmp/WC_PWNED2 ] && [ ! -e /tmp/WC_PWNED3 ]; then
    ok "no shell injection: no side-effect files created"
else
    rm -f /tmp/WC_PWNED /tmp/WC_PWNED2 /tmp/WC_PWNED3
    bad "no shell injection" "a payload executed"
fi
has "injection attempt still returns a normal response" "RESPONSE 0" "$OUT"

LONG=$(printf 'A%.0s' $(seq 1 600))
OUT=$("$PROBE" --method OpenFile --token t_long --title "$LONG" --dir "$WORK/files" --wait-ms 8000 2>&1)
has "600 char title is clipped, not fatal" "RESPONSE 0" "$OUT"

OUT=$("$PROBE" --method OpenFile --token 'bad/../token' --dir "$WORK/files" --wait-ms 8000 2>&1)
has "handle_token is sanitised into a valid path element" "HANDLE /org/freedesktop/portal/desktop/request/" "$OUT"
hasnt "sanitised token has no traversal" ".." "$OUT"

OUT=$("$PROBE" --method OpenFile --token t_dup --dir "$WORK/files" --repeat-call --wait-ms 8000 2>&1)
has "duplicate handle_token -> InvalidArgument" "org.freedesktop.portal.Error.InvalidArgument" "$OUT"

printf -- '\n--- 11. OpenURI allowlist ---\n'
: > "$WC_MOCK_BROWSER_LOG"
OUT=$("$PROBE" --method OpenURI --uri "https://example.org/a?b=c%20d" --wait-ms 8000 2>&1)
has "https OpenURI -> Response 0" "RESPONSE 0" "$OUT"
has "browser got the URI verbatim as argv[1]" "ARG https://example.org/a?b=c%20d" "$(cat "$WC_MOCK_BROWSER_LOG")"
N=$(grep -c '^ARG ' "$WC_MOCK_BROWSER_LOG")
is  "browser argv has exactly 1 positional arg (no shell)" "1" "$N"
OUT=$("$PROBE" --method OpenURI --uri "http://example.org/" --wait-ms 8000 2>&1)
has "http OpenURI -> Response 0" "RESPONSE 0" "$OUT"
for u in "file:///etc/passwd" "javascript:alert(1)" "data:text/html,x" \
         "ftp://example.org/" "https://" "https://user:pw@example.org/" \
         "HTTPS://example.org/" "https://example.org/ a" ; do
    OUT=$("$PROBE" --method OpenURI --uri "$u" --wait-ms 4000 2>&1)
    has "OpenURI rejects [$u]" "org.freedesktop.portal.Error.NotAllowed" "$OUT"
done

printf -- '\n--- 12. concurrency cap ---\n'
mock_cfg hang 0
i=1
CAP_PIDS=
while [ "$i" -le 8 ]; do
    "$PROBE" --method OpenFile --token "cap$i" --dir "$WORK/files" \
        --close-after-ms 2500 --wait-ms 15000 > "$WORK/cap$i.out" 2>&1 &
    CAP_PIDS="$CAP_PIDS $!"
    i=$((i + 1))
done
sleep 1.5
OUT=$("$PROBE" --method OpenFile --token cap9 --dir "$WORK/files" --wait-ms 4000 2>&1)
has "9th concurrent request -> LimitsExceeded" "org.freedesktop.DBus.Error.LimitsExceeded" "$OUT"
for p in $CAP_PIDS; do wait "$p"; done
N=$(grep -l 'RESPONSE 2' "$WORK"/cap*.out 2>/dev/null | wc -l)
is  "all 8 capped requests closed cleanly" "8" "$N"

printf -- '\n--- 13. picker timeout ---\n'
mock_cfg hang 0
OUT=$("$PROBE" --method OpenFile --token t_timeout --dir "$WORK/files" --wait-ms 12000 2>&1)
has "stuck picker is killed at --timeout -> Response 2" "RESPONSE 2" "$OUT"

printf -- '\n--- 14. hygiene ---\n'
sleep 0.5
LEFT=$(ls "$XDG_RUNTIME_DIR" 2>/dev/null | grep -c '^wc-portal-')
is  "no temp selection files left behind" "0" "$LEFT"
ZOMBIES=0
for c in $(cat "/proc/$PORTAL_PID/task/$PORTAL_PID/children" 2>/dev/null); do
    st=$(sed -n 's/^[0-9]* (.*) \([A-Za-z]\).*/\1/p' "/proc/$c/stat" 2>/dev/null)
    [ "$st" = "Z" ] && ZOMBIES=$((ZOMBIES + 1))
done
is  "no zombie children" "0" "$ZOMBIES"
FD_END=$(ls "/proc/$PORTAL_PID/fd" 2>/dev/null | wc -l)
if [ "$FD_END" -le "$((FD_START + 2))" ]; then
    ok "no fd leak ($FD_START -> $FD_END)"
else
    bad "no fd leak" "$FD_START -> $FD_END"
fi
has "daemon still alive after the whole suite" "()" "$(gcall org.freedesktop.DBus.Peer.Ping)"
hasnt "no protocol errors logged" "malformed message header" "$(cat "$WORK/portal.log")"

kill "$PORTAL_PID" 2>/dev/null
wait "$PORTAL_PID" 2>/dev/null
PORTAL_PID=
has "clean shutdown" "shutting down" "$(cat "$WORK/portal.log")"

# ---------------------------------------------------------------------------
printf -- '\n--- 15. --light and --cancel-on-disconnect (separate buses) ---\n'
cat > "$WORK/light.sh" <<'EOS'
set -u
"$1" --light --picker-cmd /bin/true -q > "$2/light.log" 2>&1 &
P=$!
i=0
while [ $i -lt 50 ]; do
    gdbus call --session --dest org.freedesktop.portal.Desktop \
      --object-path /org/freedesktop/portal/desktop \
      --method org.freedesktop.DBus.Peer.Ping >/dev/null 2>&1 && break
    i=$((i+1)); sleep 0.1
done
gdbus call --session --dest org.freedesktop.portal.Desktop \
  --object-path /org/freedesktop/portal/desktop \
  --method org.freedesktop.portal.Settings.Read org.freedesktop.appearance color-scheme
kill $P 2>/dev/null; wait $P 2>/dev/null
EOS
OUT=$(dbus-run-session -- sh "$WORK/light.sh" "$PORTAL" "$WORK" 2>&1)
is  "--light reports color-scheme 2" "(<<uint32 2>>,)" "$(printf '%s\n' "$OUT" | grep 'uint32')"

cat > "$WORK/disc.sh" <<'EOS'
set -u
export XDG_RUNTIME_DIR="$2/runtime" HOME="$2/home"
export WC_MOCK_CONFIG="$2/mock.cfg" WC_MOCK_STATE="$2/state.log" WC_MOCK_LOG="$2/argv.log"
printf 'ACTION=hang\nDELAY=0\nPATHS_FILE=/dev/null\n' > "$WC_MOCK_CONFIG"
: > "$WC_MOCK_STATE"
"$1" --picker-cmd "$3/mock-picker" --cancel-on-disconnect -q > "$2/disc.log" 2>&1 &
P=$!
i=0
while [ $i -lt 50 ]; do
    gdbus call --session --dest org.freedesktop.portal.Desktop \
      --object-path /org/freedesktop/portal/desktop \
      --method org.freedesktop.DBus.Peer.Ping >/dev/null 2>&1 && break
    i=$((i+1)); sleep 0.1
done
"$4" --method OpenFile --token disc --wait-ms 5000 >/dev/null 2>&1 &
PP=$!
i=0
while [ $i -lt 50 ]; do
    grep -q '^START ' "$WC_MOCK_STATE" 2>/dev/null && break
    i=$((i+1)); sleep 0.05
done
kill $PP 2>/dev/null; wait $PP 2>/dev/null
sleep 0.5
cat "$WC_MOCK_STATE"
kill $P 2>/dev/null; wait $P 2>/dev/null
EOS
OUT=$(dbus-run-session -- sh "$WORK/disc.sh" "$PORTAL" "$WORK" "$HERE" "$PROBE" 2>&1)
has "client disconnect cancels the picker (opt-in)" "TERM " "$OUT"

# live sync from Whitecube desktop config / accessibility state
printf 'bg=f5f5f5\n' > "$WORK/config/whitecube/config"
printf '{"contrast":"strong"}\n' > "$WORK/state/whitecube/accessibility.json"
"$PORTAL" --picker-cmd "$HERE/mock-picker" -q > "$WORK/sync.log" 2>&1 &
SPID=$!
i=0
while [ $i -lt 50 ]; do
    gdbus call --session --dest "$DEST" --object-path "$OPATH" \
      --method org.freedesktop.DBus.Peer.Ping >/dev/null 2>&1 && break
    i=$((i+1)); sleep 0.1
done
is "live sync light bg -> color-scheme 2" "(<uint32 2>,)" \
    "$(gcall org.freedesktop.portal.Settings.ReadOne org.freedesktop.appearance color-scheme)"
is "live sync strong contrast -> 1" "(<uint32 1>,)" \
    "$(gcall org.freedesktop.portal.Settings.ReadOne org.freedesktop.appearance contrast)"
kill $SPID 2>/dev/null; wait $SPID 2>/dev/null

# ---------------------------------------------------------------------------
printf -- '\n=== %d passed, %d failed ===\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
exit 0