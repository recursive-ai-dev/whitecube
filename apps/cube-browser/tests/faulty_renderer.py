#!/usr/bin/env python3
"""Test renderer that violates frame or user-authorized interaction contracts."""
import array
import fcntl
import os
import socket
import struct
import sys

sock = socket.socket(fileno=int(sys.argv[2]))

def integer(value, major=0):
    return bytes([(major << 5) | 26]) + struct.pack('!I', value)

def send(kind, a=0, b=0, c=0, text='', fd=None):
    encoded = text.encode()
    body = b'\x85' + b''.join(integer(v) for v in (kind, a, b, c)) + integer(len(encoded), 3) + encoded
    ancillary = [] if fd is None else [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [fd]))]
    sock.sendmsg([struct.pack('!I', len(body)) + body], ancillary)

def receive():
    body, ancillary, _, _ = sock.recvmsg(16384, socket.CMSG_SPACE(16))
    for _, _, payload in ancillary:
        fds = array.array('i'); fds.frombytes(payload)
        for fd in fds: os.close(fd)
    values, offset = [], 5
    for _ in range(4):
        value = body[offset] & 31; offset += 1
        if value >= 24:
            length = {24: 1, 25: 2, 26: 4}[value]
            value = int.from_bytes(body[offset:offset+length], 'big'); offset += length
        values.append(value)
    return values

message = receive()
while message[0] in (21, 24): message = receive()
assert message[0] == 2  # document
send(16)                # empty resource catalogue
kind, width, height, _ = receive()
assert kind == 4        # layout
mode = os.environ['CB_TEST_FRAME_FAULT']
if mode == 'unsolicited-submit':
    send(18, a=4, c=0xffffffff, text=os.environ['CB_TEST_ACTION_URL'])
elif mode == 'unsolicited-copy':
    send(18, a=5, b=2, text='UNSOLICITED_CLIPBOARD_VALUE')
elif mode == 'unsolicited-link':
    send(7, text=os.environ['CB_TEST_ACTION_URL'])
else:
    assert mode in ('mutable', 'nonfinite')
    fd = os.memfd_create('invalid-frame', os.MFD_ALLOW_SEALING)
    os.ftruncate(fd, width*height*4)
    if mode != 'mutable':
        fcntl.fcntl(fd, fcntl.F_ADD_SEALS, fcntl.F_SEAL_WRITE|fcntl.F_SEAL_SHRINK|fcntl.F_SEAL_GROW|fcntl.F_SEAL_SEAL)
    scroll = 'nan' if mode == 'nonfinite' else '0'
    send(5, width, height, height, scroll+'\nInvalid frame', fd)
    os.close(fd)
sock.recv(1)  # UI must disconnect; the harness also bounds total execution time.
