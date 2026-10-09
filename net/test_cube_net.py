"""Offline tests for the Whitecube network controller.

Standard library only; needs no root, no radio and no network. Run:
    python3 -I net/test_cube_net.py          (BUILD.sh runs it)

The headline test replays the 2026-10-07 incident: the previous controller (kept in
net/legacy/cube-net.v1, driven through its real decision functions) bounced a working
link and then disconnected it; the same timeline must leave the new Supervisor idle.
"""
import ast
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True   # loading scripts by path must not litter the project with __pycache__

HERE = Path(__file__).resolve().parent
REPO = HERE.parent


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


net = load("cube_net", HERE / "cube-net")
legacy = load("cube_net_v1", HERE / "legacy" / "cube-net.v1")
dns_hook = load("cube_net_dns", HERE / "cube-net-dns")
helper = load("cube_settings_helper", REPO / "cube-settings-helper")


# ------------------------------------------------------------------ the incident

class FakeClock:
    """time.monotonic() is large on a real machine (uptime): the first probe is immediately due."""
    def __init__(self):
        self.t = 5000.0

    def monotonic(self):
        return self.t

    def time(self):
        return 1_800_000_000.0 + self.t

    def sleep(self, seconds):
        self.t += seconds


def replay_legacy(assoc_at, lease_at, dns_ok, conn_ok, horizon=240):
    """Run v1's real diagnostic()/recovery_step() through the decision part of its service loop.

    Returns [(seconds after service start, action, diagnosis)]. After a reassociation the link is
    modelled as healthy again, so only the first decisions are meaningful."""
    clock = FakeClock()
    start = clock.t
    legacy.time = types.SimpleNamespace(time=clock.time, monotonic=clock.monotonic, sleep=clock.sleep,
                                        strftime=lambda *a: "", gmtime=lambda *a: None)
    config = dict(legacy.DEFAULT_DEVICE)
    seen = [{"interface": "wlan0", "usb_id": config["usb_id"], "mac": config["mac"], "driver": config["driver"]}]
    runtime = {"association_state": "waiting-for-dongle", "recovery_attempts": 0, "recovery_exhausted": False,
               "ipv4_address": None, "default_route": None, "dns_ok": None, "connectivity_ok": None,
               "checks_epoch": None}
    failure_since = last_attempt = clock.monotonic()
    last_probe = 0
    events = []
    while clock.t - start < horizon:
        now = clock.monotonic()
        rel = now - start
        runtime["association_state"] = "COMPLETED" if rel >= assoc_at else "SCANNING"
        has_ip = runtime["association_state"] == "COMPLETED" and rel >= lease_at
        if runtime["association_state"] == "COMPLETED" and now - last_probe >= legacy.PROBE_INTERVAL:
            runtime.update(ipv4_address="10.0.0.133" if has_ip else None, default_route="10.0.0.1" if has_ip else None,
                           dns_ok=dns_ok(rel) if has_ip else None, connectivity_ok=conn_ok(rel) if has_ip else None,
                           checks_epoch=clock.time())
            last_probe = now
        diagnosis = legacy.diagnostic(config, seen, runtime, True)["diagnostic_code"]
        if diagnosis in ("online", "checks-pending"):
            failure_since = last_attempt = now
            if diagnosis == "online":
                runtime.update(recovery_attempts=0, recovery_exhausted=False)
        else:
            action = legacy.recovery_step(runtime, now - failure_since, now - last_attempt)
            if action:
                events.append((round(rel), action, diagnosis))
                last_attempt = now
                if action == "reassociate":
                    assoc_at, lease_at = rel + 5, rel + 12
                else:
                    assoc_at = 10 ** 9
        clock.sleep(1)
    return events


def run_supervisor(supervisor, seconds, associated, have_lease=lambda i: True, gateway_ok=lambda i: True, start=1000.0):
    """One tick per second; returns [(i, reason)]. The predicates take the tick index."""
    acted = []
    for i in range(seconds):
        reason = supervisor.step(start + i, associated(i), have_lease(i), gateway_ok(i))
        if reason:
            acted.append((i, reason))
    return acted


class IncidentReplay(unittest.TestCase):
    def test_legacy_logic_bounces_a_healthy_link_when_dhcp_is_merely_slow(self):
        # Journal 20:51: associated at +11 s, DHCP lease at +30 s, DNS and Internet both fine.
        events = replay_legacy(11, 30, lambda t: True, lambda t: True)
        self.assertEqual([(e[0], e[1]) for e in events[:2]], [(30, "reassociate"), (45, "reassociate")])

    def test_legacy_logic_reassociates_after_one_slow_dns_probe(self):
        # Journal 20:54: lease reused at +13 s; one probe sees DNS answer late.
        events = replay_legacy(6, 13, lambda t: t > 40, lambda t: True)
        self.assertEqual(events[0][:2], (30, "reassociate"))

    def test_legacy_logic_is_quiet_when_the_timing_happens_to_fit(self):
        self.assertEqual(replay_legacy(6, 13, lambda t: True, lambda t: True), [])

    def test_new_supervisor_ignores_slow_dhcp(self):
        sup = net.Supervisor(1000.0)
        acted = run_supervisor(sup, 3600, associated=lambda i: i >= 11, have_lease=lambda i: i >= 30)
        self.assertEqual(acted, [])

    def test_new_supervisor_never_sees_dns_or_internet_state(self):
        # step() has no DNS/Internet parameter at all: reassociating cannot repair them.
        import inspect
        self.assertEqual(list(inspect.signature(net.Supervisor.step).parameters),
                         ["self", "now", "associated", "have_lease", "gateway_ok"])

    def test_there_is_no_disconnect_anywhere(self):
        source = (HERE / "cube-net").read_text()
        self.assertNotIn("'disconnect'", source)
        self.assertNotIn('"disconnect"', source)
        self.assertNotIn("recovery_exhausted", source)


# ------------------------------------------------------------------ the supervisor

