#!/usr/bin/env python3
"""Independent wire conformance: framing, 8-byte alignment, a{sv}, SCM_RIGHTS, match rules."""
import os, socket, struct, sys, time

PATH = sys.argv[1]
NAME = "org.whitecube.Test"

def al(n, a): return (n + a - 1) & ~(a - 1)

def pad(b, a): return b + b"\0" * (al(len(b), a) - len(b))
def u32(v): return struct.pack("<I", v)
def s(v):
    e = v.encode(); return u32(len(e)) + e + b"\0"

def marsh(sig, vals):
    """Supports s, u, t, a{sv} (values: str->'s', int->'u')."""
    out = b""; i = 0
    for t in sig_split(sig):
        v = vals[i]; i += 1
        if t == "s": out = pad(out, 4) + s(v)
        elif t == "u": out = pad(out, 4) + u32(v)
        elif t == "t": out = pad(out, 8) + struct.pack("<Q", v)
        elif t == "as":
            out = pad(out, 4); lp = len(out); out += u32(0); start = len(out)
            for item in v: out = pad(out, 4) + s(item)
            out = out[:lp] + u32(len(out) - start) + out[lp + 4:]
        elif t == "a{ss}":
            out = pad(out, 4); lp = len(out); out += u32(0); out = pad(out, 8); start = len(out)
            for k, val in v.items(): out = pad(out, 8) + s(k) + pad(b"", 4) + s(val)
            out = out[:lp] + u32(len(out) - start) + out[lp + 4:]
        elif t == "a{sv}":
            out = pad(out, 4); lp = len(out); out += u32(0)
            out = pad(out, 8); start = len(out)
            for k, val in v.items():
                out = pad(out, 8) + s(k)
                if isinstance(val, int): out = pad(out + b"\x01u\0", 4) + u32(val)
                else: out = pad(out + b"\x01s\0", 4) + s(val)
            out = out[:lp] + u32(len(out) - start) + out[lp + 4:]
        else: raise AssertionError(t)
    return out

def sig_split(sig):
    out = []; i = 0
    while i < len(sig):
        if sig.startswith("a{sv}", i): out.append("a{sv}"); i += 5
        elif sig.startswith("a{ss}", i): out.append("a{ss}"); i += 5
        elif sig.startswith("as", i): out.append("as"); i += 2
        else: out.append(sig[i]); i += 1
    return out

def field(code, sig, val):
    b = bytes([code, len(sig)]) + sig.encode() + b"\0"
    if sig in ("s", "o"): return b + s(val)
    if sig == "u": return b + u32(val)
    if sig == "g": return b + bytes([len(val)]) + val.encode() + b"\0"
    raise AssertionError(sig)

def msg(mtype, serial, fields, body=b"", flags=0):
    f = b""
    for code, sig, val in fields:
        f = pad(f, 8) + field(code, sig, val)
    head = bytes([ord('l'), mtype, flags, 1]) + u32(len(body)) + u32(serial) + u32(len(f))
    out = head + f
    return pad(out, 8) + body

