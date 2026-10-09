Whitecube dongle controller (cube-net), revision 3 - 2026-10-09

REVISION 3: NO wpa_supplicant
cube-net now does itself everything it used to borrow from wpa_supplicant and wpa_cli. The
package can be uninstalled once you have seen it work (steps at the end of this section).
It is still ONE stdlib-only Python file, and dhcpcd is still used for DHCP.

  what wpa_supplicant did            where it lives now (all in net/cube-net)
  scan for access points             Nl80211Driver.trigger_scan/get_scan over generic netlink
  choose a BSS                       Station._choose: saved name, strongest signal, WPA2-PSK/CCMP only
  authenticate + associate           nl80211 CONNECT (kernel SME), port held closed (CONTROL_PORT)
  WPA2 four-way handshake            Handshake: PBKDF2 PSK, PRF-SHA1 PTK, HMAC-SHA1 MIC, AES key unwrap
  group-key updates                  Handshake._group
  install keys, open the port        NEW_KEY (pairwise, group), SET_STATION authorized
  retry, never give up               Station: scan again after 1, 3, 5, then every 10 s
  status / signal_poll               Station.snapshot, GET_STATION signal (falls back to /proc/net/wireless)
  wpa_cli scan/select/reassociate    root-only unix socket /run/whitecube-net/control.sock (ControlServer)
  wpa.conf                           /etc/whitecube/network/networks.json (0600, SSID + 256-bit PSK)

Everything is deliberately narrower than wpa_supplicant: WPA2-personal (RSN) with CCMP only, one
radio, IPv4. It will NOT join: open, WEP, WPA1/TKIP, WPA3-only (SAE), enterprise, or networks that
REQUIRE management frame protection (WPA3 transition mode, which does not require it, is fine). Such
networks show in scans with their security string and are skipped with the reason in the journal.
Your router (EMPIRE) is WPA2-PSK/CCMP, no PMF: `cube-net selftest` says "usable".

Security properties kept: the passphrase is hashed (never stored); message 3 is only accepted with a
valid MIC, a higher replay counter and the same ANonce; its security elements must match the beacon
(downgrade check); a repeated message 3 is answered but never reinstalls keys (no key reinstallation);
group updates need a valid MIC and replay counter; an AP-initiated pairwise rekey makes us reconnect.
Not implemented: PMF/802.11w, SAE, TKIP, 802.11r roaming, PMKSA caching, background roaming scans.

Saved networks: networks.json replaces wpa.conf. Until networks.json exists the old wpa.conf is still
read (read-only), so the first start after installing needs no migration. `sudo cube-net migrate`
copies the profiles across once; the old file is left in place for rollback. Every later add/remove
writes networks.json (with a 0600 backup of the previous one).

Journal lines are now "wifi_state SCANNING -> COMPLETED" (was wpa_state) plus "wifi: ..." lines for
connect attempts, refusals, link loss and handshake problems. No SSIDs, passphrases or keys.

TRY IT WITHOUT RISK, BEFORE INSTALLING
  python3 -I net/cube-net selftest        no root, changes nothing: crypto test vectors, real
                                          netlink to the kernel, decodes the live scan cache and says
                                          whether your access point is usable
  sudo python3 -I net/cube-net selftest   additionally checks that each saved network is joinable

INSTALL, VERIFY, THEN REMOVE wpa_supplicant (restarting drops Wi-Fi for ~20 s)
  1. sudo bash net/apply.sh --apply            (backs up the five files; prints the restart commands)
  2. sudo systemctl daemon-reload && sudo systemctl restart whitecube-network.service
  3. journalctl -u whitecube-network.service -f
       expect: wifi_state - -> DISCONNECTED/SCANNING/AUTHENTICATING/ASSOCIATED/4WAY_HANDSHAKE/COMPLETED,
       "wifi: handshake complete, link authorized", "lease 10.0.0.x via 10.0.0.1", then "checks: ..."
  4. cube-net status | grep -E 'association_state|diagnostic_code'      -> COMPLETED / online
  5. sudo cube-net migrate                      (optional now; networks.json then wins over wpa.conf)
  6. only after steps 3-4 look right and you have used the link for a while:
       sudo apt purge wpasupplicant && sudo apt autoremove      (removes nothing else but libnl-route-3-200)
