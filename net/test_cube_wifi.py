"""Offline tests for the native Wi-Fi station inside cube-net (no wpa_supplicant, no root, no radio).

Standard library only (plus the openssl CLI as an independent AES oracle when it is installed). Run:
    python3 -I net/test_cube_wifi.py          (BUILD.sh runs it)

The access point side of every handshake is written here, separately from the code under test, straight
from IEEE 802.11-2016 12.7: its own frame layout, its own MIC, its own key derivation. A real
authenticator is therefore played against the real Handshake and the real Station.
"""
import contextlib
import errno
import hashlib
import hmac
import importlib.machinery
import importlib.util
import io
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True

HERE = Path(__file__).resolve().parent


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


net = load("cube_net_wifi", HERE / "cube-net")

AP = bytes.fromhex("c09435adecaa")
STA = bytes.fromhex("8876b9f29d29")
CCMP, PSK = 0x000FAC04, 0x000FAC02
AP_RSN = b"\x30\x14\x01\x00\x00\x0f\xac\x04\x01\x00\x00\x0f\xac\x04\x01\x00\x00\x0f\xac\x02\x0c\x00"


def shutil_which(name):
    return next((str(Path(d) / name) for d in os.environ.get("PATH", "").split(":") if (Path(d) / name).exists()), None)


def openssl_wrap(kek, plaintext):
    """RFC 3394 wrap by the openssl CLI: an AES implementation that shares no code with cube-net.

    openssl calls key wrap an unstreamable mode, so it needs real files rather than pipes."""
    with tempfile.TemporaryDirectory() as directory:
        source, target = Path(directory, "in"), Path(directory, "out")
        source.write_bytes(plaintext)
        done = subprocess.run(["openssl", "enc", "-id-aes128-wrap", "-K", kek.hex(), "-iv", "A6A6A6A6A6A6A6A6",
                               "-in", str(source), "-out", str(target)], capture_output=True)
        if done.returncode:
            raise unittest.SkipTest("openssl cannot do AES key wrap here")
        return target.read_bytes()


# ------------------------------------------------------------------ crypto

class CryptoTests(unittest.TestCase):
    def test_aes_decrypts_the_fips_197_vector(self):
        key = bytes(range(16))
        self.assertEqual(net.aes_decrypt_block(key, bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")).hex(),
                         "00112233445566778899aabbccddeeff")

    def test_aes_agrees_with_openssl_on_random_blocks(self):
        if not shutil_which("openssl"):
            self.skipTest("no openssl")
        for _ in range(12):
            key, block = os.urandom(16), os.urandom(16)
            enc = subprocess.run(["openssl", "enc", "-aes-128-ecb", "-nopad", "-K", key.hex()], input=block,
                                 capture_output=True).stdout
            self.assertEqual(net.aes_decrypt_block(key, enc), block)

    def test_aes_rejects_bad_sizes(self):
        with self.assertRaises(ValueError):
            net.aes_decrypt_block(b"short", bytes(16))
        with self.assertRaises(ValueError):
            net.aes_decrypt_block(bytes(16), b"short")

    def test_key_unwrap_rfc3394_vector(self):
        kek = bytes.fromhex("000102030405060708090A0B0C0D0E0F")
        wrapped = bytes.fromhex("1FA68B0A8112B447AEF34BD8FB5A7B829D3E862371D2CFE5")
        self.assertEqual(net.aes_unwrap(kek, wrapped).hex(), "00112233445566778899aabbccddeeff")

    def test_key_unwrap_detects_tampering_and_bad_lengths(self):
        kek = bytes.fromhex("000102030405060708090A0B0C0D0E0F")
        wrapped = bytearray.fromhex("1FA68B0A8112B447AEF34BD8FB5A7B829D3E862371D2CFE5")
        for index in (0, 7, 8, 23):
            damaged = bytearray(wrapped)
            damaged[index] ^= 0x01
            self.assertIsNone(net.aes_unwrap(kek, bytes(damaged)), index)
        self.assertIsNone(net.aes_unwrap(kek, bytes(wrapped[:-8])[:16]))
        self.assertIsNone(net.aes_unwrap(kek, bytes(wrapped) + b"x"))
        self.assertIsNone(net.aes_unwrap(bytes(16), bytes(wrapped)))

    def test_key_unwrap_inverts_openssl_wrap_for_many_sizes(self):
        if not shutil_which("openssl"):
            self.skipTest("no openssl")
        for size in (16, 24, 40, 64):
            kek, plain = os.urandom(16), os.urandom(size)
            self.assertEqual(net.aes_unwrap(kek, openssl_wrap(kek, plain)), plain, size)

    def test_psk_is_the_ieee_802_11i_test_vector(self):
        self.assertEqual(net.wpa_psk(b"password", b"IEEE").hex(),
                         "f42c6fc52df0ebef9ebb4b90b38a5f902e83fe1b135a70e23aed762e9710a12e")

    def test_prf_is_the_documented_hmac_sha1_counter_construction(self):
        key, label, data = os.urandom(32), b"Pairwise key expansion", os.urandom(76)
        expected = b"".join(hmac.new(key, label + b"\0" + data + bytes([i]), hashlib.sha1).digest() for i in range(3))[:48]
        self.assertEqual(net.prf_sha1(key, label, data, 48), expected)
        self.assertEqual(len(net.prf_sha1(key, label, data, 20)), 20)
        self.assertEqual(net.prf_sha1(key, label, data, 21)[:20], net.prf_sha1(key, label, data, 20))

    def test_ptk_is_symmetric_in_who_asks_and_changes_with_every_input(self):
        pmk, an, sn = os.urandom(32), os.urandom(32), os.urandom(32)
        ptk = net.derive_ptk(pmk, AP, STA, an, sn)
        self.assertEqual(len(ptk), 48)
        self.assertEqual(ptk, net.derive_ptk(pmk, STA, AP, sn, an))          # the authenticator derives the same key
        for changed in (net.derive_ptk(os.urandom(32), AP, STA, an, sn), net.derive_ptk(pmk, AP, STA, os.urandom(32), sn),
                        net.derive_ptk(pmk, AP, STA, an, os.urandom(32)), net.derive_ptk(pmk, AP, bytes(6), an, sn)):
            self.assertNotEqual(changed, ptk)


# ------------------------------------------------------------------ information elements

class ElementTests(unittest.TestCase):
    def test_parse_ies_stops_at_a_truncated_element(self):
        data = b"\x00\x03abc" + b"\x30\x02\x01\x00" + b"\xdd\x09short"
        self.assertEqual(net.parse_ies(data), [(0, b"abc"), (0x30, b"\x01\x00")])

    def test_the_access_points_rsn_element_parses_and_is_usable(self):
        rsn = net.parse_rsn(AP_RSN[2:])
        self.assertEqual(rsn, {"group": CCMP, "pairwise": [CCMP], "akm": [PSK], "caps": 0x000C, "mgmt": None})
        self.assertIsNone(net.unsupported_reason(rsn))

    def test_our_own_element_is_wellformed_and_matches_what_we_claim(self):
        ie = net.build_rsn_ie()
        self.assertEqual(ie[0], 0x30)
        self.assertEqual(ie[1], len(ie) - 2)
        rsn = net.parse_rsn(ie[2:])
        self.assertEqual((rsn["group"], rsn["pairwise"], rsn["akm"]), (CCMP, [CCMP], [PSK]))
        self.assertFalse(rsn["caps"] & 0x00C0)           # no management frame protection claimed

    def test_unsupported_networks_are_named(self):
        def rsn(group=CCMP, pairwise=(CCMP,), akm=(PSK,), caps=0):
            body = struct.pack("<H", 1) + group.to_bytes(4, "big") + struct.pack("<H", len(pairwise))
            body += b"".join(p.to_bytes(4, "big") for p in pairwise) + struct.pack("<H", len(akm))
            return net.parse_rsn(body + b"".join(a.to_bytes(4, "big") for a in akm) + struct.pack("<H", caps))
        self.assertIsNone(net.unsupported_reason(rsn()))
        self.assertIn("RSN", net.unsupported_reason(None))
        self.assertIn("PSK", net.unsupported_reason(rsn(akm=(0x000FAC08,))))                 # WPA3-only (SAE)
        self.assertIn("PSK", net.unsupported_reason(rsn(akm=(0x000FAC01,))))                 # enterprise
        self.assertIn("CCMP", net.unsupported_reason(rsn(group=0x000FAC02)))                 # TKIP group
        self.assertIn("CCMP", net.unsupported_reason(rsn(pairwise=(0x000FAC02,))))
        self.assertIsNone(net.unsupported_reason(rsn(caps=0x00C0)))                           # PMF required: now supported
        self.assertIsNone(net.unsupported_reason(rsn(akm=(0x000FAC06,), caps=0x00C0)))        # PSK-SHA256 with PMF: supported
        self.assertIn("PSK", net.unsupported_reason(rsn(akm=(0x000FAC08,), caps=0x00C0)))     # SAE only: still not
        self.assertIsNone(net.unsupported_reason(rsn(akm=(PSK, 0x000FAC08), caps=0x0080)))   # WPA3 transition mode is fine

    def test_malformed_rsn_elements_are_rejected_not_guessed(self):
        for body in (b"", b"\x01", b"\x02\x00" + bytes(20), AP_RSN[2:12], AP_RSN[2:10] + b"\x00\x00"):
            self.assertIsNone(net.parse_rsn(body), body)

    def test_security_strings_read_like_a_scan_listing(self):
        ies = net.parse_ies(AP_RSN)
        self.assertEqual(net.security_string(ies, 0x0411), "[WPA2-PSK-CCMP][ESS]")
        self.assertEqual(net.security_string([], 0x0001), "[ESS]")
        self.assertEqual(net.security_string([], 0x0011), "[WEP][ESS]")
        self.assertIn("[WPA-PSK", net.security_string([(0xDD, b"\x00\x50\xf2\x01\x01\x00")], 0x0001))
        sae = b"\x30\x14\x01\x00\x00\x0f\xac\x04\x01\x00\x00\x0f\xac\x04\x01\x00\x00\x0f\xac\x08\x00\x00"
        self.assertTrue(net.security_string(net.parse_ies(sae), 1).startswith("[WPA3-SAE-CCMP]"))


# ------------------------------------------------------------------ the access point, written separately

def ap_frame(info, replay, nonce, data, key_length=16, rsc=bytes(8), mic=bytes(16), version=2):
    body = (bytes([2]) + struct.pack(">HHQ", info, key_length, replay) + nonce + bytes(16) + rsc + bytes(8) + mic
            + struct.pack(">H", len(data)) + data)
    return bytes([version, 3]) + struct.pack(">H", len(body)) + body


def openssl_cmac(key, data):
    """AES-128-CMAC by the openssl CLI: an implementation that shares no code with cube-net."""
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory, "in")
        source.write_bytes(data)
        done = subprocess.run(["openssl", "mac", "-cipher", "AES-128-CBC", "-macopt", f"hexkey:{key.hex()}", "-binary",
                               "-in", str(source), "CMAC"], capture_output=True)
        if done.returncode:
            raise unittest.SkipTest("openssl cannot do CMAC here")
        return done.stdout


def ap_mic(kck, frame, ver=2):
    zeroed = frame[:81] + bytes(16) + frame[97:]
    return openssl_cmac(kck, zeroed) if ver == 3 else hmac.new(kck, zeroed, hashlib.sha1).digest()[:16]


def ap_sign(kck, frame, ver=2):
    return frame[:81] + ap_mic(kck, frame, ver) + frame[97:]


def ap_ptk(pmk, anonce, snonce, aa=AP, sha256=False):
    data = min(aa, STA) + max(aa, STA) + min(anonce, snonce) + max(anonce, snonce)
    if sha256:       # IEEE 802.11-2016 12.7.1.7.2 with SHA-256: counter and length are 16-bit little-endian
        return b"".join(hmac.new(pmk, struct.pack("<H", i) + b"Pairwise key expansion" + data + struct.pack("<H", 384), hashlib.sha256).digest()
                        for i in (1, 2))[:48]
    return b"".join(hmac.new(pmk, b"Pairwise key expansion\0" + data + bytes([i]), hashlib.sha1).digest() for i in range(3))[:48]


def rsn_bytes(group=CCMP, pairwise=(CCMP,), akm=(PSK,), caps=0x000C, mgmt=None):
    body = struct.pack("<H", 1) + group.to_bytes(4, "big") + struct.pack("<H", len(pairwise))
    body += b"".join(p.to_bytes(4, "big") for p in pairwise) + struct.pack("<H", len(akm)) + b"".join(a.to_bytes(4, "big") for a in akm)
    body += struct.pack("<H", caps)
    if mgmt is not None:
        body += struct.pack("<H", 0) + mgmt.to_bytes(4, "big")
    return b"\x30" + bytes([len(body)]) + body


def ap_igtk_kde(index, ipn, key):
    return b"\xdd" + bytes([4 + 2 + 6 + 16]) + b"\x00\x0f\xac\x09" + struct.pack("<H", index) + ipn + key


PMF_AP_RSN = rsn_bytes(caps=0x00CC, mgmt=0x000FAC06)                         # PSK, PMF required
SHA256_AP_RSN = rsn_bytes(akm=(0x000FAC06,), caps=0x00CC, mgmt=0x000FAC06)  # PSK-SHA256 only, PMF required


def ap_kde(index, key, tx=True):
    return b"\xdd" + bytes([4 + 2 + len(key)]) + b"\x00\x0f\xac\x01" + bytes([index | (4 if tx else 0), 0]) + key


def ap_pad(data):
    if len(data) % 8 or len(data) < 16:
        data += b"\xdd" + bytes(-(len(data) + 1) % 8)
    while len(data) < 16:
        data += bytes(8)
    return data


