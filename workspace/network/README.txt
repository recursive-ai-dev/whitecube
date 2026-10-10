Whitecube dongle controller

This is a custom userspace connection controller, using the existing Linux
network stack, rtw88_8822cu, wpa_supplicant, dhcpcd and systemd-resolved. It does
not replace TCP/IP, install packages, restore deleted modules, modprobe modules,
change blacklists, unmask services, or change another controller's configuration.

The audited dongle is USB 2001:3329, MAC 88:76:b9:f2:9d:29. Selection requires
USB ID, MAC and driver together and therefore survives interface renaming while
refusing a different dongle. This build refuses device.json values that change
the exact audited USB/MAC/driver scope. Existing module autoloading is retained.

The target already has every dependency of rtw88_8822cu, its firmware, and all
userspace programs needed. audit.json records read-only findings from the
mounted NVMe and the live device. The live kernel is different from the NVMe's;
booted-target connection testing remains required.

Install mapping (root-owned files):
  cube-net                    /usr/bin/cube-net                     0755
  cube-net-dns                /usr/libexec/whitecube/cube-net-dns             0755
  device.json                 /etc/whitecube/network/device.json          0644
  dhcpcd.conf                 /etc/whitecube/network/dhcpcd.conf           0644
  whitecube-network.service   /etc/systemd/system/whitecube-network.service 0644

The parent directory /etc/whitecube/network should be root-owned and mode 0755,
so desktop status can read the non-secret device.json. wpa.conf and its backups
are individually mode 0600 and never read by desktop status.
The service's only mutable persistent files are DHCP state under /var/lib.
The service publishes root-owned sanitized status.json and legacy profile-name
metadata in /run/whitecube-net (directory 0755, metadata 0644). Its WPA control
subdirectory is 0700. Public runtime files never contain passphrases or PSKs.
No activation is performed by these files or read-only commands.

Read-only commands:
  cube-net status
  cube-net profiles
  cube-net check --root /mnt/nvme_root

After installation, configure while booted into the NVMe system:
  sudo cube-net import-nm
or, for a new WPA2 personal connection:
  sudo cube-net setup

import-nm locally converts supported saved NetworkManager WPA2 personal profiles
without printing or transmitting passwords. It preserves the original files;
unsupported, enterprise, WPA3-only and escaped/binary profiles are skipped.
setup and add append a profile after confirmation of the passphrase; existing
profiles are preserved. import-nm also preserves existing controller profiles.
Both commands back up an existing wpa.conf, write mode 0600, encode SSIDs as hex,
and derive WPA2 PSKs without passing passwords on a command line. Backup files
remain secrets; keep them root-only. These commands only store configuration.

The desktop Wi-Fi controls open real terminal commands:
  sudo cube-net manage
  sudo cube-net scan
  sudo cube-net add
  sudo cube-net connect PROFILE_ID
  sudo cube-net remove PROFILE_ID

manage lists saved networks, scans, connects, adds and removes profiles in an
interactive menu. IDs are stable 16-character lowercase hexadecimal values
shown by profiles. Credentials are requested only with hidden terminal input;
they are never command arguments or HTTP API parameters. All mutations lock
the profile store. Profile files and backups stay 0600; profiles.json exposes
only SSIDs and IDs with mode 0644. Unsupported hand-edited WPA directives are
refused by the editor rather than discarded. Remove needs the exact ID and
reloads the running scoped controller when available. Connecting reloads saved
configuration, maps the SSID to a numeric WPA network index and selects it.
Hostile SSID text is always encoded or treated as data, never shell commands.

Scan and Connect require the existing Whitecube controller to be running and
the exact configured USB/MAC/driver to be present. They never start or replace
another controller. Scan returns JSON from the controller's latest scan; very
slow scans may still be completing. Choose Scan again to refresh if necessary.

To enable after configuration on the booted target:
  sudo systemctl daemon-reload
  sudo systemctl enable --now whitecube-network.service