IF IT DOES NOT CONNECT: wpa_supplicant is still installed until step 6, so the way back is
  sudo bash net/apply.sh --rollback /var/backups/whitecube/net-STAMP   (STAMP is printed by step 1)
  sudo systemctl daemon-reload && sudo systemctl restart whitecube-network.service
and "journalctl -u whitecube-network.service -b" names the failing step (scan, association, handshake).
A wrong-passphrase signature is "link lost, reason 15 ... during the handshake; check the saved passphrase".

WHAT WAS AND WAS NOT TESTED FOR REVISION 3
  Tested offline (net/test_cube_wifi.py, 128 more checks): AES against FIPS-197 and openssl; RFC 3394
  unwrap against the RFC vector and openssl wrap; PSK against the IEEE 802.11i vector and wpa_passphrase;
  the whole four-way and group handshakes against an access point written separately from the
  spec (own frame layout, MIC, PTK, openssl key wrap), including wrong passphrase, forged/replayed/
  downgraded frames and retransmissions; the netlink layer against a fake kernel; the exact attribute
  layout of CONNECT/NEW_KEY/SET_STATION; the station against a simulated radio (retries, bans, roaming
  between BSSs, link loss, key refusal); the control socket as a real unix socket; the whole service
  with real threads. 33 deliberate breakages of the code (wrong key slice, no MIC check, missing
  CONTROL_PORT flag, ...) were each caught by a test.
  Tested read-only against the live kernel and dongle (no root, link untouched): family and multicast
  group resolution, scan-cache dump and decoding, the access point's RSN element, GET_STATION.
  NOT tested: an actual association, handshake and key installation with the real kernel and router.
  That needs root and drops the Wi-Fi this session runs on, so it is the first thing the install does.
  The commands in the kernel interface (CONNECT, NEW_KEY, SET_KEY, SET_STATION) follow the nl80211
  documentation and wpa_supplicant's own call sequence, and are covered by byte-level tests, but only
  the real radio can prove them.

--- revision 2 text follows (wpa_supplicant references below describe the previous design) ---

WHY REVISION 2
The first real boot (installed system, 2026-10-07 20:51 and 20:54) showed the old
watchdog destroying a working link. It treated "DHCP, DNS and Internet probes not
all green 30 s after the SERVICE STARTED" as a broken radio, reassociated at +30 s,
+45 s and +75 s, then disconnected wpa_supplicant and paused for good. On your router
DHCP alone takes 13-19 s and probes ran on a fixed 15 s tick, so the deadline passed
before a probe could pass. Replaying the journal through the old decision functions
(net/legacy/cube-net.v1) reproduces the bounce at +30 s and +45 s even with every
probe healthy. Two more time bombs sat next to it: dhcpcd's default 30 s timeout makes
a single-interface dhcpcd EXIT (the whole service then restarts), and systemd stopped
restarting the unit after 5 failures in 300 s.

RECOVERY POLICY (class Supervisor)
Observation and action are separate. Probes only observe. The only remedy is asking
wpa_supplicant to reassociate; nothing ever disconnects, and nothing ever gives up.

  layer / fault                    first action after      then spaced by
  not associated                   45 s                    45, 90, 180, then every 300 s
  associated, no IPv4 lease/route  240 s without a lease   240, 480, then every 900 s
  router fails neighbour discovery 120 s while associated  120, 300, then every 900 s
  DNS or Internet probe failing    never (reported only)   -
  dhcpcd / wpa_supplicant exits    service exits; systemd restarts it with growing delay

