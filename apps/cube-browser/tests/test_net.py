#!/usr/bin/env python3
"""Deterministic HTTP parser and TLS rejection tests using loopback only.

Pass the cube-netd binary as argv[1], or use build/cube-netd.
No system CA, resolver or browser configuration is modified.
"""
import contextlib
from pathlib import Path
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / 'build/cube-netd'
MAX = 2 * 1024 * 1024


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        request = b''
        self.request.settimeout(3)
        while b'\r\n\r\n' not in request:
            packet = self.request.recv(8192)
            if not packet:
                return
            request += packet
        path = request.split(b' ', 2)[1].decode('ascii')
        self.server.requests.append(request)
        if path.startswith('/chain/'):
            count = int(path.rsplit('/', 1)[1])
            reply = b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK' if not count else f'HTTP/1.1 302 Found\r\nLocation: /chain/{count-1}\r\n\r\n'.encode()
        else:
            reply = self.server.replies.get(path, b'HTTP/1.1 404 Not Found\r\nContent-Length: 7\r\n\r\nmissing')
        try:
            # Multiple writes exercise incremental header/body reads.
            for start in range(0, len(reply), 733):
                self.request.sendall(reply[start:start + 733])
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
            pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, replies, tls=None):
        self.replies, self.requests, self.tls = replies, [], tls
        super().__init__(('127.0.0.1', 0), Handler)

    def get_request(self):
        sock, addr = super().get_request()
        if self.tls:
            try:
                sock = self.tls.wrap_socket(sock, server_side=True)
            except ssl.SSLError:
                sock.close()
                raise
        return sock, addr


@contextlib.contextmanager
def serve(replies, tls=None):
    with Server(replies, tls) as server:
        worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        worker.start()
        try:
            yield server, f'{"https" if tls else "http"}://127.0.0.1:{server.server_address[1]}'
        finally:
            server.shutdown()
            worker.join()


def run(url):
    return subprocess.run([str(BINARY), '--fetch', url], capture_output=True, timeout=25)


def resource(kind, initiator, target, binary=None):
    return subprocess.run([str(binary or BINARY), '--resource', str(kind), initiator, target], capture_output=True, timeout=25)


def response(body=b'body', mime='text/css', status=200):
    return f'HTTP/1.1 {status} Status\r\nContent-Type: {mime}\r\nContent-Length: {len(body)}\r\n\r\n'.encode() + body