class SupervisorTests(unittest.TestCase):
    def test_unassociated_ladder_backs_off_then_repeats_the_last_delay(self):
        acted = run_supervisor(net.Supervisor(1000.0), 1300, associated=lambda i: False, have_lease=lambda i: False)
        self.assertEqual([i for i, _ in acted], [45, 90, 180, 360, 660, 960, 1260])
        self.assertTrue(all(reason.startswith("not associated for") for _, reason in acted))

    def test_never_gives_up(self):
        acted = run_supervisor(net.Supervisor(1000.0), 86400, associated=lambda i: False, have_lease=lambda i: False)
        times = [i for i, _ in acted]
        self.assertGreater(len(times), 250)
        self.assertEqual(max(b - a for a, b in zip(times, times[1:])), 300)

    def test_brief_flaps_never_act(self):
        up = lambda i: (i // 10) % 2 == 0
        self.assertEqual(run_supervisor(net.Supervisor(1000.0), 3600, associated=up, have_lease=up), [])

    def test_short_outage_then_recovery_restarts_the_grace_period(self):
        # down 44 s, up 100 s, down again: the second outage gets the full 45 s grace.
        pattern = lambda i: not (0 <= i < 44 or i >= 144)
        acted = run_supervisor(net.Supervisor(1000.0), 144 + 50, associated=pattern, have_lease=pattern)
        self.assertEqual([i for i, _ in acted], [144 + 45])

    def test_backoff_survives_flapping_but_resets_after_a_stable_association(self):
        sup = net.Supervisor(1000.0)
        # first outage: actions at 45 and 90 s into it; then 30 s up (< STABLE_AFTER), then down again
        timeline = lambda i: 140 <= i < 170
        acted = run_supervisor(sup, 330, associated=timeline, have_lease=timeline)
        times = [i for i, _ in acted]
        self.assertEqual(times[:2], [45, 90])
        # after the brief 30 s of association the old spacing still applies to the next outage
        self.assertGreaterEqual(times[2], 170 + 45)
        self.assertGreaterEqual(times[2] - 90, 90)
        # now a long stable association resets the ladder
        sup = net.Supervisor(1000.0)
        stable = lambda i: 100 <= i < 400
        acted = run_supervisor(sup, 520, associated=stable, have_lease=stable)
        self.assertEqual([i for i, _ in acted if i >= 400][:1], [445])

    def test_associated_without_a_lease_is_slow_and_spaced(self):
        acted = run_supervisor(net.Supervisor(1000.0), 3000, associated=lambda i: True, have_lease=lambda i: False)
        self.assertEqual([i for i, _ in acted], [240, 480, 960, 1860, 2760])

    def test_losing_a_lease_after_a_long_association_is_not_an_emergency(self):
        # up for an hour with a lease, then the lease disappears: wait the full 240 s, not zero.
        lease = lambda i: i < 3600
        acted = run_supervisor(net.Supervisor(1000.0), 4000, associated=lambda i: True, have_lease=lease)
        self.assertEqual([i for i, _ in acted], [3600 + 240])

    def test_a_lease_arriving_clears_the_no_lease_ladder(self):
        lease = lambda i: i >= 200
        self.assertEqual(run_supervisor(net.Supervisor(1000.0), 1500, associated=lambda i: True, have_lease=lease), [])

    def test_router_unreachable_ladder(self):
        bad = lambda i: 50 <= i < 3000
        acted = run_supervisor(net.Supervisor(1000.0), 2000, associated=lambda i: True, gateway_ok=lambda i: False if bad(i) else True)
        # first after GW_DEAD_AFTER (50+120); then spaced by GW_BACKOFF[0..]: +120, +300, +900
        self.assertEqual([i for i, _ in acted], [170, 290, 590, 1490])

    def test_router_check_recovery_resets_its_ladder(self):
        gateway = lambda i: False if 0 <= i < 130 else True
        acted = run_supervisor(net.Supervisor(1000.0), 600, associated=lambda i: True, gateway_ok=gateway)
        self.assertEqual([i for i, _ in acted], [120])
        # healthy again: a later failure starts from the short 120 s wait, not the spaced one
        gateway = lambda i: False if (0 <= i < 130 or 300 <= i < 440) else True
        acted = run_supervisor(net.Supervisor(1000.0), 600, associated=lambda i: True, gateway_ok=gateway)
        self.assertEqual([i for i, _ in acted], [120, 420])

    def test_unknown_router_state_never_acts(self):
        self.assertEqual(run_supervisor(net.Supervisor(1000.0), 3600, associated=lambda i: True, gateway_ok=lambda i: None), [])

    def test_unknown_router_state_does_not_break_a_failure_run(self):
        # False ... None ... False is one continuous failure, as Health keeps the debounced value.
        sup = net.Supervisor(1000.0)
        acted = run_supervisor(sup, 200, associated=lambda i: True, gateway_ok=lambda i: False)
        self.assertEqual([i for i, _ in acted], [120])

    def test_reset_starts_every_ladder_over(self):
        sup = net.Supervisor(1000.0)
        run_supervisor(sup, 200, associated=lambda i: False, have_lease=lambda i: False)
        self.assertGreater(sup.l2_count, 0)
        sup.reset(1200.0)
        acted = []
        for i in range(60):
            if sup.step(1200.0 + i, False, False, None):
                acted.append(i)
        self.assertEqual(acted, [45])

    def test_decisions_depend_only_on_the_time_argument(self):
        a = run_supervisor(net.Supervisor(0.0), 1000, associated=lambda i: False, have_lease=lambda i: False, start=10.0)
        b = run_supervisor(net.Supervisor(0.0), 1000, associated=lambda i: False, have_lease=lambda i: False, start=10 ** 8)
        self.assertEqual([i for i, _ in a], [i for i, _ in b])

    def test_outlook_reports_the_pending_wait(self):
        sup = net.Supervisor(1000.0)
        sup.step(1000.0, False, False, None)
        self.assertEqual(sup.outlook(1010.0, False, False), 35)
        self.assertIsNone(net.Supervisor(1000.0).outlook(1000.0, True, True))
        sup = net.Supervisor(1000.0)
        sup.step(1000.0, True, False, None)
        self.assertEqual(sup.outlook(1100.0, True, False), 140)

    def test_actions_total_counts_requests(self):
        sup = net.Supervisor(1000.0)
        run_supervisor(sup, 100, associated=lambda i: False, have_lease=lambda i: False)
        self.assertEqual(sup.actions_total, 2)


class HealthTests(unittest.TestCase):
    def suite(self, **kw):
        base = {"dns_ok": True, "connectivity_ok": True, "gateway_ok": True}
        base.update(kw)
        return base

    def test_one_failure_does_not_turn_a_check_red(self):
        health = net.Health()
        health.feed(self.suite())
        self.assertIs(health.feed(self.suite(dns_ok=False))["dns_ok"], True)
        self.assertIs(health.feed(self.suite(dns_ok=False))["dns_ok"], False)

    def test_a_pass_shows_at_once_and_resets_the_streak(self):
        health = net.Health()
        health.feed(self.suite(dns_ok=False))
        self.assertIsNone(health.value["dns_ok"])
        health.feed(self.suite())
        self.assertIs(health.value["dns_ok"], True)
        health.feed(self.suite(dns_ok=False))
        self.assertIs(health.value["dns_ok"], True)

    def test_unknown_changes_nothing(self):
        health = net.Health()
        health.feed(self.suite(gateway_ok=False))
        health.feed(self.suite(gateway_ok=None))
        self.assertEqual(health.streak["gateway_ok"], 1)
        health.feed(self.suite(gateway_ok=False))
        self.assertIs(health.value["gateway_ok"], False)

    def test_all_good_needs_dns_and_internet_and_tolerates_unknown_router(self):
        health = net.Health()
        health.feed(self.suite(gateway_ok=None))
        self.assertTrue(health.all_good())
        health.clear()
        self.assertFalse(health.all_good())


# ------------------------------------------------------------------ samplers and probes

ROUTE_TABLE = (
    "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
    "wlan0\t00000000\t0100000A\t0003\t0\t0\t1002\t00000000\t0\t0\t0\n"
    "wlan0\t0000000A\t00000000\t0001\t0\t0\t1002\t00FFFFFF\t0\t0\t0\n"
    "eth9\t00000000\t0101A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"
)


class SamplerTests(unittest.TestCase):
    def table(self, text):
        handle = tempfile.NamedTemporaryFile("w", delete=False)
        handle.write(text)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return Path(handle.name)

    def test_default_gateway_parses_the_kernel_table(self):
        self.assertEqual(net.default_gateway("wlan0", self.table(ROUTE_TABLE)), "10.0.0.1")
        self.assertEqual(net.default_gateway("eth9", self.table(ROUTE_TABLE)), "192.168.1.1")
        self.assertIsNone(net.default_gateway("wlan1", self.table(ROUTE_TABLE)))

    def test_default_gateway_ignores_down_and_malformed_routes(self):
        down = ROUTE_TABLE.replace("0100000A\t0003", "0100000A\t0002")
        self.assertIsNone(net.default_gateway("wlan0", self.table(down)))
        junk = "Iface\n" + "wlan0 zz zz zz\n" + "wlan0\t00000000\tXYZ\t0003\t0\t0\t1\t00000000\n"
        self.assertIsNone(net.default_gateway("wlan0", self.table(junk)))
        self.assertIsNone(net.default_gateway("wlan0", Path("/nonexistent/route")))

    def test_on_link_default_route_is_direct(self):
        text = "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n" \
               "wlan0\t00000000\t00000000\t0001\t0\t0\t1\t00000000\n"
        self.assertEqual(net.default_gateway("wlan0", self.table(text)), "direct")

    def test_interface_ipv4_on_loopback(self):
        self.assertEqual(net.interface_ipv4("lo"), "127.0.0.1")
        self.assertIsNone(net.interface_ipv4("definitely-not-an-interface"))

    def test_interface_ipv4_rejects_unspecified_and_link_local(self):
        def answer(address):
            return mock.patch.object(net.fcntl, "ioctl", return_value=bytes(20) + socket.inet_aton(address) + bytes(8))
        with answer("10.0.0.133"):
            self.assertEqual(net.interface_ipv4("wlan0"), "10.0.0.133")
        with answer("0.0.0.0"):
            self.assertIsNone(net.interface_ipv4("wlan0"))
        with answer("169.254.7.7"):
            self.assertIsNone(net.interface_ipv4("wlan0"))
        with mock.patch.object(net.fcntl, "ioctl", side_effect=OSError):
            self.assertIsNone(net.interface_ipv4("wlan0"))

    def test_read_resolvers(self):
        path = self.table("# c\nnameserver 64.71.255.204\nnameserver 64.71.255.198\nnameserver 2001:db8::1\n"
                          "nameserver 64.71.255.204\nnameserver nonsense\nnameserver 1.1.1.1\nnameserver 9.9.9.9\noptions x\n")
        self.assertEqual(net.read_resolvers(path), ["64.71.255.204", "64.71.255.198", "1.1.1.1"])
        self.assertEqual(net.read_resolvers(Path("/nonexistent/resolv.conf")), [])


class FakeDnsServer:
    """A throwaway UDP resolver on loopback. mode: ok | noanswer | refused | silent | stray-then-ok."""
    def __init__(self, mode):
        self.mode = mode
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(3)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def reply(self, query, rcode, answers, ident=None):
        ident = ident if ident is not None else query[:2]
        header = ident + struct.pack(">HHHHH", 0x8180 | rcode, 1, answers, 0, 0)
        body = query[12:]
        tail = b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 60, 4) + bytes([151, 101, 130, 132]) if answers else b""
        return header + body + tail

    def serve(self):
        try:
            query, peer = self.sock.recvfrom(1500)
            if self.mode == "ok":
                self.sock.sendto(self.reply(query, 0, 1), peer)
            elif self.mode == "noanswer":
                self.sock.sendto(self.reply(query, 0, 0), peer)
            elif self.mode == "refused":
                self.sock.sendto(self.reply(query, 5, 0), peer)
            elif self.mode == "stray-then-ok":
                self.sock.sendto(self.reply(query, 0, 1, ident=bytes([query[0] ^ 0xFF, query[1]])), peer)
                self.sock.sendto(self.reply(query, 0, 1), peer)
        except OSError:
            pass
        finally:
            self.sock.close()


