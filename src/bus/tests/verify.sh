#!/usr/bin/env bash
# Independent verification: sd-bus, GDBus and dbus-send drive the broker.
set -uo pipefail
BIN=${BIN:-./whitecube-bus}
DIR=$(mktemp -d "${XDG_RUNTIME_DIR:-/tmp}/wc-bus.XXXXXX")   # mktemp -d => 0700
SOCK="$DIR/bus"
LOG="$DIR/audit.log"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$SOCK"
fail=0

"$BIN" ${EXTRA_ARGS:-} --socket "$SOCK" --audit --allow 'org.whitecube.*' --allow 'org.freedesktop.impl.portal.*' --allow 'org.gnome.Shell.*' --allow 'org.freedesktop.portal.*' >"$LOG" 2>&1 &
BPID=$!
cleanup() { kill "$BPID" 2>/dev/null; wait "$BPID" 2>/dev/null;
            echo; echo "=== last audit lines ==="; tail -n 25 "$LOG"; rm -rf "$DIR"; }
trap cleanup EXIT
for _ in $(seq 1 100); do [ -S "$SOCK" ] && break; sleep 0.05; done
[ -S "$SOCK" ] || { echo "FAIL: broker did not start"; exit 1; }

check() { if [ "$1" = 0 ]; then echo "PASS: $2"; else echo "FAIL: $2"; fail=1; fi; }

echo "--- socket hygiene ---"
[ "$(stat -c %a "$SOCK")" = 600 ]; check $? "socket mode 0600"
[ "$(stat -c %a "$DIR")" = 700 ];  check $? "parent mode 0700"

echo "--- 1. sd-bus handshake + ListNames ---"
busctl --user list >/dev/null 2>&1; check $? "busctl --user list"
busctl --user list | grep -q 'org.freedesktop.DBus'; check $? "driver name present"

echo "--- 2. GDBus handshake + NEGOTIATE_UNIX_FD ---"
out=$(gdbus call --session --dest org.freedesktop.DBus \
      --object-path /org/freedesktop/DBus \
      --method org.freedesktop.DBus.GetConnectionUnixProcessID org.freedesktop.DBus 2>&1)
echo "$out" | grep -q "$BPID"; check $? "GDBus reports broker pid ($out)"

echo "--- 3. allowlist enforcement ---"
if gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus \
     --method org.freedesktop.DBus.RequestName org.evil.SpoofedService 0 >/dev/null 2>&1; then
  check 1 "unauthorized RequestName must fail"
else
  check 0 "unauthorized RequestName rejected"
fi
grep -q "DENIED RequestName 'org.evil.SpoofedService'" "$LOG"; check $? "denial audited"

gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus \
  --method org.freedesktop.DBus.RequestName org.whitecube.Allowed 0 | grep -q 'uint32 1'
check $? "allowlisted RequestName => PRIMARY_OWNER"

echo "--- 3b. screen-capture names are hard-denied even if allowed ---"
for n in org.freedesktop.impl.portal.desktop.wlr org.freedesktop.portal.ScreenCast \
         org.freedesktop.portal.Screenshot org.gnome.Shell.Screenshot; do
  if gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus \
       --method org.freedesktop.DBus.RequestName "$n" 0 >/dev/null 2>&1; then
    check 1 "$n must be refused"
  else
    check 0 "$n refused"
  fi
done

echo "--- 3c. BecomeMonitor refused by default ---"
if busctl --user call org.freedesktop.DBus /org/freedesktop/DBus org.freedesktop.DBus.Monitoring \
     BecomeMonitor asu 0 0 >/dev/null 2>&1; then
  check 1 "BecomeMonitor must be refused"
else
  check 0 "BecomeMonitor refused"
fi
grep -q "DENIED BecomeMonitor" "$LOG"; check $? "monitor denial audited"

echo "--- 4. no service activation ---"
gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus \
  --method org.freedesktop.DBus.StartServiceByName org.nope.Service 0 >/dev/null 2>&1
[ $? -ne 0 ]; check $? "StartServiceByName refused"
busctl --user call org.freedesktop.DBus /org/freedesktop/DBus org.freedesktop.DBus \
  ListActivatableNames | grep -q '^as 0'; check $? "ListActivatableNames empty"

echo "--- 5. dbus-send + introspection + properties + activation env ---"
dbus-send --session --print-reply --dest=org.freedesktop.DBus /org/freedesktop/DBus \
  org.freedesktop.DBus.ListNames >/dev/null 2>&1; check $? "dbus-send ListNames"
dbus-send --session --print-reply --dest=org.freedesktop.DBus /org/freedesktop/DBus \
  org.freedesktop.DBus.Introspectable.Introspect >/dev/null 2>&1; check $? "Introspect"
busctl --user get-property org.freedesktop.DBus /org/freedesktop/DBus org.freedesktop.DBus Features | grep -q '^as 0'; check $? "Properties.Get Features"
busctl --user get-property org.freedesktop.DBus /org/freedesktop/DBus org.freedesktop.DBus Interfaces | grep -q 'org.freedesktop.DBus.Monitoring'; check $? "Properties.Get Interfaces"
gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus \
  --method org.freedesktop.DBus.Properties.GetAll org.freedesktop.DBus | grep -q 'Interfaces'; check $? "Properties.GetAll"
dbus-update-activation-environment WHITECUBE_TEST=1 >/dev/null 2>&1; check $? "dbus-update-activation-environment"

HERE=$(cd "$(dirname "$0")" && pwd)
echo "--- 6. independent wire test: alignment, a{sv}, SCM_RIGHTS, match filtering ---"
SOCK2="$DIR/bus-monitor"
"$BIN" --socket "$SOCK2" --allow 'org.whitecube.*' --allow-monitor >/dev/null 2>&1 &
B2=$!
sleep 0.5
python3 "$HERE/wire_test.py" "$SOCK2"; check $? "raw wire conformance"
kill "$B2" 2>/dev/null; wait "$B2" 2>/dev/null

echo
[ $fail = 0 ] && echo "ALL CHECKS PASSED" || echo "SOME CHECKS FAILED"
exit $fail