class Authenticator:
    """The access point's half of the handshakes, with every knob a test needs to break something."""

    def __init__(self, pmk, gtk=b"G" * 16, gtk_index=1, rsn=AP_RSN, anonce=b"A" * 32, aa=AP, ver=2, igtk=None, expect_ie=None):
        self.pmk, self.gtk, self.gtk_index, self.rsn, self.anonce, self.aa = pmk, gtk, gtk_index, rsn, anonce, aa
        self.ver, self.igtk = ver, igtk                  # igtk = (index, ipn, key) or None
        self.expect_ie = expect_ie
        self.ptk = self.snonce = None
        self.replay = 0
        self.msg2_ok = self.msg4_ok = False
        self.group_ok = False

    def msg1(self):
        self.replay += 1
        return ap_frame(0x0088 | self.ver, self.replay, self.anonce, b"")

    def receive(self, frame):
        """Check a frame from the supplicant. Returns 'msg2', 'msg4', 'group2' or a reason it is bad."""
        info = struct.unpack_from(">H", frame, 5)[0]
        replay = struct.unpack_from(">Q", frame, 9)[0]
        if replay != self.replay:
            return "wrong replay counter"
        if info & 0x0008 and info & 0x0100 and not info & 0x0200:                 # message 2
            self.snonce = frame[17:49]
            self.ptk = ap_ptk(self.pmk, self.anonce, self.snonce, self.aa, sha256=self.ver == 3)
            if not hmac.compare_digest(ap_mic(self.ptk[:16], frame, self.ver), frame[81:97]):
                return "bad MIC"
            if info != 0x0108 | self.ver or frame[99:] != (self.expect_ie or net.build_rsn_ie()):
                return "bad message 2 contents"
            self.msg2_ok = True
            return "msg2"
        if info & 0x0008 and info & 0x0200:                                          # message 4
            if info != 0x0308 | self.ver or not hmac.compare_digest(ap_mic(self.ptk[:16], frame, self.ver), frame[81:97]) or frame[99:]:
                return "bad message 4"
            self.msg4_ok = True
            return "msg4"
        if not info & 0x0008:                                                         # group message 2
            if info != 0x0300 | self.ver or not hmac.compare_digest(ap_mic(self.ptk[:16], frame, self.ver), frame[81:97]):
                return "bad group message 2"
            self.group_ok = True
            return "group2"
        return "unexpected"

    def msg3(self, tamper=None, rsc=bytes(6) + bytes(2), anonce=None, info=None, data=None, bump=True):
        if bump:
            self.replay += 1
        info = 0x13C8 | self.ver if info is None else info
        default = self.rsn + ap_kde(self.gtk_index, self.gtk) + (ap_igtk_kde(*self.igtk) if self.igtk else b"")
        plain = ap_pad(data if data is not None else default)
        key_data = openssl_wrap(self.ptk[16:32], plain)
        frame = ap_frame(info, self.replay, anonce or self.anonce, key_data, rsc=rsc)
        frame = ap_sign(self.ptk[:16], frame, self.ver)
        if tamper == "mic":
            frame = frame[:81] + bytes(16) + frame[97:]
        return frame

    def group1(self, gtk, index=2, rsc=bytes(8), igtk=None):
        self.replay += 1
        plain = (ap_kde(index, gtk) if gtk else b"") + (ap_igtk_kde(*igtk) if igtk else b"")
        key_data = openssl_wrap(self.ptk[16:32], ap_pad(plain))
        return ap_sign(self.ptk[:16], ap_frame(0x1380 | self.ver | (index << 4), self.replay, bytes(32), key_data, key_length=0, rsc=rsc), self.ver)


PMK = net.wpa_psk(b"correct horse battery", b"EMPIRE")


def fixed_random(seed=b"S"):
    return lambda n: (seed * n)[:n]


class EapolTests(unittest.TestCase):
    def test_a_frame_built_here_parses_in_cube_net_and_back(self):
        built = ap_frame(0x13CA, 7, b"N" * 32, b"data!" * 4, key_length=16, rsc=b"\x01\x02\x03\x04\x05\x06\x00\x00")
        parsed = net.eapol_key_parse(built)
        self.assertEqual((parsed["info"], parsed["replay"], parsed["key_length"]), (0x13CA, 7, 16))
        self.assertEqual((parsed["nonce"], parsed["data"], parsed["rsc"][:6]), (b"N" * 32, b"data!" * 4, b"\x01\x02\x03\x04\x05\x06"))
        plain = ap_frame(0x13CA, 7, b"N" * 32, b"data!" * 4)
        self.assertEqual(net.eapol_key_build(2, 0x13CA, 16, 7, b"N" * 32, b"data!" * 4), plain)   # same layout byte for byte
        self.assertEqual(len(built), 99 + 20)

    def test_the_mic_is_hmac_sha1_over_the_frame_with_a_zeroed_mic_field(self):
        kck = os.urandom(16)
        signed = ap_sign(kck, ap_frame(0x010A, 1, b"S" * 32, b"ie"))
        self.assertEqual(net.eapol_key_build(2, 0x010A, 16, 1, b"S" * 32, b"ie", kck), signed)
        tampered = signed[:-1] + b"X"
        self.assertNotEqual(net.eapol_key_mic(kck, tampered), signed[81:97])

    def test_frames_that_are_not_rsn_key_frames_are_ignored(self):
        good = ap_frame(0x008A, 1, bytes(32), b"")
        self.assertIsNotNone(net.eapol_key_parse(good))
        self.assertIsNone(net.eapol_key_parse(good[:60]))                                      # truncated
        self.assertIsNone(net.eapol_key_parse(b"\x02\x00" + good[2:]))                       # EAP packet, not Key
        self.assertIsNone(net.eapol_key_parse(good[:4] + b"\xfe" + good[5:]))                # WPA (1) descriptor
        self.assertIsNone(net.eapol_key_parse(good[:2] + b"\xff\xff" + good[4:]))            # length beyond the frame
        lying = bytearray(good)
        lying[97:99] = b"\x01\x00"                                                             # key data longer than the frame
        self.assertIsNone(net.eapol_key_parse(bytes(lying)))
        self.assertEqual(len(net.eapol_key_parse(good + b"padding")["raw"]), len(good))       # trailing Ethernet padding dropped

    def test_key_data_elements_find_the_group_key_and_stop_at_padding(self):
        rsn, gtks = net.key_data_elements(AP_RSN + ap_kde(2, b"K" * 16) + b"\xdd\x00\x00\x00")
        self.assertEqual(rsn, AP_RSN[2:])
        self.assertEqual(gtks, [(2, True, b"K" * 16)])
        self.assertEqual(net.key_data_elements(b"\xdd\x00" + AP_RSN)[0], None)               # padding ends the list
        self.assertEqual(net.key_data_elements(b"\xdd\x40abc")[1], [])                       # truncated element


# ------------------------------------------------------------------ the handshake against the access point

def new_handshake(pmk=PMK, ap_rsn=None, random=fixed_random()):
    return net.Handshake(pmk, AP, STA, net.build_rsn_ie(), ap_rsn if ap_rsn is not None else net.parse_rsn(AP_RSN[2:]), random)


def kinds(actions):
    return [a[0] for a in actions]


class HandshakeTests(unittest.TestCase):
    def setUp(self):
        if not shutil_which("openssl"):
            self.skipTest("the access point side wraps keys with the openssl CLI")
        self.ap = Authenticator(PMK)
        self.sta = new_handshake()

    def through_message_2(self):
        actions = self.sta.receive(self.ap.msg1())
        self.assertEqual(kinds(actions), ["send"])
        self.assertEqual(self.ap.receive(actions[0][1]), "msg2")
        return actions

    def complete(self, **kw):
        self.through_message_2()
        return self.sta.receive(self.ap.msg3(**kw))

    def test_the_full_four_way_handshake_with_a_standard_authenticator(self):
        actions = self.complete()
        self.assertEqual(kinds(actions), ["send", "install_ptk", "install_gtk", "complete"])
        self.assertEqual(self.ap.receive(actions[0][1]), "msg4")
        self.assertEqual(actions[1][1], self.ap.ptk[32:48])                      # the temporal key, identical on both sides
        self.assertEqual(actions[2][1:], (1, b"G" * 16, bytes(6)))               # group key index, key, receive sequence
        self.assertTrue(self.ap.msg2_ok and self.ap.msg4_ok)

    def test_the_well_known_key_info_values_appear_on_the_wire(self):
        # 0x008a 0x010a 0x13ca 0x030a 0x1382 0x0302: the values every Wi-Fi capture shows for WPA2-CCMP
        self.assertEqual(struct.unpack_from(">H", self.ap.msg1(), 5)[0], 0x008A)
        self.sta = new_handshake()
        msg2 = self.sta.receive(self.ap.msg1())[0][1]
        self.assertEqual(struct.unpack_from(">H", msg2, 5)[0], 0x010A)
        self.ap.receive(msg2)
        msg3 = self.ap.msg3()
        self.assertEqual(struct.unpack_from(">H", msg3, 5)[0], 0x13CA)
        msg4 = self.sta.receive(msg3)[0][1]
        self.assertEqual(struct.unpack_from(">H", msg4, 5)[0], 0x030A)
        self.ap.receive(msg4)
        group2 = self.sta.receive(self.ap.group1(b"H" * 16))[0][1]
        self.assertEqual(struct.unpack_from(">H", group2, 5)[0], 0x0302)

    def test_a_group_key_update_after_the_handshake_is_installed_and_acknowledged(self):
        self.ap.receive(self.complete()[0][1])
        actions = self.sta.receive(self.ap.group1(b"H" * 16, index=2, rsc=b"\x09\x00\x00\x00\x00\x00\x00\x00"))
        self.assertEqual(kinds(actions), ["send", "install_gtk", "group_done"])
        self.assertEqual(actions[1][1:], (2, b"H" * 16, b"\x09\x00\x00\x00\x00\x00"))
        self.assertEqual(self.ap.receive(actions[0][1]), "group2")

    def test_a_group_update_before_the_handshake_is_finished_is_ignored(self):
        self.through_message_2()
        self.assertEqual(kinds(self.sta.receive(self.ap.group1(b"H" * 16))), ["ignore"])

    def test_the_wrong_passphrase_is_visible_to_the_authenticator_not_the_supplicant(self):
        wrong = new_handshake(pmk=net.wpa_psk(b"not the password", b"EMPIRE"))
        msg2 = wrong.receive(self.ap.msg1())[0][1]
        self.assertEqual(self.ap.receive(msg2), "bad MIC")          # the AP rejects message 2 and never sends message 3

    def test_a_forged_or_damaged_message_3_is_ignored_and_does_not_install_anything(self):
        self.through_message_2()
        for frame in (self.ap.msg3(tamper="mic"),):
            self.assertEqual(kinds(self.sta.receive(frame)), ["ignore"])
        damaged = bytearray(self.ap.msg3())
        damaged[110] ^= 0x01                                         # inside the wrapped key data, MIC no longer matches
        self.assertEqual(kinds(self.sta.receive(bytes(damaged))), ["ignore"])
        self.assertEqual(kinds(self.sta.receive(self.ap.msg3(bump=True))), ["send", "install_ptk", "install_gtk", "complete"])

    def test_message_3_must_not_change_the_authenticator_nonce(self):
        self.through_message_2()
        self.assertEqual(kinds(self.sta.receive(self.ap.msg3(anonce=b"B" * 32))), ["ignore"])

    def test_a_replayed_message_3_is_ignored_unless_it_is_a_retransmission_after_completion(self):
        self.through_message_2()
        msg3 = self.ap.msg3()
        self.assertEqual(kinds(self.sta.receive(msg3)), ["send", "install_ptk", "install_gtk", "complete"])
        self.assertEqual(kinds(self.sta.receive(msg3)), ["ignore"])         # same counter: a replay

    def test_a_retransmitted_message_3_gets_message_4_again_but_never_reinstalls_keys(self):
        self.ap.receive(self.complete()[0][1])
        again = self.sta.receive(self.ap.msg3())                              # the AP missed message 4 and resent
        self.assertEqual(kinds(again), ["send"])
        self.assertEqual(self.ap.receive(again[0][1]), "msg4")

    def test_message_3_without_the_install_flag_is_ignored(self):
        self.through_message_2()
        self.assertEqual(kinds(self.sta.receive(self.ap.msg3(info=0x138A))), ["ignore"])

    def test_a_downgrade_in_message_3_ends_the_connection(self):
        tkip = AP_RSN[:4] + b"\x00\x0f\xac\x02" + AP_RSN[8:]                  # group cipher changed to TKIP
        self.through_message_2()
        actions = self.sta.receive(self.ap.msg3(data=tkip + ap_kde(1, b"G" * 16)))
        self.assertEqual(kinds(actions), ["fail"])
        self.assertIn("downgrade", actions[0][1])

    def test_message_3_without_a_group_key_ends_the_connection(self):
        self.through_message_2()
        self.assertEqual(kinds(self.sta.receive(self.ap.msg3(data=AP_RSN))), ["fail"])
        self.assertEqual(kinds(self.sta.receive(self.ap.msg3(data=AP_RSN + ap_kde(1, b"G" * 32)))), ["fail"])   # TKIP-sized key

    def test_message_3_before_message_1_is_ignored(self):
        self.through_message_2()
        fresh = new_handshake()
        self.assertEqual(kinds(fresh.receive(self.ap.msg3())), ["ignore"])

    def test_old_key_descriptor_versions_are_refused_with_a_reason(self):
        actions = self.sta.receive(ap_frame(0x0089, 1, b"A" * 32, b""))
        self.assertEqual(kinds(actions), ["fail"])
        self.assertIn("version 1", actions[0][1])

    def test_garbage_and_requests_are_ignored(self):
        for frame in (b"", b"\x02\x03", ap_frame(0x0108, 1, bytes(32), b""), ap_frame(0x0A8A, 1, bytes(32), b"")):
            self.assertEqual(kinds(self.sta.receive(frame)), ["ignore"], frame)

    def test_a_pairwise_key_refresh_after_completion_reconnects_instead_of_misbehaving(self):
        self.ap.receive(self.complete()[0][1])
        self.assertEqual(kinds(self.sta.receive(self.ap.msg1())), ["fail"])

    def test_a_restarted_handshake_before_completion_uses_a_fresh_nonce(self):
        counter = iter(b"123456789")
        sta = new_handshake(random=lambda n: bytes([next(counter)]) * n)
        first = sta.receive(self.ap.msg1())[0][1]
        second = sta.receive(self.ap.msg1())[0][1]
        self.assertNotEqual(first[17:49], second[17:49])
        self.assertEqual(self.ap.receive(second), "msg2")


# ------------------------------------------------------------------ netlink

def nlmsg(kind, seq, payload=b"", flags=0):
    return struct.pack("=IHHII", 16 + len(payload), kind, flags, seq, 0) + payload + bytes(-len(payload) % 4)