class ProbeTests(unittest.TestCase):
    def query(self, mode, timeout=1.0):
        server = FakeDnsServer(mode)
        return net.dns_query("127.0.0.1", "deb.debian.org", timeout, port=server.port)

    def test_dns_answer(self):
        answered, milliseconds = self.query("ok")
        self.assertTrue(answered)
        self.assertGreaterEqual(milliseconds, 0)

    def test_dns_empty_or_refused_answers_are_failures(self):
        self.assertFalse(self.query("noanswer")[0])
        self.assertFalse(self.query("refused")[0])

    def test_dns_silence_times_out(self):
        with self.assertRaises(OSError):
            self.query("silent", timeout=0.4)

    def test_dns_ignores_a_stray_reply_with_the_wrong_id(self):
        self.assertTrue(self.query("stray-then-ok")[0])

    def test_dns_query_is_well_formed(self):
        captured = {}
        listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        listener.bind(("127.0.0.1", 0))
        listener.settimeout(2)

        def capture():
            data, _ = listener.recvfrom(1500)
            captured["packet"] = data
        thread = threading.Thread(target=capture)
        thread.start()
        with self.assertRaises(OSError):
            net.dns_query("127.0.0.1", "deb.debian.org", 0.3, port=listener.getsockname()[1])
        thread.join()
        listener.close()
        packet = captured["packet"]
        self.assertEqual(struct.unpack(">HHHHH", packet[2:12]), (0x0100, 1, 0, 0, 0))
        self.assertEqual(packet[12:], b"\x03deb\x06debian\x03org\x00" + struct.pack(">HH", 1, 1))

    def test_tcp_probe(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        self.addCleanup(server.close)
        live = ("127.0.0.1", server.getsockname()[1])
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        dead = ("127.0.0.1", closed.getsockname()[1])
        closed.close()
        self.assertTrue(net.tcp_probe(None, [live], 1)[0])
        self.assertTrue(net.tcp_probe(None, [dead, live], 1)[0])
        self.assertEqual(net.tcp_probe(None, [dead], 1), (False, None))

    def test_gateway_alive_reads_the_neighbour_cache(self):
        def states(*sequence):
            iterator = iter(sequence)
            return lambda iface, gateway: next(iterator, sequence[-1])
        poked = []
        poke = lambda iface, gateway: poked.append((iface, gateway))
        quiet = lambda seconds: None
        self.assertTrue(net.gateway_alive("wlan0", "10.0.0.1", poke=poke, state=states("DELAY", "PROBE", "REACHABLE"), pause=quiet))
        self.assertEqual(poked, [("wlan0", "10.0.0.1")])
        self.assertFalse(net.gateway_alive("wlan0", "10.0.0.1", poke=poke, state=states("INCOMPLETE", "FAILED"), pause=quiet))
        self.assertIsNone(net.gateway_alive("wlan0", "10.0.0.1", poke=poke, state=states("STALE"), pause=quiet))
        self.assertIsNone(net.gateway_alive("wlan0", "10.0.0.1", poke=poke, state=states(None), pause=quiet))
        self.assertFalse(net.gateway_alive("wlan0", "10.0.0.1", poke=poke, state=states("INCOMPLETE"), pause=quiet))

    def test_gateway_alive_declines_unusable_gateways_without_touching_the_network(self):
        boom = lambda *a: self.fail("must not probe")
        self.assertIsNone(net.gateway_alive("wlan0", "direct", poke=boom, state=boom))
        self.assertIsNone(net.gateway_alive("wlan0", None, poke=boom, state=boom))
        self.assertIsNone(net.gateway_alive("wlan0", "10.0.0.1; reboot", poke=boom, state=boom))

    def test_neighbour_state_parses_ip_json_and_refuses_non_addresses(self):
        # Real iproute2 output (6.15 on the target, 6.19 live): with a "dev" filter the key is omitted.
        real = '[{"dst":"10.0.0.1","lladdr":"c0:94:35:ad:ec:a8","state":["REACHABLE"]}]'
        with mock.patch.object(net.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout=real)) as run:
            self.assertEqual(net.neighbour_state("wlan0", "10.0.0.1"), "REACHABLE")
            self.assertEqual(run.call_args.args[0],
                             ["/usr/sbin/ip", "-j", "-4", "neigh", "show", "to", "10.0.0.1", "dev", "wlan0"])
        failed = '[{"dst":"10.0.0.1","state":["FAILED"]}]'
        with mock.patch.object(net.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout=failed)):
            self.assertEqual(net.neighbour_state("wlan0", "10.0.0.1"), "FAILED")
        # an unfiltered listing carries "dev": only our own interface's entry counts
        other = json.dumps([{"dst": "10.0.0.1", "dev": "eth9", "state": ["REACHABLE"]}])
        with mock.patch.object(net.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout=other)):
            self.assertIsNone(net.neighbour_state("wlan0", "10.0.0.1"))
        with mock.patch.object(net.subprocess, "run", side_effect=AssertionError("must not run")):
            self.assertIsNone(net.neighbour_state("wlan0", "10.0.0.1 && poweroff"))
        with mock.patch.object(net.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="garbage")):
            self.assertIsNone(net.neighbour_state("wlan0", "10.0.0.1"))

    def test_probe_suite_combines_the_checks(self):
        dns = FakeDnsServer("ok")
        web = socket.socket()
        web.bind(("127.0.0.1", 0))
        web.listen(1)
        self.addCleanup(web.close)
        real_dns, real_tcp = net.dns_query, net.tcp_probe
        with mock.patch.object(net, "gateway_alive", return_value=True), \
                mock.patch.object(net, "TCP_PROBE_TARGETS", (("127.0.0.1", web.getsockname()[1]),)), \
                mock.patch.object(net, "tcp_probe", lambda iface, targets, timeout: real_tcp(None, targets, timeout)), \
                mock.patch.object(net, "dns_query", lambda server, name, timeout: real_dns(server, name, timeout, port=dns.port)):
            result = net.probe_suite("wlan0", "10.0.0.1", resolvers=["127.0.0.1"])
        self.assertIs(result["gateway_ok"], True)
        self.assertIs(result["dns_ok"], True)
        self.assertIs(result["connectivity_ok"], True)
        self.assertIsInstance(result["checks_epoch"], float)

    def test_probe_suite_reports_dead_dns_and_dead_internet_independently(self):
        with mock.patch.object(net, "gateway_alive", return_value=None), \
                mock.patch.object(net, "dns_query", side_effect=TimeoutError("x")), \
                mock.patch.object(net, "tcp_probe", return_value=(False, None)):
            result = net.probe_suite("wlan0", "10.0.0.1", resolvers=["1.2.3.4", "5.6.7.8"])
        self.assertEqual((result["gateway_ok"], result["dns_ok"], result["connectivity_ok"]), (None, False, False))
        with mock.patch.object(net, "gateway_alive", return_value=True), \
                mock.patch.object(net, "dns_query", side_effect=[OSError("first dead"), (True, 21)]), \
                mock.patch.object(net, "tcp_probe", return_value=(True, 99)):
            result = net.probe_suite("wlan0", "10.0.0.1", resolvers=["1.2.3.4", "5.6.7.8"])
        self.assertEqual((result["dns_ok"], result["dns_ms"], result["connectivity_ok"], result["tcp_ms"]), (True, 21, True, 99))

    def test_probe_suite_with_no_resolvers_is_a_dns_failure(self):
        with mock.patch.object(net, "gateway_alive", return_value=True), mock.patch.object(net, "tcp_probe", return_value=(True, 1)):
            self.assertIs(net.probe_suite("wlan0", "10.0.0.1", resolvers=[])["dns_ok"], False)


# ------------------------------------------------------------------ diagnosis and status file

class DiagnosticTests(unittest.TestCase):
    config = dict(net.DEFAULT_DEVICE)
    seen = [{"interface": "wlan0", "usb_id": "2001:3329", "mac": "88:76:b9:f2:9d:29", "driver": "rtw88_8822cu"}]

    def code(self, **runtime):
        base = {"association_state": "COMPLETED", "ipv4_address": "10.0.0.133", "default_route": "10.0.0.1",
                "gateway_ok": True, "dns_ok": True, "connectivity_ok": True, "checks_epoch": net.time.time(), "assoc_age_s": 300}
        base.update(runtime)
        return net.diagnostic(self.config, self.seen, base, True)

    def test_every_stage_has_its_own_code(self):
        self.assertEqual(self.code()["diagnostic_code"], "online")
        self.assertEqual(self.code(association_state="SCANNING")["diagnostic_code"], "association-failed")
        self.assertEqual(self.code(association_state="unknown")["diagnostic_code"], "controller-unavailable")
        self.assertEqual(self.code(ipv4_address=None, default_route=None, assoc_age_s=5)["diagnostic_code"], "dhcp-pending")
        self.assertEqual(self.code(ipv4_address=None, default_route=None, assoc_age_s=200)["diagnostic_code"], "dhcp-failed")
        self.assertEqual(self.code(ipv4_address=None, default_route=None, assoc_age_s=None)["diagnostic_code"], "dhcp-failed")
        self.assertEqual(self.code(gateway_ok=False)["diagnostic_code"], "gateway-unreachable")
        self.assertEqual(self.code(checks_epoch=net.time.time() - 500)["diagnostic_code"], "checks-pending")
        self.assertEqual(self.code(dns_ok=False)["diagnostic_code"], "dns-failed")
        self.assertEqual(self.code(connectivity_ok=False)["diagnostic_code"], "connectivity-lost")
        self.assertEqual(self.code(dns_ok=None, connectivity_ok=None)["diagnostic_code"], "checks-pending")

    def test_a_status_file_from_the_old_controller_cannot_pause_anything(self):
        self.assertEqual(self.code(recovery_exhausted=True)["diagnostic_code"], "online")

    def test_dns_and_internet_failures_promise_not_to_touch_wifi(self):
        for key in ("dns_ok", "connectivity_ok"):
            text = " ".join(self.code(**{key: False})["recovery_actions"])
            self.assertTrue("left untouched" in text or "does not restart Wi-Fi" in text, text)

    def test_recovery_actions_are_fixed_strings(self):
        for runtime in ({}, {"dns_ok": False}, {"association_state": "SCANNING"}, {"gateway_ok": False}):
            self.assertTrue(all(isinstance(item, str) for item in self.code(**runtime)["recovery_actions"]))

    def test_device_level_codes_still_work(self):
        absent = net.diagnostic(self.config, [], {}, False)
        self.assertEqual(absent["diagnostic_code"], "missing-dongle")
        wrong_mac = [dict(self.seen[0], mac="aa:bb:cc:dd:ee:ff")]
        self.assertEqual(net.diagnostic(self.config, wrong_mac, {}, True)["diagnostic_code"], "identity-mismatch")
        twice = self.seen + [dict(self.seen[0], interface="wlan1")]
        self.assertEqual(net.diagnostic(self.config, twice, {}, True)["diagnostic_code"], "identity-ambiguous")


