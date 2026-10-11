#!/usr/bin/env python3
"""Exercise the actual isolated workers and descriptor protocol without Wayland."""
import array
import contextlib
import fcntl
import mmap
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import unittest
import zlib

from test_net import serve

ROOT = Path(__file__).resolve().parents[1]
RENDERER = Path(sys.argv.pop(1)).resolve() if len(sys.argv) > 1 and not sys.argv[1].startswith('-') else ROOT / 'build/cube-renderd'
NET = Path(sys.argv.pop(1)).resolve() if len(sys.argv) > 1 and not sys.argv[1].startswith('-') else ROOT / 'build/cube-netd'
FETCH, DOCUMENT, ERROR, LAYOUT, FRAME, LINK, LINK_RESULT, FIND, FOUND, TEXT, TEXT_RESULT, QUIT = range(1, 13)
RESOURCE_REQUEST, RESOURCE_DATA, RESOURCE_DONE, RESOURCES_END = range(13, 17)
INTERACT, INTERACTION = 17, 18
SEALS = fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE | fcntl.F_SEAL_SEAL


def integer(value, major=0):
    if value < 24:
        return bytes([major << 5 | value])
    if value < 256:
        return bytes([major << 5 | 24, value])
    if value < 65536:
        return bytes([major << 5 | 25]) + struct.pack('!H', value)
    return bytes([major << 5 | 26]) + struct.pack('!I', value)


def send(sock, kind, a=0, b=0, c=0, text='', fd=None):
    text = text.encode()
    data = b'\x85' + b''.join(integer(x) for x in (kind, a, b, c)) + integer(len(text), 3) + text
    ancillary = [] if fd is None else [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [fd]))]
    sock.sendmsg([struct.pack('!I', len(data)) + data], ancillary)


def receive(sock):
    data, ancillary, flags, _ = sock.recvmsg(16384, socket.CMSG_SPACE(16))
    if not data:
        return None
    assert flags == 0 and struct.unpack('!I', data[:4])[0] == len(data)-4
    assert data[4] == 0x85
    at, values = 5, []
    for expected in (0, 0, 0, 0, 3):
        assert data[at] >> 5 == expected
        size = data[at] & 31
        at += 1
        if size < 24:
            value = size
        else:
            length = {24: 1, 25: 2, 26: 4}[size]
            value = int.from_bytes(data[at:at+length], 'big')
            at += length
        values.append(value)
    result = dict(zip(('type', 'a', 'b', 'c'), values[:4]))
    assert at + values[4] == len(data)
    result['text'], result['fd'] = data[at:].decode(), None
    for level, kind, payload in ancillary:
        assert level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS
        descriptors = array.array('i')
        descriptors.frombytes(payload)
        assert len(descriptors) == 1
        result['fd'] = descriptors[0]
    return result