def genl(cmd, attrs=b""):
    return struct.pack("=BBH", cmd, 1, 0) + attrs


class FakeKernel(threading.Thread):
    """The other end of a socketpair: answers each request with whatever handler(request, seq) returns."""

    def __init__(self, sock, handler):
        super().__init__(daemon=True)
        self.sock, self.handler, self.requests = sock, handler, []

    def run(self):
        while True:
            try:
                data = self.sock.recv(65536)
            except OSError:
                return
            if not data:
                return
            seq = struct.unpack_from("=I", data, 8)[0]
            self.requests.append(data)
            for reply in self.handler(data, seq):
                self.sock.send(reply)


class NetlinkTests(unittest.TestCase):
    def pair(self, handler):
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        kernel = FakeKernel(b, handler)
        kernel.start()
        return net.Netlink(sock=a, timeout=3), kernel

    def test_attribute_encoding_pads_to_four_bytes_and_parses_back(self):
        blob = net.nla(3, b"abc") + net.nla_u32(4, 0xDEADBEEF) + net.nla_u8(5, 7) + net.nla(6)
        self.assertEqual(len(blob), 8 + 8 + 8 + 4)
        parsed = net.parse_attrs(blob)
        self.assertEqual(parsed, {3: b"abc", 4: struct.pack("=I", 0xDEADBEEF), 5: b"\x07", 6: b""})
        nested = net.nla_nest(9, net.nla_u8(1, 1), net.nla_u8(2, 2))
        self.assertEqual(struct.unpack_from("=HH", nested)[1], 9 | net.NLA_F_NESTED)
        self.assertEqual(net.parse_attrs(net.parse_attrs(nested)[9]), {1: b"\x01", 2: b"\x02"})

    def test_attribute_parsing_survives_truncation_and_nonsense(self):
        good = net.nla_u32(1, 5)
        self.assertEqual(net.parse_attrs(good + b"\x08\x00"), {1: struct.pack("=I", 5)})
        self.assertEqual(net.parse_attrs(b"\x02\x00\x01\x00"), {})                 # length below the header size
        self.assertEqual(net.parse_attrs(b"\xff\x00\x01\x00abcd"), {})             # length beyond the data
        self.assertEqual(net.parse_attrs(b""), {})

    def test_a_request_returns_the_replies_before_the_ack(self):
        def handler(data, seq):
            return [nlmsg(33, seq, genl(7, net.nla_u32(1, 42))), nlmsg(net.NLMSG_ERROR, seq, struct.pack("=i", 0) + data[:16])]
        netlink, kernel = self.pair(handler)
        self.assertEqual(netlink.request(33, 5, net.nla_u32(3, 2)), [(7, {1: struct.pack("=I", 42)})])
        header = struct.unpack_from("=IHHII", kernel.requests[0])
        self.assertEqual((header[1], header[2]), (33, net.NLM_F_REQUEST | net.NLM_F_ACK))
        self.assertEqual(kernel.requests[0][16], 5)
        self.assertEqual(net.parse_attrs(kernel.requests[0][20:]), {3: struct.pack("=I", 2)})

    def test_a_kernel_error_becomes_an_oserror_with_the_errno(self):
        netlink, _ = self.pair(lambda data, seq: [nlmsg(net.NLMSG_ERROR, seq, struct.pack("=i", -errno.EBUSY) + data[:16])])
        with self.assertRaises(OSError) as caught:
            netlink.request(33, 5)
        self.assertEqual(caught.exception.errno, errno.EBUSY)

    def test_a_dump_collects_every_part_until_done_and_skips_other_sequence_numbers(self):
        def handler(data, seq):
            first = nlmsg(33, seq, genl(34, net.nla_u32(1, 1)), flags=2) + nlmsg(33, seq, genl(34, net.nla_u32(1, 2)), flags=2)
            return [nlmsg(33, seq + 100, genl(34, net.nla_u32(1, 99))), first,
                    nlmsg(33, seq, genl(34, net.nla_u32(1, 3)), flags=2), nlmsg(net.NLMSG_DONE, seq, struct.pack("=i", 0))]
        netlink, kernel = self.pair(handler)
        replies = netlink.request(33, 32, dump=True)
        self.assertEqual([struct.unpack("=I", attrs[1])[0] for _, attrs in replies], [1, 2, 3])
        self.assertEqual(struct.unpack_from("=IHHII", kernel.requests[0])[2], net.NLM_F_REQUEST | net.NLM_F_DUMP)

    def test_sequence_numbers_advance(self):
        netlink, kernel = self.pair(lambda data, seq: [nlmsg(net.NLMSG_ERROR, seq, struct.pack("=i", 0) + data[:16])])
        netlink.request(33, 1)
        netlink.request(33, 1)
        seqs = [struct.unpack_from("=I", request, 8)[0] for request in kernel.requests]
        self.assertEqual(seqs[1], seqs[0] + 1)

    def test_family_resolution_reads_the_id_and_multicast_groups(self):
        group = lambda name, ident: net.nla_nest(1, net.nla(1, name.encode() + b"\0"), net.nla_u32(2, ident))
        reply = (net.nla(2, b"nl80211\0") + net.nla(1, struct.pack("=H", 33)) +
                 net.nla_nest(7, group("scan", 20).replace(struct.pack("=H", 1 | net.NLA_F_NESTED), struct.pack("=H", 1 | net.NLA_F_NESTED)),
                              net.nla_nest(2, net.nla(1, b"mlme\0"), net.nla_u32(2, 22))))
        netlink, kernel = self.pair(lambda data, seq: [nlmsg(net.GENL_ID_CTRL, seq, genl(1, reply)),
                                                       nlmsg(net.NLMSG_ERROR, seq, struct.pack("=i", 0) + data[:16])])
        family, groups = net.resolve_nl80211(netlink)
        self.assertEqual(family, 33)
        self.assertEqual(groups["mlme"], 22)
        self.assertEqual(struct.unpack_from("=B", kernel.requests[0], 16)[0], 3)       # CTRL_CMD_GETFAMILY

    def test_an_unknown_family_is_an_error(self):
        netlink, _ = self.pair(lambda data, seq: [nlmsg(net.NLMSG_ERROR, seq, struct.pack("=i", -errno.ENOENT) + data[:16])])
        with self.assertRaises(OSError):
            net.resolve_nl80211(netlink)


def bss_attrs(bssid=AP, freq=5220, ssid=b"EMPIRE", rsn=AP_RSN, signal=-5900, capability=0x0411, with_ies=True):
    ies = (b"\x00" + bytes([len(ssid)]) + ssid + rsn) if with_ies else b""
    return (net.nla(net.BSS_BSSID, bssid) + net.nla_u32(net.BSS_FREQUENCY, freq) + net.nla(net.BSS_CAPABILITY, struct.pack("=H", capability))
            + net.nla(net.BSS_SIGNAL_MBM, struct.pack("=i", signal)) + net.nla(net.BSS_IES, ies))


class ScanParsingTests(unittest.TestCase):
    def test_a_scan_result_is_decoded(self):
        bss = net.parse_bss(bss_attrs())
        self.assertEqual((bss["bssid"], bss["freq"], bss["signal"], bss["ssid"]), ("c0:94:35:ad:ec:aa", 5220, -59.0, b"EMPIRE"))
        self.assertEqual(bss["security"], "[WPA2-PSK-CCMP][ESS]")
        self.assertIsNone(net.unsupported_reason(bss["rsn"]))

    def test_hidden_and_open_networks_are_reported_honestly(self):
        hidden = net.parse_bss(bss_attrs(ssid=b"\0\0\0\0", rsn=b""))
        self.assertEqual(hidden["ssid"], b"")
        self.assertIsNone(hidden["rsn"])
        self.assertIn("no WPA2", net.unsupported_reason(hidden["rsn"]))
        self.assertIsNone(net.parse_bss(net.nla(net.BSS_BSSID, b"\x01\x02")))     # short BSSID: unusable
        self.assertIsNone(net.parse_bss(b""))

    def test_missing_optional_fields_do_not_break_parsing(self):
        bss = net.parse_bss(net.nla(net.BSS_BSSID, AP))
        self.assertEqual((bss["freq"], bss["signal"], bss["ssid"], bss["security"]), (0, None, b"", ""))

    def test_beacon_elements_are_used_when_there_are_no_probe_response_elements(self):
        attrs = net.nla(net.BSS_BSSID, AP) + net.nla(net.BSS_BEACON_IES, b"\x00\x03abc")
        self.assertEqual(net.parse_bss(attrs)["ssid"], b"abc")

    def test_scan_entries_are_json_safe_and_strip_control_characters(self):
        entry = net.scan_entry(net.parse_bss(bss_attrs(ssid="café\x1b[31m".encode())))
        self.assertEqual(entry["ssid"], "café[31m")
        json.dumps(entry)
        self.assertEqual(entry["signal_dbm"], -59)
        self.assertEqual(set(entry), {"bssid", "frequency_mhz", "signal_dbm", "security", "ssid"})
        self.assertEqual(net.scan_entry(net.parse_bss(net.nla(net.BSS_BSSID, AP)))["signal_dbm"], -127)


# ------------------------------------------------------------------ the driver's wire format

class Recorder:
    """Stands in for the command socket: records requests, answers from a script."""

    def __init__(self):
        self.sent, self.script = [], {}

    def request(self, family, cmd, attrs=b"", dump=False, version=0):
        self.sent.append((family, cmd, parse_all(attrs), dump))
        result = self.script.get(cmd, [])
        if isinstance(result, Exception):
            raise result
        return result

    def close(self):
        pass


def parse_all(attrs):
    return net.parse_attrs(attrs)


def make_driver(recorder=None, events=None, eapol=None):
    driver = net.Nl80211Driver.__new__(net.Nl80211Driver)
    driver.iface, driver.ifindex, driver.mac, driver.family = "wlan0", 2, STA, 33
    driver.commands = recorder or Recorder()
    driver.events, driver.eapol = events, eapol
    return driver


class DriverEncodingTests(unittest.TestCase):
    def setUp(self):
        self.rec = Recorder()
        self.driver = make_driver(self.rec)

    def last(self):
        family, cmd, attrs, dump = self.rec.sent[-1]
        self.assertEqual(family, 33)
        self.assertEqual(struct.unpack("=I", attrs[net.A_IFINDEX])[0], 2)
        return cmd, attrs

    def test_connect_asks_for_wpa2_psk_ccmp_with_the_port_held_closed(self):
        bss = net.parse_bss(bss_attrs())
        self.driver.connect(bss, b"EMPIRE")
        cmd, a = self.last()
        self.assertEqual(cmd, net.CMD_CONNECT)
        self.assertEqual(a[net.A_SSID], b"EMPIRE")
        self.assertEqual(a[net.A_MAC], AP)
        self.assertEqual(struct.unpack("=I", a[net.A_WIPHY_FREQ])[0], 5220)
        self.assertEqual(a[net.A_IE], net.build_rsn_ie())
        self.assertEqual(struct.unpack("=I", a[net.A_AUTH_TYPE])[0], 0)           # open system authentication
        self.assertEqual(struct.unpack("=I", a[net.A_WPA_VERSIONS])[0], 2)
        self.assertEqual(struct.unpack("=I", a[net.A_CIPHER_PAIRWISE])[0], CCMP)
        self.assertEqual(struct.unpack("=I", a[net.A_CIPHER_GROUP])[0], CCMP)
        self.assertEqual(struct.unpack("=I", a[net.A_AKM])[0], PSK)
        self.assertEqual(struct.unpack("=I", a[net.A_USE_MFP])[0], 0)
        self.assertIn(net.A_PRIVACY, a)
        self.assertIn(net.A_CONTROL_PORT, a)       # without this the kernel would open the port before the handshake

    def test_the_pairwise_key_goes_in_with_the_access_points_address_and_no_empty_sequence(self):
        self.driver.new_key(0, b"T" * 16, bssid="c0:94:35:ad:ec:aa")
        cmd, a = self.last()
        self.assertEqual(cmd, net.CMD_NEW_KEY)
        self.assertEqual(a[net.A_MAC], AP)
        key = net.parse_attrs(a[net.A_KEY])
        self.assertEqual(key[net.K_DATA], b"T" * 16)
        self.assertEqual(key[net.K_IDX], b"\x00")
        self.assertEqual(struct.unpack("=I", key[net.K_CIPHER])[0], CCMP)
        self.assertNotIn(net.K_SEQ, key)           # an empty sequence attribute makes the kernel answer EINVAL

    def test_the_group_key_carries_its_index_and_receive_sequence_and_no_address(self):
        self.driver.new_key(2, b"G" * 16, b"\x01\x02\x03\x04\x05\x06")
        _, a = self.last()
        self.assertNotIn(net.A_MAC, a)
        key = net.parse_attrs(a[net.A_KEY])
        self.assertEqual((key[net.K_IDX], key[net.K_SEQ]), (b"\x02", b"\x01\x02\x03\x04\x05\x06"))

    def test_default_key_delete_and_authorize(self):
        self.driver.default_group_key(1)
        cmd, a = self.last()
        key = net.parse_attrs(a[net.A_KEY])
        self.assertEqual((cmd, key[net.K_IDX], net.K_DEFAULT in key, net.K_DEFAULT_TYPES in key), (net.CMD_SET_KEY, b"\x01", True, True))
        self.driver.delete_group_key(3)
        cmd, a = self.last()
        self.assertEqual((cmd, net.parse_attrs(a[net.A_KEY])[net.K_IDX]), (net.CMD_DEL_KEY, b"\x03"))
        self.driver.authorize("c0:94:35:ad:ec:aa")
        cmd, a = self.last()
        self.assertEqual(cmd, net.CMD_SET_STATION)
        self.assertEqual(a[net.A_MAC], AP)
        self.assertEqual(struct.unpack("=II", a[net.A_STA_FLAGS2]), (2, 2))      # mask and value: the AUTHORIZED flag

    def test_power_save_and_country_are_encoded_as_the_kernel_expects(self):
        self.driver.set_power_save(False)
        cmd, a = self.last()
        self.assertEqual((cmd, struct.unpack("=I", a[net.A_PS_STATE])[0]), (net.CMD_SET_POWER_SAVE, 0))
        self.driver.set_power_save(True)
        self.assertEqual(struct.unpack("=I", self.last()[1][net.A_PS_STATE])[0], 1)
        self.driver.set_country("CA")
        family, cmd, attrs, _ = self.rec.sent[-1]
        self.assertEqual((cmd, attrs[net.A_REG_ALPHA2]), (net.CMD_REQ_SET_REG, b"CA\0"))
        self.assertNotIn(net.A_IFINDEX, attrs)                                 # the regulatory request is global
        self.rec.script[net.CMD_GET_POWER_SAVE] = [(62, {net.A_PS_STATE: struct.pack("=I", 1)})]
        self.assertIs(self.driver.power_save(), True)
        self.rec.script[net.CMD_GET_POWER_SAVE] = OSError(errno.EOPNOTSUPP, "no")
        self.assertIsNone(self.driver.power_save())
        self.rec.script[net.CMD_GET_POWER_SAVE] = []
        self.assertIsNone(self.driver.power_save())

    def test_disconnect_says_leaving(self):
        self.driver.disconnect()
        cmd, a = self.last()
        self.assertEqual((cmd, struct.unpack("=H", a[net.A_REASON_CODE])[0]), (net.CMD_DISCONNECT, 3))

    def test_a_scan_always_includes_the_wildcard_and_the_saved_names(self):
        self.driver.trigger_scan([b"EMPIRE", b"Cafe"])
        cmd, a = self.last()
        self.assertEqual(cmd, net.CMD_TRIGGER_SCAN)
        entries = list(net.parse_attrs(a[net.A_SCAN_SSIDS]).values())
        self.assertEqual(entries, [b"", b"EMPIRE", b"Cafe"])

    def test_get_scan_decodes_the_dump_and_skips_unusable_entries(self):
        self.rec.script[net.CMD_GET_SCAN] = [(34, {net.A_BSS: bss_attrs()}), (34, {net.A_BSS: b""}), (34, {}),
                                             (34, {net.A_BSS: bss_attrs(bssid=bytes(5) + b"\x01", ssid=b"Other")})]
        found = self.driver.get_scan()
        self.assertEqual([b["ssid"] for b in found], [b"EMPIRE", b"Other"])
        self.assertTrue(self.rec.sent[-1][3])                                  # asked as a dump

    def test_signal_comes_from_the_station_table_and_zero_means_unknown(self):
        sta_info = lambda value: [(17, {net.A_STA_INFO: net.nla(net.STA_SIGNAL, struct.pack("b", value))})]
        self.rec.script[net.CMD_GET_STATION] = sta_info(-61)
        self.assertEqual(self.driver.signal("c0:94:35:ad:ec:aa"), -61)
        self.rec.script[net.CMD_GET_STATION] = sta_info(0)               # what rtw88 over USB reports
        self.assertIsNone(self.driver.signal("c0:94:35:ad:ec:aa"))
        self.rec.script[net.CMD_GET_STATION] = OSError(errno.ENOENT, "gone")
        self.assertIsNone(self.driver.signal("c0:94:35:ad:ec:aa"))
        self.rec.script[net.CMD_GET_STATION] = []
        self.assertIsNone(self.driver.signal("c0:94:35:ad:ec:aa"))

    def test_eapol_goes_out_as_an_ethernet_frame_from_our_address(self):
        sent = []
        self.driver.eapol = types_namespace(send=sent.append)
        self.driver.send_eapol("c0:94:35:ad:ec:aa", b"PAYLOAD")
        self.assertEqual(sent, [AP + STA + b"\x88\x8e" + b"PAYLOAD"])


