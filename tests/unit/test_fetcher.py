"""Guards on the one seam in ring 1 that touches the network.

Nothing here opens a socket. What is tested is :func:`check_url`, which is a
pure string check and the only thing standing between a URL that arrived from a
saved project or a settings file and ``urllib.request.urlopen``; and the
:class:`StubFetcher` contract, which is what makes "a cache hit never touches
the network" an assertion rather than an intention.

``UrllibFetcher.fetch``'s own HTTP paths -- the ``HTTPError`` body read and the
zero-byte-200 guard -- need a real socket and are out of scope here.
"""

from __future__ import annotations

import unittest

from nasa_power.core.errors import PowerValidationError
from nasa_power.core.fetcher import (
    ALLOWED_HOST,
    DEFAULT_TIMEOUT,
    USER_AGENT,
    Fetcher,
    StubFetcher,
    UrllibFetcher,
    check_url,
)

GOOD = "https://power.larc.nasa.gov/api/temporal/daily/point?parameters=T2M"


class CheckUrlTest(unittest.TestCase):
    """The allow-list. Every rejection here is a URL that must not be fetched."""

    def test_the_real_power_url_is_accepted(self):
        check_url(GOOD)  # must not raise

    def test_plain_http_is_refused_even_to_the_right_host(self):
        # POWER needs no credentials, so there is no secret to leak -- but a
        # downgraded scheme means the response can be rewritten in transit, and
        # the response is what every number on the map comes from.
        with self.assertRaises(PowerValidationError):
            check_url("http://power.larc.nasa.gov/api/temporal/daily/point")

    def test_another_host_is_refused(self):
        with self.assertRaises(PowerValidationError):
            check_url("https://example.com/api/temporal/daily/point")

    def test_a_lookalike_suffix_host_is_refused(self):
        # The check is an equality on the parsed hostname, not a substring or a
        # suffix test, so an attacker-controlled domain that merely *contains*
        # the real host does not pass.
        for host in (
            "power.larc.nasa.gov.evil.example",
            "evil-power.larc.nasa.gov.example",
            "notpower.larc.nasa.gov",
        ):
            with self.subTest(host=host):
                with self.assertRaises(PowerValidationError):
                    check_url(f"https://{host}/api/temporal/daily/point")

    def test_userinfo_cannot_smuggle_the_allowed_host(self):
        # "https://power.larc.nasa.gov@evil.example/" resolves to evil.example;
        # urlsplit().hostname reads the authority, which is what makes this safe.
        with self.assertRaises(PowerValidationError):
            check_url("https://power.larc.nasa.gov@evil.example/api/x")

    def test_non_http_schemes_are_refused(self):
        for url in (
            "file:///etc/passwd",
            "ftp://power.larc.nasa.gov/x",
            "data:text/plain,hello",
            "power.larc.nasa.gov/api/x",  # no scheme at all
        ):
            with self.subTest(url=url):
                with self.assertRaises(PowerValidationError):
                    check_url(url)

    def test_the_refusal_names_the_url(self):
        # A refused fetch is unactionable without the URL that was refused.
        with self.assertRaises(PowerValidationError) as caught:
            check_url("https://example.com/api/x")
        self.assertIn("https://example.com/api/x", str(caught.exception))

    def test_the_allowed_host_is_the_one_power_serves_from(self):
        self.assertEqual(ALLOWED_HOST, "power.larc.nasa.gov")


class StubFetcherTest(unittest.TestCase):
    """The stub's contract: it must be impossible to fetch by accident."""

    def test_a_known_url_is_served_and_recorded(self):
        stub = StubFetcher({GOOD: b"payload"})
        self.assertEqual(stub.fetch(GOOD), b"payload")
        self.assertEqual(stub.calls, [GOOD])

    def test_an_unexpected_url_fails_loudly_rather_than_returning_empty(self):
        # This is the whole point of the class. If an unknown URL returned b""
        # instead of raising, "a cache hit never calls the fetcher" and "the
        # planner built the URL I think it did" would both silently pass.
        stub = StubFetcher({GOOD: b"payload"})
        with self.assertRaises(AssertionError):
            stub.fetch("https://power.larc.nasa.gov/api/temporal/daily/point?other")

    def test_the_unexpected_url_is_named_in_the_failure(self):
        stub = StubFetcher({GOOD: b"payload"})
        with self.assertRaises(AssertionError) as caught:
            stub.fetch("https://power.larc.nasa.gov/nope")
        self.assertIn("https://power.larc.nasa.gov/nope", str(caught.exception))

    def test_an_empty_stub_serves_nothing(self):
        with self.assertRaises(AssertionError):
            StubFetcher().fetch(GOOD)

    def test_the_call_log_records_every_attempt_including_failed_ones(self):
        # fetch_with_retries counts attempts through this log, so a URL that
        # raised must still be recorded.
        stub = StubFetcher({GOOD: b"payload"})
        stub.fetch(GOOD)
        with self.assertRaises(AssertionError):
            stub.fetch("https://power.larc.nasa.gov/nope")
        self.assertEqual(stub.calls, [GOOD, "https://power.larc.nasa.gov/nope"])


class FetcherProtocolTest(unittest.TestCase):
    """Both implementations must satisfy the seam every core function takes."""

    def test_both_implementations_are_fetchers(self):
        self.assertIsInstance(StubFetcher(), Fetcher)
        self.assertIsInstance(UrllibFetcher(), Fetcher)

    def test_something_without_fetch_is_not_a_fetcher(self):
        class NotAFetcher:
            pass

        self.assertNotIsInstance(NotAFetcher(), Fetcher)

    def test_the_user_agent_identifies_the_plugin(self):
        # POWER publishes no rate limit but asks that clients be identifiable.
        self.assertIn("qgis-nasa-power", USER_AGENT)
        self.assertEqual(UrllibFetcher().user_agent, USER_AGENT)

    def test_the_default_timeout_is_finite(self):
        # A fetch with no timeout hangs a QgsTask forever.
        self.assertGreater(DEFAULT_TIMEOUT, 0)
        self.assertLess(DEFAULT_TIMEOUT, 600)


if __name__ == "__main__":
    unittest.main()