def c_json_value(data, key):
    """Faithful port of cube-status.c json_value(): strstr the quoted key, then skip to ':' and spaces."""
    pattern = f'"{key}"'
    index = data.find(pattern)
    if index < 0:
        return None
    colon = data.find(":", index + len(pattern))
    if colon < 0:
        return None
    colon += 1
    while colon < len(data) and data[colon] == " ":
        colon += 1
    return data[colon:]


class StatusFileTests(unittest.TestCase):
    C_READS = ("updated_epoch", "association_state", "ssid", "signal_dbm", "connectivity_ok")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "run/whitecube-net").mkdir(parents=True)
        self.status = self.root / "run/whitecube-net/status.json"
        patcher = mock.patch.object(net, "protected", lambda path, allow_absent=False: Path(path))
        patcher.start()
        self.addCleanup(patcher.stop)

    def realistic(self):
        runtime = net.public_runtime(self.root)
        runtime.update(association_state="COMPLETED", signal_dbm=-52, ssid="EMPIRE", ipv4_address="10.0.0.133",
                       default_route="10.0.0.1", dns_ok=True, connectivity_ok=True, gateway_ok=True, dns_ms=21, tcp_ms=99,
                       checks_epoch=net.time.time(), assoc_age_s=300, lease_age_s=280, reassociate_in_s=None,
                       actions_total=0, reconnect_count=2)
        return runtime

    def test_published_status_is_flat_small_and_readable_by_the_c_reader(self):
        runtime = self.realistic()
        with mock.patch.object(net, "RUNTIME_DIR", self.root / "run/whitecube-net"), \
                mock.patch.object(net, "atomic_write", lambda path, content, mode: Path(path).write_text(content)):
            net.publish_runtime(runtime)
        data = self.status.read_text()
        self.assertLess(len(data), 4096)
        parsed = json.loads(data)
        self.assertTrue(all(not isinstance(v, (dict, list)) for v in parsed.values()), "must stay flat")
        for key in self.C_READS:
            self.assertEqual(data.count(f'"{key}"'), 1, f"{key} must appear exactly once or the C scanner may pick the wrong one")
        self.assertTrue(c_json_value(data, "association_state").startswith('"COMPLETED"'))
        self.assertTrue(c_json_value(data, "connectivity_ok").startswith("true"))
        self.assertTrue(c_json_value(data, "signal_dbm").startswith("-52"))
        self.assertTrue(c_json_value(data, "ssid").startswith('"EMPIRE"'))
        # strtod() stops at the first non-numeric character, which is '}' for the last key.
        self.assertGreater(float(re.match(r"-?[0-9.eE+]+", c_json_value(data, "updated_epoch")).group()), 1e9)

    def test_new_key_names_do_not_contain_a_key_the_c_reader_looks_for(self):
        data = json.dumps(self.realistic())
        for key in self.C_READS:
            for other in json.loads(data):
                if other != key:
                    self.assertNotIn(f'"{key}"', f'"{other}"')

    def test_removed_budget_fields_are_not_published(self):
        runtime = net.public_runtime(self.root)
        self.assertNotIn("recovery_attempts", runtime)
        self.assertNotIn("recovery_exhausted", runtime)

    def test_public_runtime_sanitises_hostile_or_garbled_values(self):
        self.status.write_text(json.dumps({
            "association_state": "COMPLETED", "updated_epoch": net.time.time(), "reconnect_count": -5,
            "actions_total": True, "assoc_age_s": 10 ** 12, "dns_ms": "fast", "dns_ok": "yes", "gateway_ok": None,
            "signal_dbm": -999, "checks_epoch": net.time.time() + 10_000, "reassociate_in_s": 30, "ssid": "x" * 300}))
        runtime = net.public_runtime(self.root)
        self.assertEqual(runtime["reconnect_count"], 0)
        self.assertEqual(runtime["actions_total"], 0)
        self.assertIsNone(runtime["assoc_age_s"])
        self.assertIsNone(runtime["dns_ms"])
        self.assertIsNone(runtime["dns_ok"])
        self.assertIsNone(runtime["signal_dbm"])
        self.assertIsNone(runtime["checks_epoch"])
        self.assertEqual(runtime["reassociate_in_s"], 30)
        self.assertEqual(runtime["ssid"], None)
        self.assertEqual(runtime["association_state"], "COMPLETED")

    def test_old_status_files_are_marked_stale(self):
        self.status.write_text(json.dumps({"association_state": "COMPLETED", "updated_epoch": net.time.time() - 60}))
        self.assertEqual(net.public_runtime(self.root)["association_state"], "stale")

    def test_oversized_status_file_is_ignored(self):
        self.status.write_text(json.dumps({"association_state": "COMPLETED", "pad": "x" * 20000}))
        self.assertEqual(net.public_runtime(self.root)["association_state"], "unknown")


# ------------------------------------------------------------------ DHCP hook

class HookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.resolv = Path(self.tmp.name) / "resolv.conf"

    def run_hook(self, reason, servers="", interface="wlan0"):
        dns_hook.main({"interface": interface, "reason": reason, "new_domain_name_servers": servers}, self.resolv)

    def test_bound_writes_validated_resolvers_with_short_timeouts(self):
        self.run_hook("BOUND", "64.71.255.204 64.71.255.198")
        self.assertEqual(self.resolv.read_text(), "# wlan0 via dhcpcd (cube-net-dns)\noptions timeout:2 attempts:2\n"
                                                  "nameserver 64.71.255.204\nnameserver 64.71.255.198\n")
        self.assertEqual(stat.S_IMODE(self.resolv.stat().st_mode), 0o644)
        self.assertFalse(self.resolv.with_suffix(".tmp").exists())

    def test_unusable_addresses_are_dropped(self):
        self.run_hook("BOUND", "127.0.0.1 0.0.0.0 224.0.0.1 169.254.1.1 255.255.255.255 ::1 fe80::1 junk 10.0.0.1 10.0.0.1 1.1.1.1 8.8.8.8 9.9.9.9")
        servers = [l.split()[1] for l in self.resolv.read_text().splitlines() if l.startswith("nameserver")]
        self.assertEqual(servers, ["10.0.0.1", "1.1.1.1", "8.8.8.8"])

    def test_a_fresh_lease_with_no_usable_dns_removes_the_old_networks_resolvers(self):
        self.run_hook("BOUND", "10.0.0.1")
        self.run_hook("BOUND", "127.0.0.1")
        self.assertFalse(self.resolv.exists())
        self.run_hook("REBOOT", "10.0.0.1")
        self.run_hook("REBOOT", "")
        self.assertFalse(self.resolv.exists())

    def test_a_renewal_that_omits_dns_keeps_the_working_file(self):
        self.run_hook("BOUND", "10.0.0.1")
        for reason in ("RENEW", "REBIND", "INFORM"):
            self.run_hook(reason, "")
            self.assertTrue(self.resolv.exists(), reason)

    def test_link_loss_and_expiry_remove_the_file(self):
        for reason in ("EXPIRE", "FAIL", "STOP", "STOPPED", "NOCARRIER", "DEPARTED", "RELEASE"):
            self.run_hook("BOUND", "10.0.0.1")
            self.run_hook(reason)
            self.assertFalse(self.resolv.exists(), reason)

    def test_unrelated_reasons_do_nothing(self):
        self.run_hook("BOUND", "10.0.0.1")
        before = self.resolv.read_text()
        for reason in ("PREINIT", "CARRIER", "TEST", "", "weird"):
            self.run_hook(reason, "8.8.8.8")
            self.assertEqual(self.resolv.read_text(), before, reason)

    def test_invalid_interface_names_exit(self):
        for name in ("", "wlan0; reboot", "a" * 16, "../x"):
            with self.assertRaises(SystemExit):
                self.run_hook("BOUND", "10.0.0.1", interface=name)
        self.assertFalse(self.resolv.exists())

    def test_a_planted_symlink_at_the_temporary_path_is_not_followed(self):
        victim = Path(self.tmp.name) / "victim"
        victim.write_text("keep")
        self.resolv.with_suffix(".tmp").symlink_to(victim)
        self.run_hook("BOUND", "10.0.0.1")
        self.assertEqual(victim.read_text(), "keep")
        self.assertIn("nameserver 10.0.0.1", self.resolv.read_text())