def types_namespace(**kw):
    import types
    return types.SimpleNamespace(**kw)


class PacketSocket:
    """A readable fake of the AF_PACKET socket: frames are written to a socketpair as pkttype byte + frame."""

    def __init__(self):
        self.a, self.b = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.a.setblocking(False)

    def push(self, frame, pkttype=0):
        self.b.send(bytes([pkttype]) + frame)

    def fileno(self):
        return self.a.fileno()

    def recvfrom(self, size):
        data = self.a.recv(size)
        return data[1:], ("wlan0", 0x888E, data[0], 1, b"")

    def close(self):
        self.a.close()
        self.b.close()


class DriverEventTests(unittest.TestCase):
    def setUp(self):
        a, self.kernel = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(a.close)
        self.addCleanup(self.kernel.close)
        self.packets = PacketSocket()
        self.addCleanup(self.packets.close)
        self.driver = make_driver(events=net.Netlink(sock=a, timeout=0.0), eapol=self.packets)

    def event(self, cmd, *attrs, ifindex=2):
        self.kernel.send(nlmsg(33, 0, genl(cmd, net.nla_u32(net.A_IFINDEX, ifindex) + b"".join(attrs))))

    def test_nothing_happening_returns_an_empty_list(self):
        self.assertEqual(self.driver.wait(0), [])

    def test_scan_connect_and_disconnect_events_are_translated(self):
        self.event(net.CMD_NEW_SCAN_RESULTS)
        self.event(net.CMD_SCAN_ABORTED)
        self.event(net.CMD_CONNECT, net.nla(net.A_MAC, AP), net.nla(net.A_STATUS_CODE, struct.pack("=H", 0)))
        self.event(net.CMD_CONNECT, net.nla(net.A_MAC, AP), net.nla(net.A_STATUS_CODE, struct.pack("=H", 17)))
        self.event(net.CMD_CONNECT, net.nla(net.A_MAC, AP), net.nla(net.A_TIMED_OUT))
        self.event(net.CMD_DISCONNECT, net.nla(net.A_REASON_CODE, struct.pack("=H", 15)), net.nla(net.A_DISCONNECTED_BY_AP))
        self.event(net.CMD_DISCONNECT)
        self.assertEqual(self.driver.wait(0.5), [
            ("scan_done", False), ("scan_done", True), ("connected", "c0:94:35:ad:ec:aa", 0),
            ("connected", "c0:94:35:ad:ec:aa", 17), ("connected", "c0:94:35:ad:ec:aa", -1),
            ("disconnected", 15, True), ("disconnected", 0, False)])

    def test_events_for_other_interfaces_and_unknown_commands_are_ignored(self):
        self.event(net.CMD_CONNECT, net.nla(net.A_MAC, AP), ifindex=9)
        self.event(99)
        self.event(net.CMD_NEW_SCAN_RESULTS, ifindex=9)
        self.assertEqual(self.driver.wait(0.2), [])

    def test_eapol_frames_arrive_with_their_source_and_outgoing_copies_are_dropped(self):
        frame = AP_RSN  # any payload
        self.packets.push(STA + AP + b"\x88\x8e" + frame)                 # received from the AP (STA is destination)
        self.packets.push(AP + STA + b"\x88\x8e" + b"ours", pkttype=net.PACKET_OUTGOING)
        self.packets.push(STA + AP + b"\x08\x00" + b"not eapol")
        self.packets.push(b"short")
        self.assertEqual(self.driver.wait(0.5), [("eapol", net.mac_str(AP), frame)])

    def test_an_overrun_is_reported(self):
        with mock.patch.object(self.driver.events, "read_events", side_effect=OSError(errno.ENOBUFS, "full")):
            self.kernel.send(b"x")
            self.assertEqual(self.driver.wait(0.5), [("overrun",)])


# ------------------------------------------------------------------ the station, against a simulated radio and access point

class FakeAP:
    def __init__(self, ssid=b"EMPIRE", bssid="c0:94:35:ad:ec:aa", signal=-59.0, freq=5220, passphrase=b"correct horse battery",
                 rsn=None, refuse=False, answers=True, join_delay=0.3, rsn_bytes=None):
        self.ssid, self.bssid, self.signal, self.freq = ssid, bssid, signal, freq
        self.rsn_bytes = rsn_bytes or AP_RSN
        self.pmk = net.wpa_psk(passphrase, ssid)
        self.rsn = rsn if rsn is not None else net.parse_rsn(AP_RSN[2:])
        self.refuse, self.answers, self.join_delay = refuse, answers, join_delay
        self.auth = None

    def scan_entry(self):
        return {"bssid": self.bssid, "freq": self.freq, "signal": self.signal, "ssid": self.ssid, "rsn": self.rsn,
                "security": net.security_string(net.parse_ies(AP_RSN), 0x0411)}


class FakeRadio:
    """Implements the driver interface of cube-net. Time is whatever the test says it is."""
    mac = STA

    def __init__(self, aps, scan_delay=2.0):
        self.aps, self.scan_delay = aps, scan_delay
        self.now = 0.0
        self.queue = []                 # (due time, event)
        self.calls = []                 # (name, details)
        self.joined = None
        self.scan_error = None
        self.scan_ssid_limit = None
        self.get_scan_error = None
        self.key_error = None
        self.signal_value = -58
        self.deauth_after_msg2 = None
        self.power_save_error = self.country_error = None
        self.last_connect = None

    def at(self, delay, event):
        self.queue.append((self.now + delay, event))

    def record(self, name, *details):
        self.calls.append((name, details, self.now))

    def names(self):
        return [c[0] for c in self.calls]

    def count(self, name):
        return self.names().count(name)

    # --- the driver interface
    def close(self):
        self.record("close")

    def bring_up(self):
        self.record("bring_up")

    def wait(self, timeout):
        due = [item for item in self.queue if item[0] <= self.now]
        self.queue = [item for item in self.queue if item[0] > self.now]
        return [event for _, event in sorted(due, key=lambda item: item[0])]

    def disconnect(self):
        self.record("disconnect")
        if self.joined is None:
            raise OSError(errno.ENOTCONN, "not connected")
        self.joined = None
        self.at(0.05, ("disconnected", 3, False))

    def trigger_scan(self, ssids):
        self.record("trigger_scan", tuple(ssids))
        if self.scan_error is not None:
            raise self.scan_error
        if self.scan_ssid_limit is not None and len(ssids) + 1 > self.scan_ssid_limit:
            raise OSError(errno.EINVAL, "too many ssids")
        self.at(self.scan_delay, ("scan_done", False))

    def get_scan(self):
        if self.get_scan_error is not None:
            raise self.get_scan_error
        return [ap.scan_entry() for ap in self.aps]

    def connect(self, bss, ssid, akm=PSK, pmf=False):
        self.record("connect", bss["bssid"], ssid)
        self.last_connect = {"akm": akm, "pmf": pmf}
        ap = next(ap for ap in self.aps if ap.bssid == bss["bssid"])
        self.joined = ap
        if ap.refuse:
            self.at(ap.join_delay, ("connected", ap.bssid, 17))
            self.joined = None
            return
        self.at(ap.join_delay, ("connected", ap.bssid, 0))
        if ap.answers:
            ap.auth = Authenticator(ap.pmk, aa=net.mac_bytes(ap.bssid), rsn=ap.rsn_bytes, ver=3 if akm == 0x000FAC06 else 2,
                                    igtk=(4, b"\x01\x00\x00\x00\x00\x00", b"I" * 16) if pmf else None,
                                    expect_ie=net.build_rsn_ie(akm, pmf))
            self.at(ap.join_delay + 0.6, ("eapol", ap.bssid, ap.auth.msg1()))

    def send_eapol(self, destination, payload):
        self.record("send_eapol", destination, payload)
        ap = self.joined
        if ap is None or ap.auth is None:
            return
        verdict = ap.auth.receive(payload)
        if verdict == "msg2":
            self.at(0.1, ("eapol", ap.bssid, ap.auth.msg3()))
            if self.deauth_after_msg2 is not None:
                self.at(self.deauth_after_msg2, ("disconnected", 15, True))
        elif verdict == "bad MIC":
            self.at(3.0, ("disconnected", 15, True))              # a real AP gives up and deauthenticates
            self.joined = None

    def new_key(self, index, key, seq=b"", bssid=None):
        self.record("new_key", index, key, seq, bssid)
        if self.key_error is not None:
            raise self.key_error

    def default_group_key(self, index):
        self.record("default_group_key", index)

    def delete_group_key(self, index):
        self.record("delete_group_key", index)
        raise OSError(errno.ENOENT, "no such key")

    def authorize(self, bssid):
        self.record("authorize", bssid)

    def new_igtk(self, index, key, ipn):
        self.record("new_igtk", index, key, ipn)
        if self.key_error is not None:
            raise self.key_error

    def default_igtk(self, index):
        self.record("default_igtk", index)

    def set_power_save(self, enabled):
        self.record("set_power_save", enabled)
        if self.power_save_error is not None:
            raise self.power_save_error

    def set_country(self, alpha2):
        self.record("set_country", alpha2)
        if self.country_error is not None:
            raise self.country_error

    def signal(self, bssid):
        return self.signal_value


class Bench:
    def __init__(self, aps, profiles=(("EMPIRE", "correct horse battery"),), start=True, **radio_kw):
        self.radio = FakeRadio(aps, **radio_kw)
        self.profiles = [(ssid, net.wpa_psk(secret.encode(), ssid.encode()).hex()) for ssid, secret in profiles]
        self.t = 100.0
        self.logs = []
        self.station = net.Station(self.radio, lambda: list(self.profiles), self.logs.append, now=lambda: self.t, pause=lambda s: None)
        self.pending = []
        self.history = []
        if start:
            self.station.reload(self.t)

    @property
    def state(self):
        return self.station.snapshot()["state"]

    def run(self, seconds, step=0.25):
        for _ in range(int(seconds / step)):
            self.t += step
            self.radio.now = self.t
            self.station.tick(self.t, self.radio.wait(0))
            if not self.history or self.history[-1][1] != self.state:
                self.history.append((self.t, self.state))
        return self

    def command(self, name, *args):
        done, box = threading.Event(), {}
        self.station.commands.put((name, args, done, box))
        return done, box

    def states(self):
        return [state for _, state in self.history]


