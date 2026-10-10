#!/bin/bash
# Lock sudo to the single user "woez" and turn system logging off.
# Dry-run by default; pass --apply to change anything. Run as root (sudo).
# Backups of everything touched go to /root/hardening-backup-<stamp>/.
#
# Not touched: woez's NOPASSWD: ALL line (see the note printed at the end).
set -euo pipefail

USER_ONLY=woez
APPLY=0; DO_SUDO=1; DO_LOG=1
for a in "$@"; do case $a in
    --apply) APPLY=1 ;;
    --sudo-only) DO_LOG=0 ;;
    --logging-only) DO_SUDO=0 ;;
    *) echo "usage: $0 [--apply] [--sudo-only|--logging-only]"; exit 2 ;; esac; done
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }

STAMP=$(date +%Y%m%d-%H%M%S)
BK=/root/hardening-backup-$STAMP
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

say() { printf '%s %s\n' "$([ $APPLY = 1 ] && echo '[apply]' || echo '[dry-run]')" "$*"; }
run() { say "$*"; [ $APPLY = 1 ] && "$@" || true; }

[ $APPLY = 1 ] && install -d -m 0700 "$BK"
backup() { [ $APPLY = 1 ] && [ -e "$1" ] && cp -a --parents "$1" "$BK"; return 0; }

# ---------------------------------------------------------------- sudo
if [ $DO_SUDO = 1 ]; then
# 1. whitecube-settings: %sudo -> woez, so nobody else gets the helper even if added to the group.
cp /etc/sudoers.d/whitecube-settings "$WORK/wc"
sed -i "s/^%sudo /$USER_ONLY /" "$WORK/wc"
visudo -cf "$WORK/wc" >/dev/null

# 2. hardening drop-in (loaded via @include, because /etc/sudoers has no @includedir)
cat > "$WORK/hard" <<EOF
# Managed by whitecube/tools/harden-sudo-logging.sh
# No sudo logging of any kind.
Defaults !syslog
Defaults !logfile
Defaults !log_input
Defaults !log_output
Defaults !log_allowed
Defaults !log_denied
Defaults !mail_always
Defaults !mail_badpass
Defaults !mail_no_user
Defaults !mail_no_host
Defaults !mail_no_perms
# Hardening
Defaults use_pty
Defaults env_reset
Defaults !visiblepw
Defaults secure_path="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
Defaults timestamp_timeout=5
Defaults timestamp_type=tty
Defaults passwd_tries=3
Defaults umask=0022
Defaults umask_override
EOF
visudo -cf "$WORK/hard" >/dev/null

# 3. /etc/sudoers: add the include once, validate the whole result before installing.
cp /etc/sudoers "$WORK/sudoers"
grep -q '90-hardening' "$WORK/sudoers" || echo '@include /etc/sudoers.d/90-hardening' >> "$WORK/sudoers"
# Defaults/includes are only read if the include target exists, so validate with it in place.
if [ $APPLY = 1 ]; then
    backup /etc/sudoers; backup /etc/sudoers.d/whitecube-settings
    install -m 0440 -o root -g root "$WORK/hard" /etc/sudoers.d/90-hardening
    install -m 0440 -o root -g root "$WORK/wc" /etc/sudoers.d/whitecube-settings
    if visudo -cf "$WORK/sudoers" >/dev/null; then
        install -m 0440 -o root -g root "$WORK/sudoers" /etc/sudoers
    else
        echo "sudoers validation failed; rolling back"
        cp -a "$BK/etc/sudoers.d/whitecube-settings" /etc/sudoers.d/whitecube-settings
        rm -f /etc/sudoers.d/90-hardening
        exit 1
    fi
    visudo -c >/dev/null && echo "[apply] sudoers valid"
else
    say "install 90-hardening, rewrite whitecube-settings (%sudo -> $USER_ONLY), add @include to /etc/sudoers"
fi

# sudo group: only woez
for u in $(getent group sudo | cut -d: -f4 | tr , ' '); do
    [ "$u" = "$USER_ONLY" ] || run gpasswd -d "$u" sudo
done

fi

# ---------------------------------------------------------------- logging
if [ $DO_LOG = 1 ]; then
# journald: nothing stored, nothing forwarded
CONF=/etc/systemd/journald.conf.d/10-no-logging.conf
say "write $CONF"
if [ $APPLY = 1 ]; then
    install -d /etc/systemd/journald.conf.d
    cat > "$CONF" <<'EOF'
[Journal]
Storage=none
ForwardToSyslog=no
ForwardToKMsg=no
ForwardToConsole=no
ForwardToWall=no
MaxLevelStore=emerg
EOF
    (systemctl restart systemd-journald 2>&1 || pkill -TERM systemd-journald || true) | head -2
    sleep 1
fi
say "remove /var/log/journal and /run/log/journal contents"
if [ $APPLY = 1 ]; then
    find /var/log/journal /run/log/journal -mindepth 1 -delete 2>/dev/null || true
fi

# firewall logging
command -v ufw >/dev/null && run ufw logging off

# dpkg stops writing its log
if [ $APPLY = 1 ]; then
    echo 'no-log' > /etc/dpkg/dpkg.cfg.d/90-no-log
fi
say "dpkg no-log"

# truncate existing logs (keep files so permissions and logrotate stay sane)
for f in /var/log/*.log /var/log/*.log.[0-9]* /var/log/auth.log /var/log/syslog /var/log/kern.log \
         /var/log/user.log /var/log/cron.log /var/log/apt/*.log /var/log/apt/history.log \
         /var/log/unattended-upgrades/*.log; do
    [ -f "$f" ] && [ -s "$f" ] && run truncate -s 0 "$f"
done
for f in /var/log/*.gz /var/log/*/*.gz /var/log/*.old; do
    [ -f "$f" ] && run rm -f "$f"
done

# login records (absent here; ensure they stay absent)
for f in /var/log/wtmp /var/log/btmp /var/log/lastlog /var/log/faillog; do
    [ -e "$f" ] && run truncate -s 0 "$f"
done

# woez's shell history
RC=/home/$USER_ONLY/.bashrc
if ! grep -q 'whitecube: no shell history' "$RC"; then
    say "append no-history block to $RC"
    if [ $APPLY = 1 ]; then
        backup "$RC"
        cat >> "$RC" <<'EOF'

# whitecube: no shell history
unset HISTFILE
HISTSIZE=0
HISTFILESIZE=0
EOF
        chown "$USER_ONLY": "$RC"
    fi
fi
run truncate -s 0 /home/$USER_ONLY/.bash_history
fi

echo
if [ $APPLY = 1 ]; then
    echo "Done. Backups: $BK"
    echo "Verify: sudo -l -U $USER_ONLY ; getent group sudo ; journalctl --disk-usage"
else
    echo "Nothing changed. Re-run with --apply."
fi
echo
echo "NOT changed: '$USER_ONLY ALL=(ALL:ALL) NOPASSWD: ALL' in /etc/sudoers. Any process running as $USER_ONLY"
echo "(browser exploit, a script, an AI agent) gets root with no prompt. Removing NOPASSWD makes sudo ask for"
echo "your password; edit with 'sudo visudo' (Whitecube's top bar uses 'sudo -n' only for the CUBE_FREE commands)."
