# Whitecube Desktop 0.10.1

Whitecube 0.10.1 makes the native Wi-Fi controller usable as the system's only network
stack. Earlier packages installed `cube-net`, but nothing could switch a machine over to it.
The supported package target is Debian 13, amd64.

## Takeover

`sudo cube-net takeover` imports saved WPA2 networks from NetworkManager when none exist,
stops and masks NetworkManager, wpa_supplicant and systemd-resolved, links
`/etc/resolv.conf` to `/run/whitecube-net/resolv.conf`, enables and starts
`whitecube-network.service`, and waits until association, lease, DNS and a TCP probe all
pass. If they do not within 150 seconds, or the command is interrupted, the previous
stack is restored. Installing the package still changes no running service.

After a successful takeover, remove the old stack with
`sudo apt purge network-manager wpasupplicant systemd-resolved`. apt also removes packages
that depend on them, such as Proton VPN.

## Fixes

- The dongle is identified by its permanent MAC. A randomized current MAC, which is
  NetworkManager's privacy setting, made `cube-net` report the dongle as absent.
- The service no longer fails when the optional `/etc/whitecube/network/device.json` is absent.

## Limits

`cube-net` joins WPA2-personal networks over IPv4. It uses the dongle's permanent MAC and
the DHCP server's DNS resolvers; it has no MAC randomization or DNS-over-TLS. It remains
scoped to USB 2001:3329 with its audited MAC and driver.

Build with `bash tools/build-debian.sh`. The result is
`packaging/out/whitecube-desktop_0.10.1_amd64.deb` with its checksum and validation manifest.
