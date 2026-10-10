#!/usr/bin/env bash
# scripts/install-daemon.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "error: must be run with sudo/root" >&2
  exit 1
fi

TARGET_USER="${SUDO_USER:-}"
if [[ -z "$TARGET_USER" ]]; then
  echo "error: run this via 'sudo scripts/install-daemon.sh' from a normal user session (SUDO_USER must be set)" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR="$SCRIPT_DIR/install"
BIN_DIR="/usr/local/libexec/command-center"

echo "==> Building daemon binary"
( cd "$SCRIPT_DIR/daemon" && go build -o "$SCRIPT_DIR/daemon/command-center-helperd" ./cmd/command-center-helperd )

echo "==> Installing binary to $BIN_DIR"
install -d -m 0755 "$BIN_DIR"
install -m 0755 "$SCRIPT_DIR/daemon/command-center-helperd" "$BIN_DIR/command-center-helperd"

echo "==> Installing systemd unit"
install -m 0644 "$INSTALL_DIR/command-center-helperd.service" /etc/systemd/system/command-center-helperd.service

echo "==> Installing D-Bus activation file"
install -m 0644 "$INSTALL_DIR/ca.smddev.CommandCenter1.service" /usr/share/dbus-1/system-services/ca.smddev.CommandCenter1.service

echo "==> Installing D-Bus policy for user $TARGET_USER"
sed "s/__COMMAND_CENTER_USER__/$TARGET_USER/" "$INSTALL_DIR/ca.smddev.CommandCenter1.conf.in" > /usr/share/dbus-1/system.d/ca.smddev.CommandCenter1.conf
chmod 0644 /usr/share/dbus-1/system.d/ca.smddev.CommandCenter1.conf

echo "==> Installing polkit action"
install -m 0644 "$INSTALL_DIR/ca.smddev.commandcenter.sysctl.policy" /usr/share/polkit-1/actions/ca.smddev.commandcenter.sysctl.policy

echo "==> Reloading D-Bus and systemd"
systemctl reload dbus.service || true
systemctl daemon-reload

echo "==> Done. The daemon will be D-Bus-activated on first use by $TARGET_USER."