class StationTests(unittest.TestCase):
    def setUp(self):
        if not shutil_which("openssl"):
            self.skipTest("the access point side wraps keys with the openssl CLI")

    def test_it_scans_joins_runs_the_handshake_and_reaches_completed(self):
        bench = Bench([FakeAP()]).run(12)
        self.assertEqual(bench.state, "COMPLETED")
        self.assertEqual(bench.station.snapshot()["ssid"], "EMPIRE")
        self.assertEqual(bench.states()[:5], ["SCANNING", "AUTHENTICATING", "ASSOCIATED", "4WAY_HANDSHAKE", "COMPLETED"])

    def test_keys_are_installed_after_message_4_and_the_port_opens_last(self):
        bench = Bench([FakeAP()]).run(12)
        names = bench.radio.names()
        send2, send4 = [i for i, n in enumerate(names) if n == "send_eapol"][:2]
        first_key = names.index("new_key")
        self.assertLess(send4, first_key)                                  # message 4 leaves before any key is installed
        self.assertLess(names.index("new_key", first_key + 1), names.index("authorize"))
        keys = [c for c in bench.radio.calls if c[0] == "new_key"]
        ap = bench.radio.aps[0]
        self.assertEqual(keys[0][1][:1] + (keys[0][1][1],), (0, ap.auth.ptk[32:48]))          # pairwise: index 0, the derived TK
        self.assertEqual(keys[0][1][3], "c0:94:35:ad:ec:aa")
        self.assertEqual(keys[1][1][:3], (1, b"G" * 16, bytes(6)))
        self.assertEqual([c[1] for c in bench.radio.calls if c[0] == "authorize"], [("c0:94:35:ad:ec:aa",)])
        self.assertEqual(bench.radio.count("default_group_key"), 1)

    def test_old_group_keys_are_cleared_before_joining_even_when_the_kernel_says_none_exist(self):
        bench = Bench([FakeAP()]).run(12)
        self.assertEqual(bench.radio.count("delete_group_key"), 3)
        self.assertEqual(bench.state, "COMPLETED")

    def test_it_asks_for_the_saved_names_so_hidden_networks_are_found(self):
        bench = Bench([FakeAP()]).run(3)
        scan = next(c for c in bench.radio.calls if c[0] == "trigger_scan")
        self.assertEqual(scan[1], ((b"EMPIRE",),))

    def test_power_save_is_turned_off_after_every_successful_connection(self):
        bench = Bench([FakeAP()]).run(12)
        self.assertEqual([c[1] for c in bench.radio.calls if c[0] == "set_power_save"], [(False,)])
        self.assertLess(bench.radio.names().index("authorize"), bench.radio.names().index("set_power_save"))
        bench.radio.joined = None
        bench.radio.at(0, ("disconnected", 4, True))
        bench.run(14)
        self.assertEqual(bench.radio.count("set_power_save"), 2)               # again on the new association

    def test_power_save_can_be_left_on_by_configuration(self):
        bench = Bench([FakeAP()])
        bench.station.options["power_save"] = True
        bench.run(12)
        self.assertEqual([c[1] for c in bench.radio.calls if c[0] == "set_power_save"], [(True,)])

    def test_a_radio_that_refuses_the_power_save_change_keeps_its_link(self):
        bench = Bench([FakeAP()])
        bench.radio.power_save_error = OSError(errno.EOPNOTSUPP, "not supported")
        bench.run(12)
        self.assertEqual(bench.state, "COMPLETED")
        self.assertTrue(any("could not change power save" in line for line in bench.logs))

    def test_the_country_is_set_once_at_start_only_when_configured(self):
        quiet = Bench([FakeAP()], start=False)
        quiet.station.options["country"] = None
        quiet.radio.wait = lambda timeout: (__import__("time").sleep(0.02), [])[1]
        quiet.station.now = __import__("time").monotonic
        threading.Timer(0.2, quiet.station.stop).start()
        quiet.station.run()
        self.assertEqual(quiet.radio.count("set_country"), 0)
        bench = Bench([FakeAP()], start=False)
        bench.station.options["country"] = "CA"
        bench.radio.wait = lambda timeout: (__import__("time").sleep(0.02), [])[1]
        bench.station.now = __import__("time").monotonic
        threading.Timer(0.2, bench.station.stop).start()
        bench.station.run()
        self.assertEqual([c[1] for c in bench.radio.calls if c[0] == "set_country"], [("CA",)])
        self.assertLess(bench.radio.names().index("set_country"), bench.radio.names().index("disconnect"))
        self.assertTrue(any("country set to CA" in line for line in bench.logs))

    def test_a_refused_country_does_not_stop_the_station(self):
        bench = Bench([FakeAP()], start=False)
        bench.station.options["country"] = "CA"
        bench.radio.country_error = OSError(errno.EPERM, "denied")
        bench.radio.wait = lambda timeout: (__import__("time").sleep(0.02), [])[1]
        bench.station.now = __import__("time").monotonic
        threading.Timer(0.2, bench.station.stop).start()
        bench.station.run()
        self.assertIsNone(bench.station.error)
        self.assertTrue(any("could not set the regulatory country" in line for line in bench.logs))

    def test_signal_is_polled_while_connected(self):
        bench = Bench([FakeAP()]).run(12)
        self.assertEqual(bench.station.snapshot()["rssi"], -58)
        bench.radio.signal_value = -71
        bench.run(3)
        self.assertEqual(bench.station.snapshot()["rssi"], -71)

    def test_the_journal_never_names_the_network(self):
        bench = Bench([FakeAP(refuse=True), FakeAP(bssid="aa:aa:aa:aa:aa:aa", answers=False)]).run(120)
        self.assertTrue(bench.logs)
        self.assertFalse([line for line in bench.logs if "EMPIRE" in line or "correct horse" in line])

    def test_a_wrong_passphrase_never_connects_and_keeps_trying_with_growing_delays(self):
        bench = Bench([FakeAP(passphrase=b"the router really uses this")]).run(300)
        self.assertNotIn("COMPLETED", bench.states())
        self.assertGreaterEqual(bench.radio.count("connect"), 6)
        self.assertEqual(bench.radio.count("new_key"), 0)                    # nothing is installed without a verified handshake
        self.assertEqual(bench.radio.count("authorize"), 0)
        self.assertTrue(any("passphrase" in line for line in bench.logs))
        gaps = [b[2] - a[2] for a, b in zip([c for c in bench.radio.calls if c[0] == "connect"][:-1],
                                              [c for c in bench.radio.calls if c[0] == "connect"][1:])]
        self.assertGreater(max(gaps[-3:]), min(gaps[:2]))                      # the ladder climbed
        self.assertLess(max(gaps), 30)                                          # ... but stays capped, it never gives up

    def test_an_access_point_that_never_answers_the_handshake_times_out_and_is_retried(self):
        bench = Bench([FakeAP(answers=False)]).run(120)
        self.assertNotIn("COMPLETED", bench.states())
        self.assertGreaterEqual(bench.radio.count("connect"), 3)
        self.assertTrue(any("handshake did not finish" in line for line in bench.logs))

    def test_a_refused_association_moves_on_to_the_other_access_point(self):
        bench = Bench([FakeAP(refuse=True, signal=-40.0), FakeAP(bssid="aa:aa:aa:aa:aa:aa", signal=-70.0)]).run(40)
        self.assertEqual(bench.state, "COMPLETED")
        connects = [c[1][0] for c in bench.radio.calls if c[0] == "connect"]
        self.assertEqual(connects[0], "c0:94:35:ad:ec:aa")                     # the strongest is tried first
        self.assertEqual(connects[-1], "aa:aa:aa:aa:aa:aa")

    def test_a_lone_access_point_that_failed_is_still_retried(self):
        bench = Bench([FakeAP(refuse=True)]).run(120)
        self.assertGreaterEqual(bench.radio.count("connect"), 4)               # bans never strand the only network in range

    def test_the_strongest_access_point_wins(self):
        bench = Bench([FakeAP(bssid="aa:aa:aa:aa:aa:aa", signal=-80.0), FakeAP(signal=-45.0), FakeAP(bssid="bb:bb:bb:bb:bb:bb", signal=-60.0)]).run(15)
        self.assertEqual([c[1][0] for c in bench.radio.calls if c[0] == "connect"], ["c0:94:35:ad:ec:aa"])

    def test_losing_the_link_reconnects_quickly_and_the_ladder_restarts(self):
        bench = Bench([FakeAP()]).run(12)
        self.assertEqual(bench.state, "COMPLETED")
        for round_number in range(3):
            lost_at = bench.t
            bench.radio.joined = None
            bench.radio.at(0, ("disconnected", 4, True))
            bench.run(14)
            self.assertEqual(bench.state, "COMPLETED", round_number)
            reconnect = [t for t, s in bench.history if s == "COMPLETED" and t > lost_at][0]
            self.assertLess(reconnect - lost_at, 10)
        self.assertEqual(bench.station.failures, 0)
        self.assertTrue(any("link lost" in line for line in bench.logs))

    def test_a_late_disconnect_confirmation_while_idle_is_ignored(self):
        bench = Bench([FakeAP(passphrase=b"x" * 10)]).run(1)
        bench.radio.at(0, ("disconnected", 3, False))
        bench.run(1)
        self.assertIn(bench.state, ("DISCONNECTED", "SCANNING"))
        self.assertEqual(bench.station.failures, 0)

    def test_a_message_1_that_beats_the_connect_event_is_not_lost(self):
        bench = Bench([FakeAP(join_delay=1.0)])
        original = bench.radio.connect

        def fast_ap(bss, ssid, **kw):
            original(bss, ssid, **kw)
            ap = bench.radio.joined
            bench.radio.queue = [(due, ("eapol", ap.bssid, ap.auth.msg1()) if event[0] == "eapol" else event) for due, event in bench.radio.queue]
            bench.radio.queue = [(0 if event[0] == "eapol" else due, event) for due, event in bench.radio.queue]   # message 1 arrives first
            bench.radio.queue.sort(key=lambda item: item[0])
        bench.radio.connect = fast_ap
        bench.run(15)
        self.assertEqual(bench.state, "COMPLETED")
        self.assertEqual(bench.radio.count("send_eapol") >= 2, True)

    def test_early_frames_from_strangers_or_outside_a_connection_are_not_kept(self):
        bench = Bench([FakeAP()], start=True).run(1)
        bench.station._eapol(bench.t, "c0:94:35:ad:ec:aa", b"x")      # nothing is being joined yet
        self.assertEqual(bench.station.early, [])

    def test_a_late_connect_result_after_giving_up_is_ignored(self):
        bench = Bench([FakeAP()], start=True).run(12)
        bench.radio.at(0, ("connected", "c0:94:35:ad:ec:aa", 0))
        bench.run(1)
        self.assertEqual(bench.state, "COMPLETED")

    def test_without_saved_networks_it_stays_inactive_and_leaves_the_radio_alone(self):
        bench = Bench([FakeAP()], profiles=()).run(30)
        self.assertEqual(bench.state, "INACTIVE")
        self.assertEqual(bench.radio.count("trigger_scan"), 0)
        self.assertEqual(bench.radio.count("connect"), 0)

    def test_a_network_added_later_is_picked_up_by_reload(self):
        bench = Bench([FakeAP()], profiles=()).run(5)
        bench.profiles.append(("EMPIRE", net.wpa_psk(b"correct horse battery", b"EMPIRE").hex()))
        done, box = bench.command("reload")
        bench.run(15)
        self.assertEqual((done.is_set(), box["result"]), (True, "OK"))
        self.assertEqual(bench.state, "COMPLETED")

    def test_removing_the_joined_network_disconnects_it(self):
        bench = Bench([FakeAP()]).run(12)
        bench.profiles.clear()
        bench.command("reload")
        bench.run(3)
        self.assertEqual(bench.state, "INACTIVE")
        self.assertEqual(bench.radio.count("disconnect"), 1)
        self.assertIsNone(bench.station.snapshot()["ssid"])

    def test_an_unreadable_store_keeps_the_previous_list_and_the_link(self):
        bench = Bench([FakeAP()]).run(12)
        bench.station.load_profiles = mock.Mock(side_effect=RuntimeError("damaged"))
        done, box = bench.command("reload")
        bench.run(2)
        self.assertEqual((box["result"], bench.state), ("kept", "COMPLETED"))
        self.assertTrue(any("keeping the previous list" in line for line in bench.logs))

    def test_a_network_that_needs_wpa3_or_pmf_is_reported_and_never_joined(self):
        wpa3 = net.parse_rsn(AP_RSN[2:14] + b"\x01\x00\x00\x0f\xac\x08\xc0\x00")                     # SAE only
        other_mgmt = net.parse_rsn(AP_RSN[2:20] + b"\xc0\x00\x00\x00\x00\x0f\xac\x0b")                 # BIP-GMAC-256
        for rsn, word in ((wpa3, "PSK"), (other_mgmt, "management frame cipher")):
            bench = Bench([FakeAP(rsn=rsn)]).run(30)
            self.assertEqual(bench.radio.count("connect"), 0, word)
            self.assertTrue(any(word in line for line in bench.logs), word)
            self.assertGreaterEqual(bench.radio.count("trigger_scan"), 3)

    def test_selecting_a_saved_network_prefers_it_while_it_is_in_range(self):
        strong, weak = FakeAP(), FakeAP(ssid=b"Cafe", bssid="dd:dd:dd:dd:dd:dd", signal=-75.0, passphrase=b"coffee and cake")
        bench = Bench([strong, weak], profiles=(("EMPIRE", "correct horse battery"), ("Cafe", "coffee and cake"))).run(15)
        self.assertEqual(bench.station.snapshot()["ssid"], "EMPIRE")
        done, box = bench.command("select", net.profile_id("Cafe"))
        bench.run(20)
        self.assertEqual(box["result"], "OK")
        self.assertEqual(bench.station.snapshot()["ssid"], "Cafe")
        # when the preferred network disappears the other one is used
        bench.radio.aps.remove(weak)
        bench.radio.joined = None
        bench.radio.at(0, ("disconnected", 4, True))
        bench.run(30)
        self.assertEqual(bench.station.snapshot()["ssid"], "EMPIRE")

    def test_selecting_an_unknown_profile_is_an_error(self):
        bench = Bench([FakeAP()]).run(12)
        done, box = bench.command("select", "0" * 16)
        bench.run(1)
        self.assertTrue(done.is_set())
        self.assertIn("does not exist", box["error"])
        self.assertEqual(bench.state, "COMPLETED")

    def test_selecting_the_network_already_joined_changes_nothing(self):
        bench = Bench([FakeAP()]).run(12)
        before = bench.radio.count("connect")
        bench.command("select", net.profile_id("EMPIRE"))
        bench.run(5)
        self.assertEqual((bench.radio.count("connect"), bench.state), (before, "COMPLETED"))

    def test_reassociate_drops_the_link_and_joins_again(self):
        bench = Bench([FakeAP()]).run(12)
        done, box = bench.command("reassociate")
        bench.run(14)
        self.assertEqual(box["result"], "OK")
        self.assertEqual(bench.radio.count("connect"), 2)
        self.assertEqual(bench.state, "COMPLETED")
        self.assertIn("DISCONNECTING", bench.states())

    def test_reassociate_while_looking_scans_immediately_and_resets_the_ladder(self):
        bench = Bench([FakeAP(passphrase=b"wrong wrong wrong")]).run(200)
        self.assertGreater(bench.station.failures, 2)
        bench.command("reassociate")
        bench.run(0.5)
        self.assertEqual(bench.station.failures, 0)

    def test_a_user_scan_while_connected_returns_entries_and_leaves_the_link_alone(self):
        bench = Bench([FakeAP(), FakeAP(ssid=b"Neighbour", bssid="ee:ee:ee:ee:ee:ee", signal=-82.0)]).run(12)
        scans = bench.radio.count("trigger_scan")
        done, box = bench.command("scan")
        bench.run(4)
        self.assertTrue(done.is_set())
        self.assertEqual(bench.radio.count("trigger_scan"), scans + 1)
        self.assertEqual({e["ssid"] for e in box["result"]}, {"EMPIRE", "Neighbour"})
        json.dumps(box["result"])
        self.assertEqual(bench.state, "COMPLETED")

    def test_a_user_scan_with_no_saved_networks_does_not_change_the_state(self):
        bench = Bench([FakeAP()], profiles=()).run(3)
        done, box = bench.command("scan")
        bench.run(4)
        self.assertTrue(done.is_set())
        self.assertEqual(len(box["result"]), 1)
        self.assertEqual(bench.state, "INACTIVE")

    def test_a_user_scan_during_a_connection_attempt_gets_the_cached_results_at_once(self):
        bench = Bench([FakeAP(join_delay=5.0)]).run(3.5)
        self.assertEqual(bench.state, "AUTHENTICATING")
        done, box = bench.command("scan")
        bench.run(0.5)
        self.assertTrue(done.is_set())
        self.assertEqual(len(box["result"]), 1)

    def test_a_scan_that_never_finishes_is_given_up_on_and_repeated(self):
        bench = Bench([FakeAP()], scan_delay=10_000).run(80)
        self.assertGreaterEqual(bench.radio.count("trigger_scan"), 2)
        self.assertTrue(any("scan timed out" in line for line in bench.logs))

    def test_a_radio_that_allows_only_one_scan_name_still_scans(self):
        bench = Bench([FakeAP()])
        bench.radio.scan_ssid_limit = 1
        bench.run(15)
        self.assertEqual(bench.state, "COMPLETED")
        self.assertEqual([c[1][0] for c in bench.radio.calls if c[0] == "trigger_scan"][:2], [(b"EMPIRE",), ()])

    def test_a_busy_radio_is_waited_for_and_other_scan_errors_are_retried(self):
        bench = Bench([FakeAP()])
        bench.radio.scan_error = OSError(errno.EBUSY, "busy")
        bench.run(4)
        self.assertEqual(bench.state, "SCANNING")                              # an earlier scan is under way; its end will tell us
        bench = Bench([FakeAP()])
        bench.radio.scan_error = OSError(errno.EIO, "io")
        bench.run(10)
        self.assertGreaterEqual(bench.radio.count("trigger_scan"), 2)
        bench.radio.scan_error = None
        bench.run(30)
        self.assertEqual(bench.state, "COMPLETED")

    def test_a_failing_scan_dump_does_not_drop_a_working_link(self):
        bench = Bench([FakeAP()]).run(12)
        bench.radio.get_scan_error = OSError(errno.EBUSY, "dump interrupted")
        done, box = bench.command("scan")
        bench.run(5)
        self.assertEqual(bench.state, "COMPLETED")
        self.assertIn("error", box)

    def test_a_key_the_kernel_refuses_fails_the_attempt_and_it_tries_again(self):
        bench = Bench([FakeAP()])
        bench.radio.key_error = OSError(errno.EINVAL, "bad key")
        bench.run(40)
        self.assertNotIn("COMPLETED", bench.states())
        self.assertEqual(bench.radio.count("authorize"), 0)                   # never open the port without keys
        self.assertGreaterEqual(bench.radio.count("connect"), 2)
        bench.radio.key_error = None
        bench.run(40)
        self.assertEqual(bench.state, "COMPLETED")

    def test_a_connect_the_kernel_refuses_is_retried(self):
        bench = Bench([FakeAP()])
        original = bench.radio.connect
        attempts = []

        def flaky(bss, ssid, **kw):
            attempts.append(1)
            if len(attempts) == 1:
                raise OSError(errno.EALREADY, "already connected")
            return original(bss, ssid, **kw)
        bench.radio.connect = flaky
        bench.run(30)
        self.assertEqual(bench.state, "COMPLETED")
        self.assertEqual(len(attempts), 2)

    def test_group_key_updates_and_retransmitted_message_3_are_handled_while_connected(self):
        bench = Bench([FakeAP()]).run(12)
        ap = bench.radio.aps[0]
        keys = bench.radio.count("new_key")
        bench.radio.at(0, ("eapol", ap.bssid, ap.auth.msg3()))               # the AP resends message 3
        bench.run(1)
        self.assertEqual(bench.radio.count("new_key"), keys)                  # no key was reinstalled
        self.assertTrue(ap.auth.msg4_ok)
        bench.radio.at(0, ("eapol", ap.bssid, ap.auth.group1(b"H" * 16)))
        bench.run(1)
        self.assertEqual(bench.radio.count("new_key"), keys + 1)
        self.assertEqual(bench.state, "COMPLETED")
        self.assertTrue(ap.auth.group_ok)

    def test_frames_from_other_stations_are_ignored(self):
        bench = Bench([FakeAP()]).run(12)
        keys = bench.radio.count("new_key")
        bench.radio.at(0, ("eapol", "66:66:66:66:66:66", bench.radio.aps[0].auth.group1(b"X" * 16)))
        bench.run(1)
        self.assertEqual(bench.radio.count("new_key"), keys)

    def test_a_forged_group_update_is_not_installed(self):
        bench = Bench([FakeAP()]).run(12)
        keys = bench.radio.count("new_key")
        ap = bench.radio.aps[0]
        forged = bytearray(ap.auth.group1(b"X" * 16))
        forged[81:97] = bytes(16)
        bench.radio.at(0, ("eapol", ap.bssid, bytes(forged)))
        bench.run(1)
        self.assertEqual(bench.radio.count("new_key"), keys)
        self.assertEqual(bench.state, "COMPLETED")

    def test_a_downgraded_message_3_aborts_and_disconnects(self):
        bench = Bench([FakeAP()])
        original = Authenticator.msg3
        tkip = AP_RSN[:4] + b"\x00\x0f\xac\x02" + AP_RSN[8:]
        with mock.patch.object(Authenticator, "msg3", lambda self, **kw: original(self, data=tkip + ap_kde(1, b"G" * 16), **kw)):
            bench.run(20)
        self.assertNotIn("COMPLETED", bench.states())
        self.assertEqual(bench.radio.count("new_key"), 0)
        self.assertTrue(any("downgrade" in line for line in bench.logs))

    def test_the_status_names_stay_within_what_the_rest_of_the_system_reads(self):
        bench = Bench([FakeAP(), FakeAP(ssid=b"x", bssid="ee:ee:ee:ee:ee:ee")]).run(60)
        self.assertTrue(set(bench.states()) <= set(net.Controller.STATES) | {"DISCONNECTING"})

    def test_the_loop_starts_clean_and_leaves_cleanly(self):
        bench = Bench([FakeAP()], start=False)
        stopper = threading.Timer(0.3, bench.station.stop)
        stopper.start()
        self.addCleanup(stopper.cancel)
        bench.station.now = __import__("time").monotonic                      # run() uses the real clock
        bench.radio.wait = lambda timeout: (__import__("time").sleep(0.05), [])[1]
        bench.station.run()
        self.assertIsNone(bench.station.error)
        self.assertEqual(bench.radio.names()[:2], ["bring_up", "disconnect"])    # a stale link from an earlier run is cleared
        self.assertEqual(bench.radio.names()[-1], "disconnect")                  # and the link is dropped on the way out

    def test_a_crash_in_the_loop_is_recorded_for_the_service_to_see(self):
        bench = Bench([FakeAP()])
        bench.radio.bring_up = mock.Mock(side_effect=OSError(errno.EPERM, "no capability"))
        bench.station.run()
        self.assertIsInstance(bench.station.error, OSError)
        with self.assertRaises(RuntimeError):
            bench.station.call("scan")


