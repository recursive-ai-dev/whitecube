#!/usr/bin/env python3
"""HTTP document -> broker -> external CSS/images -> real browser pixels."""
import collections
import contextlib
import ctypes
import http.server
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zlib

ROOT = Path(__file__).resolve().parents[1]
BROWSER = Path(sys.argv.pop(1)).resolve() if len(sys.argv) > 1 else ROOT/'build/cube-browser'


def solid_png(color, width=8, height=4):
    def chunk(kind, body):
        return struct.pack('!I', len(body))+kind+body+struct.pack('!I', zlib.crc32(kind+body))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!IIBBBBB', width, height, 8, 2, 0, 0, 0)) +
            chunk(b'IDAT', zlib.compress((b'\0'+bytes(color)*width)*height)) + chunk(b'IEND', b''))


def color_count(path, rgb, tolerance=0):
    cairo = ctypes.CDLL(str(ROOT/'.deps/prefix/lib/libcairo.so.2'))
    cairo.cairo_image_surface_create_from_png.argtypes = [ctypes.c_char_p]
    cairo.cairo_image_surface_create_from_png.restype = ctypes.c_void_p
    for name in ('cairo_surface_status', 'cairo_image_surface_get_height', 'cairo_image_surface_get_stride'):
        getattr(cairo, name).argtypes = [ctypes.c_void_p]
        getattr(cairo, name).restype = ctypes.c_int
    cairo.cairo_image_surface_get_data.argtypes = [ctypes.c_void_p]
    cairo.cairo_image_surface_get_data.restype = ctypes.c_void_p
    cairo.cairo_surface_destroy.argtypes = [ctypes.c_void_p]
    surface = cairo.cairo_image_surface_create_from_png(os.fsencode(path))
    try:
        assert cairo.cairo_surface_status(surface) == 0
        size = cairo.cairo_image_surface_get_height(surface)*cairo.cairo_image_surface_get_stride(surface)
        pixels = ctypes.string_at(cairo.cairo_image_surface_get_data(surface), size)
        if not tolerance:
            return pixels.count(struct.pack('=I', 0xff000000 | rgb))
        target = ((rgb >> 16) & 255, (rgb >> 8) & 255, rgb & 255)
        count = 0
        for (pixel,) in struct.iter_unpack('=I', pixels):
            if (pixel >> 24) == 255 and all(
                    abs(((pixel >> shift) & 255) - channel) <= tolerance
                    for shift, channel in zip((16, 8, 0), target)):
                count += 1
        return count
    finally:
        cairo.cairo_surface_destroy(surface)


@contextlib.contextmanager
def server(routes):
    requests = []
    lock = threading.Lock()
    stats = {'active': 0, 'max_active': 0, 'headers': []}
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            with lock:
                requests.append(self.path)
                stats['headers'].append((self.path, dict(self.headers)))
                stats['active'] += 1
                stats['max_active'] = max(stats['max_active'], stats['active'])
            try:
                route = routes.get(self.path, (404, 'text/plain', b'missing', 0))
                status, mime, body, delay = route[:4]
                time.sleep(delay)
                self.send_response(status)
                self.send_header('Content-Type', mime)
                for key, value in (route[4] if len(route) > 4 else {}).items():
                    self.send_header(key, value)
                if status == 302:
                    self.send_header('Location', body.decode())
                    body = b''
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with lock: stats['active'] -= 1
        def log_message(self, *_): pass
    httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{httpd.server_port}', requests, stats
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


