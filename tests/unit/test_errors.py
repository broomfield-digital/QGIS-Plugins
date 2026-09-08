"""Turning POWER's three failure shapes into one usable message.

POWER fails in three ways and only one of them is pleasant:

* a validation error with a ``messages`` list naming exactly what was wrong
  (``error_422_bbox.json``);
* a FastAPI ``detail`` list, which names the field and enumerates the values it
  would have accepted;
* a **19 KB HTML page** (``error_404_hourly_regional.html``), returned by
  ``hourly/regional`` and by anything else that lands on the website instead of
  the API.

The third is what this module is really for. Dumped into a QGIS message bar it
buries the one line that matters, and it arrives with a 404 that says nothing
about the combination being unsupported -- so it is truncated to a sentence and
the request is refused in pre-flight instead.

Every error also carries its URL, because a POWER 422 is unactionable without
one: the user cannot tell which of six tiled requests was the malformed one.
"""

from __future__ import annotations

import json
import unittest

from nasa_power.core.errors import (
    RETRY_STATUSES,
    PowerCacheMiss,
    PowerError,
    PowerHTTPError,
    PowerValidationError,
    describe_error_body,
)
from tests.unit.support import load_bytes

URL = (
    "https://power.larc.nasa.gov/api/temporal/daily/regional?parameters=T2M"
    "&community=RE&latitude-min=40&latitude-max=41&longitude-min=-106"
    "&longitude-max=-104&start=20240201&end=20240201&format=JSON&time-standard=UTC"
)

#: A real FastAPI validation body: POWER answers this shape for a bad enum,
#: e.g. an unknown ``format``.
FASTAPI_DETAIL = json.dumps(
    {
        "detail": [
            {
                "loc": ["query", "format"],
                "msg": (
                    "value is not a valid enumeration member; permitted: "
                    "'NETCDF', 'JSON', 'ASCII', 'CSV', 'ICASA'"
                ),
                "type": "type_error.enum",
            }
        ]
    }
)


def _text(name: str) -> str:
    return load_bytes(name).decode("utf-8", "replace")


class DescribeValidationBodyTests(unittest.TestCase):
    def test_the_422_messages_are_surfaced(self) -> None:
        # The whole value of this body: it names the constraint, and the
        # constraint is a MINIMUM span, which is the surprising direction.
        described = describe_error_body(_text("error_422_bbox.json"))
        self.assertIn("at least a 2 degree range in latitude", described)

    def test_the_messages_list_beats_the_generic_header(self) -> None:
        # The fixture carries both. The header is boilerplate ("please review
        # the errors below"); the messages are the errors below.
        described = describe_error_body(_text("error_422_bbox.json"))
        self.assertNotIn("please review the errors below", described)

    def test_a_multi_message_body_keeps_every_line(self) -> None:
        body = json.dumps({"messages": ["first problem", "second problem"]})
        described = describe_error_body(body)
        self.assertIn("first problem", described)
        self.assertIn("second problem", described)

    def test_a_fastapi_detail_list_names_the_field_and_the_message(self) -> None:
        described = describe_error_body(FASTAPI_DETAIL)
        self.assertIn("format", described)
        self.assertIn("not a valid enumeration member", described)
        self.assertIn("'NETCDF'", described)
        # 'query' is the FastAPI request part, not something the user chose.
        self.assertTrue(described.startswith("format:"), described)

    def test_a_string_detail_is_passed_through(self) -> None:
        self.assertEqual("Not Found", describe_error_body(json.dumps({"detail": "Not Found"})))


class DescribeHtmlBodyTests(unittest.TestCase):
    def test_the_19kb_html_404_is_not_dumped(self) -> None:
        # hourly/regional answers text/html, not a JSON API error. Measured at
        # 19,456 bytes; a message bar showing that shows nothing.
        body = _text("error_404_hourly_regional.html")
        self.assertGreater(len(body), 19_000)

        described = describe_error_body(body)
        self.assertLess(len(described), 400)
        self.assertIn("HTML", described)
        self.assertNotIn("<!DOCTYPE", described)
        self.assertNotIn("<html", described)
        self.assertNotIn("<script", described)

    def test_leading_whitespace_does_not_hide_the_html(self) -> None:
        described = describe_error_body("\n\n  <html><body>oh dear</body></html>")
        self.assertIn("HTML", described)
        self.assertNotIn("<html", described)


class DescribeOtherBodyTests(unittest.TestCase):
    def test_an_empty_body_says_so(self) -> None:
        self.assertIn("empty", describe_error_body("").lower())

    def test_unparseable_text_is_capped(self) -> None:
        described = describe_error_body("x" * 5000)
        self.assertLessEqual(len(described), 1000)

    def test_an_unrecognised_json_shape_is_capped(self) -> None:
        described = describe_error_body(json.dumps({"surprise": "y" * 5000}))
        self.assertLessEqual(len(described), 1000)


