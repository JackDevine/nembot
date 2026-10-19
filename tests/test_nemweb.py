"""NEMWEB courtesy: spacing, identification, retries and listing cache (no network)."""
from email.message import Message
from io import BytesIO
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from nemsplainer import nemweb

URL = nemweb.ROOT + '2026/'


def response(body=b'<a href="MMSDM_2026_08/">x</a>'):
    stream = BytesIO(body)
    stream.__enter__ = lambda: stream
    return stream


def http_error(code, retry_after=None):
    headers = Message()
    if retry_after is not None:
        headers['Retry-After'] = retry_after
    return HTTPError(URL, code, 'error', headers, BytesIO())


class Courtesy(unittest.TestCase):
    def setUp(self):
        nemweb.clear_cache()
        self.addCleanup(nemweb.clear_cache)
        self.sleeps = []
        for target in (patch('nemsplainer.nemweb.time.sleep', side_effect=self.sleeps.append),
                       patch('nemsplainer.nemweb._last_request', 0.0)):
            target.start()
            self.addCleanup(target.stop)

    def serve(self, *outcomes):
        """Patch urlopen to return/raise ``outcomes`` in order, recording requests."""
        self.requests = []
        outcomes = list(outcomes)

        def urlopen(request, timeout=60):
            self.requests.append(request)
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        target = patch('urllib.request.urlopen', side_effect=urlopen)
        target.start()
        self.addCleanup(target.stop)

    def test_requests_identify_the_tool(self):
        self.serve(response())
        nemweb._listing(URL)
        self.assertIn('nemsplainer', self.requests[0].get_header('User-agent'))

    def test_listings_are_cached_within_the_ttl(self):
        self.serve(response())
        first = nemweb._listing(URL)
        self.assertEqual(nemweb._listing(URL), first)
        self.assertEqual(len(self.requests), 1)

    def test_throttling_honours_retry_after_then_succeeds(self):
        self.serve(http_error(429, '30'), http_error(503), response())
        nemweb._listing(URL)
        self.assertEqual(len(self.requests), 3)
        backoffs = [s for s in self.sleeps if s >= 1]
        self.assertEqual(backoffs, [30.0, nemweb.BACKOFF * 2])

    def test_missing_pages_are_not_retried(self):
        self.serve(http_error(404))
        with self.assertRaises(nemweb._Unavailable):
            nemweb._listing(URL)
        self.assertEqual(len(self.requests), 1)

    def test_gives_up_after_max_attempts(self):
        self.serve(*[URLError('down')] * nemweb.MAX_ATTEMPTS)
        with self.assertRaises(RuntimeError):
            nemweb._listing(URL)
        self.assertEqual(len(self.requests), nemweb.MAX_ATTEMPTS)

    def test_requests_are_spaced(self):
        self.serve(response(), response())
        with patch('nemsplainer.nemweb.REQUEST_INTERVAL', 1.0):
            nemweb._listing(URL)
            nemweb._listing(URL + 'MMSDM_2026_08/')
        self.assertTrue(any(0 < s <= 1.0 for s in self.sleeps))


if __name__ == '__main__':
    unittest.main()
