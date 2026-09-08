"""The one place ring 1 talks to the network -- and it does so through a seam.

Every function that needs bytes takes a :class:`Fetcher` as an explicit
parameter. There is deliberately no module-level default and no
``set_fetcher()``: the QGIS implementation carries a per-request ``QgsFeedback``
as constructor state, and several fetches run concurrently on the task pool. A
shared global would bind one arbitrary subtask's feedback for every thread, so
cancelling one download would abort a different one.

The stdlib implementation here is what tests and any headless path use. QGIS
supplies its own in ``nasa_power.qgis_bridge.net``, which routes through
``QgsNetworkAccessManager`` and so inherits the user's configured proxy, CA
bundle and authentication -- on a corporate network raw ``urllib`` would simply
look broken.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

from nasa_power.core.errors import PowerHTTPError, PowerValidationError

#: Identifies the plugin to POWER. They publish no rate limit but do ask that
#: clients be identifiable and not hammer the same location.
USER_AGENT = "qgis-nasa-power/0.1.0 (+https://power.larc.nasa.gov/)"

#: The only host any fetcher in this package will talk to. POWER needs no
#: credentials, so there is nothing to leak -- but a URL is the one input that
#: reaches this code from config and from saved projects, and an allow-list
#: costs one comparison.
ALLOWED_HOST = "power.larc.nasa.gov"

DEFAULT_TIMEOUT = 60.0


def check_url(url: str) -> None:
    """Reject anything that is not HTTPS to the POWER host.

    Raises
    ------
    PowerValidationError
        If the scheme is not ``https`` or the host is not :data:`ALLOWED_HOST`.
    """
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise PowerValidationError(
            f"Refusing to fetch a non-HTTPS URL ({parts.scheme or 'no scheme'}): {url}"
        )
    if parts.hostname != ALLOWED_HOST:
        raise PowerValidationError(
            f"Refusing to fetch from {parts.hostname!r}; this client only talks to "
            f"{ALLOWED_HOST}. URL: {url}"
        )


@runtime_checkable
class Fetcher(Protocol):
    """Something that can turn a POWER URL into bytes."""

    def fetch(self, url: str, timeout: float = DEFAULT_TIMEOUT) -> bytes:
        """Return the response body, or raise :class:`PowerHTTPError`."""
        ...


class UrllibFetcher:
    """Standard-library fetcher. No dependencies, no proxy awareness."""

    def __init__(self, user_agent: str = USER_AGENT) -> None:
        self.user_agent = user_agent

    def fetch(self, url: str, timeout: float = DEFAULT_TIMEOUT) -> bytes:
        check_url(url)
        request = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310 - scheme and host checked by check_url
                body: bytes = response.read()
        except urllib.error.HTTPError as exc:
            # Reading the body is best-effort context; a POWER 422 puts the
            # actionable message there, so it is worth trying.
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except Exception:  # pragma: no cover - body is best-effort
                detail = ""
            raise PowerHTTPError(exc.code, detail, url) from exc
        except urllib.error.URLError as exc:
            raise PowerHTTPError(0, f"Network error: {exc.reason}", url) from exc

        if not body:
            # A zero-byte 200 is not a valid POWER response, and it is the
            # single most dangerous thing that can reach the cache: cache hits
            # never re-fetch, so an empty file would be read as real data
            # forever. Fail here rather than let the caller write it.
            raise PowerHTTPError(0, "Empty response body", url)
        return body


class StubFetcher:
    """Serve canned bytes by URL. For tests, and for offline reruns.

    Raises :class:`AssertionError` when asked for a URL it was not given, which
    is what makes "a cache hit never touches the network" a testable claim
    rather than an intention.
    """

    def __init__(self, responses: dict[str, bytes] | None = None) -> None:
        self.responses = dict(responses or {})
        self.calls: list[str] = []

    def fetch(self, url: str, timeout: float = DEFAULT_TIMEOUT) -> bytes:
        self.calls.append(url)
        try:
            return self.responses[url]
        except KeyError:
            known = "\n  ".join(self.responses) or "(none)"
            raise AssertionError(
                f"StubFetcher was asked for an unexpected URL:\n  {url}\n"
                f"Known URLs:\n  {known}"
            ) from None