class Peer:
    def __init__(self):
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.s.connect(PATH); self.buf = b""; self.fds = []; self.serial = 0
        self.s.sendall(b"\0AUTH EXTERNAL " + str(os.getuid()).encode().hex().encode() + b"\r\n")
        assert self.line().startswith(b"OK "), "SASL EXTERNAL failed"
        self.s.sendall(b"NEGOTIATE_UNIX_FD\r\n")
        assert self.line() == b"AGREE_UNIX_FD", "fd negotiation failed"
        self.s.sendall(b"BEGIN\r\n")
        hello = self.call("Hello", "", [])
        n = struct.unpack_from("<I", hello, 0)[0]
        self.name = hello[4:4 + n].decode()

    def line(self):
        while b"\r\n" not in self.buf: self.buf += self.s.recv(4096)
        l, self.buf = self.buf.split(b"\r\n", 1); return l

    def send(self, data, fds=()):
        if fds: socket.send_fds(self.s, [data], fds)
        else: self.s.sendall(data)

    def call(self, member, sig, vals, dest="org.freedesktop.DBus", iface=None):
        self.serial += 1
        body = marsh(sig, vals)
        fl = [(1, "o", "/org/freedesktop/DBus"), (3, "s", member), (6, "s", dest)]
        if iface: fl.append((2, "s", iface))
        if sig: fl.append((8, "g", sig))
        self.send(msg(1, self.serial, fl, body))
        m = self.recv()
        assert m["type"] in (2, 3), m
        if m["type"] == 3: raise RuntimeError(m["body"])
        return m["body"]

    def recv(self, timeout=3.0):
        self.s.settimeout(timeout)
        while True:
            m = self.try_parse()
            if m: return m
            data, fds, _, _ = socket.recv_fds(self.s, 65536, 16)
            if not data: raise EOFError("broker closed connection")
            self.buf += data; self.fds += fds

    def try_parse(self):
        if len(self.buf) < 16: return None
        bl, serial, fl = struct.unpack_from("<III", self.buf, 4)
        bo = al(16 + fl, 8); total = bo + bl
        if len(self.buf) < total: return None
        raw = self.buf[:total]; self.buf = self.buf[total:]
        assert raw[3] == 1
        f = {}; off = 16; end = 16 + fl
        while off < end:
            off = al(off, 8)
            if off >= end: break
            code = raw[off]; sl = raw[off + 1]
            sig = raw[off + 2:off + 2 + sl].decode(); off += 2 + sl + 1
            off = al(off, {"s": 4, "o": 4, "u": 4, "g": 1}[sig])
            if sig in ("s", "o"):
                n = struct.unpack_from("<I", raw, off)[0]
                f[code] = raw[off + 4:off + 4 + n].decode(); off += 4 + n + 1
            elif sig == "u":
                f[code] = struct.unpack_from("<I", raw, off)[0]; off += 4
            else:
                n = raw[off]; f[code] = raw[off + 1:off + 1 + n].decode(); off += 1 + n + 1
        nf = f.get(9, 0)
        got, self.fds = self.fds[:nf], self.fds[nf:]
        return {"type": raw[1], "serial": serial, "f": f, "raw": raw,
                "body_off": bo, "body": raw[bo:], "fds": got}

