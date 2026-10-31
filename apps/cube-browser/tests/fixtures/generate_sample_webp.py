#!/usr/bin/env python3
"""Generate deterministic decoder fixtures using libwebp's lossless encoder."""
import ctypes
import ctypes.util
from pathlib import Path
import struct

root = Path(__file__).resolve().parent
codec = ctypes.CDLL(ctypes.util.find_library('webp'))
codec.WebPEncodeLosslessRGBA.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
codec.WebPEncodeLosslessRGBA.restype = ctypes.c_size_t
codec.WebPFree.argtypes = [ctypes.c_void_p]


def encode(width, height, pixels):
    source = ctypes.create_string_buffer(bytes(pixels))
    target = ctypes.c_void_p()
    size = codec.WebPEncodeLosslessRGBA(source, width, height, width*4, ctypes.byref(target))
    if not size:
        raise RuntimeError('WebP encoder failed')
    try:
        return ctypes.string_at(target, size)
    finally:
        codec.WebPFree(target)


def chunk(tag, payload):
    return tag + struct.pack('<I', len(payload)) + payload + (b'\0' if len(payload) % 2 else b'')


sample = encode(2, 1, [255, 0, 0, 255, 0, 255, 128, 128])
(root/'sample.webp').write_bytes(sample)
solid = encode(8, 4, [36, 147, 188, 255]*32)
(root/'solid.webp').write_bytes(solid)
# Two valid lossless frames in an animated container; the browser must refuse it.
assert solid[12:16] == b'VP8L'
extended = chunk(b'VP8X', b'\x02\0\0\0' + (7).to_bytes(3, 'little') + (3).to_bytes(3, 'little'))
animation = chunk(b'ANIM', bytes(6))
frame = bytes(6) + (7).to_bytes(3, 'little') + (3).to_bytes(3, 'little') + (100).to_bytes(3, 'little') + b'\x02' + solid[12:]
body = b'WEBP' + extended + animation + chunk(b'ANMF', frame)*2
(root/'animated.webp').write_bytes(b'RIFF' + struct.pack('<I', len(body)) + body)
