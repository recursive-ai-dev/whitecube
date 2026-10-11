#!/usr/bin/env python3
"""Exercise real Wayland presentation and keyboard UI on a private Whitecube.

Whitecube currently has no virtual-keyboard or screencopy extension. The test
therefore relays the player's connection through a private protocol proxy. The
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
    def __init__(self, endpoint: Path, upstream: Path):
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(str(endpoint))
        self.listener.listen(1)
        self.upstream = upstream
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
        self.pointer = None
        self.device = None
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
                        if not self.main_surface and source is self.client:
                            self.client.close(); self.server.close()
                            self.objects = {1: 'wl_display'}
                            self.client_bytes = self.server_bytes = b''
                            self.run()
                            return
                        for fd in fds:
                            os.close(fd)
                        return
                    try:
                        if source is self.client:
                            self.client_request(data, fds)
                        else:
                            self.inspect_server(data)
                            with self.send_lock:
                                self.forward(self.client, data, fds)
                    finally:
                        for fd in fds:
                            os.close(fd)
        except (BrokenPipeError, ConnectionResetError) as error:
            # libwayland can close with unread pending events on orderly exit.
            if not self.commits: self.error = error
            return
        except BaseException as error:
            if not self.stop.is_set():
                self.error = error

    def client_request(self, data, fds):
        self.inspect_client(data, fds)
        self.forward(self.server, data, fds)

    def inspect_client(self, data, fds):
        with self.lock:
            self.fds.extend(os.dup(fd) for fd in fds)
            self.client_bytes += data
            while len(self.client_bytes) >= 8:
                obj, word = struct.unpack_from("=II", self.client_bytes)
                size, opcode = word >> 16, word & 65535
                assert size >= 8
                if len(self.client_bytes) < size:
                    break
                body, self.client_bytes = self.client_bytes[8:size], self.client_bytes[size:]
                kind = self.objects.get(obj)
                if kind == "wl_display" and opcode == 1:
                    self.objects[struct.unpack_from("=I", body)[0]] = "wl_registry"
                elif kind == "wl_registry" and opcode == 0:
                    interface, end = string_at(body, 4)
                    _, new_id = struct.unpack_from("=II", body, end)
                    self.objects[new_id] = interface
                elif kind == "wl_compositor" and opcode == 0:
                    self.objects[struct.unpack_from("=I", body)[0]] = "wl_surface"
                elif kind == "wl_seat" and opcode == 0:
                    self.pointer, = struct.unpack_from("=I", body)
                    self.objects[self.pointer] = "wl_pointer"
                elif kind == "wl_data_device_manager" and opcode == 1:
                    self.device, = struct.unpack_from("=I", body)
                    self.objects[self.device] = "wl_data_device"
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

    def inspect_server(self, data):
        with self.lock:
            self.server_bytes += data
            while len(self.server_bytes) >= 8:
                obj, word = struct.unpack_from("=II", self.server_bytes)
                size, opcode = word >> 16, word & 65535
                assert size >= 8
                if len(self.server_bytes) < size:
                    break
                body, self.server_bytes = self.server_bytes[8:size], self.server_bytes[size:]
                kind = self.objects.get(obj)
                if obj == 1 and opcode == 0:
                    raise AssertionError(f"Wayland protocol error: {body!r}")
                if kind == "xdg_surface" and opcode == 0:
                    self.configures += 1
                if kind == "wl_buffer" and opcode == 0:
                    self.releases += 1

    def send_events(self, events, fd=None):
        if self.error:
            raise self.error
        assert self.client is not None
        with self.send_lock:
            self.forward(self.client, b"".join(events), [fd] if fd is not None else [])

    def keyboard_ready(self):
        wait_for(lambda: self.keyboard and self.main_surface, "keyboard and player surface")
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

