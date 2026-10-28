#!/usr/bin/env python3
"""Exercise real Wayland presentation and keyboard UI on a private Whitecube.

Whitecube currently has no virtual-keyboard or screencopy extension. The test
therefore relays the browser's connection through a private protocol proxy. The
real compositor still creates/configures/maps the window and consumes its shm
buffers. Only keyboard events are supplied by the test proxy, to that single
client. No desktop socket, physical input device, or user configuration is used.
"""
from __future__ import annotations

import argparse
import array
import collections
import ctypes
import ctypes.util
import hashlib
import http.server
import os
from pathlib import Path
import select
import signal
import sqlite3
import socket
import struct
import subprocess
import tempfile
import threading
import time
import zlib


def u32(*values):
    return struct.pack("=" + "I" * len(values), *values)


def message(obj, opcode, payload=b""):
    return u32(obj, ((8 + len(payload)) << 16) | opcode) + payload


def string_at(payload, start=0):
    length, = struct.unpack_from("=I", payload, start)
    return payload[start + 4:start + 3 + length].decode("utf-8", "replace"), start + 4 + (length + 3) // 4 * 4


def wait_for(check, description, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.025)
    raise AssertionError(f"timed out: {description}")


class WaylandProxy:
    """Observe buffers and feed keyboard events without changing application code."""
    def __init__(self, endpoint: Path, upstream: Path, test_seat=False):
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(str(endpoint))
        self.listener.listen(1)
        self.upstream = upstream
        self.test_seat = test_seat
        self.test_seat_global = 0xfffffff0
        self.forward_fds = []
        self.server_forward_fds = []
        self.stop = threading.Event()
        self.lock = threading.RLock()
        self.send_lock = threading.Lock()
        self.client = None
        self.server = None
        self.error = None
        self.objects = {1: "wl_display"}
        self.client_bytes = b""
        self.server_bytes = b""
        self.fds = collections.deque()
        self.pools = {}
        self.buffers = {}
        self.attached = {}
        self.main_surface = None
        self.keyboard = None
        self.latest_buffer = None
        self.title = ""
        self.app_id = ""
        self.commits = 0
        self.releases = 0
        self.configures = 0
        self.acks = 0
        self.serial = 100000
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    @staticmethod
    def forward(destination, data, fds):
        ancillary = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", fds))] if fds else []
        sent = destination.sendmsg([data], ancillary)
        if sent < len(data):
            destination.sendall(data[sent:])

    def run(self):
        try:
            self.listener.settimeout(10)
            self.client, _ = self.listener.accept()
            self.server = socket.socket(socket.AF_UNIX)
            self.server.connect(str(self.upstream))
            while not self.stop.is_set():
                ready, _, _ = select.select([self.client, self.server], [], [], 0.1)
                for source in ready:
                    data, ancillary, flags, _ = source.recvmsg(65536, socket.CMSG_SPACE(64 * 4))
                    assert not flags & socket.MSG_CTRUNC, "truncated Wayland descriptor message"
                    fds = []
                    for level, kind, raw in ancillary:
                        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                            values = array.array("i")
                            values.frombytes(raw[:len(raw) // values.itemsize * values.itemsize])
                            fds.extend(values)
                    if not data:
                        for fd in fds:
                            os.close(fd)
                        return
                    try:
                        if source is self.client:
                            self.forward_fds.extend(os.dup(fd) for fd in fds)
                            forwarded = self.inspect_client(data, fds)
                            if forwarded:
                                self.forward(self.server, forwarded, self.forward_fds)
                                for fd in self.forward_fds:
                                    os.close(fd)
                                self.forward_fds.clear()
                        else:
                            self.server_forward_fds.extend(os.dup(fd) for fd in fds)
                            forwarded = self.inspect_server(data)
                            if forwarded:
                                with self.send_lock:
                                    self.forward(self.client, forwarded, self.server_forward_fds)
                                for fd in self.server_forward_fds:
                                    os.close(fd)
                                self.server_forward_fds.clear()
                    finally:
                        for fd in fds:
                            os.close(fd)
        except (BrokenPipeError, ConnectionResetError):
            # libwayland can close with unread pending events on orderly exit.
            return
        except BaseException as error:
            if not self.stop.is_set():
                self.error = error

    def inspect_client(self, data, fds):
        forwarded = bytearray()
        with self.lock:
            self.fds.extend(os.dup(fd) for fd in fds)
            self.client_bytes += data
            while len(self.client_bytes) >= 8:
                obj, word = struct.unpack_from("=II", self.client_bytes)
                size, opcode = word >> 16, word & 65535
                assert size >= 8
                if len(self.client_bytes) < size:
                    break
                raw = self.client_bytes[:size]
                body, self.client_bytes = self.client_bytes[8:size], self.client_bytes[size:]
                kind = self.objects.get(obj)
                suppress = kind in ("test_seat", "test_keyboard", "test_data_device")
                if kind == "wl_display" and opcode == 1:
                    registry, = struct.unpack_from("=I", body)
                    self.objects[registry] = "wl_registry"
                    if self.test_seat:
                        # Weston headless has no physical input seat. Expose one
                        # only to this isolated test client; presentation still
                        # traverses the real compositor unchanged.
                        interface = b"wl_seat\0"
                        payload = u32(self.test_seat_global, len(interface)) + interface + b"\0" * (-len(interface) % 4) + u32(5)
                        self.send_events([message(registry, 0, payload)])
                elif kind == "wl_registry" and opcode == 0:
                    interface, end = string_at(body, 4)
                    _, new_id = struct.unpack_from("=II", body, end)
                    self.objects[new_id] = interface
                    global_name, = struct.unpack_from("=I", body)
                    if self.test_seat and global_name == self.test_seat_global:
                        suppress = False
                        raw = message(1, 1, u32(new_id))  # reserve the client id upstream
                        self.objects[new_id] = "test_seat"
                        self.send_events([message(new_id, 0, u32(2))])  # keyboard capability
                elif kind == "test_seat" and opcode == 1:
                    self.keyboard, = struct.unpack_from("=I", body)
                    self.objects[self.keyboard] = "test_keyboard"
                    suppress = False
                    raw = message(1, 1, u32(self.keyboard))
                elif kind == "wl_data_device_manager" and opcode == 1 and self.test_seat:
                    device, seat = struct.unpack_from("=II", body)
                    if self.objects.get(seat) == "test_seat":
                        suppress = False
                        raw = message(1, 1, u32(device))
                        self.objects[device] = "test_data_device"
                elif kind == "wl_compositor" and opcode == 0:
                    self.objects[struct.unpack_from("=I", body)[0]] = "wl_surface"
                elif kind == "wl_seat" and opcode == 1:
                    self.keyboard, = struct.unpack_from("=I", body)
                    self.objects[self.keyboard] = "wl_keyboard"
                elif kind == "wl_shm" and opcode == 0:
                    pool, size = struct.unpack_from("=II", body)
                    assert self.fds, "missing shared-memory descriptor"
                    self.pools[pool] = self.fds.popleft()
                    self.objects[pool] = "wl_shm_pool"
                elif kind == "wl_shm_pool" and opcode == 0:
                    buffer, offset, width, height, stride, format_ = struct.unpack("=IIIIII", body)
                    assert format_ in (0, 1), "unexpected non-RGB shared-memory format"
                    self.buffers[buffer] = (os.dup(self.pools[obj]), offset, width, height, stride)
                    self.objects[buffer] = "wl_buffer"
                elif kind == "wl_shm_pool" and opcode == 1:
                    os.close(self.pools.pop(obj))
                    self.objects.pop(obj, None)
                elif kind == "wl_buffer" and opcode == 0:
                    old = self.buffers.pop(obj, None)
                    if old:
                        os.close(old[0])
                    self.objects.pop(obj, None)
                elif kind == "wl_surface" and opcode == 1:
                    self.attached[obj], = struct.unpack_from("=I", body)
                elif kind == "wl_surface" and opcode == 6 and obj == self.main_surface:
                    buffer = self.attached.get(obj)
                    if buffer:
                        self.latest_buffer = buffer
                        self.commits += 1
                elif kind == "xdg_wm_base" and opcode == 2:
                    xdg, self.main_surface = struct.unpack_from("=II", body)
                    self.objects[xdg] = "xdg_surface"
                elif kind == "xdg_surface" and opcode == 1:
                    self.objects[struct.unpack_from("=I", body)[0]] = "xdg_toplevel"
                elif kind == "xdg_surface" and opcode == 4:
                    self.acks += 1
                elif kind == "xdg_toplevel" and opcode == 2:
                    self.title, _ = string_at(body)
                elif kind == "xdg_toplevel" and opcode == 3:
                    self.app_id, _ = string_at(body)
                if not suppress:
                    forwarded.extend(raw)
        return bytes(forwarded)

    def inspect_server(self, data):
        forwarded = bytearray()
        with self.lock:
            self.server_bytes += data
            while len(self.server_bytes) >= 8:
                obj, word = struct.unpack_from("=II", self.server_bytes)
                size, opcode = word >> 16, word & 65535
                assert size >= 8
                if len(self.server_bytes) < size:
                    break
                raw = self.server_bytes[:size]
                body, self.server_bytes = self.server_bytes[8:size], self.server_bytes[size:]
                kind = self.objects.get(obj)
                if kind in ("test_seat", "test_keyboard", "test_data_device"):
                    continue
                forwarded.extend(raw)
                if obj == 1 and opcode == 0:
                    raise AssertionError(f"Wayland protocol error: {body!r}")
                if kind == "xdg_surface" and opcode == 0:
                    self.configures += 1
                if kind == "wl_buffer" and opcode == 0:
                    self.releases += 1
        return bytes(forwarded)

    def send_events(self, events, fd=None):
        if self.error:
            raise self.error
        assert self.client is not None
        with self.send_lock:
            self.forward(self.client, b"".join(events), [fd] if fd is not None else [])

    def keyboard_ready(self):
        wait_for(lambda: self.keyboard and self.main_surface, "keyboard and browser surface")
        xkb = ctypes.CDLL(ctypes.util.find_library("xkbcommon"))
        xkb.xkb_context_new.argtypes = [ctypes.c_int]
        xkb.xkb_context_new.restype = ctypes.c_void_p
        xkb.xkb_keymap_new_from_names.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
        xkb.xkb_keymap_new_from_names.restype = ctypes.c_void_p
        xkb.xkb_keymap_get_as_string.argtypes = [ctypes.c_void_p, ctypes.c_int]
        xkb.xkb_keymap_get_as_string.restype = ctypes.c_void_p
        xkb.xkb_keymap_mod_get_index.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        xkb.xkb_keymap_mod_get_index.restype = ctypes.c_uint
        xkb.xkb_keymap_unref.argtypes = [ctypes.c_void_p]
        xkb.xkb_context_unref.argtypes = [ctypes.c_void_p]
        libc = ctypes.CDLL(None)
        libc.free.argtypes = [ctypes.c_void_p]
        class Names(ctypes.Structure):
            _fields_ = [(name, ctypes.c_char_p) for name in ("rules", "model", "layout", "variant", "options")]
        names = Names(b"evdev", b"pc105", b"us", None, None)
        context = xkb.xkb_context_new(0)
        keymap = xkb.xkb_keymap_new_from_names(context, ctypes.byref(names), 0)
        assert context and keymap, "cannot create test keymap"
        address = xkb.xkb_keymap_get_as_string(keymap, 1)
        mapping = ctypes.string_at(address) + b"\0"
        libc.free(address)
        self.control = 1 << xkb.xkb_keymap_mod_get_index(keymap, b"Control")
        self.shift = 1 << xkb.xkb_keymap_mod_get_index(keymap, b"Shift")
        self.alt = 1 << xkb.xkb_keymap_mod_get_index(keymap, b"Mod1")
        xkb.xkb_keymap_unref(keymap)
        xkb.xkb_context_unref(context)
        fd = os.memfd_create("cube-e2e-keymap", os.MFD_CLOEXEC)
        try:
            os.write(fd, mapping)
            self.send_events([message(self.keyboard, 0, u32(1, len(mapping)))], fd)
            self.send_events([message(self.keyboard, 1, u32(self.serial, self.main_surface, 0)),
                              message(self.keyboard, 5, u32(0, 250))])
        finally:
            os.close(fd)

    def key(self, code, modifiers=0):
        self.serial += 1
        stamp = int(time.monotonic() * 1000) & 0xffffffff
        self.send_events([
            message(self.keyboard, 4, u32(self.serial, modifiers, 0, 0, 0)),
            message(self.keyboard, 3, u32(self.serial, stamp, code, 1)),
            message(self.keyboard, 3, u32(self.serial, stamp + 1, code, 0)),
            message(self.keyboard, 4, u32(self.serial, 0, 0, 0, 0)),
        ])

    def type(self, text):
        letters = dict(zip("qwertyuiopasdfghjklzxcvbnm", (16,17,18,19,20,21,22,23,24,25,30,31,32,33,34,35,36,37,38,44,45,46,47,48,49,50)))
        plain = {**letters, **{str(i): i + 1 for i in range(1, 10)}, "0": 11, "/": 53, ".": 52, "-": 12, " ": 57, "=": 13}
        shifted = {":": 39, "_": 12, "?": 53, "&": 8, "+": 13, "#": 4, "%": 6, "@": 3}
        for char in text:
            if char in plain:
                self.key(plain[char])
            elif char in shifted:
                self.key(shifted[char], self.shift)
            elif char.lower() in letters:
                self.key(letters[char.lower()], self.shift)
            else:
                raise AssertionError(f"unsupported test input {char!r}")

    def snapshot(self):
        if self.error:
            raise self.error
        with self.lock:
            entry = self.buffers.get(self.latest_buffer)
            if entry is None:
                return None
            fd, offset, width, height, stride = entry
            data = os.pread(fd, stride * height, offset)
        assert len(data) == stride * height
        return data, width, height, stride

    def has_color(self, value):
        frame = self.snapshot()
        if not frame:
            return False
        return frame[0].count(struct.pack("<I", 0xff000000 | value)) > 4000

    def close(self):
        self.stop.set()
        for sock in (self.listener, self.client, self.server):
            if sock:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                sock.close()
        self.thread.join(timeout=2)
        for entry in self.buffers.values():
            os.close(entry[0])
        for fd in self.pools.values():
            os.close(fd)
        for fd in self.fds:
            os.close(fd)
        for fd in self.forward_fds:
            os.close(fd)
        for fd in self.server_forward_fds:
            os.close(fd)


def save_frame(proxy, directory, name):
    if directory is None:
        return
    directory.mkdir(parents=True, exist_ok=True)
    data, width, height, stride = proxy.snapshot()
    rows = bytearray()
    for y in range(height):
        row = data[y * stride:y * stride + width * 4]
        rgba = bytearray(len(row))
        rgba[0::4], rgba[1::4], rgba[2::4], rgba[3::4] = row[2::4], row[1::4], row[0::4], row[3::4]
        rows.extend(b"\0")
        rows.extend(rgba)
    def chunk(kind, content):
        return struct.pack("!I", len(content)) + kind + content + struct.pack("!I", zlib.crc32(kind + content))
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack("!IIBBBBB", width, height, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")
    (directory / name).write_bytes(png)


def stop_process(process):
    if process and process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=4)


def exercise_interactions(proxy, origin, requests, artifacts):
    """Use the same real wl_keyboard path for page focus, forms and anchors."""
    visits = 0
    def open_interaction_page():
        nonlocal visits
        visits += 1
        path = f'/interaction?test={visits}'
        previous = requests.count(path)
        proxy.key(38, proxy.control)
        proxy.type(origin + path)
        proxy.key(28)
        wait_for(lambda: requests.count(path) > previous and
                 proxy.title == 'Cube E2E interactions' and proxy.has_color(0xd6e3f0),
                 'interactive HTTP page and controls')

    def focus_key(modifiers=0):
        commits = proxy.commits
        proxy.key(15, modifiers)
        wait_for(lambda: proxy.commits > commits, 'page focus repaint')
        # A human-sized key interval also lets a newly focused widget receive
        # its renderer response before the subsequent activation key.
        time.sleep(.04)

    open_interaction_page()
    before = hashlib.sha256(proxy.snapshot()[0]).digest()
    proxy.key(38, proxy.control)  # Focus omnibox; Tab must enter page traversal.
    focus_key()
    wait_for(lambda: hashlib.sha256(proxy.snapshot()[0]).digest() != before,
             'visible keyboard focus on the first page link')
    count = len(requests)
    proxy.key(28)
    wait_for(lambda: proxy.has_color(0x6a9f58), 'Enter followed same-document anchor')
    assert len(requests) == count, 'anchor activation fetched the document again'
    save_frame(proxy, artifacts, 'wayland-anchor.png')
    proxy.key(105, proxy.alt)
    wait_for(lambda: proxy.has_color(0xd6e3f0), 'back from fragment returned to document top')
    assert len(requests) == count, 'same-document back navigation issued an HTTP request'
    proxy.key(106, proxy.alt)
    wait_for(lambda: proxy.has_color(0x6a9f58), 'forward restored the anchored section')
    assert len(requests) == count, 'same-document forward navigation issued an HTTP request'

    open_interaction_page()
    focus_key()  # Anchor link.
    focus_key()  # Ordinary link.
    focus_key(proxy.shift)  # Back to anchor link.
    focus_key()  # Ordinary link again.
    proxy.key(28)
    wait_for(lambda: '/visited' in requests and proxy.title == 'Cube E2E visited link',
             'Tab and Shift+Tab traversal activated the intended ordinary link')
    proxy.key(105, proxy.alt)
    wait_for(lambda: proxy.title == 'Cube E2E interactions' and proxy.has_color(0xd6e3f0),
             'back from ordinary link restored the form document')

    focus_key()  # Anchor link.
    focus_key()  # Ordinary link.
    # Type immediately after Tab: keyboard events can arrive before the
    # renderer's focus acknowledgement, and must retain their event order.
    proxy.key(15)  # Editable input; hidden input is not a tab stop.
    proxy.key(30, proxy.control)  # Ctrl+A selects the existing input value.
    proxy.type('cube + &')
    focus_key()  # Named submit button.
    proxy.key(28)
    expected = '/submit?token=a%26b&q=cube+%2B+%26&go=run'
    wait_for(lambda: expected in requests and proxy.title == 'Cube E2E form submitted',
             'edited GET form submitted exact URL-encoded fields')
    submitted = [path for path in requests if path.startswith('/submit?')]
    assert submitted == [expected], f'unexpected GET submissions: {submitted!r}'
    save_frame(proxy, artifacts, 'wayland-form-submitted.png')
    proxy.key(105, proxy.alt)
    wait_for(lambda: proxy.title == 'Cube E2E interactions' and proxy.has_color(0xd6e3f0),
             'back from form results returned to the form page')

    proxy.key(38, proxy.control)
    proxy.type(origin + '/post-form')
    proxy.key(28)
    wait_for(lambda: proxy.title == 'Cube E2E blocked PUT' and proxy.has_color(0xc1d5b0),
             'unsupported PUT form is displayed')
    focus_key()  # Editable input.
    proxy.type('keep this local')
    focus_key()  # Submit button.
    count, commits = len(requests), proxy.commits
    before = hashlib.sha256(proxy.snapshot()[0]).digest()
    proxy.key(28)
    wait_for(lambda: proxy.commits > commits and hashlib.sha256(proxy.snapshot()[0]).digest() != before,
             'PUT rejection status repaint')
    time.sleep(.15)
    assert len(requests) == count and proxy.title == 'Cube E2E blocked PUT', \
        'unsupported PUT submission navigated or issued a network request'
    assert not any('/must-not-submit' in path for path in requests), 'unsupported form reached the server'
    save_frame(proxy, artifacts, 'wayland-post-blocked.png')


def exercise_sessions(proxy, origin, requests, observations):
    proxy.key(38, proxy.control)
    proxy.type(origin + '/login')
    proxy.key(28)
    wait_for(lambda: proxy.title == 'Cube E2E login', 'login form')
    proxy.key(15)
    proxy.type('alice')
    proxy.key(15)
    proxy.type('secret + &')
    proxy.key(15)
    proxy.key(28)
    wait_for(lambda: proxy.title == 'Cube E2E account', 'POST and redirect login')
    assert observations['post'] == b'user=alice&password=secret+%2B+%26'
    assert observations['account_cookie'] == 'session=ok'
    assert not any('secret' in path for path in requests), 'password leaked to URL'
    proxy.key(19, proxy.control)
    wait_for(lambda: requests.count('/account') >= 2, 'session survived reload')
    proxy.key(38, proxy.control)
    proxy.type(origin + '/logout')
    proxy.key(28)
    wait_for(lambda: proxy.title == 'Cube E2E logged out', 'logout expired cookie')
    assert not observations['logout_cookie']
    proxy.key(38, proxy.control)
    proxy.type(origin + '/cached')
    proxy.key(28)
    wait_for(lambda: proxy.title == 'Cube E2E cached', 'cache initial body')
    proxy.key(19, proxy.control)
    wait_for(lambda: observations.get('conditional') and proxy.title == 'Cube E2E cached',
             '304 reused validated body')


def exercise_scripts(proxy, origin, requests, async_started, async_release):
    proxy.key(38, proxy.control); proxy.type(origin + '/scripted'); proxy.key(28)
    wait_for(lambda: proxy.title == 'Cube E2E scripted 1 1', 'JavaScript storage on first visit')
    proxy.key(15); proxy.type('edited by keyboard'); proxy.key(15); proxy.key(28)
    wait_for(lambda: proxy.has_color(0x448877), 'submit event prevented navigation and repainted the DOM')
    assert '/must-not-submit-js' not in requests
    proxy.key(15); proxy.key(28)
    wait_for(lambda: proxy.title == 'Cube E2E clicked', 'keyboard activation fired JavaScript click')
    proxy.key(19, proxy.control)
    wait_for(lambda: proxy.title == 'Cube E2E scripted 2 2', 'localStorage and sessionStorage survived reload')
    proxy.key(38, proxy.control); proxy.type(origin.replace('127.0.0.1','localhost') + '/scripted'); proxy.key(28)
    wait_for(lambda: proxy.title == 'Cube E2E scripted 1 1', 'storage isolated from a different origin')
    proxy.key(38, proxy.control); proxy.type(origin + '/async-cancel'); proxy.key(28)
    wait_for(async_started.is_set, 'asynchronous fetch started')
    proxy.key(38, proxy.control); proxy.type('cube:home'); proxy.key(28)
    wait_for(lambda: proxy.title == 'Private browsing — Cube Browser', 'navigation cancelled JavaScript fetch')
    async_release.set(); time.sleep(.1)
    assert proxy.title == 'Private browsing — Cube Browser', 'old async callback modified the new document'


def exercise_workflows(proxy, origin, requests, root, download_started, cancel_started, artifacts):
    proxy.key(38, proxy.control);proxy.type(origin + '/scripted');proxy.key(28)
    wait_for(lambda: proxy.title.startswith('Cube E2E scripted'), 'scripted page before site settings')
    proxy.key(51, proxy.control)  # Ctrl+, browser settings.
    wait_for(lambda: proxy.has_color(0xc9c9c9), 'native settings panel')
    save_frame(proxy, artifacts, 'wayland-settings.png')
    proxy.key(108);proxy.key(28)  # Site JavaScript toggle.
    wait_for(lambda: proxy.title == 'Cube E2E script source', 'site scripts disabled and page reloaded')
    proxy.key(19, proxy.control)
    wait_for(lambda: proxy.title == 'Cube E2E script source', 'site script choice retained')
    proxy.key(51, proxy.control);proxy.key(108);proxy.key(28)
    wait_for(lambda: proxy.title.startswith('Cube E2E scripted'), 'site scripts reenabled')
    proxy.key(88)  # F12, page console.
    wait_for(lambda: proxy.has_color(0xf0f0f0), 'native console panel')
    save_frame(proxy, artifacts, 'wayland-console.png')
    commits=proxy.commits
    proxy.key(15)  # Network tab.
    wait_for(lambda: proxy.commits>commits, 'network diagnostics frame')
    save_frame(proxy, artifacts, 'wayland-network.png')
    proxy.key(1)
    proxy.key(38, proxy.control);proxy.type(origin + '/download.bin');proxy.key(28)
    wait_for(lambda: proxy.title == 'Unable to open page', 'binary response offers download')
    proxy.key(31, proxy.control)  # Ctrl+S saves URL.
    wait_for(download_started.is_set, 'bounded download transfer')
    wait_for(lambda: proxy.has_color(0x1a1a1a), 'visible download progress')
    save_frame(proxy, artifacts, 'wayland-download-progress.png')
    target=root/'home/Downloads/download.bin'
    wait_for(lambda: target.exists() and target.stat().st_size == 4*1024*1024, 'completed download file')
    assert target.read_bytes() == b'd'*(4*1024*1024)
    assert target.stat().st_mode & 0o777 == 0o600
    wait_for(lambda: not proxy.has_color(0x1a1a1a), 'download saved frame replaces progress')
    save_frame(proxy, artifacts, 'wayland-download-complete.png')
    proxy.key(1)
    proxy.key(38, proxy.control);proxy.type(origin + '/cancel.bin');proxy.key(28)
    wait_for(lambda: proxy.title == 'Unable to open page', 'cancel fixture address')
    proxy.key(31, proxy.control)
    wait_for(cancel_started.is_set, 'cancelled transfer started')
    proxy.key(1)
    time.sleep(.1)
    assert not (root/'home/Downloads/cancel.bin').exists(), 'cancelled transfer produced a file'
    proxy.key(1)
    proxy.key(38, proxy.control);proxy.type(origin + '/scripted');proxy.key(28)
    wait_for(lambda: proxy.title.startswith('Cube E2E scripted'), 'page before clearing private data')
    proxy.key(51, proxy.control)
    for _ in range(6): proxy.key(108)
    proxy.key(28)
    wait_for(lambda: proxy.title == 'Cube E2E scripted 1 1', 'clearing private origin storage resets script state')
    settings=root/'state/whitecube/accessibility.json';settings.parent.mkdir(parents=True,exist_ok=True)
    before=hashlib.sha256(proxy.snapshot()[0]).digest()
    settings.write_text('{"ui_scale":1.5}')
    wait_for(lambda: hashlib.sha256(proxy.snapshot()[0]).digest()!=before, 'live interface and page scaling')
    commits=proxy.commits;settings.write_text('{"ui_scale":1.0}')
    wait_for(lambda: proxy.commits>commits, 'interface scale restored')



def exercise_responsive(proxy, origin, requests, artifacts, state_directory, responsive):
    """Observe CSS environment changes and static vector/table content on Wayland."""
    proxy.key(11, proxy.control)  # Ctrl+0 restores a known page zoom.
    responsive['breakpoint'] = int(proxy.snapshot()[1] * .9)
    proxy.key(38, proxy.control)
    proxy.type(origin + '/responsive')
    proxy.key(28)
    wait_for(lambda: proxy.title == 'Cube E2E responsive table' and
             proxy.has_color(0xc6dcf0) and proxy.has_color(0xb04acd) and
             proxy.has_color(0xb5dcc7) and proxy.has_color(0xdbc9ed),
             'external SVG and table cells in the wide responsive document')
    save_frame(proxy, artifacts, 'wayland-responsive-wide.png')
    count = len(requests)

    for _ in range(2):  # Table link, then table input.
        commits = proxy.commits
        proxy.key(15)
        wait_for(lambda: proxy.commits > commits, 'table focus repaint')
        time.sleep(.04)
    proxy.key(30, proxy.control)
    proxy.type('responsive value')
    proxy.key(13, proxy.control)
    proxy.key(13, proxy.control)
    wait_for(lambda: proxy.has_color(0xe2b86d), 'page zoom crossed the CSS-pixel media breakpoint')
    assert len(requests) == count, 'zoom must reuse the existing stylesheet and image'
    save_frame(proxy, artifacts, 'wayland-responsive-narrow.png')

    preferences = state_directory / 'whitecube' / 'accessibility.json'
    preferences.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    preferences.write_text('{"ui_scale":1.0,"contrast":"normal","reduced_motion":true}\n')
    preferences.chmod(0o600)
    wait_for(lambda: proxy.has_color(0xcb7094), 'live reduced-motion media query repaint')
    assert len(requests) == count, 'preference changes must not refetch static resources'
    save_frame(proxy, artifacts, 'wayland-responsive-preferences.png')

    commits = proxy.commits
    proxy.key(15)  # Submit button after zoom and environment relayouts.
    wait_for(lambda: proxy.commits > commits, 'table submit focus')
    time.sleep(.04)
    proxy.key(28)
    expected = '/table-submit?cell=responsive+value&action=apply'
    wait_for(lambda: expected in requests and proxy.title == 'Cube E2E table submitted',
             'table input value survived responsive and preference changes')
    proxy.key(105, proxy.alt)
    wait_for(lambda: proxy.title == 'Cube E2E responsive table' and proxy.has_color(0xe2b86d),
             'back returned to the responsive table')
    commits = proxy.commits
    proxy.key(15)
    wait_for(lambda: proxy.commits > commits, 'table link focus')
    time.sleep(.04)
    proxy.key(28)
    wait_for(lambda: '/table-link' in requests and proxy.title == 'Cube E2E table link',
             'table-cell link activated through page keyboard traversal')


def exercise_bookmarks(browser, browser_env, upstream, root, origin, log, artifacts, test_seat):
    """Exercise Ctrl+D persistence, the library shortcut, and private nonwriting."""
    database = root / "data/cube-browser/bookmarks.sqlite3"
    saved_url = origin + "/bookmarks-fixture"

    def saved():
        if not database.exists():
            return False
        try:
            with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
                return db.execute("SELECT count(*) FROM bookmarks WHERE url=?", (saved_url,)).fetchone()[0] == 1
        except sqlite3.OperationalError:
            return False

    def launch(name, arguments, title):
        proxy = WaylandProxy(root / "runtime" / name, upstream, test_seat=test_seat)
        try:
            process = subprocess.Popen([str(browser), "--quit-after", "25", *arguments],
                env=dict(browser_env, WAYLAND_DISPLAY=name), stdout=log, stderr=log, start_new_session=True)
            wait_for(lambda: proxy.title == title and proxy.commits >= 2, name + " frame")
            proxy.keyboard_ready()
            return process, proxy
        except BaseException:
            if "process" in locals():
                stop_process(process)
            proxy.close()
            raise

    def close(process, proxy):
        try:
            proxy.key(17, proxy.control)
            assert process.wait(timeout=5) == 0, "bookmark window failed to close"
            assert proxy.error is None, f"bookmark protocol proxy failed: {proxy.error}"
        finally:
            stop_process(process)
            proxy.close()

    process, proxy = launch("bookmarks-save", [saved_url], "Cube E2E navigation")
    try:
        proxy.key(32, proxy.control)  # Ctrl+D.
        wait_for(saved, "bookmark saved to the normal profile")
        proxy.key(48, proxy.control | proxy.shift)  # Ctrl+Shift+B.
        wait_for(lambda: proxy.title == "Bookmarks — Cube Browser", "bookmark library shortcut")
        save_frame(proxy, artifacts, "wayland-bookmarks.png")
    finally:
        close(process, proxy)
    assert saved(), "bookmark was lost when the normal window closed"
    before = (database.read_bytes(), database.stat().st_mtime_ns)

    process, proxy = launch("bookmarks-private", ["--private", "cube:bookmarks"], "Bookmarks — Cube Browser")
    try:
        save_frame(proxy, artifacts, "wayland-private-bookmarks.png")
        for url in (saved_url, origin + "/private-bookmark"):
            proxy.key(38, proxy.control); proxy.type(url); proxy.key(28)
            wait_for(lambda: proxy.title == "Cube E2E navigation", "private page before bookmark mutation")
            commits = proxy.commits
            proxy.key(32, proxy.control)
            wait_for(lambda: proxy.commits > commits, "private bookmark action status")
            assert (database.read_bytes(), database.stat().st_mtime_ns) == before, "private bookmark action changed saved pages"
        assert saved(), "private window removed a normal bookmark"
    finally:
        close(process, proxy)
    assert (database.read_bytes(), database.stat().st_mtime_ns) == before

    process, proxy = launch("bookmarks-remove", [saved_url], "Cube E2E navigation")
    try:
        proxy.key(32, proxy.control)
        wait_for(lambda: not saved(), "Ctrl+D removed a bookmark saved by an earlier window")
    finally:
        close(process, proxy)
    print("e2e: Ctrl+D bookmark persistence/removal, Ctrl+Shift+B library, private read and nonwrite passed")


def run(options):
    browser = options.browser.resolve()
    compositor = options.compositor.resolve()
    compositor_args = []
    compositor_env = {}
    if options.weston_dir:
        weston = options.weston_dir.resolve()
        compositor = weston / "usr/bin/weston"
        backends = list(weston.glob("usr/lib/*/libweston-*/headless-backend.so"))
        assert len(backends) == 1, f"expected one Weston headless backend beneath {weston}"
        backend = backends[0]
        lib = backend.parent.parent
        shell = lib / "weston/kiosk-shell.so"
        assert shell.is_file(), f"Weston kiosk shell missing: {shell}"
        compositor_env["LD_LIBRARY_PATH"] = os.pathsep.join(filter(None,
            (str(lib), str(lib / "weston"), os.environ.get("LD_LIBRARY_PATH"))))
        compositor_env["WESTON_MODULE_MAP"] = f"headless-backend.so={backend};kiosk-shell.so={shell}"
        compositor_args = ["--backend=headless", f"--shell={shell}", "--renderer=pixman",
                           "--no-config", "--socket=wayland-0", "--width=1280", "--height=960", "--idle-time=0"]
    assert browser.is_file(), f"browser binary missing: {browser}"
    if not compositor.is_file():
        print(f"SKIP: headless compositor unavailable: {compositor}")
        return 77
    with tempfile.TemporaryDirectory(prefix="cube-e2e-") as temp:
        root = Path(temp)
        for name in ("home", "runtime", "state", "config", "cache", "data"):
            (root / name).mkdir(mode=0o700)
        env = dict(os.environ, XDG_DOWNLOAD_DIR=str(root / "home/Downloads"), HOME=str(root / "home"), XDG_RUNTIME_DIR=str(root / "runtime"),
                   XDG_STATE_HOME=str(root / "state"), XDG_CONFIG_HOME=str(root / "config"),
                   XDG_CACHE_HOME=str(root / "cache"), XDG_DATA_HOME=str(root / "data"),
                   WLR_BACKENDS="headless", WLR_RENDERER="pixman", WLR_HEADLESS_OUTPUTS="1",
                   WAYLAND_DISPLAY="", NO_PROXY="127.0.0.1,localhost")
        for name in ("DISPLAY", "WAYLAND_SOCKET", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy", "WAYLAND_DEBUG"):
            env.pop(name, None)
        requests = []
        slow_started = threading.Event()
        slow_release = threading.Event()
        def png_chunk(kind, content):
            return struct.pack('!I', len(content)) + kind + content + struct.pack('!I', zlib.crc32(kind + content))
        png = (b'\x89PNG\r\n\x1a\n' + png_chunk(b'IHDR', struct.pack('!IIBBBBB', 8, 4, 8, 2, 0, 0, 0)) +
               png_chunk(b'IDAT', zlib.compress((b'\0'+bytes((216, 87, 35))*8)*4)) + png_chunk(b'IEND', b''))
        css = b'.hero{padding:24px;background:#a1b2c3;min-height:180px}'
        html = ("<html><head><title>Cube E2E navigation</title><link rel='stylesheet' href='/assets/layout.css'></head><body style='margin:0'>"
                "<div class='hero'><h1>Native integration</h1><img src='/assets/image.png' width='128' height='64' alt='Downloaded image'></div>"
                + "".join(f"<p>Scroll paragraph {i}: a real HTTP response in a native Wayland window.</p>" for i in range(70))
                + "<section style='background:#fae3a0;padding:24px;min-height:200px'><h2>far-marker</h2><p>Find reached the end of the document.</p></section></body></html>").encode()
        interaction_html = (Path(__file__).resolve().parents[1] / 'tests/fixtures/interaction.html').read_bytes()
        fixtures = Path(__file__).resolve().parents[1] / 'tests/fixtures'
        responsive_html = (fixtures / 'responsive.html').read_bytes()
        vector = (fixtures / 'vector.svg').read_bytes()
        responsive = {'breakpoint': 1100}
        observations = {}
        async_started, async_release = threading.Event(), threading.Event()
        download_started, cancel_started = threading.Event(), threading.Event()
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                if self.path in ('/download.bin','/cancel.bin'):
                    self.send_response(200)
                    self.send_header('Content-Type','application/octet-stream')
                    self.send_header('Content-Length',str(4*1024*1024))
                    self.end_headers()
                    try:
                        for i in range(64):
                            self.wfile.write(b'd'*65536);self.wfile.flush()
                            if i >= 4:
                                (download_started if self.path=='/download.bin' else cancel_started).set()
                            time.sleep(.012 if self.path=='/download.bin' else .05)
                    except (BrokenPipeError,ConnectionResetError): pass
                    return
                if self.path == '/scripted':
                    body, mime = b"""<title>Cube E2E script source</title><body><form method="post" action="/must-not-submit-js"><input id="field" name="q"><button>Save</button></form><button id="counter" type="button">Count</button><p id="result">Initial</p><script>
                    const local=Number(localStorage.getItem('n')||0)+1,session=Number(sessionStorage.getItem('n')||0)+1;
                    localStorage.setItem('n',local);sessionStorage.setItem('n',session);document.title='Cube E2E scripted '+local+' '+session;
                    document.querySelector('form').addEventListener('submit',event=>{event.preventDefault();const p=document.getElementById('result');p.textContent='Saved '+document.getElementById('field').value;p.style.cssText='min-height:180px;background:#448877';});
                    document.getElementById('counter').onclick=()=>document.title='Cube E2E clicked';
                    </script></body>""", 'text/html'
                elif self.path == '/async-cancel':
                    body, mime = b'<title>Async cancellation</title><script>fetch("/slow-api").then(r=>r.text()).then(()=>document.title="OLD CALLBACK LEAKED")</script>', 'text/html'
                elif self.path == '/slow-api':
                    async_started.set(); async_release.wait(3)
                    body, mime = b'late response', 'text/plain'
                elif self.path == '/login':
                    body, mime = (b'<title>Cube E2E login</title><form method="post" action="/authenticate">'
                                  b'<input name="user"><input type="password" name="password"><button>Sign in</button></form>'), 'text/html'
                elif self.path == '/account':
                    if requests.count('/account') >= 2: time.sleep(.05)
                    observations['account_cookie'] = self.headers.get('Cookie', '')
                    body, mime = b'<title>Cube E2E account</title><p>Authenticated</p>', 'text/html'
                elif self.path == '/logout':
                    self.send_response(303)
                    self.send_header('Location', '/logged-out')
                    self.send_header('Set-Cookie', 'session=; Path=/; Max-Age=0')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                elif self.path == '/logged-out':
                    observations['logout_cookie'] = self.headers.get('Cookie', '')
                    body, mime = b'<title>Cube E2E logged out</title>', 'text/html'
                elif self.path == '/cached':
                    if self.headers.get('If-None-Match') == '"v1"':
                        observations['conditional'] = True
                        self.send_response(304)
                        self.send_header('ETag', '"v1"')
                        self.end_headers()
                        return
                    body, mime = b'<title>Cube E2E cached</title><p>Validated cached body</p>', 'text/html'
                elif self.path == '/assets/layout.css':
                    body, mime = css, 'text/css'
                elif self.path == '/assets/image.png':
                    body, mime = png, 'image/png'
                elif self.path == '/assets/vector.svg':
                    body, mime = vector, 'image/svg+xml'
                elif self.path == '/assets/responsive.css':
                    body = ('.responsive-banner{min-height:90px;padding:16px;background:#c6dcf0}'
                            '.preference-band{min-height:48px;background:#f3ecd7}'
                            'td,th{padding:12px;border:1px solid #4d5963}input{width:180px}'
                            f'@media (max-width:{responsive["breakpoint"]}px){{.responsive-banner{{background:#e2b86d}}}}'
                            '@media (prefers-reduced-motion:reduce){.preference-band{background:#cb7094}}').encode()
                    mime = 'text/css'
                elif self.path == '/responsive':
                    body, mime = responsive_html, 'text/html'
                elif self.path.startswith('/table-submit?'):
                    body, mime = b'<title>Cube E2E table submitted</title><p>Table input survived layout changes.</p>', 'text/html'
                elif self.path == '/table-link':
                    body, mime = b'<title>Cube E2E table link</title><p>Table link activation succeeded.</p>', 'text/html'
                elif self.path == '/slow-page':
                    body = b'<title>Pending resources</title><link rel="stylesheet" href="/slow.css"><p>Initial page</p>'
                    mime = 'text/html'
                elif self.path == '/slow.css':
                    slow_started.set()
                    slow_release.wait(3)
                    body, mime = b'body{background:#ff00ff}', 'text/css'
                elif self.path.partition('?')[0] == '/interaction':
                    body, mime = interaction_html, 'text/html; charset=utf-8'
                elif self.path == '/visited':
                    body, mime = b'<title>Cube E2E visited link</title><p>Keyboard link activation succeeded.</p>', 'text/html'
                elif self.path.startswith('/submit?'):
                    body, mime = (b'<title>Cube E2E form submitted</title><p style="background:#83b6c4;min-height:160px">'
                                  b'The GET form request reached the local HTTP server.</p>'), 'text/html'
                elif self.path == '/post-form':
                    body, mime = (b'<title>Cube E2E blocked PUT</title><form method="put" action="/must-not-submit">'
                                  b'<input name="q"><button>Submit</button></form>'
                                  b'<p style="background:#c1d5b0;min-height:200px">Unsupported method stays on this page.</p>'), 'text/html'
                else:
                    body, mime = html, 'text/html; charset=utf-8'
                self.send_response(200)
                if self.path == '/cached': self.send_header('ETag', '"v1"')
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            def log_message(self, *_):
                pass
            def do_POST(self):
                requests.append('POST ' + self.path)
                observations['post'] = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                self.send_response(303 if self.path == '/authenticate' else 405)
                if self.path == '/authenticate':
                    self.send_header('Location', '/account')
                    self.send_header('Set-Cookie', 'session=ok; Path=/; HttpOnly; SameSite=Lax')
                self.send_header('Content-Length', '0')
                self.end_headers()
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        compositor_process = browser_process = proxy = None
        with (root / "compositor.log").open("w+") as compositor_log, (root / "browser.log").open("w+") as browser_log:
            try:
                compositor_process = subprocess.Popen([str(compositor), *compositor_args], env=dict(env, **compositor_env), stdout=compositor_log,
                                                       stderr=compositor_log, start_new_session=True)
                upstream = root / "runtime" / "wayland-0"
                wait_for(lambda: upstream.exists() or compositor_process.poll() is not None, "headless Wayland socket")
                assert compositor_process.poll() is None, "headless compositor exited"
                proxy = WaylandProxy(root / "runtime" / "cube-test", upstream, test_seat=bool(options.weston_dir))
                browser_env = dict(env, WAYLAND_DISPLAY="cube-test")
                browser_process = subprocess.Popen([str(browser), "--private", "--quit-after", "55", "cube:home"],
                    env=browser_env, stdout=browser_log, stderr=browser_log, start_new_session=True)
                wait_for(lambda: proxy.title == "Private browsing — Cube Browser" and proxy.commits >= 2, "home frame and title")
                assert proxy.app_id == "cube-browser", "missing native app id"
                assert proxy.configures and proxy.acks, "xdg configure was not acknowledged"
                wait_for(lambda: proxy.releases > 0, "real compositor buffer release")
                save_frame(proxy, options.artifacts, "wayland-home.png")
                proxy.keyboard_ready()
                url = f"http://127.0.0.1:{server.server_port}/page"
                proxy.key(38, proxy.control)  # Ctrl+L
                proxy.type(url)
                proxy.key(28)  # Enter
                wait_for(lambda: requests and proxy.title == "Cube E2E navigation" and proxy.has_color(0xa1b2c3) and proxy.has_color(0xd85723), "external stylesheet and image in native HTTP frame")
                assert '/assets/layout.css' in requests and '/assets/image.png' in requests
                save_frame(proxy, options.artifacts, "wayland-http.png")
                commits = proxy.commits
                initial = hashlib.sha256(proxy.snapshot()[0]).digest()
                proxy.key(13, proxy.control)  # Ctrl+= zoom
                wait_for(lambda: proxy.commits > commits and hashlib.sha256(proxy.snapshot()[0]).digest() != initial, "zoom repaint")
                proxy.key(33, proxy.control)  # Ctrl+F
                proxy.type("far-marker")
                proxy.key(28)
                wait_for(lambda: proxy.has_color(0xfae3a0), "find-in-page scrolled to the distant match")
                save_frame(proxy, options.artifacts, "wayland-find.png")
                proxy.key(1)  # Escape
                count = len(requests)
                proxy.key(19, proxy.control)  # Ctrl+R
                wait_for(lambda: len(requests) > count and proxy.has_color(0xa1b2c3), "reload returned to document top")
                proxy.key(105, proxy.alt)  # Alt+Left
                wait_for(lambda: proxy.title == "Private browsing — Cube Browser", "back navigation")
                count = len(requests)
                proxy.key(106, proxy.alt)  # Alt+Right
                wait_for(lambda: len(requests) > count and proxy.title == "Cube E2E navigation", "forward navigation")
                proxy.key(38, proxy.control)
                proxy.type(f'http://127.0.0.1:{server.server_port}/slow-page')
                proxy.key(28)
                wait_for(lambda: slow_started.is_set(), "slow stylesheet request started")
                proxy.key(38, proxy.control)
                proxy.type('cube:home')
                proxy.key(28)
                wait_for(lambda: proxy.title == 'Private browsing — Cube Browser', "navigation cancels pending resources")
                slow_release.set()
                def no_network_children():
                    children = Path(f'/proc/{browser_process.pid}/task/{browser_process.pid}/children').read_text().split()
                    for pid in children:
                        try:
                            if Path(f'/proc/{pid}/comm').read_text().strip() == 'cube-netd':
                                return False
                        except FileNotFoundError:
                            pass
                    return True
                wait_for(no_network_children, "no resource workers survived cancelled navigation")
                time.sleep(.08)
                assert proxy.title == 'Private browsing — Cube Browser' and not proxy.has_color(0xff00ff), 'old resource modified new page'
                exercise_interactions(proxy, f'http://127.0.0.1:{server.server_port}', requests, options.artifacts)
                exercise_sessions(proxy, f'http://127.0.0.1:{server.server_port}', requests, observations)
                exercise_scripts(proxy, f'http://127.0.0.1:{server.server_port}', requests, async_started, async_release)
                exercise_workflows(proxy, f'http://127.0.0.1:{server.server_port}', requests, root, download_started, cancel_started, options.artifacts)
                exercise_responsive(proxy, f'http://127.0.0.1:{server.server_port}', requests,
                                    options.artifacts, root / 'state', responsive)
                children = Path(f'/proc/{browser_process.pid}/task/{browser_process.pid}/children').read_text().split()
                renderers = [int(pid) for pid in children
                             if Path(f'/proc/{pid}/comm').read_text().strip() == 'cube-renderd']
                assert len(renderers) == 1, 'expected one owned renderer before crash test'
                commits = proxy.commits
                os.kill(renderers[0], signal.SIGKILL)
                wait_for(lambda: proxy.commits > commits, 'renderer crash reported in native UI')
                assert browser_process.poll() is None, 'renderer crash killed browser UI'
                count = requests.count('/table-link')
                proxy.key(19, proxy.control)
                wait_for(lambda: requests.count('/table-link') > count and proxy.title == 'Cube E2E table link',
                         'reload recovers after renderer crash')
                assert compositor_process.poll() is None, "compositor stopped during browser navigation"
                assert not [path for path in (root / "data").rglob("*") if path.is_file()], "private window persisted data"
                proxy.key(17, proxy.control)  # Ctrl+W
                assert browser_process.wait(timeout=5) == 0, "browser failed on keyboard close"
                assert proxy.error is None, f"protocol proxy failed: {proxy.error}"
                print(f"e2e: native app id, xdg configure/ack, {proxy.commits} shm commits, {proxy.releases} compositor releases; "
                      "external CSS/PNG, Ctrl+L HTTP navigation, zoom, find, reload, back/forward, pending-resource cancellation, "
                      "Tab/Shift+Tab page focus, immediate input typing, exact GET submission, blocked PUT, POST login/logout, cookies, conditional cache, scripted events/storage/cancellation, settings, downloads/cancel, diagnostics, private clearing, anchors without refetch, "
                      "responsive zoom, live reduced-motion CSS, SVG, table links/edited inputs, renderer crash recovery, private history and Ctrl+W passed")
                print("Input source: isolated standard wl_keyboard events; real headless Wayland compositor handled all presentation.")
                exercise_bookmarks(browser, browser_env, upstream, root, f'http://127.0.0.1:{server.server_port}',
                                   browser_log, options.artifacts, bool(options.weston_dir))
                return 0
            except BaseException:
                for name, log in (("compositor", compositor_log), ("browser", browser_log)):
                    log.flush()
                    print(f"--- {name} log ---\n{(root / (name + '.log')).read_text()[-12000:]}")
                if proxy:
                    print(f"proxy: title={proxy.title!r}, commits={proxy.commits}, releases={proxy.releases}, error={proxy.error!r}")
                    print(f"HTTP requests: {requests!r}")
                    if proxy.latest_buffer:
                        save_frame(proxy, options.artifacts, "wayland-failure.png")
                raise
            finally:
                slow_release.set(); async_release.set()
                stop_process(browser_process)
                if proxy:
                    proxy.close()
                stop_process(compositor_process)
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    base = Path(__file__).resolve().parents[1]
    parser.add_argument("--browser", type=Path, default=base / "build" / "cube-browser")
    # In the Whitecube tree (apps/cube-browser) the compositor is built two levels up.
    compositor = base.parents[1] / "build" / "whitecube"
    if not compositor.exists():
        compositor = base.parent / "whitecube" / "build" / "whitecube"
    parser.add_argument("--compositor", type=Path, default=compositor)
    parser.add_argument("--weston-dir", type=Path, help="extracted Weston package root; use its headless compositor and an isolated test input seat")
    parser.add_argument("--artifacts", type=Path, help="save actual submitted Wayland buffers as PNG files")
    raise SystemExit(run(parser.parse_args()))