Association must hold 60 s before the not-associated ladder's counter resets. All timers use
time.monotonic(), so chrony stepping the clock cannot trigger anything. "cube-net
reconnect" / "connect" reassociates now and restarts the ladders.

PROBES (worker thread; the control loop never blocks, status.json never goes stale)
  router    kernel neighbour entry for the gateway after a 0-byte UDP poke to its discard
            port: REACHABLE = true, FAILED/INCOMPLETE = false, still revalidating = unknown
  DNS       a UDP A query for deb.debian.org to each resolver in /etc/resolv.conf, as
            applications see it (2 s each, first answer wins)
  Internet  TCP 443 to 1.1.1.1, then 9.9.9.9, bound to the dongle (3 s each)
A check turns False only after two failed suites in a row (10 s apart); a pass shows
at once. A new or lost lease discards in-flight results.

STATUS (/run/whitecube-net/status.json; flat, <16 KiB, read by cube-status with strstr)
Kept: association_state, ssid, signal_dbm, ipv4_address, default_route, dns_ok,
connectivity_ok, checks_epoch, last_error, reconnect_count, service_restart_count,
updated_epoch, updated_utc. Added: gateway_ok, dns_ms, tcp_ms, assoc_age_s,
lease_age_s, reassociate_in_s (seconds to the next scheduled reassociation, or null),
actions_total. REMOVED: recovery_attempts and recovery_exhausted - the browser drawer
prints "Bounded reconnect budget: 3 ... paused" whenever recovery_attempts exists,
which is no longer true. New diagnostic codes: dhcp-pending, gateway-unreachable.
"recovery-paused" no longer exists.

JOURNAL (journalctl -u whitecube-network.service) - one line per decision, no SSIDs
  wpa_state - -> SCANNING            lease 10.0.0.133 via 10.0.0.1      lease lost
  checks: router=ok dns=ok(21ms) internet=ok(99ms) - reported only; Wi-Fi is not touched for these
  recovery: requested reassociation #1 - not associated for 45s
  recovery: retry delays reset by cube-net

DHCP HOOK (cube-net-dns)
Writes /run/whitecube-net/resolv.conf (/etc/resolv.conf links to it). Accepts only
unicast IPv4 resolvers (not loopback, multicast, link-local, reserved, 0.0.0.0), at
most three, with "options timeout:2 attempts:2" so a dead first resolver costs 2 s
instead of 5. A BOUND/REBOOT lease with nothing usable removes the file; a RENEW or
REBIND that omits DNS leaves it alone. Search domains and hostnames are never applied.

OTHER FILES
  dhcpcd.conf                 adds "timeout 0" (never exit on a slow lease); comment fixed
  whitecube-network.service   Restart=always, StartLimitIntervalSec=0 (restart delay
                              grows from 5 s to 60 s); adds ProtectKernelModules/Logs, ProtectControlGroups,
                              ProtectClock, ProtectHostname, LockPersonality, RestrictRealtime,
                              RestrictSUIDSGID, RestrictNamespaces, SystemCallArchitectures.
                              systemd-analyze exposure 8.5 -> 6.1. NOT added, because it cannot be
                              tested without the radio: CapabilityBoundingSet, PrivateDevices,
                              ProtectKernelTunables, MemoryDenyWriteExecute.
  ../cube-settings-helper     (1) the Network page now receives diagnostic_summary/code (it was
                              reading keys that were never published, so the "why" line never
                              appeared) and lower-cased association_state (the panel only colours
                              "completed" green); (2) "Turn firewall on" is refused while
                              /etc/default/ufw has DEFAULT_OUTPUT_POLICY DROP/REJECT, because ufw
                              would then set OUTPUT DROP and cut off DNS, web, NTP and DHCP
                              renewals. Say "sudo ufw default allow outgoing" first.
  legacy/                     the replaced files, byte for byte, for the replay test and rollback