@contextlib.contextmanager
def document(html, sealed=True):
    fd = os.memfd_create('cube-worker-test', os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    try:
        os.write(fd, html.encode() if isinstance(html, str) else html)
        if sealed:
            fcntl.fcntl(fd, fcntl.F_ADD_SEALS, SEALS)
        yield fd
    finally:
        os.close(fd)


@contextlib.contextmanager
def worker(binary=RENDERER):
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    parent.settimeout(5)
    proc = subprocess.Popen([str(binary), '--ipc', str(child.fileno())], pass_fds=[child.fileno()], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    child.close()
    try:
        yield proc, parent
    finally:
        parent.close()
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()


def load(sock, html, base='https://example.org/docs/page'):
    with document(html) as fd:
        send(sock, DOCUMENT, text=base, fd=fd)
    catalog = []
    while True:
        reply = receive(sock)
        assert reply is not None and reply['fd'] is None, reply
        if reply['type'] == RESOURCES_END:
            assert reply['a'] == len(catalog) and reply['b'] == reply['c'] == 0, reply
            return catalog
        assert reply['type'] == RESOURCE_REQUEST and reply['a'] == len(catalog), reply
        assert reply['b'] in (1, 2, 3) and reply['c'] == 0 and len(catalog) < 32, reply
        catalog.append(reply)


def put_resource(sock, index, kind, body, mime, sealed=True):
    with document(body, sealed) as fd:
        send(sock, RESOURCE_DATA, a=index, b=kind, text=mime, fd=fd)
    result = receive(sock)
    assert result['type'] == RESOURCE_DONE and result['a'] == index, result
    assert result['fd'] is None and result['b'] in (0, 1) and result['c'] == 0, result
    return result['b']


def frame_pixel(sock, x=100, y=40, *, width=400, height=200, zoom=1, flags=None):
    layout = str(zoom) if flags is None else f'{zoom}\n{flags}'
    send(sock, LAYOUT, width, height, text=layout)
    reply = receive(sock)
    assert reply['type'] == FRAME and reply['fd'] is not None, reply
    try:
        return struct.unpack('=I', os.pread(reply['fd'], 4, (y*width+x)*4))[0] & 0xffffff
    finally:
        os.close(reply['fd'])


def layout_pixels(sock, width=600, height=360, zoom=1, flags=0):
    send(sock, LAYOUT, width, height, text=f'{zoom}\n{flags}')
    reply = receive(sock)
    assert reply['type'] == FRAME and reply['fd'] is not None, reply
    try:
        pixels = os.pread(reply['fd'], width*height*4, 0)
        assert len(pixels) == width*height*4
        return pixels, reply
    finally:
        os.close(reply['fd'])


def interact(sock, action, text='', x=0, y=0):
    send(sock, INTERACT, action, x, y, text=text)
    reply = receive(sock)
    assert reply['type'] == INTERACTION and reply['fd'] is None, reply
    return reply


def color_bounds(pixels, width, colors):
    bounds = {color: None for color in colors}
    for index, (pixel,) in enumerate(struct.iter_unpack('=I', pixels)):
        color = pixel & 0xffffff
        if color not in bounds:
            continue
        x, y = index % width, index // width
        old = bounds[color]
        bounds[color] = (x, y, x, y) if old is None else (min(x, old[0]), min(y, old[1]), max(x, old[2]), max(y, old[3]))
    return bounds


def tiny_png():
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data))
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', 1, 1, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(b'\0\xff\0\0')) + chunk(b'IEND', b'')


def get_text(sock):
    send(sock, TEXT)
    reply = receive(sock)
    assert reply['type'] == TEXT_RESULT and reply['fd'] is not None
    try:
        assert fcntl.fcntl(reply['fd'], fcntl.F_GET_SEALS) == SEALS
        return os.pread(reply['fd'], 2*1024*1024, 0).decode()
    finally:
        os.close(reply['fd'])