class Subresources(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cube-subresources-')
        self.root = Path(self.temp.name)
        self.env = dict(os.environ, HOME=str(self.root), XDG_CONFIG_HOME=str(self.root/'config'),
                        XDG_STATE_HOME=str(self.root/'state'), XDG_DATA_HOME=str(self.root/'data'))
        self.env.pop('DISPLAY', None)
    def tearDown(self): self.temp.cleanup()
    def render(self, url, size='900x700'):
        target = self.root/'page.png'
        result = subprocess.run([str(BROWSER), '--render', url, str(target), size],
                                env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return target

    def test_redirect_cookie_is_available_to_document_and_same_site_assets(self):
        routes = {
            '/login': (303, 'text/html', b'', 0,
                       {'Location': '/account', 'Set-Cookie': 'session=ok; Path=/; HttpOnly; SameSite=Lax'}),
            '/account': (200, 'text/html', b'<link rel="stylesheet" href="/style.css"><body><p>Signed in</p></body>', 0),
            '/style.css': (200, 'text/css', b'body{background:#337755;min-height:400px}', 0),
        }
        with server(routes) as (url, requests, stats):
            image = self.render(url+'/login')
            self.assertGreater(color_count(image, 0x337755), 100000)
            headers = dict(stats['headers'])
            self.assertNotIn('Cookie', headers['/login'])
            self.assertEqual(headers['/account'].get('Cookie'), 'session=ok')
            self.assertEqual(headers['/style.css'].get('Cookie'), 'session=ok')
        self.assertFalse((self.root/'data').exists())

    def test_csp_blocks_inline_styles_images_and_cross_origin_redirects(self):
        html = (b'<style>body{background:#aa1122}</style>'
                b'<link rel="stylesheet" href="/ok.css">'
                b'<link rel="stylesheet" href="/redirect.css">'
                b'<body style="background:#bb2233"><p>CSP survives</p>'
                b'<img src="/forbidden.png"></body>')
        routes = {
            '/page': (200, 'text/html', html, 0,
                      {'Content-Security-Policy': "default-src 'none'; style-src 'self'"}),
            '/ok.css': (200, 'text/css', b'body{background:#337755;min-height:400px}', 0),
            '/forbidden.png': (200, 'image/png', solid_png((120, 90, 80)), 0),
            '/foreign.css': (200, 'text/css', b'body{background:#aa1122}', 0),
        }
        with server(routes) as (url, requests, _):
            routes['/redirect.css'] = (302, 'text/css',
                (url.replace('127.0.0.1', 'localhost')+'/foreign.css').encode(), 0)
            image = self.render(url+'/page')
            self.assertGreater(color_count(image, 0x337755), 100000)
            self.assertEqual(color_count(image, 0xaa1122), 0)
            self.assertEqual(color_count(image, 0xbb2233), 0)
            self.assertEqual(set(requests), {'/page', '/ok.css', '/redirect.css'})

    def test_redirect_fragment_waits_for_external_stylesheet_layout(self):
        html = (b'<link rel="stylesheet" href="/move.css"><body style="margin:0">'
                b'<div class="space" style="background:#ad354f">Before target</div>'
                b'<section id="bottom" style="background:#6a9f58;min-height:900px">Anchor target</section></body>')
        routes = {
            '/start': (302, 'text/html', b'/anchor#bottom', 0),
            '/anchor': (200, 'text/html', html, 0),
            '/move.css': (200, 'text/css', b'.space{min-height:1500px}', .2),
        }
        with server(routes) as (url, requests, _):
            image = self.render(url+'/start#overridden')
            self.assertGreater(color_count(image, 0x6a9f58), 450000,
                               'initial fragment must scroll after the final external CSS layout')
            self.assertEqual(color_count(image, 0xad354f), 0)
            self.assertEqual(collections.Counter(requests),
                             collections.Counter(['/start', '/anchor', '/move.css']))

    def test_async_css_source_order_images_relative_urls_and_dedup(self):
        html = (b'<title>External resource test</title>'
                b'<style>.ordered{background:#d94d71}</style>'
                b'<link rel="stylesheet" href="slow.css">'
                b'<link rel="stylesheet" href="fast.css">'
                b'<style>.after{background:#7654b1}</style>'
                b'<body style="margin:0"><div class="ordered" style="height:70px;min-height:70px">Ordered</div>'
                b'<div class="after" style="min-height:70px">Inline after link</div>'
                b'<p class="gone">HIDDEN_BY_EXTERNAL_CSS</p>'
                b'<img src="picture.png" width="160" height="80" alt="Picture">'
                b'<img src="picture.png" width="80" height="40" alt="Repeated"></body>')
        routes = {
            '/start': (302, 'text/html', b'/docs/page', 0),
            '/docs/page': (200, 'text/html', html, 0),
            '/docs/slow.css': (200, 'text/css', b'.ordered{background:#334477}.after{background:#aabbcc}.gone{display:none}', .2),
            '/docs/fast.css': (200, 'text/css; charset=utf-8', b'.ordered{background:#45a361}', 0),
            '/docs/picture.png': (200, 'image/png', solid_png((216, 87, 35)), .05),
        }
        with server(routes) as (url, requests, stats):
            image = self.render(url+'/start')
            self.assertGreater(color_count(image, 0x45a361), 10000, 'later stylesheet must win even when it arrives first')
            self.assertGreater(color_count(image, 0x7654b1), 10000, 'inline style following link must retain source order')
            self.assertGreater(color_count(image, 0xd85723), 10000, 'downloaded image pixels must be painted')
            self.assertEqual(collections.Counter(requests)['/docs/picture.png'], 1)
            self.assertTrue(2 <= stats['max_active'] <= 3, stats)
            result = subprocess.run([str(BROWSER), '--dump', url+'/start'], env=self.env,
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('HIDDEN_BY_EXTERNAL_CSS', result.stdout)
            self.assertIn('Ordered', result.stdout)
        self.assertFalse((self.root/'data').exists(), 'headless rendering must not record history')

    def test_failed_assets_preserve_document_and_skip_unsupported_fetches(self):
        html = (b'<title>Graceful failure</title><style>body{background:#aabbcc}.keep{display:block}</style>'
                b'<link rel="stylesheet" href="spoof.css"><link rel="stylesheet" href="missing.css">'
                b'<link rel="stylesheet" media="print" href="print.css">'
                b'<link rel="alternate stylesheet" href="alternate.css">'
                b'<link rel="stylesheet" disabled href="disabled.css">'
                b'<body><p class="keep">PAGE_SURVIVES_BAD_ASSETS</p>'
                b'<img src="broken.png" alt="Unavailable image">'
                b'<img src="file:///etc/passwd"><img src="data:image/png,ignore"></body>')
        routes = {
            '/page': (200, 'text/html', html, 0),
            '/spoof.css': (200, 'text/html', b'.keep{display:none}', 0),
            '/broken.png': (200, 'image/png', b'not a PNG', 0),
        }
        with server(routes) as (url, requests, _):
            image = self.render(url+'/page')
            self.assertGreater(color_count(image, 0xaabbcc), 10000)
            self.assertEqual(set(requests), {'/page','/spoof.css','/missing.css','/broken.png'})
            result = subprocess.run([str(BROWSER), '--dump', url+'/page'], env=self.env,
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('PAGE_SURVIVES_BAD_ASSETS', result.stdout)

    def test_discovery_and_worker_concurrency_are_bounded(self):
        html = ''.join(f'<link rel="stylesheet" href="/style{i}.css">' for i in range(45))+'<p>Bounded page</p>'
        routes = {'/page': (200, 'text/html', html.encode(), 0)}
        routes.update({f'/style{i}.css': (200, 'text/css', b'p{color:#202020}', .02) for i in range(45)})
        with server(routes) as (url, requests, stats):
            self.render(url+'/page')
            self.assertEqual(len([path for path in requests if path.endswith('.css')]), 32)
            self.assertLessEqual(stats['max_active'], 3)

    def test_local_documents_do_not_trigger_remote_requests(self):
        with server({}) as (url, requests, _):
            path=self.root/'local.html'
            path.write_text(f'<link rel="stylesheet" href="{url}/style.css"><img src="{url}/image.png"><p>Local page</p>')
            self.render(str(path))
            self.assertEqual(requests, [])

    def test_jpeg_is_fetched_decoded_and_painted(self):
        jpeg = (ROOT/'tests/fixtures/sample.jpg').read_bytes()
        html = (b'<title>JPEG rendering</title><body style="margin:0">'
                b'<img src="assets/sample.jpg" width="160" height="80" alt="Red JPEG"></body>')
        routes = {
            '/page': (200, 'text/html', html, 0),
            '/assets/sample.jpg': (200, 'image/jpeg', jpeg, 0),
        }
        with server(routes) as (url, requests, _):
            image = self.render(url+'/page')
            self.assertGreater(color_count(image, 0xff0000, tolerance=2), 10000,
                               'JPEG decoder pixels must reach the browser raster output')
            self.assertEqual(collections.Counter(requests), {'/page': 1, '/assets/sample.jpg': 1})

    def test_png_and_jpeg_accept_case_insensitive_mime_with_parameters(self):
        jpeg = (ROOT/'tests/fixtures/sample.jpg').read_bytes()
        html = (b'<title>Image media types</title><body style="margin:0">'
                b'<img src="picture.png" width="160" height="80">'
                b'<img src="picture.jpg" width="160" height="80"></body>')
        routes = {
            '/page': (200, 'text/html', html, 0),
            '/picture.png': (200, 'IMAGE/PNG; test=parameter', solid_png((41, 167, 132)), 0),
            '/picture.jpg': (200, 'Image/Jpeg; test="parameter"', jpeg, 0),
        }
        with server(routes) as (url, requests, _):
            image = self.render(url+'/page')
            self.assertGreater(color_count(image, 0x29a784), 10000)
            self.assertGreater(color_count(image, 0xff0000, tolerance=2), 10000)
            self.assertEqual(set(requests), {'/page', '/picture.png', '/picture.jpg'})

    def test_empty_and_failed_stylesheets_preserve_document_and_later_styles(self):
        html = (b'<title>Empty and failed CSS</title><style>body{background:#aabbcc}</style>'
                b'<link rel="stylesheet" href="empty.css">'
                b'<link rel="stylesheet" href="failed.css">'
                b'<link rel="stylesheet" href="valid.css">'
                b'<body><p class="keep">PAGE_SURVIVES_EMPTY_AND_FAILED_CSS</p></body>')
        routes = {
            '/page': (200, 'text/html', html, 0),
            '/empty.css': (200, 'text/css', b'', 0),
            '/failed.css': (503, 'text/css', b'.keep{display:none}', 0),
            '/valid.css': (200, 'text/css', b'.keep{background:#527a91;min-height:80px}', 0),
        }
        with server(routes) as (url, requests, _):
            image = self.render(url+'/page')
            self.assertGreater(color_count(image, 0xaabbcc), 10000)
            self.assertGreater(color_count(image, 0x527a91), 10000,
                               'successful CSS must apply after an empty sheet or failed response')
            self.assertEqual(set(requests), {'/page', '/empty.css', '/failed.css', '/valid.css'})
            result = subprocess.run([str(BROWSER), '--dump', url+'/page'], env=self.env,
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('PAGE_SURVIVES_EMPTY_AND_FAILED_CSS', result.stdout)

    def test_conditional_stylesheets_and_svg_render_across_viewport_sizes(self):
        html = (b'<title>Responsive SVG page</title><style>body{margin:0}.band{min-height:100px;background:#e8eef0}</style>'
                b'<link rel="stylesheet" href="/screen.css" media="(min-width:1000px)">'
                b'<link rel="stylesheet" href="/print.css" media="print">'
                b'<style media="(max-width:999px)">.band{background:#b98a64}</style>'
                b'<div class="band">Responsive document</div>'
                b'<img src="/vector.svg" width="160" height="80" alt="Static SVG">')
        routes = {
            '/page': (200, 'text/html', html, 0),
            '/screen.css': (200, 'text/css', b'.band{background:#5e9178}', 0),
            '/vector.svg': (200, 'IMAGE/SVG+XML; charset=utf-8', (ROOT/'tests/fixtures/vector.svg').read_bytes(), 0),
        }
        with server(routes) as (url, requests, _):
            narrow = self.render(url+'/page', '900x700')
            self.assertGreater(color_count(narrow, 0xb98a64), 20000)
            self.assertGreater(color_count(narrow, 0xb04acd), 10000, 'static SVG pixels must reach the HTTP document')
            wide = self.render(url+'/page', '1200x700')
            self.assertGreater(color_count(wide, 0x5e9178), 20000)
            self.assertGreater(color_count(wide, 0xb04acd), 10000)
            self.assertEqual(collections.Counter(requests), {'/page': 2, '/screen.css': 2, '/vector.svg': 2},
                             'responsive stylesheets are fetched once per document; print-only CSS stays unfetched')

    def test_bad_oversized_and_external_reference_svg_keep_the_document(self):
        valid = b'<svg xmlns="http://www.w3.org/2000/svg" width="16" height="8"><rect width="16" height="8" fill="#2f8bba"/></svg>'
        html = (b'<title>SVG failures</title><style>body{background:#aaadca}</style><body><p>SVG_FAILURES_PRESERVE_DOCUMENT</p>'
                b'<img src="/valid.svg" width="140" height="70"><img src="/bad.svg" alt="Malformed SVG">'
                b'<img src="/oversized.svg" alt="Too large"><img src="/external.svg" alt="External reference">'
                b'<img src="/spoof.svg" alt="Wrong MIME"><img src="/large.svg" alt="Too many bytes"></body>')
        routes = {
            '/page': (200, 'text/html', html, 0),
            '/valid.svg': (200, 'image/svg+xml', valid, 0),
            '/bad.svg': (200, 'image/svg+xml', b'<svg><rect', 0),
            '/oversized.svg': (200, 'image/svg+xml', b'<svg width="4097" height="1"><rect width="4097" height="1" fill="#ff0000"/></svg>', 0),
            '/external.svg': (200, 'image/svg+xml', b'<svg width="8" height="8"><image href="/must-not-fetch.png" width="8" height="8"/></svg>', 0),
            '/spoof.svg': (200, 'image/svg+xml-evil', valid, 0),
            '/large.svg': (200, 'image/svg+xml', b' ' * (2*1024*1024 + 1), 0),
        }
        with server(routes) as (url, requests, _):
            image = self.render(url+'/page')
            self.assertGreater(color_count(image, 0xaaadca), 10000)
            self.assertGreater(color_count(image, 0x2f8bba), 7000)
            self.assertNotIn('/must-not-fetch.png', requests)
            self.assertEqual(set(requests), set(routes))
            result = subprocess.run([str(BROWSER), '--dump', url+'/page'], env=self.env,
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('SVG_FAILURES_PRESERVE_DOCUMENT', result.stdout)


if __name__ == '__main__': unittest.main()