class PowerHTTPErrorTests(unittest.TestCase):
    def test_retryable_statuses(self) -> None:
        # Rate limiting and transient server faults only.
        for status in (429, 500, 502, 503, 504):
            with self.subTest(status=status):
                self.assertTrue(PowerHTTPError(status, "", URL).is_retryable)
        self.assertEqual({429, 500, 502, 503, 504}, set(RETRY_STATUSES))

    def test_non_retryable_statuses(self) -> None:
        # A 422 is a validation failure: the same request fails identically
        # forever, so retrying only burns goodwill against a free API.
        for status in (400, 404, 422):
            with self.subTest(status=status):
                self.assertFalse(PowerHTTPError(status, "", URL).is_retryable)

    def test_the_message_carries_the_url(self) -> None:
        # Without it a 422 is unactionable: which of six tiled requests was it?
        error = PowerHTTPError(422, _text("error_422_bbox.json"), URL)
        self.assertIn(URL, str(error))
        self.assertIn("422", str(error))

    def test_the_message_carries_the_described_body(self) -> None:
        error = PowerHTTPError(422, _text("error_422_bbox.json"), URL)
        self.assertIn("at least a 2 degree range in latitude", str(error))

    def test_an_html_body_does_not_reach_the_message(self) -> None:
        error = PowerHTTPError(404, _text("error_404_hourly_regional.html"), URL)
        self.assertLess(len(str(error)), 1000)
        self.assertNotIn("<!DOCTYPE", str(error))
        self.assertIn(URL, str(error))

    def test_the_status_body_and_url_stay_available(self) -> None:
        body = _text("error_422_bbox.json")
        error = PowerHTTPError(422, body, URL)
        self.assertEqual(422, error.status)
        self.assertEqual(body, error.body)
        self.assertEqual(URL, error.url)

    def test_a_transport_failure_has_no_status(self) -> None:
        # status 0 means DNS/TLS/timeout: no HTTP status to report, but the URL
        # is still the useful context.
        error = PowerHTTPError(0, "Network error: timed out", URL)
        self.assertEqual(0, error.status)
        self.assertIn(URL, str(error))


class ExceptionHierarchyTests(unittest.TestCase):
    def test_everything_is_a_power_error(self) -> None:
        for exc in (
            PowerValidationError("x"),
            PowerCacheMiss("x"),
            PowerHTTPError(500, "", URL),
        ):
            with self.subTest(exc=type(exc).__name__):
                self.assertIsInstance(exc, PowerError)

    def test_a_validation_error_is_still_a_value_error(self) -> None:
        # So callers that only care that an argument was wrong keep working.
        self.assertIsInstance(PowerValidationError("x"), ValueError)

    def test_a_cache_miss_is_still_a_file_not_found(self) -> None:
        self.assertIsInstance(PowerCacheMiss("x"), FileNotFoundError)

    def test_an_http_error_is_still_a_runtime_error(self) -> None:
        self.assertIsInstance(PowerHTTPError(500, "", URL), RuntimeError)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class BodyShapeBoundaryTest(unittest.TestCase):
    """The three-way sort between HTML, JSON and plain text, at its edges.

    ``describe_error_body`` uses ``stripped.startswith("<")`` rather than the
    shorter ``stripped[:1] in "<"``. The difference only shows on a body that
    strips to nothing: the empty string is a substring of every string, so the
    ``in`` form calls a whitespace-only body an HTML page and tells the user to
    check the endpoint name when the real fault is an empty reply. The module
    carries a comment saying so; this is the test that makes it true.
    """

    def test_a_whitespace_only_body_is_not_an_html_page(self):
        for body in ("   ", "\n", "\t\n  \n"):
            with self.subTest(body=repr(body)):
                self.assertNotIn("HTML", describe_error_body(body))

    def test_a_real_html_page_still_is_one(self):
        self.assertIn("HTML", describe_error_body(_text("error_404_hourly_regional.html")))

    def test_leading_whitespace_does_not_hide_html(self):
        self.assertIn("HTML", describe_error_body("\n\n  <!DOCTYPE html><html></html>"))

    def test_a_plain_text_body_is_echoed_rather_than_swallowed(self):
        # Not JSON, not HTML: POWER's gateway can return a bare string, and
        # dropping it leaves the user with a status code and nothing else.
        self.assertIn("upstream connect error", describe_error_body("upstream connect error"))

    def test_a_very_long_plain_text_body_is_truncated_not_dropped(self):
        described = describe_error_body("x" * 5000)
        self.assertTrue(described.startswith("x"))
        self.assertLessEqual(len(described), 1000)

    def test_a_json_body_with_no_recognised_key_is_still_shown(self):
        # Falls through messages/detail/header to the raw dump, so a schema
        # change degrades to "here is what it said" rather than to silence.
        described = describe_error_body('{"unexpected": "surprise"}')
        self.assertIn("surprise", described)