# ------------------------------------------------------------------ management frame protection (802.11w) and SHA-256 key management

class PmfCryptoTests(unittest.TestCase):
    def test_aes_encrypt_matches_fips_197_and_inverts_decrypt(self):
        key = bytes(range(16))
        self.assertEqual(net.aes_encrypt_block(key, bytes.fromhex("00112233445566778899aabbccddeeff")).hex(), "69c4e0d86a7b0430d8cdb78070b4c55a")
        for _ in range(10):
            key, block = os.urandom(16), os.urandom(16)
            self.assertEqual(net.aes_decrypt_block(key, net.aes_encrypt_block(key, block)), block)

    def test_aes_encrypt_agrees_with_openssl(self):
        if not shutil_which("openssl"):
            self.skipTest("no openssl")
        for _ in range(8):
            key, block = os.urandom(16), os.urandom(16)
            theirs = subprocess.run(["openssl", "enc", "-aes-128-ecb", "-nopad", "-K", key.hex()], input=block, capture_output=True).stdout
            self.assertEqual(net.aes_encrypt_block(key, block), theirs)

    def test_cmac_matches_the_rfc_4493_vectors(self):
        key = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
        message = bytes.fromhex("6bc1bee22e409f96e93d7e117393172aae2d8a571e03ac9c9eb76fac45af8e5130c81c46a35ce411e5fbc1191a0a52eff69f2445df4f9b17ad2b417be66c3710")
        for size, expected in ((0, "bb1d6929e95937287fa37d129b756746"), (16, "070a16b46b4d4144f79bdd9dd04a287c"),
                               (40, "dfa66747de9ae63030ca32611497c827"), (64, "51f0bebf7e3b9d92fc49741779363cfe")):
            self.assertEqual(net.aes_cmac(key, message[:size]).hex(), expected, size)

    def test_cmac_agrees_with_openssl_for_every_length_around_the_block_size(self):
        if not shutil_which("openssl"):
            self.skipTest("no openssl")
        key = os.urandom(16)
        for size in (0, 1, 15, 16, 17, 31, 32, 33, 95, 99, 119):
            data = os.urandom(size)
            self.assertEqual(net.aes_cmac(key, data), openssl_cmac(key, data), size)

    def test_the_sha256_kdf_is_the_documented_little_endian_counter_construction(self):
        key, data = os.urandom(32), os.urandom(76)
        expected = b"".join(hmac.new(key, struct.pack("<H", i) + b"Pairwise key expansion" + data + struct.pack("<H", 384), hashlib.sha256).digest()
                            for i in (1, 2))[:48]
        self.assertEqual(net.kdf_sha256(key, b"Pairwise key expansion", data, 48), expected)
        self.assertNotEqual(net.kdf_sha256(key, b"Pairwise key expansion", data, 32), expected[:32])   # the length is part of the input

    def test_the_sha256_ptk_differs_from_the_sha1_ptk_and_is_symmetric(self):
        pmk, an, sn = os.urandom(32), os.urandom(32), os.urandom(32)
        one, two = net.derive_ptk(pmk, AP, STA, an, sn), net.derive_ptk(pmk, AP, STA, an, sn, sha256=True)
        self.assertNotEqual(one, two)
        self.assertEqual(two, net.derive_ptk(pmk, STA, AP, sn, an, sha256=True))
        self.assertEqual(two, ap_ptk(pmk, an, sn, AP, sha256=True))                    # the test-side derivation agrees


class PmfElementTests(unittest.TestCase):
    def test_the_plain_element_is_byte_for_byte_what_it_always_was(self):
        self.assertEqual(net.build_rsn_ie(), rsn_bytes(caps=0x000C))
        self.assertEqual(net.build_rsn_ie(), b"\x30\x14\x01\x00\x00\x0f\xac\x04\x01\x00\x00\x0f\xac\x04\x01\x00\x00\x0f\xac\x02\x0c\x00")

    def test_the_pmf_element_names_the_key_management_and_the_management_cipher(self):
        ie = net.build_rsn_ie(0x000FAC06, True)
        self.assertEqual(ie[1], len(ie) - 2)
        rsn = net.parse_rsn(ie[2:])
        self.assertEqual((rsn["akm"], rsn["mgmt"]), ([0x000FAC06], 0x000FAC06))
        self.assertEqual(rsn["caps"] & 0x00C0, 0x00C0)                                   # capable and required
        self.assertEqual(ie, rsn_bytes(akm=(0x000FAC06,), caps=0x00CC, mgmt=0x000FAC06))

    def test_pmkids_are_skipped_and_the_management_cipher_is_read(self):
        body = rsn_bytes(caps=0x00CC)[2:] + struct.pack("<H", 1) + b"P" * 16 + bytes.fromhex("000fac06")
        self.assertEqual(net.parse_rsn(body)["mgmt"], 0x000FAC06)
        self.assertEqual(net.parse_rsn(rsn_bytes(caps=0x0080)[2:])["mgmt"], 0x000FAC06)   # protection on, none named: the default
        self.assertIsNone(net.parse_rsn(rsn_bytes(caps=0x000C)[2:])["mgmt"])

    def test_the_plan_uses_pmf_only_when_the_network_needs_it(self):
        plan = lambda **kw: net.pmf_plan(net.parse_rsn(rsn_bytes(**kw)[2:]))
        self.assertEqual(plan(), (PSK, False))
        self.assertEqual(plan(caps=0x008C), (PSK, False))                                       # capable but not required: keep plain WPA2
        self.assertEqual(plan(akm=(PSK, 0x000FAC08), caps=0x008C), (PSK, False))                # WPA3 transition mode: plain WPA2
        self.assertEqual(plan(caps=0x00CC), (PSK, True))
        self.assertEqual(plan(akm=(0x000FAC06,), caps=0x00CC), (0x000FAC06, True))
        self.assertEqual(plan(akm=(PSK, 0x000FAC06), caps=0x00CC), (PSK, True))                 # prefers plain PSK when offered
        self.assertIsNone(plan(akm=(0x000FAC08,), caps=0x00CC))                                 # SAE only
        self.assertIsNone(net.pmf_plan(None))