class NetTests(unittest.TestCase):
    def assert_reply(self, reply, expected=None, error=None):
        with serve({'/': reply}) as (_, url):
            result = run(url)
        if error is not None:
            self.assertNotEqual(result.returncode, 0, result.stderr)
            self.assertIn(error, result.stderr.decode())
            self.assertEqual(result.stdout, b'')
        else:
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, expected)

    def test_fixed_length_and_request_headers(self):
        body = b'<h1>Hello \xe2\x98\x83</h1>'
        with serve({'/hello?a=1': b'HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body}) as (server, url):
            result = run(url + '/hello?a=1#not-sent')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, body)
            request = server.requests[0]
            self.assertIn(b'GET /hello?a=1 HTTP/1.1', request)
            self.assertIn(b'Accept-Encoding: identity\r\n', request)
            self.assertNotIn(b'Referer:', request)
            self.assertNotIn(b'not-sent', request)
            self.assertIn((url + '/hello?a=1#not-sent').encode(), result.stderr)
            series = '.'.join((ROOT/'VERSION').read_text().strip().split('.')[:2])
            self.assertIn(f'User-Agent: cube-browser/{series}\r\n'.encode(), request)

    def test_chunked_extensions_and_trailers(self):
        self.assert_reply(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5;name=value\r\nhello\r\n6\r\n world\r\n0\r\nETag: "x"\r\n\r\n', b'hello world')

    def test_close_delimited(self):
        self.assert_reply(b'HTTP/1.0 200 OK\r\n\r\nhello', b'hello')

    def test_informational(self):
        self.assert_reply(b'HTTP/1.1 103 Early Hints\r\nLink: </a>\r\n\r\nHTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK', b'OK')

    def test_no_content(self):
        self.assert_reply(b'HTTP/1.1 204 No Content\r\n\r\n', b'')

    def test_error_status_is_renderable(self):
        self.assert_reply(b'HTTP/1.1 404 Not Found\r\nContent-Length: 7\r\n\r\nmissing', b'missing')

    def test_identical_duplicate_lengths(self):
        self.assert_reply(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\nOK', b'OK')

    def test_exact_body_limit(self):
        self.assert_reply(f'HTTP/1.1 200 OK\r\nContent-Length: {MAX}\r\n\r\n'.encode() + b'a' * MAX, b'a' * MAX)

    def test_rejected_messages(self):
        cases = [
            (b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Length: 3\r\n\r\nabc', 'Conflicting'),
            (b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nTransfer-Encoding: chunked\r\n\r\n', 'Ambiguous'),
            (b'HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nabc', 'Truncated'),
            (b'HTTP/1.1 200 OK\r\nContent-Length: -1\r\n\r\n', 'Content-Length'),
            (f'HTTP/1.1 200 OK\r\nContent-Length: {MAX+1}\r\n\r\n'.encode(), '2 MiB'),
            (b'HTTP/1.1 200 OK\r\n\r\n' + b'a' * (MAX+1), '2 MiB'),
            (b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n200001\r\n', '2 MiB'),
            (b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n-1\r\n', 'chunk length'),
            (b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\nOKxx', 'chunk ending'),
            (b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\na', 'Truncated'),
            (b'HTTP/1.1 200 OK\r\nTransfer-Encoding: gzip, chunked\r\n\r\n', 'Transfer-Encoding'),
            (b'HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\n\r\n', 'identity'),
            (b'HTTP/1.1 200 OK\r\nContent-Type: text/css\r\nContent-Type: text/html\r\n\r\n', 'Conflicting Content-Type'),
            (b'HTTP/1.1 200 OK\r\nContent-Type: ' + b'a' * 128 + b'\r\n\r\n', 'Content-Type exceeds'),
            (b'HTTP/1.1 200 OK\r\nContent-Type: text/css; x=\xff\r\n\r\n', 'Content-Type contains'),
            (b'HTTP/1.1 200 OK\n\n', 'line ending'),
            (b'HTTP/1.1 200 OK\r\nBad\rHeader: x\r\n\r\n', 'carriage return'),
            (b'HTTP/1.1 200 OK\r\nX: a\x00b\r\n\r\n', 'Malformed'),
            (b'HTTP/1.1 200 OK\r\n folding: bad\r\n\r\n', 'Malformed'),
            (b'HTTP/1.1 200 OK\r\nX: ' + b'a' * 17000 + b'\r\n\r\n', 'oversized'),
            (b'HTTP/1.1 200 OK\r\n' + b'X: 1234567890\r\n' * 6000 + b'\r\n', '64 KiB'),
            (b'HTTP/1.1 101 Switching Protocols\r\n\r\n', 'upgrade'),
            (b'HTTP/1.1 abc Invalid\r\n\r\n', 'status'),
        ]
        for reply, error in cases:
            with self.subTest(error=error, prefix=reply[:80]):
                self.assert_reply(reply, error=error)

    def test_redirect_limit(self):
        with serve({}) as (_, url):
            self.assertEqual(run(url + '/chain/5').stdout, b'OK')
            self.assertIn(b'Too many redirects', run(url + '/chain/6').stderr)

    def test_relative_redirect(self):
        with serve({'/a/b/start': b'HTTP/1.1 302 Found\r\nLocation: ../final?x=1\r\n\r\n', '/a/final?x=1': b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK'}) as (_, url):
            result = run(url + '/a/b/start')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, b'OK')

    def test_disallowed_url(self):
        for url in ['file:///etc/passwd', 'javascript:alert(1)', 'http://user:secret@localhost/', 'http://localhost:65536/', 'http://localhost:9999999999999/', 'http://localhost/a\r\nInjected:yes', 'http://localhost\\x/', 'http://[::1]/']:
            with self.subTest(url=url):
                self.assertNotEqual(run(url).returncode, 0)

    def test_url_resolution(self):
        base = 'https://example.com/a/b/page?q=old'
        for ref, expected in {
            '../hello': 'https://example.com/a/hello',
            './hello': 'https://example.com/a/b/hello',
            '/hello': 'https://example.com/hello',
            '?q=new': 'https://example.com/a/b/page?q=new',
            '#anchor': base + '#anchor',
            '#': base + '#',
            '../hello#section': 'https://example.com/a/hello#section',
            '//other.example/x': 'https://other.example/x',
            '../../../../../hello': 'https://example.com/hello',
            '../.': 'https://example.com/a/',
        }.items():
            with self.subTest(ref=ref):
                result = subprocess.run([str(BINARY), '--resolve', base, ref], capture_output=True, timeout=2)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.decode().strip(), expected)
        for ref in ['javascript:alert(1)', 'data:text/html,x', 'file:///x']:
            self.assertNotEqual(subprocess.run([str(BINARY), '--resolve', base, ref], capture_output=True).returncode, 0)

    def test_redirect_fragments_inherit_override_and_clear(self):
        cases = [('/middle', '/final', '#original', '#original'),
                 ('/middle#replacement', '/final', '#original', '#replacement'),
                 ('/middle#', '/final', '#original', '#'),
                 ('/middle', '/final#replacement', '#original', '#replacement'),
                 ('/middle', '/final#', '#original', '#'),
                 ('/middle', '/final', '', ''),
                 ('/middle', '/final', '#', '#')]
        for first, second, initial, final in cases:
            with self.subTest(first=first, second=second, initial=initial):
                replies = {'/start': f'HTTP/1.1 302 Found\r\nLocation: {first}\r\n\r\n'.encode(),
                           '/middle': f'HTTP/1.1 307 Temporary Redirect\r\nLocation: {second}\r\n\r\n'.encode(),
                           '/final': response(b'done', 'text/html')}
                with serve(replies) as (server, url):
                    result = run(url+'/start'+initial)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, b'done')
                    self.assertTrue(result.stderr.decode().rstrip().endswith(url+'/final'+final), result.stderr)
                    self.assertEqual(len(server.requests), 3)
                    for request in server.requests:
                        self.assertNotIn(b'#', request)

    def test_absolute_redirect_inherits_fragment(self):
        with serve({'/final': response(b'done', 'text/html')}) as (target, destination):
            with serve({'/start': f'HTTP/1.1 302 Found\r\nLocation: {destination}/final\r\n\r\n'.encode()}) as (_, origin):
                result = run(origin+'/start#client-only')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(result.stderr.decode().rstrip().endswith(destination+'/final#client-only'))
            self.assertNotIn(b'client-only', target.requests[0])

    def test_untrusted_tls_and_tls12_rejected(self):
        with tempfile.TemporaryDirectory(prefix='cube-net-tls-') as temp:
            cert, key = Path(temp) / 'cert.pem', Path(temp) / 'key.pem'
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(key), '-out', str(cert), '-days', '1', '-subj', '/CN=localhost'], check=True, capture_output=True)
            for maximum in [ssl.TLSVersion.TLSv1_3, ssl.TLSVersion.TLSv1_2]:
                with self.subTest(maximum=maximum):
                    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                    ctx.maximum_version = maximum
                    ctx.load_cert_chain(cert, key)
                    with serve({}, ctx) as (_, url):
                        result = run(url)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(b'TLS:', result.stderr)


class ResourceTests(unittest.TestCase):
    def test_allowed_css_and_image_mimes(self):
        cases = [(1, 'text/css', b'p{color:red}'),
                 (1, 'TEXT/CSS ; charset=utf-8', b'p{color:red}'),
                 (2, 'image/png', b'\x89PNG\x00binary'),
                 (2, 'image/jpeg', b'\xff\xd8\xff\x00binary'),
                 (2, 'image/svg+xml; charset=utf-8', b'<svg xmlns="http://www.w3.org/2000/svg"/>')]
        for kind, mime, body in cases:
            with self.subTest(mime=mime), serve({'/asset': response(body, mime)}) as (server, url):
                result = resource(kind, url + '/document', url + '/asset')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, body)
                request = server.requests[0]
                self.assertIn(b'Accept-Encoding: identity\r\n', request)
                self.assertNotIn(b'Referer:', request)
                self.assertNotIn(b'Cookie:', request)

    def test_resource_mime_policy(self):
        for kind, mime in [(1, 'text/html'), (1, 'text/css-evil'), (1, 'text/css garbage'),
                           (1, 'image/png'), (2, 'text/html'), (2, 'image/jpg'),
                           (2, 'image/svg+xml-evil'), (2, 'image/svg+xml garbage'),
                           (2, 'image/png-evil'), (2, 'image/jpeg garbage')]:
            with self.subTest(kind=kind, mime=mime), serve({'/asset': response(b'\x89PNG\xff\xd8image', mime)}) as (_, url):
                result = resource(kind, url + '/document', url + '/asset')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b'rejected', result.stderr)
                self.assertEqual(result.stdout, b'')
        with serve({'/asset': b'HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\nbody'}) as (_, url):
            for kind in (1, 2):
                self.assertIn(b'no Content-Type', resource(kind, url, url + '/asset').stderr)

    def test_only_successful_resource_statuses(self):
        for status in (301, 304, 400, 404, 500):
            with self.subTest(status=status), serve({'/asset': response(mime='text/css', status=status)}) as (_, url):
                result = resource(1, url + '/document', url + '/asset')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f'HTTP {status}'.encode(), result.stderr)
                self.assertEqual(result.stdout, b'')

    def test_https_document_blocks_http_before_fetch(self):
        with serve({'/asset': response()}) as (server, url):
            for kind in (1, 2):
                result = resource(kind, 'https://example.org/document', url + '/asset')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b'Mixed content', result.stderr)
            self.assertEqual(server.requests, [])

    def test_invalid_initiators_targets_and_kinds(self):
        with serve({'/asset': response()}) as (server, url):
            for initiator in ('cube:demo', 'file:///home/page.html', 'javascript:void(0)',
                              'https://user:pass@example.org/', url + '/doc\nInjected'):
                result = resource(1, initiator, url + '/asset')
                self.assertIn(b'initiator', result.stderr)
                self.assertNotEqual(result.returncode, 0)
            for target in ('file:///etc/passwd', 'data:text/css,x', 'http://user:pass@localhost/', url + '/asset\nInjected'):
                result = resource(1, url + '/document', target)
                self.assertNotEqual(result.returncode, 0)
            for kind in ('0', '3', '-1', '1x', 'css'):
                result = resource(kind, url + '/document', url + '/asset')
                self.assertEqual(result.returncode, 2)
            self.assertEqual(server.requests, [])

    def test_http_document_http_redirect_allowed(self):
        replies = {'/start': b'HTTP/1.1 302 Found\r\nLocation: nested/hop\r\n\r\n',
                   '/nested/hop': b'HTTP/1.1 307 Temporary Redirect\r\nLocation: ../final\r\n\r\n',
                   '/final': response(b'p{color:green}')}
        with serve(replies) as (server, url):
            result = resource(1, url + '/document', url + '/start')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, b'p{color:green}')
            self.assertEqual(len(server.requests), 3)
            self.assertIn((url + '/final').encode(), result.stderr)

    def test_redirect_cannot_change_to_other_schemes_or_credentials(self):
        for destination in ('file:///etc/passwd', 'data:text/css,x', 'javascript:void(0)', 'http://user:pass@localhost/'):
            with self.subTest(destination=destination), serve({'/start': f'HTTP/1.1 302 Found\r\nLocation: {destination}\r\n\r\n'.encode()}) as (server, url):
                result = resource(1, url + '/document', url + '/start')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b'Invalid redirect URL', result.stderr)
                self.assertEqual(len(server.requests), 1)


class ResourceTLSTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Only this temporary test executable trusts the ephemeral local CA.
        # No environment variable can change the production worker's CA store.
        cls.temp = tempfile.TemporaryDirectory(prefix='cube-resource-tls-')
        directory = Path(cls.temp.name)
        cls.cert, cls.key = directory / 'cert.pem', directory / 'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                        '-keyout', str(cls.key), '-out', str(cls.cert), '-days', '1',
                        '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost'],
                       check=True, capture_output=True)
        cls.binary = directory / 'cube-netd-test-ca'
        subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Wpedantic', '-Werror',
                        f'-DCB_CA_BUNDLE="{cls.cert}"', '-I' + str(ROOT / '.deps/include'),
                        str(ROOT / 'src/net-main.c'), str(ROOT / 'src/net.c'), str(ROOT / 'src/ipc.c'),
                        str(ROOT / 'src/sandbox.c'), str(ROOT / 'src/policy.c'), str(ROOT / 'src/http.c'),
                        str(ROOT / '.deps/lib/libmbedtls.a'), str(ROOT / '.deps/lib/libmbedx509.a'),
                        str(ROOT / '.deps/lib/libmbedcrypto.a'), '-o', str(cls.binary)],
                       check=True, capture_output=True)
        cls.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cls.ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        cls.ctx.load_cert_chain(cls.cert, cls.key)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_https_redirect_chain_stays_encrypted(self):
        replies = {'/start': b'HTTP/1.1 302 Found\r\nLocation: /middle\r\n\r\n',
                   '/middle': b'HTTP/1.1 308 Permanent Redirect\r\nLocation: /final\r\n\r\n',
                   '/final': response(b'body{background:white}')}
        with serve(replies, self.ctx) as (server, address):
            address = address.replace('127.0.0.1', 'localhost')
            result = resource(1, address + '/document', address + '/start', self.binary)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, b'body{background:white}')
            self.assertEqual(len(server.requests), 3)

    def test_https_document_downgrade_rejected_at_each_redirect(self):
        with serve({'/asset': response()}) as (insecure, http):
            for depth in (0, 1, 2):
                replies = {f'/hop{i}': f'HTTP/1.1 302 Found\r\nLocation: /hop{i+1}\r\n\r\n'.encode() for i in range(depth)}
                replies[f'/hop{depth}'] = f'HTTP/1.1 302 Found\r\nLocation: {http}/asset\r\n\r\n'.encode()
                with self.subTest(depth=depth), serve(replies, self.ctx) as (secure, address):
                    address = address.replace('127.0.0.1', 'localhost')
                    result = resource(1, address + '/document', address + '/hop0', self.binary)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(b'Mixed content redirect refused', result.stderr)
                    self.assertEqual(len(secure.requests), depth+1)
                    self.assertEqual(insecure.requests, [])

    def test_http_document_still_cannot_downgrade_https_resource(self):
        with serve({'/asset': response()}) as (insecure, http):
            with serve({'/asset': f'HTTP/1.1 302 Found\r\nLocation: {http}/asset\r\n\r\n'.encode()}, self.ctx) as (_, address):
                address = address.replace('127.0.0.1', 'localhost')
                result = resource(1, http + '/document', address + '/asset', self.binary)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b'HTTPS to HTTP redirect refused', result.stderr)
                self.assertEqual(insecure.requests, [])


if __name__ == '__main__':
    if len(sys.argv) > 1 and not sys.argv[1].startswith('-'):
        BINARY = Path(sys.argv.pop(1)).resolve()
    unittest.main()