The runner waits for the exact dongle and refuses to compete with NetworkManager,
iwd, any existing dhcpcd process, or a WPA controller attached to this interface.
It never stops or disables other controllers. The existing D-Bus-only WPA
service can remain enabled unless it already controls this dongle. If refused,
inspect that conflict and deliberately choose which controller should own it.

The WPA and DHCP children are supervised in the service's own cgroup, and only
those child handles are terminated. Removal of the dongle tears down its child
processes and retries; no global kill or interface shutdown command is used.
DHCP manages IPv4 on this interface only. IPv6 is left to the existing kernel
configuration; this controller does not provide DHCPv6. The DNS hook sets only
this interface's DNS with the existing systemd-resolved and reverts it on exit.
The DHCP server's search domains and hostname suggestions are not applied.

To stop and restore the previous management arrangement:
  sudo systemctl disable --now whitecube-network.service
This stops only the new controller. No existing mask or package state is changed.

Status shows real sysfs counters, carrier state, saved profiles, association
state and signal in dBm when /proc/net/wireless or WPA signal polling supplies
it. Unknown values remain null/unknown. Reconnects count observed successful
re-associations; service restarts are tracked separately. Counters live only
in /run and reset on reboot. Association failures and controller availability
are reported without credential data. Updates older than 15 seconds are marked
stale. Carrier and association do not establish working DNS or Internet access.
The browser can request a fixed CLI action in its authenticated terminal, but
the HTTP API accepts no Wi-Fi password and performs no service activation.

Validation performed here: Python compilation, identity/conflict unit tests,
read-only target dependency audit, and unit structure/scope checks. Full
systemd-analyze verification was blocked by sandbox socket restrictions. This
sandbox cannot perform live netlink operations or write to /mnt/nvme_root.

Network diagnostic upgrade:

`cube-net status` distinguishes missing USB dongle, present USB without a
matching driver interface, identity mismatch, stale/unavailable controller,
failed Wi-Fi association, missing IPv4 DHCP lease/default route, DNS failure and
lost connectivity. A USB inventory independent of /sys/class/net detects a
dongle whose driver did not create an interface. Each result includes fixed,
useful recovery actions. Status remains read-only and never opens wpa.conf.

After association the service samples IPv4 address/default route with the
already installed ip utility, queries deb.debian.org using resolvectl forced
to the exact interface, and performs a two-second TCP connection to
1.1.1.1:443 with SO_BINDTODEVICE set to that interface. These probes transmit no
credentials. Individual commands have three-second timeouts and the TCP probe
has a two-second timeout; probes run no more often than every 15 seconds.
DNS/IP/route checks remain unknown when unavailable. Filtering those particular
endpoints may fail a probe even when a local network still works; the status
note names both endpoints. No success is inferred from carrier alone.

An unhealthy connection gets a 30-second grace period, then at most three
reassociation requests separated by 5, 15 and 30 seconds. If still unhealthy
30 seconds after the final request, the owned WPA controller disconnects and
automatic association pauses. The service continues publishing status. After
reviewing the reported cause, deliberately reset that budget:
  sudo cube-net reconnect

Selecting a saved profile also resets the budget. Commands affect only the
running Whitecube controller for the exact dongle. Successful checked connectivity
resets the budget for a future outage. The Whitecube unit additionally limits
process-failure restarts to five per 300 seconds; inspect its journal and recover
deliberately if systemd applies its start limit. No other manager is stopped.

Eight new offline tests verify distinct diagnostic stages, driverless USB
detection, strict device scope, separate bounded argv/interface-bound probes,
DHCP/DNS/TCP distinctions, three-attempt exhaustion and manual budget reset.
Existing profile, credential-scope and hardware-identity regressions also pass.
Successful Wi-Fi association, IPv4 DHCP, DNS/TCP probes and real reconnect behavior
remain untested on the booted target. The NVMe remains read-only here.