def main():
    a, b = Peer(), Peer()
    rc = b.call("RequestName", "su", [NAME, 0])
    assert struct.unpack("<I", rc)[0] == 1, "RequestName failed"
    a.call("AddMatch", "s", ["type='signal',sender='%s',interface='org.whitecube.Test'" % NAME])
    print("  ok: handshake, Hello, RequestName, AddMatch")

    # alignment torture: odd-length strings + uint64 + a{sv}, plus an fd
    payload = b"whitecube-fd-payload"
    fd = os.memfd_create("wc-test", 0)
    os.write(fd, payload)
    a.serial += 1
    body = marsh("sta{sv}", ["x" * 7, 0x1122334455667788,
                             {"Key": "value", "N": 42}])
    fl = [(1, "o", "/org/whitecube/Test"), (2, "s", "org.whitecube.Test"),
          (3, "s", "Echo"), (6, "s", NAME), (8, "g", "sta{sv}"), (9, "u", 1),
          (7, "s", ":1.999")]                       # spoofed SENDER
    a.send(msg(1, a.serial, fl, body), [fd])
    os.close(fd)

    m = b.recv()
    assert m["f"][7] == a.name, "SENDER not rewritten: %r" % m["f"].get(7)
    assert m["f"][6] == NAME
    assert m["body_off"] % 8 == 0, "body not 8-aligned"
    assert m["body"] == body, "body corrupted in transit"
    assert len(m["fds"]) == 1, "fd not forwarded"
    got = os.pread(m["fds"][0], 64, 0)
    os.close(m["fds"][0])
    assert got == payload, "fd content mismatch: %r" % got
    print("  ok: 8-byte alignment, a{sv} pass-through, SENDER injection, SCM_RIGHTS")

    # reply with an 8-aligned body
    b.serial += 1
    rb = marsh("t", [0xDEADBEEFCAFEBABE])
    b.send(msg(2, b.serial, [(5, "u", a.serial), (6, "s", a.name), (8, "g", "t")], rb))
    r = a.recv()
    assert r["type"] == 2 and r["body"] == rb
    print("  ok: method_return framing")

    # matched signal
    b.serial += 1
    sb = marsh("s", ["hello"])
    b.send(msg(4, b.serial, [(1, "o", "/org/whitecube/Test"), (2, "s", "org.whitecube.Test"),
                             (3, "s", "Pong"), (8, "g", "s")], sb))
    sig = a.recv()
    assert sig["type"] == 4 and sig["f"][3] == "Pong" and sig["f"][7] == b.name
    print("  ok: broadcast signal via well-known sender match")

    # non-matching signal must NOT arrive
    b.serial += 1
    b.send(msg(4, b.serial, [(1, "o", "/org/whitecube/Test"), (2, "s", "org.other.Iface"),
                             (3, "s", "Nope"), (8, "g", "s")], marsh("s", ["no"])))
    b.serial += 1
    b.send(msg(4, b.serial, [(1, "o", "/org/whitecube/Test"), (2, "s", "org.whitecube.Test"),
                             (3, "s", "Pong"), (8, "g", "s")], marsh("s", ["second"])))
    nxt = a.recv()
    assert nxt["f"][2] == "org.whitecube.Test", "match filtering leaked a signal"
    print("  ok: match filtering excludes non-matching interface")

    # arg0namespace + arg1 + destination + method_return matching
    a.call("AddMatch", "s", [
        "type = 'signal' , interface = 'org.whitecube.Ns' , arg0namespace = 'org.whitecube.Sub' , arg1 = 'hit'"
    ])
    b.serial += 1
    b.send(msg(4, b.serial, [(1, "o", "/org/whitecube/Test"), (2, "s", "org.whitecube.Ns"),
                             (3, "s", "Ev"), (8, "g", "ss")], marsh("ss", ["org.whitecube.SubEvil", "hit"])))
    b.serial += 1
    b.send(msg(4, b.serial, [(1, "o", "/org/whitecube/Test"), (2, "s", "org.whitecube.Ns"),
                             (3, "s", "Ev"), (8, "g", "ss")], marsh("ss", ["org.whitecube.Sub.Leaf", "hit"])))
    ns_sig = a.recv()
    assert b"org.whitecube.Sub.Leaf" in ns_sig["body"], ns_sig
    print("  ok: arg0namespace boundary + arg1 match rule")

    # filtered BecomeMonitor catches type='method_return' and destination
    mon = Peer()
    mon.call("BecomeMonitor", "asu", [[f"type='method_return',destination='{a.name}'"], 0],
             iface="org.freedesktop.DBus.Monitoring")
    b.serial += 1
    b.send(msg(2, b.serial, [(5, "u", 99), (6, "s", a.name), (8, "g", "s")], marsh("s", ["monitored-ret"])))
    got_a = a.recv()
    got_m = mon.recv()
    assert got_a["type"] == 2 and got_m["type"] == 2 and got_m["body"] == got_a["body"]
    print("  ok: filtered BecomeMonitor matches method_return + destination")

    # UpdateActivationEnvironment accepted safely
    a.call("UpdateActivationEnvironment", "a{ss}", [{}])
    print("  ok: UpdateActivationEnvironment returns cleanly")

    # malformed truncated header (bounds safety check)
    bad = Peer()
    hdr = bytes([ord('l'), 1, 0, 1]) + u32(0) + u32(1) + u32(6) + bytes([5, 1, ord('u'), 0, 0, 0])
    bad.send(pad(hdr, 8))
    try:
        r_bad = bad.recv(timeout=0.5)
        raise AssertionError(f"expected disconnect on malformed header, got {r_bad}")
    except EOFError:
        pass
    print("  ok: truncated u32 header field rejected cleanly without OOB read")
    print("WIRE TEST PASSED")

main()
