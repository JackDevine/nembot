"""NEMWEB courtesy: throttling, identification, retries, connection reuse and listing cache."""
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.request
from urllib.error import HTTPError, URLError
import zipfile

from nembot import nemweb

URL = nemweb.ROOT + '2026/'


def response(body=b'<a href="MMSDM_2026_08/">x</a>'):
    return BytesIO(body)


def http_error(code, retry_after=None):
    headers = Message()
    if retry_after is not None:
        headers['Retry-After'] = retry_after
    return HTTPError(URL, code, 'error', headers, BytesIO())


class Fresh(unittest.TestCase):
    """Each test gets an empty listing cache and a fresh throttle."""

    def setUp(self):
        nemweb.clear_cache()
        self.addCleanup(nemweb.clear_cache)
        throttle = patch('nembot.nemweb._throttle', nemweb._Throttle())
        throttle.start()
        self.addCleanup(throttle.stop)


class Courtesy(Fresh):
    def setUp(self):
        super().setUp()
        self.sleeps = []
        target = patch('nembot.nemweb.time.sleep', side_effect=self.sleeps.append)
        target.start()
        self.addCleanup(target.stop)

    def serve(self, *outcomes):
        """Patch the opener to return/raise ``outcomes`` in order, recording requests."""
        self.requests = []
        outcomes = list(outcomes)

        def opener(request, timeout=60):
            self.requests.append(request)
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        target = patch('nembot.nemweb._open', side_effect=opener)
        target.start()
        self.addCleanup(target.stop)

    def test_requests_identify_the_tool(self):
        self.serve(response())
        nemweb._listing(URL)
        self.assertIn('nembot', self.requests[0].get_header('User-agent'))

    def test_listings_are_cached_within_the_ttl(self):
        self.serve(response())
        first = nemweb._listing(URL)
        self.assertEqual(nemweb._listing(URL), first)
        self.assertEqual(len(self.requests), 1)

    def test_throttling_honours_retry_after_then_succeeds(self):
        self.serve(http_error(429, '30'), http_error(503), response())
        nemweb._listing(URL)
        self.assertEqual(len(self.requests), 3)
        backoffs = [s for s in self.sleeps if s >= 1.5]
        self.assertEqual(backoffs, [30.0, nemweb.BACKOFF * 2])

    def test_missing_pages_are_not_retried_and_do_not_slow_things_down(self):
        self.serve(http_error(404))
        with self.assertRaises(nemweb._Unavailable):
            nemweb._listing(URL)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(nemweb._throttle.allowed, nemweb.CONCURRENCY)

    def test_gives_up_after_max_attempts(self):
        self.serve(*[URLError('down')] * nemweb.MAX_ATTEMPTS)
        with self.assertRaises(RuntimeError):
            nemweb._listing(URL)
        self.assertEqual(len(self.requests), nemweb.MAX_ATTEMPTS)

    def test_repeated_403s_stop_instead_of_retrying_around_a_block(self):
        self.serve(*[http_error(403)] * nemweb.MAX_ATTEMPTS)
        with self.assertRaisesRegex(RuntimeError, '403'):
            nemweb._listing(URL)
        self.assertEqual(len(self.requests), nemweb.FORBIDDEN_LIMIT)

    def test_optional_minimum_interval_spaces_requests(self):
        self.serve(response(), response())
        with patch('nembot.nemweb.REQUEST_INTERVAL', 1.0):
            nemweb._listing(URL)
            nemweb._listing(URL + 'MMSDM_2026_08/')
        self.assertTrue(any(0 < s <= 1.0 for s in self.sleeps))


class Throttle(unittest.TestCase):
    def test_errors_drop_to_one_request_and_recovery_is_gradual(self):
        throttle = nemweb._Throttle()
        throttle.acquire(); throttle.release(0.2, 'error')
        self.assertEqual(throttle.allowed, 1)
        self.assertGreaterEqual(throttle.delay, 1.0)
        for _ in range(nemweb.RECOVER_AFTER - 1):
            throttle.next_start = 0
            throttle.acquire(); throttle.release(0.01, 'ok')
        self.assertEqual(throttle.allowed, 1)
        throttle.next_start = 0
        throttle.acquire(); throttle.release(0.01, 'ok')
        self.assertEqual(throttle.allowed, min(2, nemweb.CONCURRENCY))

    def test_delay_tracks_latency_like_autothrottle(self):
        throttle = nemweb._Throttle()
        for _ in range(10):
            throttle.next_start = 0
            throttle.acquire(); throttle.release(0.4, 'ok')
        self.assertAlmostEqual(throttle.delay, 0.4 / throttle.allowed, places=2)
        throttle.next_start = 0
        throttle.acquire(); throttle.release(5.0, 'error')  # errors never shorten the delay
        self.assertGreaterEqual(throttle.delay, 5.0 / 1)


def zip_bytes():
    out = BytesIO()
    with zipfile.ZipFile(out, 'w') as target:
        target.writestr('x.CSV', 'C,END OF REPORT,2\n')
    return out.getvalue()


class Parallel(Fresh):
    def test_download_many_keeps_order_and_caps_concurrency(self):
        lock, state = threading.Lock(), {'active': 0, 'peak': 0}
        payload = zip_bytes()

        def opener(request, timeout=60):
            with lock:
                state['active'] += 1
                state['peak'] = max(state['peak'], state['active'])
            time.sleep(0.05)
            with lock:
                state['active'] -= 1
            return BytesIO(payload)
        urls = [f'https://example.test/f{i}.zip' for i in range(12)]
        with patch('nembot.nemweb._open', side_effect=opener), tempfile.TemporaryDirectory() as cache:
            got = [url for url, path, digest in nemweb.download_many(urls, cache)]
        self.assertEqual(got, urls)
        self.assertLessEqual(state['peak'], nemweb.CONCURRENCY)
        self.assertGreater(state['peak'], 1 if nemweb.CONCURRENCY > 1 else 0)


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'  # keep-alive
    connections = set()

    def do_GET(self):
        Handler.connections.add(self.client_address)
        if self.path == '/missing':
            self.send_response(404); self.send_header('Content-Length', '0'); self.end_headers()
            return
        body = b'x' * 20000
        self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class Pool(unittest.TestCase):
    def test_real_requests_reuse_connections_and_map_errors(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        base = f'http://127.0.0.1:{server.server_address[1]}'
        Handler.connections.clear()
        for i in range(5):
            with nemweb._open(urllib.request.Request(f'{base}/file{i}')) as response:
                self.assertEqual(len(response.read()), 20000)
        self.assertEqual(len(Handler.connections), 1)  # one TCP connection for five files
        with self.assertRaises(HTTPError) as caught:
            nemweb._open(urllib.request.Request(f'{base}/missing'))
        self.assertEqual(caught.exception.code, 404)
        with self.assertRaises(URLError):
            nemweb._open(urllib.request.Request('http://127.0.0.1:9/nothing'), timeout=2)


if __name__ == '__main__':
    unittest.main()