# ------------------------------------------------------------------ unchanged code must still work

class ProfileRegression(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        directory = Path(self.tmp.name) / "network"
        for patcher in (mock.patch.object(net, "CONFIG_DIR", directory),
                        mock.patch.object(net, "require_root", lambda: None),
                        mock.patch.object(net, "protected", lambda path, allow_absent=False: Path(path))):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.directory = directory

    def test_profiles_roundtrip_without_storing_the_passphrase(self):
        net.write_profiles([("EMPIRE", "correct horse battery"), ("Cafe Wi-Fi", "0123456789abcdef")])
        path = self.directory / "networks.json"
        self.assertNotIn("correct horse", path.read_text())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual([ssid for ssid, _ in net.load_profiles()], ["EMPIRE", "Cafe Wi-Fi"])
        listed = json.loads((self.directory / "profiles.json").read_text())
        self.assertEqual(listed[0], {"id": net.profile_id("EMPIRE"), "ssid": "EMPIRE"})

    def test_the_stored_key_is_the_standard_wpa2_psk(self):
        net.write_profiles([("IEEE", "password")])
        self.assertEqual(net.load_profiles(), [("IEEE", "f42c6fc52df0ebef9ebb4b90b38a5f902e83fe1b135a70e23aed762e9710a12e")])

    def test_a_64_digit_key_is_kept_as_is(self):
        net.write_profiles([("x", "AB" * 32)])
        self.assertEqual(net.load_profiles(), [("x", "ab" * 32)])

    def test_passphrase_length_rules_and_duplicates(self):
        with self.assertRaises(ValueError):
            net.write_profiles([("x", "short")])
        with self.assertRaises(ValueError):
            net.write_profiles([("x", "a" * 64 + "z")])
        with self.assertRaises(ValueError):
            net.write_profiles([("x", "password123"), ("x", "password456")])

    def legacy(self, extra=""):
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / "wpa.conf"
        psk = net.wpa_psk(b"correct horse battery", b"EMPIRE").hex()
        path.write_text("ctrl_interface=/run/whitecube-net/wpa\nupdate_config=0\n\nnetwork={\n    ssid=%s\n    psk=%s\n"
                        "    key_mgmt=WPA-PSK\n    proto=RSN\n}\n%s" % ("EMPIRE".encode().hex(), psk, extra))
        path.chmod(0o600)
        return path, psk

    def test_the_old_wpa_conf_is_still_read_until_networks_json_exists(self):
        _, psk = self.legacy()
        self.assertEqual(net.load_profiles(), [("EMPIRE", psk)])

    def test_hand_edited_wpa_conf_is_refused_not_discarded(self):
        self.legacy("country=CA\n")
        with self.assertRaises(RuntimeError):
            net.load_profiles()

    def test_migrate_copies_the_old_profiles_and_leaves_the_old_file(self):
        path, psk = self.legacy()
        before = path.read_text()
        with contextlib.redirect_stdout(io.StringIO()):
            net.migrate_profiles()
        self.assertEqual(path.read_text(), before)
        self.assertEqual(json.loads((self.directory / "networks.json").read_text())["networks"],
                         [{"ssid": "EMPIRE".encode().hex(), "psk": psk}])
        with contextlib.redirect_stdout(io.StringIO()) as out:
            net.migrate_profiles()
        self.assertIn("nothing to migrate", out.getvalue())

    def test_networks_json_wins_over_the_old_file(self):
        self.legacy()
        net.write_profiles([("Other", "another passphrase")])
        self.assertEqual([ssid for ssid, _ in net.load_profiles()], ["Other"])

    def test_a_loose_or_damaged_store_is_refused(self):
        net.write_profiles([("EMPIRE", "correct horse battery")])
        path = self.directory / "networks.json"
        path.chmod(0o644)
        with self.assertRaises(RuntimeError):
            net.load_profiles()
        path.chmod(0o600)
        path.write_text('{"version": 1, "networks": [{"ssid": "zz", "psk": "nope"}]}')
        with self.assertRaises(RuntimeError):
            net.load_profiles()
        path.write_text("not json")
        with self.assertRaises(RuntimeError):
            net.load_profiles()

    def test_profile_ids_are_stable_hashes(self):
        self.assertEqual(net.profile_id("EMPIRE"), net.profile_id("EMPIRE"))
        self.assertRegex(net.profile_id("EMPIRE"), r"^[0-9a-f]{16}$")
        self.assertNotEqual(net.profile_id("EMPIRE"), net.profile_id("empire"))

    def test_country_and_power_save_are_validated_and_passed_to_the_station(self):
        root = Path(self.tmp.name) / "root2"
        (root / "etc/whitecube/network").mkdir(parents=True)
        path = root / "etc/whitecube/network/device.json"
        path.write_text(json.dumps(dict(net.DEFAULT_DEVICE, country="CA", power_save=True)))
        config = net.device_config(root)
        self.assertEqual(net.station_options(config), {"country": "CA", "power_save": True})
        self.assertEqual(net.station_options(net.DEFAULT_DEVICE), {"country": None, "power_save": False})
        for bad in ({"country": "canada"}, {"country": "ca"}, {"country": 5}, {"power_save": "yes"}):
            path.write_text(json.dumps(dict(net.DEFAULT_DEVICE, **bad)))
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                net.device_config(root)

    def test_device_scope_is_still_enforced(self):
        root = Path(self.tmp.name) / "root"
        (root / "etc/whitecube/network").mkdir(parents=True)
        (root / "etc/whitecube/network/device.json").write_text(json.dumps(dict(net.DEFAULT_DEVICE, mac="aa:bb:cc:dd:ee:ff")))
        with self.assertRaises(ValueError):
            net.device_config(root)


# ------------------------------------------------------------------ deployment files

class FileTests(unittest.TestCase):
    def test_python_files_parse_at_the_targets_language_level(self):
        for path in (HERE / "cube-net", HERE / "cube-net-dns", REPO / "cube-settings-helper", HERE / "test_cube_net.py"):
            ast.parse(path.read_text(), filename=str(path), feature_version=(3, 13))

    def test_python_entry_points_use_the_isolated_interpreter(self):
        for path in (HERE / "cube-net", HERE / "cube-net-dns"):
            self.assertEqual(path.read_text().splitlines()[0], "#!/usr/bin/python3 -I")

    def test_unit_keeps_the_service_alive_and_stays_sandboxed(self):
        lines = (HERE / "whitecube-network.service").read_text().splitlines()
        settings = dict(l.split("=", 1) for l in lines if "=" in l and not l.startswith("#"))
        self.assertEqual(settings["Restart"], "always")
        self.assertEqual(settings["StartLimitIntervalSec"], "0")
        self.assertEqual(settings["ExecStart"], "/usr/local/bin/cube-net run")
        for key, value in {"NoNewPrivileges": "yes", "ProtectHome": "yes", "ProtectSystem": "full", "PrivateTmp": "yes",
                           "ProtectKernelModules": "yes", "RestrictNamespaces": "yes"}.items():
            self.assertEqual(settings[key], value, key)
        self.assertEqual(settings["RestrictAddressFamilies"], "AF_UNIX AF_NETLINK AF_INET AF_INET6 AF_PACKET")

    def test_the_unit_starts_with_either_saved_network_store(self):
        lines = (HERE / "whitecube-network.service").read_text().splitlines()
        conditions = [l for l in lines if l.startswith("ConditionPathExists=")]
        self.assertEqual(sorted(conditions), ["ConditionPathExists=|/etc/whitecube/network/networks.json",
                                              "ConditionPathExists=|/etc/whitecube/network/wpa.conf"])

    def test_nothing_in_the_controller_runs_wpa_supplicant_or_wpa_cli(self):
        source = (HERE / "cube-net").read_text()
        for needle in ("/usr/sbin/wpa", "wpa_command", "wpa_cli -", "Popen([\"/usr/sbin/wpa"):
            self.assertNotIn(needle, source, needle)
        self.assertEqual(net.audit(Path("/nonexistent"))["unused_wpa_binaries_present"], {"wpa_supplicant": False, "wpa_cli": False})
        self.assertNotIn("wpa_supplicant", net.audit(Path("/nonexistent"))["binaries"])

    def test_the_staged_hardening_dropin_is_optional_and_keeps_what_both_processes_need(self):
        text = (HERE / "whitecube-network.hardening.conf").read_text()
        values = dict(l.split("=", 1) for l in text.splitlines() if "=" in l and not l.startswith("#"))
        caps = set(values["CapabilityBoundingSet"].split())
        self.assertTrue({"CAP_NET_ADMIN", "CAP_NET_RAW", "CAP_NET_BIND_SERVICE"} <= caps)      # netlink, EAPOL, DHCP port 68
        self.assertTrue({"CAP_SETUID", "CAP_SETGID", "CAP_SYS_CHROOT"} <= caps)               # dhcpcd privilege separation
        self.assertNotIn("CAP_SYS_ADMIN", caps)
        self.assertEqual(values["MemoryDenyWriteExecute"], "yes")
        self.assertNotIn("net/whitecube-network.hardening.conf", (HERE / "apply.sh").read_text())   # never installed by apply.sh

    def test_the_wifi_tests_are_part_of_the_build(self):
        self.assertIn("net/test_cube_wifi.py", (REPO / "BUILD.sh").read_text())

    def test_dhcpcd_cannot_exit_on_its_own_timeout(self):
        active = [l.strip() for l in (HERE / "dhcpcd.conf").read_text().splitlines() if l.strip() and not l.startswith("#")]
        self.assertIn("timeout 0", active)
        self.assertFalse(any(l.startswith(("hostname", "fqdn", "static", "env ")) for l in active))
        self.assertIn("nohook wpa_supplicant, resolv.conf, hostname, ntp.conf", active)

    def test_dhcpcd_is_started_with_the_config_the_unit_protects(self):
        source = (HERE / "cube-net").read_text()
        self.assertIn('"/usr/sbin/dhcpcd", "-4", "-B", "-f", str(CONFIG_DIR / "dhcpcd.conf")', source)
        self.assertIn('"-c", "/usr/local/libexec/cube-net-dns"', source)


# ------------------------------------------------------------------ settings helper

class HelperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.defaults = Path(self.tmp.name) / "ufw"
        for patcher in (mock.patch.object(helper, "UFW_DEFAULTS", self.defaults),
                        mock.patch.object(helper, "require_root", lambda: None)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def enable(self, text):
        if text is not None:
            self.defaults.write_text(text)
        with mock.patch.object(helper, "run") as run, mock.patch.object(helper, "emit"):
            helper.firewall_set("enable")
        return run

    def test_enabling_ufw_is_refused_while_outgoing_would_be_denied(self):
        for policy in ('DEFAULT_OUTPUT_POLICY="DROP"', "DEFAULT_OUTPUT_POLICY=REJECT", "  DEFAULT_OUTPUT_POLICY = 'drop'"):
            with self.subTest(policy=policy), self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                self.enable('IPV6=yes\n' + policy + '\n')

    def test_the_refusal_names_the_remedy(self):
        self.defaults.write_text('DEFAULT_OUTPUT_POLICY="DROP"\n')
        with mock.patch.object(helper, "fail", side_effect=SystemExit) as fail, self.assertRaises(SystemExit):
            helper.firewall_set("enable")
        self.assertIn("sudo ufw default allow outgoing", fail.call_args.args[0])

    def test_enabling_proceeds_when_outgoing_is_allowed_or_unknown(self):
        for text in ('DEFAULT_OUTPUT_POLICY="ACCEPT"\n', "IPV6=yes\n", None):
            with self.subTest(text=text):
                if text is None and self.defaults.exists():
                    self.defaults.unlink()
                run = self.enable(text)
                run.assert_called_once()
                self.assertEqual(run.call_args.args[:3], ("/usr/sbin/ufw", "--force", "enable"))

    def test_other_firewall_actions_are_not_blocked(self):
        self.defaults.write_text('DEFAULT_OUTPUT_POLICY="DROP"\n')
        with mock.patch.object(helper, "run") as run, mock.patch.object(helper, "emit"):
            helper.firewall_set("disable")
            helper.firewall_set("deny-incoming")
        self.assertEqual(run.call_count, 2)

    def test_adding_a_network_tells_the_running_station_to_reload(self):
        calls = []
        module = types.SimpleNamespace(profile_lock=contextlib.nullcontext, load_profiles=lambda: [("Old", "00" * 32)],
                                       write_profiles=lambda profiles: calls.append(("write", [s for s, _ in profiles])),
                                       control_call=lambda *a: calls.append(("control",) + a), profile_id=lambda ssid: "0" * 16)
        with mock.patch.object(helper, "cube_net_module", return_value=module), mock.patch.object(helper, "emit"), \
                mock.patch.object(helper.sys, "stdin", io.StringIO("New\npassphrase1\n")):
            helper.net_add()
        self.assertEqual(calls, [("write", ["Old", "New"]), ("control", "reload")])

    def test_a_stopped_service_does_not_make_adding_a_network_fail(self):
        def down(*a):
            raise RuntimeError("Start the configured whitecube-network service")
        module = types.SimpleNamespace(profile_lock=contextlib.nullcontext, load_profiles=lambda: [], write_profiles=lambda p: None,
                                       control_call=down, profile_id=lambda ssid: "0" * 16)
        with mock.patch.object(helper, "cube_net_module", return_value=module), mock.patch.object(helper, "emit") as emit, \
                mock.patch.object(helper.sys, "stdin", io.StringIO("New\npassphrase1\n")):
            helper.net_add()
        emit.assert_called_once()

    def test_net_page_reads_the_names_the_controller_actually_publishes(self):
        status = {"present": True, "interface": {"interface": "wlan0"}, "association_state": "COMPLETED",
                  "diagnostic_summary": "Association, IPv4 lease/route, a resolver query and a bound TCP probe passed.",
                  "diagnostic_code": "online", "ipv4_address": "10.0.0.133", "reassociate_in_s": 30, "profiles": []}
        lines = []
        with mock.patch.object(helper, "run", return_value=subprocess.CompletedProcess([], 0, stdout=json.dumps(status))), \
                mock.patch.object(helper, "emit", lambda *fields: lines.append(fields)), \
                mock.patch.object(helper, "read", return_value="nameserver 10.0.0.1"):
            helper.net()
        records = {(f[0], f[1]): f[2] for f in lines if len(f) >= 3}
        self.assertEqual(records[("net", "association_state")], "completed")  # the panel colours this green
        self.assertEqual(records[("net", "code")], "online")
        self.assertIn("Association, IPv4 lease/route", records[("net", "summary")])
        self.assertIn("Next automatic reassociation in 30 s", records[("net", "summary")])



# ------------------------------------------------------------------ the real loop, scripted

class World:
    """A scripted radio, router and resolver standing in for everything Controller touches.

    Time is the tick index. reassociate() models the real effect: the link drops for 5 s, then
    returns and re-leases 7 s later. heals_assoc=False models a radio that never associates and
    heals_lease=False a router that never hands out a lease, whatever the controller does."""

    def __init__(self, assoc_at=5, lease_at=12, dns=lambda t: True, internet=lambda t: True, gateway=lambda t: True,
                 heals_assoc=True, heals_lease=True, probe_ticks=4):
        self.assoc_at, self.lease_at = assoc_at, lease_at
        self.heals_assoc, self.heals_lease = heals_assoc, heals_lease
        self.dns, self.internet, self.gateway, self.probe_ticks = dns, internet, gateway, probe_ticks
        self.t = 0
        self.pending = []           # (ready_tick, generation, suite)
        self.commands = []          # (tick, command) sent to the Wi-Fi station
        self.logs, self.published = [], []
        self.request_value = ""
        self.station_down = False

    # collaborators used by net.Controller
    def associated(self):
        return self.assoc_at is not None and self.t >= self.assoc_at

    def leased(self):
        return self.associated() and self.lease_at is not None and self.t >= self.lease_at

    def request(self):
        return self.request_value

    def link(self):
        if self.station_down:
            raise RuntimeError("controller unavailable")
        if self.associated():
            return {"state": "COMPLETED", "ssid": "EMPIRE", "rssi": -52}
        return {"state": "SCANNING", "ssid": None, "rssi": None}

    def reassociate(self):
        self.commands.append((self.t, "reassociate"))
        if self.heals_assoc:
            self.assoc_at = self.t + 5
            self.lease_at = self.t + 12 if self.heals_lease else None
        elif self.associated():
            self.assoc_at = self.t + 5               # a working link still drops while it reassociates

    def signal(self):
        return -52

    def sample(self):
        return ("10.0.0.133", "10.0.0.1") if self.leased() else (None, None)

    def start_probe(self, generation, gateway, results):
        suite = {"checks_epoch": net.time.time(), "gateway_ok": self.gateway(self.t), "dns_ok": self.dns(self.t),
                 "dns_ms": 21 if self.dns(self.t) else None, "connectivity_ok": self.internet(self.t),
                 "tcp_ms": 99 if self.internet(self.t) else None}
        ready = self.t + self.probe_ticks
        self.pending.append((ready, generation, suite))
        return types.SimpleNamespace(is_alive=lambda: self.t < ready)

    def publish(self, runtime):
        self.published.append(json.loads(json.dumps(runtime)))

    def log(self, message):
        self.logs.append((self.t, message))

    def deliver(self, results):
        for item in [p for p in self.pending if p[0] <= self.t]:
            self.pending.remove(item)
            results.put(item[1:])

    def reassociations(self):
        return [t for t, command in self.commands if command == "reassociate"]


def drive(world, seconds, start=5000.0):
    runtime = net.public_runtime(Path("/nonexistent"))
    controller = net.Controller(runtime, world, start, False, "")
    for i in range(seconds):
        world.t = i
        world.deliver(controller.results)
        controller.tick(start + i)
    return controller


class ControllerTests(unittest.TestCase):
    def diagnosis(self, controller):
        config = dict(net.DEFAULT_DEVICE)
        seen = [{"interface": "wlan0", "usb_id": config["usb_id"], "mac": config["mac"], "driver": config["driver"]}]
        return net.diagnostic(config, seen, controller.runtime, True)["diagnostic_code"]

    def test_the_incident_with_the_real_loop_nothing_is_touched(self):
        # 20:51: associated at +11 s, DHCP only at +30 s, DNS slow for the first 90 s, Internet fine.
        world = World(assoc_at=11, lease_at=30, dns=lambda t: t > 90)
        controller = drive(world, 900)
        self.assertEqual(world.reassociations(), [])
        self.assertEqual(world.commands, [])
        self.assertEqual(self.diagnosis(controller), "online")
        self.assertIs(controller.runtime["dns_ok"], True)
        self.assertEqual(controller.runtime["reconnect_count"], 0)

    def test_persistent_dns_and_internet_failure_are_reported_and_never_acted_on(self):
        world = World(dns=lambda t: False, internet=lambda t: False)
        controller = drive(world, 3600)
        self.assertEqual(world.commands, [])
        self.assertEqual(self.diagnosis(controller), "dns-failed")
        self.assertIs(controller.runtime["dns_ok"], False)
        self.assertIs(controller.runtime["connectivity_ok"], False)
        self.assertIsNone(controller.runtime["last_error"])
        self.assertTrue(any("reported only" in m for _, m in world.logs))

    def test_internet_down_with_working_dns_says_so(self):
        controller = drive(World(internet=lambda t: False), 600)
        self.assertEqual(self.diagnosis(controller), "connectivity-lost")

    def test_a_single_slow_probe_does_not_paint_anything_red(self):
        world = World(dns=lambda t: not (60 <= t < 75), internet=lambda t: not (60 <= t < 75))
        controller = drive(world, 900)
        reds = [p for p in world.published if p["dns_ok"] is False or p["connectivity_ok"] is False]
        self.assertEqual(reds, [])
        self.assertEqual(world.commands, [])

    def test_association_that_never_completes_is_retried_with_growing_delays(self):
        world = World(assoc_at=None, heals_assoc=False)
        controller = drive(world, 700)
        self.assertEqual(world.reassociations(), [45, 90, 180, 360, 660])
        self.assertEqual(self.diagnosis(controller), "association-failed")
        self.assertEqual(controller.runtime["actions_total"], 5)
        self.assertIn("reassociate_in_s", controller.runtime)

    def test_the_wait_is_visible_while_it_counts_down(self):
        world = World(assoc_at=None, heals_assoc=False)
        drive(world, 60)
        waits = [p["reassociate_in_s"] for p in world.published]
        self.assertEqual(waits[1], 44)
        self.assertLess(waits[30], waits[10])

    def test_no_lease_waits_four_minutes_then_keeps_retrying_ever_more_slowly(self):
        world = World(assoc_at=5, lease_at=None, heals_lease=False)
        controller = drive(world, 1200)
        # first after L3_REASSOC_AFTER (5+240); the link drop restarts the wait, spacing grows 240 -> 480 -> 900
        self.assertEqual(world.reassociations(), [245, 490, 970])
        self.assertEqual(self.diagnosis(controller), "dhcp-failed")

    def test_a_dead_router_is_noticed_and_retried_slowly(self):
        world = World(gateway=lambda t: t < 100)
        drive(world, 900)
        acted = world.reassociations()
        self.assertGreaterEqual(len(acted), 2)
        self.assertGreater(acted[0], 100 + 120)          # never sooner than the dead-router wait
        self.assertTrue(all(b - a >= 120 for a, b in zip(acted, acted[1:])))

    def test_a_manual_reconnect_restarts_the_ladder(self):
        world = World(assoc_at=None, heals_assoc=False)
        controller = net.Controller(net.public_runtime(Path("/nonexistent")), world, 5000.0, False, "")
        for i in range(120):
            world.t = i
            if i == 100:
                world.request_value = "reconnect-1"
            controller.tick(5000.0 + i)
        self.assertEqual(world.reassociations(), [45, 90])
        for i in range(120, 220):
            world.t = i
            controller.tick(5000.0 + i)
        self.assertEqual(world.reassociations(), [45, 90, 145, 190])   # the ladder restarts: 45 s after the reset at 100, then +45
        self.assertTrue(any("retry delays reset" in m for _, m in world.logs))

    def test_results_from_an_older_lease_are_discarded(self):
        world = World()
        controller = drive(world, 40)
        generation = controller.generation
        stale = {"checks_epoch": 12345.0, "gateway_ok": False, "dns_ok": False, "dns_ms": 7777,
                 "connectivity_ok": False, "tcp_ms": 8888}
        keys = ("dns_ok", "connectivity_ok", "gateway_ok", "dns_ms", "tcp_ms", "checks_epoch")
        before = {key: controller.runtime[key] for key in keys}
        self.assertIs(before["dns_ok"], True)
        due = controller.probe_due
        for _ in range(3):      # even repeated stale failures must neither flip a check nor touch the numbers
            controller.results.put((generation - 1, stale))
        controller.collect(6000.0)
        self.assertEqual({key: controller.runtime[key] for key in keys}, before)
        self.assertEqual(controller.probe_due, due)
        # a result for the current lease is accepted
        controller.results.put((generation, dict(stale, checks_epoch=net.time.time())))
        controller.collect(6000.0)
        self.assertEqual(controller.runtime["dns_ms"], 7777)

    def test_losing_and_regaining_the_lease_restarts_the_checks(self):
        world = World()
        controller = net.Controller(net.public_runtime(Path("/nonexistent")), world, 5000.0, False, "")
        for i in range(60):
            world.t = i
            world.deliver(controller.results)
            controller.tick(5000.0 + i)
        first = controller.generation
        self.assertIs(controller.runtime["dns_ok"], True)
        world.lease_at = 70
        for i in range(60, 69):
            world.t = i
            world.deliver(controller.results)
            controller.tick(5000.0 + i)
        self.assertGreater(controller.generation, first)
        self.assertIsNone(controller.runtime["dns_ok"])
        self.assertIsNone(controller.runtime["default_route"])

    def test_station_trouble_is_reported_without_acting_or_stopping_publication(self):
        world = World()
        world.station_down = True
        controller = drive(world, 120)
        self.assertEqual(world.commands, [])
        self.assertEqual(controller.runtime["association_state"], "controller-unavailable")
        self.assertEqual(len(world.published), 120)

    def test_reconnects_are_counted_and_ssid_and_signal_published(self):
        world = World(assoc_at=None)
        controller = net.Controller(net.public_runtime(Path("/nonexistent")), world, 5000.0, True, "")
        for i in range(200):
            world.t = i
            if i == 20:
                world.assoc_at = 30
            if i == 100:
                world.assoc_at = 110
            controller.tick(5000.0 + i)
        self.assertGreaterEqual(controller.runtime["reconnect_count"], 2)
        self.assertEqual(controller.runtime["ssid"], "EMPIRE")
        self.assertEqual(controller.runtime["signal_dbm"], -52)

    def test_every_published_status_is_flat_small_and_budget_free(self):
        world = World(dns=lambda t: False)
        drive(world, 300)
        for status in world.published:
            self.assertLess(len(json.dumps(status)), 4096)
            self.assertNotIn("recovery_attempts", status)
            self.assertNotIn("recovery_exhausted", status)
            self.assertTrue(all(not isinstance(v, (dict, list)) for v in status.values()))

    def test_the_journal_never_carries_the_network_name(self):
        world = World(assoc_at=None, heals_assoc=False)
        drive(world, 300)
        self.assertFalse([m for _, m in world.logs if "EMPIRE" in m])

    def test_journal_lines_explain_each_decision(self):
        world = World(assoc_at=None, heals_assoc=False)
        drive(world, 100)
        text = "\n".join(m for _, m in world.logs)
        self.assertIn("recovery: requested reassociation #1 - not associated for 45s", text)
        self.assertIn("wifi_state - -> SCANNING", text)


class ProbeWorkerTests(unittest.TestCase):
    def test_start_probe_delivers_a_result_tagged_with_its_generation(self):
        io = net.SystemIO("wlan0")
        results = net.queue.SimpleQueue()
        with mock.patch.object(net, "probe_suite", return_value={"dns_ok": True}) as suite:
            worker = io.start_probe(7, "10.0.0.1", results)
            worker.join(2)
        self.assertEqual(results.get_nowait(), (7, {"dns_ok": True}))
        suite.assert_called_once_with("wlan0", "10.0.0.1")
        self.assertFalse(worker.is_alive())

    def test_a_crashing_probe_is_logged_and_does_not_escape_the_thread(self):
        io = net.SystemIO("wlan0")
        results = net.queue.SimpleQueue()
        with mock.patch.object(net, "probe_suite", side_effect=ZeroDivisionError), mock.patch.object(net, "log") as log:
            io.start_probe(1, "10.0.0.1", results).join(2)
        self.assertTrue(results.empty())
        log.assert_called_once_with("probe error: ZeroDivisionError")


# ------------------------------------------------------------------ the whole service, with fakes

class SimulatedTime:
    def __init__(self, on_sleep):
        self.now = 50_000.0
        self.sleeps = 0
        self.on_sleep = on_sleep

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        self.sleeps += 1
        self.on_sleep(self)


class FakeChild:
    created = []

    def __init__(self, argv, *a, **k):
        self.argv, self.terminated, self.killed = argv, False, False
        FakeChild.created.append(self)

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


class FakeDriver:
    instances = []

    def __init__(self, iface):
        self.iface, self.closed = iface, False
        FakeDriver.instances.append(self)

    def close(self):
        self.closed = True


class FakeControl:
    instances = []

    def __init__(self, path, station):
        self.path, self.station, self.started, self.stopped, self.joined = path, station, False, False, False
        FakeControl.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def join(self, timeout=None):
        self.joined = True


class FakeStation:
    """Stands in for net.Station inside run_service(): a script says what the link looks like at each second."""
    instances = []
    script = staticmethod(lambda t: {"state": "COMPLETED", "ssid": "EMPIRE", "rssi": -50})
    clock = None

    def __init__(self, driver, load_profiles, log, options=None):
        self.driver, self.load_profiles, self.log, self.options = driver, load_profiles, log, options
        self.error, self.calls, self.halt = None, [], threading.Event()
        FakeStation.instances.append(self)

    def run(self):
        self.halt.wait(30)

    def stop(self):
        self.halt.set()

    def snapshot(self):
        return FakeStation.script(FakeStation.clock())

    def call(self, name, *args):
        self.calls.append((FakeStation.clock(), name))
        return "OK"


class ServiceSmoke(unittest.TestCase):
    """Runs net.run_service() itself: startup checks, the station and dhcpcd, the loop, shutdown cleanup."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.config, self.run_dir = root / "etc", root / "run"
        self.config.mkdir()
        (self.config / "device.json").write_text(json.dumps(net.DEFAULT_DEVICE))
        (self.config / "dhcpcd.conf").write_text("timeout 0\n")
        for name in ("device.json", "dhcpcd.conf"):
            (self.config / name).chmod(0o644)          # the controller insists nobody else can write them
        FakeChild.created, FakeDriver.instances, FakeControl.instances, FakeStation.instances = [], [], [], []
        real_stat = Path.stat

        def stat_as_root(path, *a, **k):
            result = real_stat(path, *a, **k)
            if str(path).startswith(self.tmp.name):
                fields = list(result)
                fields[4] = 0                       # st_uid: the checks insist on root ownership
                return os.stat_result(fields)
            return result
        patches = [mock.patch.object(net, "CONFIG_DIR", self.config), mock.patch.object(net, "RUNTIME_DIR", self.run_dir),
                   mock.patch.object(net, "require_root", lambda: None),
                   mock.patch.object(net, "protected", lambda path, allow_absent=False: Path(path)),
                   mock.patch.object(Path, "stat", stat_as_root)]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.write_networks()
        old = (net.signal.getsignal(net.signal.SIGTERM), net.signal.getsignal(net.signal.SIGINT))
        self.addCleanup(lambda: (net.signal.signal(net.signal.SIGTERM, old[0]), net.signal.signal(net.signal.SIGINT, old[1])))

    def write_networks(self):
        with contextlib.redirect_stdout(io.StringIO()):
            net.write_profiles([("EMPIRE", "correct horse battery")])

    def run_service(self, script, seconds, competing=(), station=FakeStation, popen=FakeChild):
        def stop_after(clock):
            if clock.sleeps >= seconds:
                net.signal.raise_signal(net.signal.SIGTERM)
        clock = SimulatedTime(stop_after)
        origin = clock.now
        FakeStation.script = staticmethod(script)
        FakeStation.clock = lambda: round(clock.now - origin)
        seen = [{"interface": "wlan0", "usb_id": "2001:3329", "mac": "88:76:b9:f2:9d:29", "driver": "rtw88_8822cu"}]
        healthy = {"checks_epoch": 1.0, "gateway_ok": True, "dns_ok": True, "dns_ms": 20, "connectivity_ok": True, "tcp_ms": 90}
        with mock.patch.object(net, "interfaces", return_value=seen), \
                mock.patch.object(net, "competing_processes", return_value=list(competing)), \
                mock.patch.object(net.subprocess, "Popen", popen), \
                mock.patch.object(net, "Nl80211Driver", FakeDriver), \
                mock.patch.object(net, "Station", station), \
                mock.patch.object(net, "ControlServer", FakeControl), \
                mock.patch.object(net, "wireless_signal", return_value=-50), \
                mock.patch.object(net, "interface_ipv4", return_value="10.0.0.133"), \
                mock.patch.object(net, "default_gateway", return_value="10.0.0.1"), \
                mock.patch.object(net, "probe_suite", return_value=dict(healthy)), \
                mock.patch.object(net.time, "monotonic", clock.monotonic), \
                mock.patch.object(net.time, "sleep", clock.sleep), \
                contextlib.redirect_stdout(io.StringIO()) as journal:
            try:
                net.run_service()
            finally:
                self.journal = journal.getvalue()
        return FakeStation.instances[0].calls if FakeStation.instances else []

    def final_status(self):
        return json.loads((self.run_dir / "status.json").read_text())

    def test_a_healthy_session_starts_the_station_and_dhcpcd_and_shuts_down_cleanly(self):
        calls = self.run_service(lambda t: {"state": "COMPLETED", "ssid": "EMPIRE", "rssi": -50}, seconds=300)
        self.assertEqual(calls, [])
        self.assertEqual([child.argv[:4] for child in FakeChild.created], [["/usr/sbin/dhcpcd", "-4", "-B", "-f"]])
        self.assertTrue(all(child.terminated for child in FakeChild.created))
        station = FakeStation.instances[0]
        self.assertTrue(station.halt.is_set())
        self.assertTrue(FakeDriver.instances[0].closed)
        control = FakeControl.instances[0]
        self.assertEqual((control.started, control.stopped, control.joined), (True, True, True))
        self.assertEqual(control.path, self.run_dir / "control.sock")
        self.assertEqual(self.final_status()["association_state"], "stopped")
        self.assertEqual((self.run_dir / "profiles.json").exists(), True)

    def test_the_station_is_given_the_saved_networks_loader(self):
        self.run_service(lambda t: {"state": "COMPLETED", "ssid": "EMPIRE", "rssi": -50}, seconds=3)
        self.assertEqual([ssid for ssid, _ in FakeStation.instances[0].load_profiles()], ["EMPIRE"])

    def test_the_status_carries_the_ssid_and_signal_from_the_station(self):
        seen = []
        original = net.publish_runtime
        with mock.patch.object(net, "publish_runtime", lambda data: (seen.append(dict(data)), original(data))[1]):
            self.run_service(lambda t: {"state": "COMPLETED", "ssid": "EMPIRE", "rssi": -48}, seconds=10)
        live = [item for item in seen if item["association_state"] == "COMPLETED"]
        self.assertTrue(live)
        self.assertEqual((live[-1]["ssid"], live[-1]["signal_dbm"]), ("EMPIRE", -48))

    def test_a_link_that_never_comes_up_is_nudged_on_the_ladder_and_never_disconnected(self):
        calls = self.run_service(lambda t: {"state": "SCANNING", "ssid": None, "rssi": None}, seconds=800)
        nudges = [t for t, c in calls if c == "reassociate"]
        self.assertEqual(len(nudges), 5)
        self.assertEqual(nudges[:3], [45, 90, 180])
        self.assertNotIn("disconnect", [c for _, c in calls])
        self.assertIn("Waiting for the configured USB dongle", self.journal)
        self.assertIn("recovery: requested reassociation #1 - not associated for 45s", self.journal)
        self.assertNotIn("EMPIRE", self.journal)

    def test_a_child_that_dies_stops_the_service_so_systemd_can_restart_it(self):
        class Dying(FakeChild):
            def poll(self):
                return 1 if self.argv[0].endswith("dhcpcd") else None
        with self.assertRaisesRegex(RuntimeError, "child process exited"):
            self.run_service(lambda t: {"state": "COMPLETED", "ssid": "EMPIRE", "rssi": -50}, seconds=30, popen=Dying)
        self.assertEqual(self.final_status()["association_state"], "failed")
        self.assertTrue(FakeStation.instances[0].halt.is_set())      # nothing is left running behind a failed start

    def test_a_dead_station_stops_the_service_so_systemd_can_restart_it(self):
        class Crashing(FakeStation):
            def run(self):
                self.error = OSError("netlink went away")
        with self.assertRaisesRegex(RuntimeError, "Wi-Fi station stopped"):
            self.run_service(lambda t: {"state": "COMPLETED", "ssid": "EMPIRE", "rssi": -50}, seconds=30, station=Crashing)
        self.assertEqual(self.final_status()["association_state"], "failed")

    def test_the_service_refuses_to_compete_with_another_manager(self):
        for rival in ("wpa_supplicant", "NetworkManager"):
            with self.subTest(rival=rival), self.assertRaisesRegex(RuntimeError, rival):
                self.run_service(lambda t: {"state": "COMPLETED"}, seconds=5, competing=[rival])
        self.assertEqual(FakeStation.instances, [])      # it never touched the radio

    def test_without_saved_networks_the_service_says_so(self):
        for name in ("networks.json", "wpa.conf"):
            (self.config / name).unlink(missing_ok=True)
        with self.assertRaisesRegex(RuntimeError, "No saved networks"):
            self.run_service(lambda t: {"state": "COMPLETED"}, seconds=5)

    def test_a_group_readable_store_is_refused(self):
        (self.config / "networks.json").chmod(0o640)
        with self.assertRaisesRegex(RuntimeError, "mode 0600"):
            self.run_service(lambda t: {"state": "COMPLETED"}, seconds=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