def pmf_handshake(akm=PSK, ap_rsn=PMF_AP_RSN, pmk=None):
    pmk = pmk or PMK
    return net.Handshake(pmk, AP, STA, net.build_rsn_ie(akm, True), net.parse_rsn(ap_rsn[2:]), fixed_random(), akm=akm, pmf=True)


class PmfHandshakeTests(unittest.TestCase):
    IGTK = (4, b"\x07\x00\x00\x00\x00\x00", b"I" * 16)

    def setUp(self):
        if not shutil_which("openssl"):
            self.skipTest("the access point side uses the openssl CLI")

    def ap(self, ver, rsn, akm):
        return Authenticator(PMK, rsn=rsn, ver=ver, igtk=self.IGTK, expect_ie=net.build_rsn_ie(akm, True))

    def complete(self, ap, sta):
        msg2 = sta.receive(ap.msg1())
        self.assertEqual(kinds(msg2), ["send"])
        self.assertEqual(ap.receive(msg2[0][1]), "msg2")
        return sta.receive(ap.msg3())

    def test_psk_with_pmf_required_keeps_descriptor_2_and_installs_the_igtk(self):
        ap, sta = self.ap(2, PMF_AP_RSN, PSK), pmf_handshake(PSK)
        actions = self.complete(ap, sta)
        self.assertEqual(kinds(actions), ["send", "install_ptk", "install_gtk", "install_igtk", "complete"])
        self.assertEqual(actions[3][1:], self.IGTK)
        self.assertEqual(ap.receive(actions[0][1]), "msg4")

    def test_psk_sha256_runs_descriptor_3_with_cmac_and_the_sha256_kdf_end_to_end(self):
        ap, sta = self.ap(3, SHA256_AP_RSN, 0x000FAC06), pmf_handshake(0x000FAC06, SHA256_AP_RSN)
        actions = self.complete(ap, sta)
        self.assertEqual(kinds(actions), ["send", "install_ptk", "install_gtk", "install_igtk", "complete"])
        self.assertEqual(actions[1][1], ap.ptk[32:48])                                  # both sides derived the same temporal key
        self.assertEqual(ap.receive(actions[0][1]), "msg4")
        self.assertTrue(ap.msg2_ok and ap.msg4_ok)

    def test_the_descriptor_3_key_info_values_on_the_wire(self):
        ap, sta = self.ap(3, SHA256_AP_RSN, 0x000FAC06), pmf_handshake(0x000FAC06, SHA256_AP_RSN)
        self.assertEqual(struct.unpack_from(">H", ap.msg1(), 5)[0], 0x008B)
        msg2 = sta.receive(ap.msg1())[0][1]
        self.assertEqual(struct.unpack_from(">H", msg2, 5)[0], 0x010B)
        ap.receive(msg2)
        msg3 = ap.msg3()
        self.assertEqual(struct.unpack_from(">H", msg3, 5)[0], 0x13CB)
        self.assertEqual(struct.unpack_from(">H", sta.receive(msg3)[0][1], 5)[0], 0x030B)

    def test_a_wrong_passphrase_is_still_caught_by_the_authenticator_under_sha256(self):
        ap = self.ap(3, SHA256_AP_RSN, 0x000FAC06)
        sta = pmf_handshake(0x000FAC06, SHA256_AP_RSN, pmk=net.wpa_psk(b"nope nope nope", b"EMPIRE"))
        self.assertEqual(ap.receive(sta.receive(ap.msg1())[0][1]), "bad MIC")

    def test_a_forged_sha256_message_3_is_ignored(self):
        ap, sta = self.ap(3, SHA256_AP_RSN, 0x000FAC06), pmf_handshake(0x000FAC06, SHA256_AP_RSN)
        ap.receive(sta.receive(ap.msg1())[0][1])
        self.assertEqual(kinds(sta.receive(ap.msg3(tamper="mic"))), ["ignore"])
        self.assertEqual(kinds(sta.receive(ap.msg3()))[0], "send")                       # the genuine one still works

    def test_message_3_without_an_igtk_fails_when_protection_is_required(self):
        ap, sta = Authenticator(PMK, rsn=PMF_AP_RSN, igtk=None, expect_ie=net.build_rsn_ie(PSK, True)), pmf_handshake(PSK)
        ap.receive(sta.receive(ap.msg1())[0][1])
        actions = sta.receive(ap.msg3())
        self.assertEqual(kinds(actions), ["fail"])
        self.assertIn("integrity group key", actions[0][1])

    def test_descriptor_versions_must_match_the_negotiated_key_management(self):
        v2_ap = self.ap(2, PMF_AP_RSN, PSK)
        self.assertEqual(kinds(pmf_handshake(0x000FAC06, SHA256_AP_RSN).receive(v2_ap.msg1())), ["fail"])
        v3_ap = self.ap(3, SHA256_AP_RSN, 0x000FAC06)
        self.assertEqual(kinds(pmf_handshake(PSK).receive(v3_ap.msg1())), ["fail"])

    def test_group_updates_can_carry_a_new_gtk_a_new_igtk_or_both(self):
        ap, sta = self.ap(3, SHA256_AP_RSN, 0x000FAC06), pmf_handshake(0x000FAC06, SHA256_AP_RSN)
        ap.receive(self.complete(ap, sta)[0][1])
        new_igtk = (5, b"\x09\x00\x00\x00\x00\x00", b"J" * 16)
        both = sta.receive(ap.group1(b"H" * 16, igtk=new_igtk))
        self.assertEqual(kinds(both), ["send", "install_gtk", "install_igtk", "group_done"])
        self.assertEqual(both[2][1:], new_igtk)
        self.assertEqual(ap.receive(both[0][1]), "group2")
        only = sta.receive(ap.group1(None, igtk=(4, b"\x0a\x00\x00\x00\x00\x00", b"K" * 16)))
        self.assertEqual(kinds(only), ["send", "install_igtk", "group_done"])

    def test_without_protection_the_igtk_is_ignored(self):
        ap = Authenticator(PMK, igtk=self.IGTK)
        sta = new_handshake()
        msg2 = sta.receive(ap.msg1())[0][1]
        self.assertEqual(ap.receive(msg2), "msg2")
        self.assertEqual(kinds(sta.receive(ap.msg3())), ["send", "install_ptk", "install_gtk", "complete"])


class PmfDriverTests(unittest.TestCase):
    def setUp(self):
        self.rec = Recorder()
        self.driver = make_driver(self.rec)

    def attrs(self):
        return self.rec.sent[-1][2]

    def test_connecting_with_protection_asks_the_kernel_for_it(self):
        bss = net.parse_bss(bss_attrs(rsn=SHA256_AP_RSN))
        self.driver.connect(bss, b"EMPIRE", akm=0x000FAC06, pmf=True)
        a = self.attrs()
        self.assertEqual(struct.unpack("=I", a[net.A_USE_MFP])[0], 1)                  # NL80211_MFP_REQUIRED
        self.assertEqual(struct.unpack("=I", a[net.A_AKM])[0], 0x000FAC06)
        self.assertEqual(a[net.A_IE], net.build_rsn_ie(0x000FAC06, True))
        self.driver.connect(net.parse_bss(bss_attrs()), b"EMPIRE")
        a = self.attrs()
        self.assertEqual((struct.unpack("=I", a[net.A_USE_MFP])[0], struct.unpack("=I", a[net.A_AKM])[0]), (0, PSK))   # the default is unchanged

    def test_the_igtk_is_a_bip_cmac_key_with_a_six_byte_packet_number(self):
        self.driver.new_igtk(4, b"I" * 16, b"\x01\x02\x03\x04\x05\x06")
        key = net.parse_attrs(self.attrs()[net.A_KEY])
        self.assertEqual((key[net.K_DATA], key[net.K_IDX], key[net.K_SEQ]), (b"I" * 16, b"\x04", b"\x01\x02\x03\x04\x05\x06"))
        self.assertEqual(struct.unpack("=I", key[net.K_CIPHER])[0], 0x000FAC06)
        self.assertNotIn(net.A_MAC, self.attrs())
        self.driver.default_igtk(5)
        key = net.parse_attrs(self.attrs()[net.A_KEY])
        self.assertEqual((key[net.K_IDX], net.K_DEFAULT_MGMT in key), (b"\x05", True))
        self.assertEqual(self.rec.sent[-1][1], net.CMD_SET_KEY)


class PmfStationTests(unittest.TestCase):
    def setUp(self):
        if not shutil_which("openssl"):
            self.skipTest("the access point side uses the openssl CLI")

    def pmf_ap(self, akm_sha256=False, **kw):
        rsn_b = SHA256_AP_RSN if akm_sha256 else PMF_AP_RSN
        return FakeAP(rsn=net.parse_rsn(rsn_b[2:]), rsn_bytes=rsn_b, **kw)

    def test_a_psk_network_that_requires_protection_is_joined_with_it(self):
        bench = Bench([self.pmf_ap()]).run(15)
        self.assertEqual(bench.state, "COMPLETED")
        self.assertEqual(bench.radio.last_connect, {"akm": PSK, "pmf": True})
        names = bench.radio.names()
        self.assertLess(names.index("new_key", names.index("new_key") + 1), names.index("new_igtk"))   # GTK first, then IGTK
        self.assertLess(names.index("new_igtk"), names.index("authorize"))                             # the port opens last
        self.assertEqual(names.count("default_igtk"), 1)
        igtk = next(c for c in bench.radio.calls if c[0] == "new_igtk")
        self.assertEqual(igtk[1], (4, b"I" * 16, b"\x01\x00\x00\x00\x00\x00"))
        self.assertTrue(any("management frame protection" in line for line in bench.logs))

    def test_a_psk_sha256_network_is_joined_with_descriptor_3(self):
        bench = Bench([self.pmf_ap(akm_sha256=True)]).run(15)
        self.assertEqual(bench.state, "COMPLETED")
        self.assertEqual(bench.radio.last_connect, {"akm": 0x000FAC06, "pmf": True})
        self.assertTrue(bench.radio.aps[0].auth.msg4_ok)

    def test_an_ordinary_network_never_touches_protection(self):
        bench = Bench([FakeAP()]).run(12)
        self.assertEqual(bench.radio.last_connect, {"akm": PSK, "pmf": False})
        self.assertEqual((bench.radio.count("new_igtk"), bench.radio.count("default_igtk")), (0, 0))
        self.assertFalse(any("management frame" in line for line in bench.logs))

    def test_a_wrong_passphrase_never_completes_a_protected_connection_either(self):
        bench = Bench([self.pmf_ap(passphrase=b"not the right one")]).run(120)
        self.assertNotIn("COMPLETED", bench.states())
        self.assertEqual((bench.radio.count("new_key"), bench.radio.count("new_igtk"), bench.radio.count("authorize")), (0, 0, 0))

    def test_a_kernel_that_refuses_the_igtk_fails_the_attempt_and_never_opens_the_port(self):
        bench = Bench([self.pmf_ap()])
        original = bench.radio.new_igtk

        def refuse(index, key, ipn):
            original(index, key, ipn)
            raise OSError(errno.EINVAL, "bad key")
        bench.radio.new_igtk = refuse
        bench.run(30)
        self.assertEqual(bench.radio.count("authorize"), 0)
        self.assertNotIn("COMPLETED", bench.states())

    def test_group_key_refreshes_with_a_new_igtk_are_installed_while_connected(self):
        bench = Bench([self.pmf_ap()]).run(15)
        ap = bench.radio.aps[0]
        before = bench.radio.count("new_igtk")
        bench.radio.at(0, ("eapol", ap.bssid, ap.auth.group1(b"H" * 16, igtk=(5, b"\x09\x00\x00\x00\x00\x00", b"J" * 16))))
        bench.run(1)
        self.assertEqual(bench.radio.count("new_igtk"), before + 1)
        self.assertEqual(bench.state, "COMPLETED")



class StationCallTests(unittest.TestCase):
    def test_call_waits_for_the_answer_and_reports_errors(self):
        station = net.Station(FakeRadio([]), lambda: [], lambda line: None)
        threading.Thread(target=lambda: [item[2].set() or item[3].update(result="OK") for item in [station.commands.get()]], daemon=True).start()
        self.assertEqual(station.call("reassociate", timeout=2), "OK")
        with self.assertRaisesRegex(RuntimeError, "did not answer"):
            station.call("reassociate", timeout=0.05)
        station.commands.get_nowait()                    # the unanswered request is still queued; drop it
        threading.Thread(target=lambda: [item[3].update(error="nope") or item[2].set() for item in [station.commands.get()]], daemon=True).start()
        with self.assertRaisesRegex(RuntimeError, "nope"):
            station.call("select", "x", timeout=2)


# ------------------------------------------------------------------ the control socket

