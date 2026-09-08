"""Exceptions for the POWER client.

Every one of these carries the request that produced it. A POWER 422 names the
field it rejected -- "Please provide at least a 2 degree range in latitude" --
and without the URL beside it that message is unactionable: the user cannot
tell *which* of six tiled requests was malformed.
"""

from __future__ import annotations

import json
from typing import Any


class PowerError(Exception):
    """Base for everything this package raises."""


class PowerValidationError(PowerError, ValueError):
    """A request the POWER API would reject, caught before it is sent.

    Subclasses ``ValueError`` so callers that only care that an argument was
    wrong can keep catching that. Raising these locally rather than letting the
    API answer matters for more than politeness: ``hourly/regional`` fails as a
    19 KB HTML page with no hint that the combination is simply unsupported.
    """


class PowerCacheMiss(PowerError, FileNotFoundError):
    """Nothing cached for a request, and fetching was not permitted."""


class PowerHTTPError(PowerError, RuntimeError):
    """An error response from the POWER API, carrying the offending URL.

    ``status`` is 0 for a transport-level failure (DNS, TLS, timeout), where
    there is no HTTP status to report but the URL is still the useful context.
    """

    def __init__(self, status: int, body: str, url: str) -> None:
        self.status = status
        self.body = body
        self.url = url
        detail = describe_error_body(body)
        super().__init__(f"POWER API returned HTTP {status} for {url}\n{detail}")

    @property
    def is_retryable(self) -> bool:
        """Whether re-sending this exact request could plausibly succeed.

        A 422 is a validation failure: the same request fails identically
        forever, so retrying only burns goodwill against a free API.
        """
        return self.status in RETRY_STATUSES


#: Rate limiting and transient server faults. Deliberately excludes 4xx other
#: than 429 -- see :attr:`PowerHTTPError.is_retryable`.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


def describe_error_body(body: str) -> str:
    """Render a POWER error body as something worth putting in a message.

    POWER's failures come in three shapes and only one of them is pleasant:

    * a validation error with a ``messages`` list naming what was wrong;
    * a FastAPI ``detail`` list, used for bad enum values such as an unknown
      ``format``, which enumerates the values it *would* have accepted;
    * a full HTML page, returned by ``hourly/regional`` and by anything that
      reaches the website rather than the API.

    HTML is truncated hard: dumping 19 KB of markup into a QGIS message bar
    buries the one line that matters.
    """
    if not body:
        return "(empty response body)"

    stripped = body.lstrip()
    # startswith, not `stripped[:1] in "<"`: the empty string is a substring of
    # every string, so the `in` form calls a whitespace-only body an HTML page.
    if stripped.startswith("<"):
        return (
            "The server returned an HTML page rather than a JSON API error. "
            "That usually means the URL does not name a real endpoint."
        )

    try:
        parsed: Any = json.loads(body)
    except (ValueError, TypeError):
        return body[:1000]

    if isinstance(parsed, dict):
        messages = parsed.get("messages")
        if isinstance(messages, list) and messages:
            return "\n".join(str(m) for m in messages)
        if isinstance(messages, dict) and messages:
            return "\n".join(f"{k}: {v}" for k, v in messages.items())

        detail = parsed.get("detail")
        if isinstance(detail, list) and detail:
            lines = []
            for item in detail:
                if isinstance(item, dict):
                    loc = ".".join(str(p) for p in item.get("loc", []) if p != "query")
                    lines.append(f"{loc}: {item.get('msg', item)}" if loc else str(item.get("msg", item)))
                else:
                    lines.append(str(item))
            return "\n".join(lines)
        if isinstance(detail, str):
            return detail

        header = parsed.get("header")
        if isinstance(header, str):
            return header

    return json.dumps(parsed)[:1000]
