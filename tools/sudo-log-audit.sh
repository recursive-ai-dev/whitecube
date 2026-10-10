#!/bin/bash
# Read-only audit of sudo access and logging. Changes nothing. Run: sudo tools/sudo-log-audit.sh
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }

echo "== effective sudoers rules (comments stripped) =="
for f in /etc/sudoers /etc/sudoers.d/*; do
    [ -f "$f" ] || continue
    echo "--- $f ($(stat -c '%a %U:%G' "$f"))"
    grep -vE '^\s*(#|$)' "$f"
done

echo; echo "== who can use sudo =="
getent group sudo admin wheel
sudo -l -U woez 2>&1 | sed 's/^/  /'

echo; echo "== other root paths =="
awk -F: '$3==0{print "uid0:",$1}' /etc/passwd
passwd -S root
grep -E '^\s*auth' /etc/pam.d/su | grep -v '^#'
ls /etc/polkit-1/rules.d /usr/share/polkit-1/rules.d 2>&1
find / -xdev -perm -4000 -type f 2>/dev/null | sort

echo; echo "== logging: daemons and config =="
pgrep -a 'rsyslogd|systemd-journal|auditd|syslog-ng' || echo "no log daemons running"
grep -vE '^\s*(#|$)' /etc/systemd/journald.conf 2>/dev/null
ls /etc/systemd/journald.conf.d 2>&1
du -sh /var/log/journal /var/log 2>/dev/null
grep -rE 'log_|syslog|logfile|mail' /etc/sudoers /etc/sudoers.d 2>/dev/null | grep -v '^\S*:\s*#'
cat /etc/sudo_logsrvd.conf 2>/dev/null | grep -vE '^\s*(#|$|\[)' | head
ls -l /var/log/{wtmp,btmp,lastlog,faillog,auth.log,syslog,kern.log,ufw.log} 2>&1
ls -ld /var/log/sudo-io /var/lib/sudo 2>&1
ufw status verbose 2>&1 | grep -i logging
auditctl -s 2>&1 | head -3
sysctl kernel.dmesg_restrict kernel.printk_ratelimit 2>&1