class Workers(unittest.TestCase):
    def test_media_width_changes_on_resize_and_zoom_in_css_pixels(self):
        html = ('<style>body{margin:0}.probe{min-height:100px;background:#123456}'
                '@media (max-width:500px){.probe{background:#41825c}}'
                '@media (min-width:700px){.probe{background:#ac7256}}'
                '@media (min-width:1600px){.probe{background:#674e98}}</style>'
                '<div class="probe">Responsive width</div>')
        with worker() as (_, sock):
            load(sock, html)
            self.assertEqual(frame_pixel(sock, width=800, height=400, flags=0), 0xac7256)
            self.assertEqual(frame_pixel(sock, width=480, height=400, flags=0), 0x41825c)
            self.assertEqual(frame_pixel(sock, width=600, height=400, flags=0), 0x123456)
            self.assertEqual(frame_pixel(sock, width=800, height=400, zoom=2, flags=0), 0x41825c,
                             'viewport media width must use CSS pixels after zoom')
            self.assertEqual(frame_pixel(sock, x=700, y=25, width=800, height=400, zoom=.4, flags=0), 0x674e98,
                             '80% interface scale with 50% page zoom must preserve effective zoom')
            self.assertEqual(frame_pixel(sock, width=800, height=400, zoom=1, flags=0), 0xac7256)

    def test_media_height_orientation_and_preference_changes_repaint(self):
        html = ('<style>body{margin:0}.probe{min-height:100px;background:#123456}'
                '@media (min-height:500px){.probe{background:#4a8563}}'
                '@media (orientation:portrait){.probe{background:#7652a1}}'
                '@media (prefers-reduced-motion:reduce){.probe{background:#ad6f52}}'
                '@media (prefers-contrast:more){.probe{background:#3869a2}}</style>'
                '<div class="probe">Environment</div>')
        with worker() as (_, sock):
            load(sock, html)
            self.assertEqual(frame_pixel(sock, width=800, height=300, flags=0), 0x123456)
            self.assertEqual(frame_pixel(sock, width=800, height=600, flags=0), 0x4a8563)
            self.assertEqual(frame_pixel(sock, width=400, height=600, flags=0), 0x7652a1)
            self.assertEqual(frame_pixel(sock, width=800, height=600, zoom=2, flags=0), 0x123456)
            self.assertEqual(frame_pixel(sock, width=800, height=300, flags=1), 0xad6f52)
            self.assertEqual(frame_pixel(sock, width=800, height=300, flags=2), 0x3869a2)
            self.assertEqual(frame_pixel(sock, width=800, height=300, flags=3), 0x3869a2)
            self.assertEqual(frame_pixel(sock, width=800, height=300, flags=0), 0x123456)

    def test_conditional_stylesheets_keep_source_order_and_edited_values(self):
        html = ('<style>body{margin:0}.probe{min-height:100px;background:#123456}</style>'
                '<link rel="stylesheet" href="/shared.css" media="(max-width:500px)">'
                '<style media="(min-width:700px)">.probe{background:#935f86}</style>'
                '<style media="(max-width:500px)">.probe{background:#c49b56}</style>'
                '<link rel="stylesheet" href="/shared.css" media="(prefers-reduced-motion:reduce)">'
                '<div class="probe">Conditional sheets</div>'
                '<form action="/submit"><input name="q" value="old"><button>Submit</button></form>')
        with worker() as (_, sock):
            catalog = load(sock, html)
            self.assertEqual([(entry['b'], entry['text']) for entry in catalog], [(1, 'https://example.org/shared.css')])
            self.assertEqual(put_resource(sock, 0, 1, '.probe{background:#4d887d}', 'text/css'), 1)
            self.assertEqual(frame_pixel(sock, width=800, height=360, flags=0), 0x935f86)
            self.assertEqual(interact(sock, 4)['b'], 2)
            interact(sock, 3, 'select-all')
            interact(sock, 2, 'edited 猫')
            self.assertEqual(frame_pixel(sock, width=480, height=360, flags=0), 0xc49b56,
                             'later conditional inline stylesheet wins at narrow width')
            self.assertEqual(frame_pixel(sock, width=480, height=360, flags=1), 0x4d887d,
                             'a later matching occurrence of the shared stylesheet wins')
            interact(sock, 4)
            result = interact(sock, 3, 'enter')
            self.assertEqual((result['a'], result['text']), (4, 'https://example.org/submit?q=edited+%E7%8C%AB'))

    def test_table_cells_form_a_readable_grid_with_working_links_and_controls(self):
        html = ('<style>body{margin:0}table{width:100%}td{padding:8px;border:1px solid #333333}'
                'input{width:110px}</style><form action="/table-submit"><table>'
                '<tr><td style="background:#e2aa66"><a href="/left">LEFT CELL</a></td>'
                '<td style="background:#7fa5d1"><input name="q" value="old"><button>Submit</button></td></tr>'
                '<tr><td style="background:#81bd94">SECOND ROW</td>'
                '<td style="background:#bb91c2">Another cell</td></tr></table></form>')
        with worker() as (_, sock):
            load(sock, html)
            pixels, _ = layout_pixels(sock, width=600)
            colors = (0xe2aa66, 0x7fa5d1, 0x81bd94, 0xbb91c2)
            bounds = color_bounds(pixels, 600, colors)
            self.assertTrue(all(bounds.values()), bounds)
            left, right, lower_left, lower_right = (bounds[color] for color in colors)
            self.assertLess(left[2], right[0], 'first-row cells must have separate horizontal extents')
            self.assertLessEqual(abs(left[1] - right[1]), 1, 'cells in one row must share a top edge')
            self.assertLessEqual(abs(left[3] - right[3]), 1, 'shorter cells must fill the shared row height')
            self.assertGreater(lower_left[1], left[1] + 12)
            self.assertLessEqual(abs(lower_left[1] - lower_right[1]), 1)
            self.assertLess(lower_left[2], lower_right[0])
            send(sock, FIND, text='LEFT CELL')
            found = receive(sock)
            self.assertEqual(found['a'], 1)
            clicked = interact(sock, 1, x=left[0] + 12, y=found['b'] + 5)
            self.assertEqual((clicked['a'], clicked['text']), (2, 'https://example.org/left'))
            field = interact(sock, 4)
            self.assertEqual(field['b'], 2)
            self.assertTrue(right[1] <= field['c'] <= right[3])
            clicked = interact(sock, 1, x=right[0] + 12, y=field['c'] + 5)
            self.assertEqual(clicked['b'], 2)
            interact(sock, 3, 'select-all')
            interact(sock, 2, 'table value')
            layout_pixels(sock, width=360)
            interact(sock, 4)
            result = interact(sock, 3, 'enter')
            self.assertEqual((result['a'], result['text']), (4, 'https://example.org/table-submit?q=table+value'))

    def test_resource_catalog_resolves_deduplicates_and_bounds(self):
        html = '<link rel="stylesheet" href="../theme.css#first"><link rel="stylesheet" href="../theme.css#again"><img src="/pixel.png"><img src="/pixel.png#same"><img src="data:image/png,x"><img src="file:///tmp/local"><script src="/inert.js"></script>'
        with worker() as (_, sock):
            catalog = load(sock, html)
            self.assertEqual([(item['b'], item['text']) for item in catalog],
                             [(1, 'https://example.org/theme.css'), (2, 'https://example.org/pixel.png'), (3, 'https://example.org/inert.js')])
            catalog = load(sock, ''.join(f'<img src="/image{i}.png">' for i in range(40)))
            self.assertEqual(len(catalog), 32)
            self.assertEqual([item['a'] for item in catalog], list(range(32)))
            self.assertEqual(load(sock, html, 'file:///tmp/local.html'), [])

    def test_stylesheet_cascade_uses_document_order_not_arrival(self):
        html = '<link rel=stylesheet href="/first.css"><style>.box{background:#112233}</style><link rel=stylesheet href="/second.css"><body style="margin:0"><div class=box style="min-height:80px">Still here</div></body>'
        with worker() as (_, sock):
            self.assertEqual(len(load(sock, html)), 2)
            self.assertEqual(frame_pixel(sock), 0x112233)
            self.assertEqual(put_resource(sock, 1, 1, '.box{background:#aabbcc}', 'text/css'), 1)
            self.assertEqual(frame_pixel(sock), 0xaabbcc)
            self.assertEqual(put_resource(sock, 0, 1, '.box{background:#445566}', 'text/css'), 1)
            self.assertEqual(frame_pixel(sock), 0xaabbcc)
            self.assertIn('Still here', get_text(sock))

    def test_bad_resource_data_preserves_page(self):
        html = '<link rel=stylesheet href="/style.css"><style>.box{background:#123456}</style><body style="margin:0"><div class=box style="min-height:80px">Surviving page</div></body>'
        with worker() as (_, sock):
            self.assertEqual(len(load(sock, html)), 1)
            for index, kind, mime, sealed in [(99, 1, 'text/css', True), (0, 2, 'text/css', True),
                                              (0, 1, 'text/html', True), (0, 1, 'text/css', False)]:
                with self.subTest(index=index, kind=kind, mime=mime, sealed=sealed):
                    self.assertEqual(put_resource(sock, index, kind, '.box{background:red}', mime, sealed), 0)
                    self.assertEqual(frame_pixel(sock), 0x123456)
            send(sock, RESOURCE_DATA, a=0, b=1, text='text/css')
            self.assertEqual(receive(sock)['type'], ERROR)
            self.assertEqual(frame_pixel(sock), 0x123456)
            self.assertIn('Surviving page', get_text(sock))
            self.assertEqual(put_resource(sock, 0, 1, '', 'text/css'), 1)
            self.assertEqual(frame_pixel(sock), 0x123456)

    def test_decoded_image_and_failed_image_keep_document(self):
        with worker() as (_, sock):
            catalog = load(sock, '<body style="margin:0"><img src="/pixel.png" width=20 height=20 style="display:block" alt="Fallback"><p>Text survives</p></body>')
            self.assertEqual(len(catalog), 1)
            self.assertEqual(catalog[0]['b'], 2)
            self.assertEqual(put_resource(sock, 0, 2, tiny_png(), 'text/html'), 0)
            self.assertEqual(put_resource(sock, 0, 2, b'not a PNG', 'image/png'), 0)
            frame_pixel(sock)
            self.assertIn('Text survives', get_text(sock))
            self.assertEqual(put_resource(sock, 0, 2, tiny_png(), 'IMAGE/PNG; example=parameter'), 1)
            self.assertEqual(frame_pixel(sock, 10, 10), 0xff0000)
            self.assertEqual(put_resource(sock, 0, 2, b'bad replacement', 'image/png'), 0)
            self.assertEqual(frame_pixel(sock, 10, 10), 0xff0000)

    def test_document_replacement_clears_old_resources(self):
        with worker() as (_, sock):
            first = load(sock, '<link rel=stylesheet href="/one.css"><body style="margin:0"><div style="min-height:80px">First document</div></body>')
            self.assertEqual(first[0]['text'], 'https://example.org/one.css')
            self.assertEqual(put_resource(sock, 0, 1, 'div{background:#998877}', 'text/css'), 1)
            self.assertEqual(frame_pixel(sock), 0x998877)
            self.assertEqual(load(sock, '<body style="margin:0"><div style="background:#556677;min-height:80px">Second document</div></body>'), [])
            self.assertEqual(put_resource(sock, 0, 1, 'div{background:red}', 'text/css'), 0)
            self.assertEqual(frame_pixel(sock), 0x556677)
            self.assertNotIn('First document', get_text(sock))
            self.assertIn('Second document', get_text(sock))

    def test_renderer_frames_resize_find_and_links(self):
        with worker() as (_, sock):
            load(sock, '<title>Worker test</title><body style="margin:0"><a href="../target">TARGET LINK</a><p>' + 'words to wrap ' * 100 + '</p></body>')
            heights = []
            for width in (600, 180):
                send(sock, LAYOUT, width, 240, text='1')
                frame = receive(sock)
                self.assertEqual(frame['type'], FRAME, frame)
                self.assertEqual((frame['a'], frame['b']), (width, 240))
                self.assertIn('Worker test', frame['text'])
                fd = frame['fd']
                try:
                    self.assertEqual(os.fstat(fd).st_size, width * 240 * 4)
                    self.assertEqual(fcntl.fcntl(fd, fcntl.F_GET_SEALS), SEALS)
                    with mmap.mmap(fd, 0, access=mmap.ACCESS_READ) as pixels:
                        self.assertGreater(len(set(pixels[:])), 2)
                    with self.assertRaises(PermissionError):
                        os.write(fd, b'x')
                finally:
                    os.close(fd)
                heights.append(frame['c'])
            self.assertGreater(heights[1], heights[0])
            send(sock, FIND, text='TARGET LINK')
            found = receive(sock)
            self.assertEqual((found['type'], found['a']), (FOUND, 1))
            send(sock, LINK, 12, found['b'] + 8)
            link = receive(sock)
            self.assertEqual(link['type'], LINK_RESULT)
            self.assertEqual(link['text'], 'https://example.org/target')
            send(sock, FIND, text='absent')
            self.assertEqual(receive(sock)['a'], 0)
            self.assertIn('TARGET LINK', get_text(sock))

    def test_separate_renderer_documents(self):
        with worker() as (_, left), worker() as (_, right):
            load(left, '<p>Site Alpha</p>')
            load(right, '<p>Site Beta</p>')
            # Layout applies styles before extracting visible document text.
            for sock in (left, right):
                send(sock, LAYOUT, 200, 100, text='1')
                os.close(receive(sock)['fd'])
            self.assertIn('Site Alpha', get_text(left))
            self.assertNotIn('Site Beta', get_text(left))
            self.assertIn('Site Beta', get_text(right))

    def test_invalid_layout_and_descriptors(self):
        with worker() as (_, sock):
            load(sock, '<p>Alive</p>')
            for width, height in [(0, 100), (100, 0), (4097, 1), (0xffffffff, 1)]:
                send(sock, LAYOUT, width, height, text='1')
                self.assertEqual(receive(sock)['type'], ERROR)
            for environment in ('1\n4', '1\n-1', '1\n-18446744073709551615',
                                '1\n1.5', '1\n1junk', '1x\n0', '1\n0\njunk'):
                with self.subTest(environment=environment):
                    send(sock, LAYOUT, 100, 100, text=environment)
                    reply = receive(sock)
                    self.assertEqual(reply['type'], ERROR)
                    self.assertIsNone(reply['fd'])
            with document('<p>Unsealed</p>', sealed=False) as fd:
                send(sock, DOCUMENT, fd=fd)
                self.assertEqual(receive(sock)['type'], ERROR)
            with document('<p>Wrong type</p>') as fd:
                # Repeated rejection exercises ancillary descriptor cleanup.
                for _ in range(80):
                    send(sock, LAYOUT, 100, 100, fd=fd)
                    self.assertEqual(receive(sock)['type'], ERROR)
            send(sock, LAYOUT, 100, 100, text='1')
            reply = receive(sock)
            self.assertEqual(reply['type'], FRAME)
            os.close(reply['fd'])
            self.assertIn('Alive', get_text(sock))

    def test_bad_framing_exits_worker(self):
        for payload in [b'\x80', b'\x85\x01\x00\x00\x00\x61\xff']:
            with self.subTest(payload=payload), worker() as (proc, sock):
                sock.send(struct.pack('!I', len(payload)) + payload)
                self.assertIsNone(receive(sock))
                self.assertEqual(proc.wait(timeout=3), 1)

    def test_peer_disconnect_and_quit(self):
        for command in ('disconnect', 'quit'):
            with worker() as (proc, sock):
                if command == 'quit':
                    send(sock, QUIT)
                else:
                    sock.shutdown(socket.SHUT_RDWR)
                self.assertEqual(proc.wait(timeout=3), 0)

    def test_sandbox_syscalls(self):
        result = subprocess.run([str(RENDERER), '--sandbox-check'], capture_output=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_network_worker_sealed_body_and_mime(self):
        responses = {
            '/ok': b'HTTP/1.1 200 OK\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: 5\r\n\r\nhello',
            '/bad': b'HTTP/1.1 200 OK\r\nContent-Type: text/html-evil\r\nContent-Length: 5\r\n\r\nhello',
        }
        with serve(responses) as (_, url):
            with worker(NET) as (proc, sock):
                send(sock, FETCH, text=url + '/ok')
                reply = receive(sock)
                self.assertEqual((reply['type'], reply['a'], reply['b']), (DOCUMENT, 200, 1))
                try:
                    self.assertEqual(fcntl.fcntl(reply['fd'], fcntl.F_GET_SEALS), SEALS)
                    self.assertEqual(os.pread(reply['fd'], 100, 0), b'hello')
                finally:
                    os.close(reply['fd'])
                self.assertEqual(proc.wait(timeout=3), 0)
            with worker(NET) as (_, sock):
                send(sock, FETCH, text=url + '/bad')
                reply = receive(sock)
                self.assertEqual(reply['type'], ERROR)
                self.assertIsNone(reply['fd'])

    def test_network_worker_rejects_attached_fd(self):
        with worker(NET) as (proc, sock), document('unexpected') as fd:
            send(sock, FETCH, text='https://example.org/', fd=fd)
            self.assertIsNone(receive(sock))
            self.assertEqual(proc.wait(timeout=3), 2)

    def test_network_resource_contract_and_invalid_requests(self):
        replies = {'/style': b'HTTP/1.1 200 OK\r\nContent-Type: text/css\r\nContent-Length: 3\r\n\r\na{}',
                   '/image': b'HTTP/1.1 200 OK\r\nContent-Type: IMAGE/PNG; example=parameter\r\nContent-Length: 3\r\n\r\n\x89\x00x'}
        with serve(replies) as (server, url):
            for kind, path, mime, body in [(1, '/style', 'text/css', b'a{}'), (2, '/image', 'IMAGE/PNG; example=parameter', b'\x89\x00x')]:
                with self.subTest(kind=kind), worker(NET) as (_, sock):
                    send(sock, FETCH, a=kind, text=url+path+'\n'+url+'/document')
                    result = receive(sock)
                    self.assertEqual((result['type'], result['a'], result['b']), (DOCUMENT, 200, kind))
                    self.assertEqual(result['text'], url+path+'\n'+mime)
                    try:
                        self.assertEqual(fcntl.fcntl(result['fd'], fcntl.F_GET_SEALS), SEALS)
                        self.assertEqual(os.pread(result['fd'], 100, 0), body)
                    finally:
                        os.close(result['fd'])
            for kind, text in [(3, url+'/style'), (1, url+'/style'), (2, url+'/style\n'),
                               (1, url+'/style\n'+url+'/document\nextra')]:
                with self.subTest(kind=kind, text=text), worker(NET) as (_, sock):
                    send(sock, FETCH, a=kind, text=text)
                    result = receive(sock)
                    self.assertEqual(result['type'], ERROR)
                    self.assertIsNone(result['fd'])
            self.assertEqual(len(server.requests), 2)


if __name__ == '__main__':
    unittest.main()