INSTALL (nothing is installed by building or testing)
  bash net/apply.sh --root /mnt/nvme_root              dry run: what would change
  sudo bash net/apply.sh --root /mnt/nvme_root --apply backup, verify, install 5 files
  sudo bash net/apply.sh --root / --apply              on the booted system
  sudo bash net/apply.sh --root R --rollback /var/backups/whitecube/net-STAMP [--force]
The root must be mounted read-write. Backups: <root>/var/backups/whitecube/net-STAMP
(0700, sha256 manifests). Rollback refuses to overwrite a file edited after install unless
--force. apply.sh never restarts anything; it prints the commands.

FIRST-BOOT CHECK (this part has only been exercised with fakes - please look)
  sudo systemctl daemon-reload && sudo systemctl restart whitecube-network.service
  journalctl -u whitecube-network.service -f
      expect: wpa_state ... COMPLETED, "lease 10.0.0.x via 10.0.0.1", then within ~15 s
      "checks: router=ok dns=ok(..ms) internet=ok(..ms)" - and NO "recovery:" line
  cube-net status | grep -E 'diagnostic_code|gateway_ok|dns_ms|reassociate_in_s'   -> online
  systemd-analyze security whitecube-network.service | tail -1                        -> about 6.1
  cube-check            "dongle-connection" should read COMPLETED
If the unit fails to start, "journalctl -xeu whitecube-network.service" names the directive.
The quickest way back is the rollback command above (or boot the live USB and run it).

TESTS
  python3 -I net/test_cube_net.py        (BUILD.sh runs it; no root, radio or network needed)
100 tests: the incident replayed through the old and the new decision code; the Supervisor's
ladders; the real Controller loop and run_service() driven by fakes; probes against loopback
servers; the status file against a port of cube-status.c's reader; the hook; unchanged profile
code; unit/dhcpcd.conf contents; the helper. Eight deliberate re-introductions of old bugs
(stale probe results, DNS-driven reassociation, no debounce, lease timer from association,
no backoff growth, short grace, ignored dead router, missing dhcpcd timeout) are each caught.
Also run against the live dongle's network (read-only): interface/route samplers, the neighbour
probe (REACHABLE in 0 s; an address nobody owns FAILED in 3.6 s), DNS to 64.71.255.204/.198
(34-84 ms) and device-bound TCP to 1.1.1.1:443 (99-316 ms). That run found and fixed a bug the
mocks hid: "ip -j neigh ... dev X" omits the "dev" key.

NOT VERIFIED (needs the booted system)
Real wpa_supplicant/dhcpcd behaviour under the new unit sandbox; an actual reassociation
(wpa_cli scans first, so the link is down ~5 s); dhcpcd running with "timeout 0"; the browser
drawer and cube-status rendering of the new status. Nothing here could be run against the
dongle without cutting the live session's own connection.

LEFT ALONE ON PURPOSE - decisions for you
  - Firewall: ufw is ENABLED=no and nftables.conf is an empty ruleset, so nothing filters
    traffic. /etc/default/ufw has all-DROP defaults and 263 mostly "deny" rules that assume
    "allow outgoing". A stateful input filter would load nf_conntrack/nft_ct (modules not loaded
    today), which your no-new-modules rule forbids, so I did not enable one.
  - sudoers: cube-settings-helper net-add/net-connect/net-forget/net-scan/net-reconnect,
    brightness, power-action, services and firewall run without a password for group sudo. Any
    process running as you can add a Wi-Fi profile or power off. Unchanged; your call.
  - chrony uses plain NTP (pool 2.debian.pool.ntp.org). NTS would authenticate time but adds
    outbound dependencies; not changed.
  - wpa.conf has no country= line, so the regulatory domain flips WORLD <-> CA on every
    reassociation (see the journal). Setting it needs a deliberate country choice.
  - PMF (ieee80211w), WPA3/SAE and MAC randomisation: not changed; they can break association
    with some access points and cannot be tested without the radio.
  - /usr/local/share/whitecube-workspace/network/README.txt (installed copy) still describes
    resolvectl/systemd-resolved and the three-attempt budget; this file supersedes it.