class FakeControlled:
    def __init__(self):
        self.calls = []

    def call(self, name, *args):
        self.calls.append((name, args))
        if name == "select" and args == ("bad",):
            raise RuntimeError("Saved profile does not exist or is ambiguous.")
        return {"scan": [{"ssid": "EMPIRE"}], "select": "OK"}.get(name, "OK")


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        for patcher in (mock.patch.object(net, "RUNTIME_DIR", self.dir), mock.patch.object(net, "require_root", lambda: None),
                        mock.patch.object(net, "protected", lambda path, allow_absent=False: Path(path))):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.station = FakeControlled()

    def serve(self, **kw):
        server = net.ControlServer(self.dir / "control.sock", self.station, **{"allowed_uid": os.getuid(), **kw})
        server.start()
        self.addCleanup(lambda: (server.stop(), server.join(3)))
        return server

    def raw(self, data):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(5)
            sock.connect(str(self.dir / "control.sock"))
            sock.sendall(data)
            return json.loads(sock.recv(65536))

    def test_commands_reach_the_station_and_answers_come_back(self):
        self.serve()
        self.assertEqual(net.control_call("scan"), [{"ssid": "EMPIRE"}])
        self.assertEqual(net.control_call("select", "0123456789abcdef"), "OK")
        self.assertEqual(net.control_call("reassociate"), "OK")
        self.assertEqual(net.control_call("reload"), "OK")
        self.assertEqual(self.station.calls, [("scan", ()), ("select", ("0123456789abcdef",)), ("reassociate", ()), ("reload", ())])

    def test_a_refusal_from_the_station_becomes_a_runtime_error(self):
        self.serve()
        with self.assertRaisesRegex(RuntimeError, "does not exist"):
            net.control_call("select", "bad")

    def test_the_socket_is_private_and_removed_on_stop(self):
        server = self.serve()
        self.assertEqual(os.stat(self.dir / "control.sock").st_mode & 0o777, 0o600)
        server.stop()
        server.join(3)
        self.assertFalse((self.dir / "control.sock").exists())

    def test_a_stale_socket_file_is_replaced(self):
        (self.dir / "control.sock").write_text("left over")
        self.serve()
        self.assertEqual(net.control_call("reload"), "OK")

    def test_only_the_allowed_user_may_command_the_station(self):
        self.serve(allowed_uid=os.getuid() + 1)
        with self.assertRaisesRegex(RuntimeError, "Only root"):
            net.control_call("reassociate")
        self.assertEqual(self.station.calls, [])

    def test_unknown_malformed_and_oversized_requests_are_rejected_without_calling_the_station(self):
        self.serve()
        for data in (b'{"cmd": "disconnect"}\n', b'{"cmd": "select", "args": [5]}\n', b'{"cmd": "scan", "args": "x"}\n',
                     b"not json\n", b'{"nocmd": 1}\n', b"[]\n", b'{"cmd": "' + b"a" * 5000 + b'"}\n'):
            reply = self.raw(data)
            self.assertFalse(reply["ok"], data[:30])
        self.assertEqual(self.station.calls, [])
        self.assertEqual(net.control_call("reload"), "OK")                    # and the server is still up

    def test_without_a_running_service_the_message_says_what_to_do(self):
        with self.assertRaisesRegex(RuntimeError, "Start the configured whitecube-network service"):
            net.control_call("scan")


class SelftestTests(unittest.TestCase):
    def run_selftest(self, scan, profiles=None, competing=()):
        class Driver:
            family, mac = 33, STA

            def __init__(self, iface, subscribe=True):
                pass

            def power_save(self):
                return True

            def get_scan(self):
                if isinstance(scan, Exception):
                    raise scan
                return scan

            def close(self):
                pass
        seen = [{"interface": "wlan0", "usb_id": "2001:3329", "mac": "88:76:b9:f2:9d:29", "driver": "rtw88_8822cu"}]
        with mock.patch.object(net, "interfaces", return_value=seen), mock.patch.object(net, "Nl80211Driver", Driver), \
                mock.patch.object(net, "competing_processes", return_value=list(competing)), \
                mock.patch.object(net.os, "geteuid", return_value=0 if profiles is not None else 1000), \
                mock.patch.object(net, "load_profiles", return_value=profiles or []), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            failures = net.selftest()
        return failures, out.getvalue()

    def test_a_good_environment_passes_and_lists_what_it_sees(self):
        failures, text = self.run_selftest([FakeAP().scan_entry()])
        self.assertEqual(failures, 0)
        self.assertIn("EMPIRE", text)
        self.assertIn("usable", text)
        self.assertIn("only checked when run as root", text)

    def test_a_saved_network_that_cannot_be_joined_is_a_failure_with_the_reason(self):
        sae_only = net.parse_rsn(AP_RSN[2:14] + b"\x01\x00\x00\x0f\xac\x08\xc0\x00")
        failures, text = self.run_selftest([FakeAP(rsn=sae_only).scan_entry()], profiles=[("EMPIRE", "00" * 32)])
        self.assertEqual(failures, 1)
        self.assertIn("not WPA2-personal", text)

    def test_a_saved_network_that_is_out_of_range_says_to_scan(self):
        failures, text = self.run_selftest([], profiles=[("EMPIRE", "00" * 32)])
        self.assertEqual(failures, 1)
        self.assertIn("run a scan first", text)

    def test_a_network_that_requires_protection_is_called_joinable(self):
        protected = FakeAP(rsn=net.parse_rsn(PMF_AP_RSN[2:]), rsn_bytes=PMF_AP_RSN)
        failures, _ = self.run_selftest([protected.scan_entry()], profiles=[("EMPIRE", "00" * 32)])
        self.assertEqual(failures, 0)

    def test_a_joinable_saved_network_passes_and_a_running_rival_is_only_a_note(self):
        failures, text = self.run_selftest([FakeAP().scan_entry()], profiles=[("EMPIRE", "00" * 32)], competing=["wpa_supplicant"])
        self.assertEqual(failures, 0)
        self.assertIn("note these will compete", text)

    def test_a_kernel_that_cannot_be_read_fails_instead_of_crashing(self):
        failures, text = self.run_selftest(OSError(errno.EPERM, "denied"))
        self.assertEqual(failures, 1)
        self.assertIn("FAIL the kernel scan cache decodes", text)

    def test_the_command_exits_nonzero_on_failure(self):
        with mock.patch.object(net, "selftest", return_value=2), mock.patch.object(sys, "argv", ["cube-net", "selftest"]), \
                self.assertRaises(SystemExit) as caught:
            net.main()
        self.assertEqual(caught.exception.code, 1)


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "network"
        for patcher in (mock.patch.object(net, "CONFIG_DIR", self.dir), mock.patch.object(net, "RUNTIME_DIR", Path(self.tmp.name) / "run"),
                        mock.patch.object(net, "require_root", lambda: None),
                        mock.patch.object(net, "protected", lambda path, allow_absent=False: Path(path))):
            patcher.start()
            self.addCleanup(patcher.stop)
        (Path(self.tmp.name) / "run").mkdir()
        with contextlib.redirect_stdout(io.StringIO()):
            net.write_profiles([("EMPIRE", "correct horse battery")])

    def test_scan_prints_what_the_station_found(self):
        with mock.patch.object(net, "selected_interface", return_value="wlan0"), \
                mock.patch.object(net, "control_call", return_value=[{"ssid": "EMPIRE"}]) as call:
            result = net.scan_networks()
        call.assert_called_once_with("scan")
        self.assertEqual((result["interface"], result["networks"]), ("wlan0", [{"ssid": "EMPIRE"}]))

    def test_connect_reloads_then_selects_and_restarts_the_retry_ladders(self):
        calls = []
        with mock.patch.object(net, "control_call", lambda *a: calls.append(a) or "OK"), contextlib.redirect_stdout(io.StringIO()) as out:
            net.connect_profile(net.profile_id("EMPIRE"))
        self.assertEqual(calls, [("reload",), ("select", net.profile_id("EMPIRE"))])
        self.assertEqual(json.loads(out.getvalue())["state"], "connecting")
        self.assertTrue((Path(self.tmp.name) / "run/recovery.request").exists())

    def test_connect_rejects_bad_and_unknown_ids_before_talking_to_the_station(self):
        with mock.patch.object(net, "control_call") as call:
            for bad in ("nope", "0" * 16):
                with self.assertRaises(ValueError):
                    net.connect_profile(bad)
        call.assert_not_called()

    def test_remove_deletes_the_profile_then_reloads_and_survives_a_stopped_service(self):
        identifier = net.profile_id("EMPIRE")
        with mock.patch.object(net, "control_call", side_effect=RuntimeError("down")), contextlib.redirect_stdout(io.StringIO()) as out:
            net.remove_profile(identifier)
        self.assertEqual(net.load_profiles(), [])
        self.assertIn("reload unavailable", out.getvalue())

    def test_reconnect_asks_for_reassociation_and_resets_the_ladders(self):
        with mock.patch.object(net, "control_call", return_value="OK") as call, contextlib.redirect_stdout(io.StringIO()):
            net.reconnect()
        call.assert_called_once_with("reassociate")
        self.assertTrue((Path(self.tmp.name) / "run/recovery.request").exists())


# ------------------------------------------------------------------ the whole service, real threads and socket

class RealtimeRadio(FakeRadio):
    """The simulated radio on the wall clock, so the station thread and the service loop can run for real."""

    @property
    def now(self):
        return __import__("time").monotonic()

    @now.setter
    def now(self, value):
        pass

    def wait(self, timeout):
        __import__("time").sleep(min(timeout, 0.05))
        return super().wait(0)


class IdleChild:
    created = []

    def __init__(self, argv, *a, **k):
        self.argv, self.terminated = argv, False
        IdleChild.created.append(self)

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 0


class ServiceIntegration(unittest.TestCase):
    """run_service() with the real Station, thread and control socket; only the radio, dhcpcd and probes are fake."""

    def test_the_service_joins_serves_a_scan_over_its_socket_and_leaves_cleanly(self):
        if not shutil_which("openssl"):
            self.skipTest("the access point side wraps keys with the openssl CLI")
        import shutil
        import signal as signal_module
        import time
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        config, run_dir = root / "etc", root / "run"
        config.mkdir()
        (config / "device.json").write_text(json.dumps(net.DEFAULT_DEVICE))
        (config / "dhcpcd.conf").write_text("timeout 0\n")
        for name in ("device.json", "dhcpcd.conf"):
            (config / name).chmod(0o644)
        old = (net.signal.getsignal(net.signal.SIGTERM), net.signal.getsignal(net.signal.SIGINT))
        self.addCleanup(lambda: (net.signal.signal(net.signal.SIGTERM, old[0]), net.signal.signal(net.signal.SIGINT, old[1])))
        real_stat = Path.stat

        def stat_as_root(path, *a, **k):
            result = real_stat(path, *a, **k)
            if str(path).startswith(str(root)):
                fields = list(result)
                fields[4] = 0                                   # the checks insist on root ownership
                return os.stat_result(fields)
            return result

        radio = RealtimeRadio([FakeAP(join_delay=0.1)], scan_delay=0.2)
        IdleChild.created = []
        stations, observed = [], {}
        seen = [{"interface": "wlan0", "usb_id": "2001:3329", "mac": "88:76:b9:f2:9d:29", "driver": "rtw88_8822cu"}]
        healthy = {"checks_epoch": 1.0, "gateway_ok": True, "dns_ok": True, "dns_ms": 20, "connectivity_ok": True, "tcp_ms": 90}

        real_station, real_control = net.Station, net.ControlServer

        def make_station(driver, load_profiles, log, options=None):
            stations.append(real_station(driver, load_profiles, log, options=options))
            return stations[0]

        def client():
            try:
                time.sleep(2.6)
                observed["status"] = json.loads((run_dir / "status.json").read_text())
                observed["scan"] = net.control_call("scan")
                observed["reload"] = net.control_call("reload")
                observed["state_after"] = stations[0].snapshot()["state"]
            except Exception as error:                          # reported by the assertions below
                observed["error"] = repr(error)
            finally:
                os.kill(os.getpid(), signal_module.SIGTERM)

        patches = [mock.patch.object(net, "CONFIG_DIR", config), mock.patch.object(net, "RUNTIME_DIR", run_dir),
                   mock.patch.object(net, "require_root", lambda: None),
                   mock.patch.object(net, "protected", lambda path, allow_absent=False: Path(path)),
                   mock.patch.object(Path, "stat", stat_as_root),
                   mock.patch.object(net, "interfaces", return_value=seen),
                   mock.patch.object(net, "competing_processes", return_value=[]),
                   mock.patch.object(net, "subprocess", types_namespace(      # only cube-net sees the fake dhcpcd;
                       Popen=IdleChild, run=subprocess.run, SubprocessError=subprocess.SubprocessError,   # the AP's openssl is real
                       TimeoutExpired=subprocess.TimeoutExpired)),
                   mock.patch.object(net, "Nl80211Driver", lambda iface: radio),
                   mock.patch.object(net, "Station", make_station),
                   mock.patch.object(net, "ControlServer", lambda path, station: real_control(path, station, allowed_uid=os.getuid())),
                   mock.patch.object(net, "wireless_signal", return_value=-50),
                   mock.patch.object(net, "interface_ipv4", return_value="10.0.0.133"),
                   mock.patch.object(net, "default_gateway", return_value="10.0.0.1"),
                   mock.patch.object(net, "probe_suite", return_value=dict(healthy))]
        with contextlib.ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            journal = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            net.write_profiles([("EMPIRE", "correct horse battery")])
            threading.Thread(target=client, daemon=True).start()
            net.run_service()
        text = journal.getvalue()
        self.assertNotIn("error", observed)
        self.assertEqual((observed["status"]["association_state"], observed["status"]["ssid"]), ("COMPLETED", "EMPIRE"))
        self.assertEqual([entry["ssid"] for entry in observed["scan"]], ["EMPIRE"])
        self.assertEqual((observed["reload"], observed["state_after"]), ("OK", "COMPLETED"))
        self.assertEqual(radio.names()[-2:], ["disconnect", "close"])             # the link is dropped, then the radio is released
        self.assertFalse((run_dir / "control.sock").exists())
        self.assertTrue(all(child.terminated for child in IdleChild.created))
        self.assertIn("built-in WPA2 station", text)
        self.assertNotIn("EMPIRE", text)


# ------------------------------------------------------------------ the real kernel, read-only

def live_interface():
    try:
        if not Path("/sys/class/net/wlan0/wireless").exists() and not Path("/sys/class/net/wlan0/phy80211").exists():
            return None
        return net.Nl80211Driver("wlan0", subscribe=False)
    except OSError:
        return None


class LiveReadOnlyTests(unittest.TestCase):
    """Talks to the running kernel's nl80211 without root and without touching the connection."""

    def setUp(self):
        self.driver = live_interface()
        if self.driver is None:
            self.skipTest("no wlan0 / nl80211 here")
        self.addCleanup(self.driver.close)

    def test_the_family_and_multicast_groups_resolve(self):
        family, groups = net.resolve_nl80211(self.driver.commands)
        self.assertEqual(family, self.driver.family)
        self.assertTrue({"scan", "mlme"} <= set(groups))

    def test_the_cached_scan_decodes_into_usable_entries(self):
        results = self.driver.get_scan()
        for bss in results:
            self.assertRegex(bss["bssid"], r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")
            self.assertIsInstance(bss["ssid"], bytes)
            self.assertGreater(bss["freq"], 2000)
            json.dumps(net.scan_entry(bss))
            net.unsupported_reason(bss["rsn"])

    def test_our_own_address_matches_the_kernels(self):
        self.assertEqual(net.mac_str(self.driver.mac), net.read("/sys/class/net/wlan0/address"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
