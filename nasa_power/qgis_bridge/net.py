"""A :class:`~nasa_power.core.fetcher.Fetcher` that goes through QGIS.

Why not stdlib ``urllib`` here: ``QgsNetworkAccessManager::instance()`` calls
``setupDefaultProxyAndCache()`` per thread, so a worker thread inherits the
user's configured proxy, CA bundle and authentication with no wiring at all.
On an institutional network raw ``urllib`` would simply fail and the plugin
would look broken. It also puts the request in QGIS's network activity
indicator, so a long tiled fetch is visible where users already look.

The feedback object is **constructor state, per instance**. That is not a
style choice: several chunk tasks run concurrently, and a module-level fetcher
mutated by each would bind one arbitrary task's feedback for every thread, so
cancelling one download would abort a different one.

``QgsBlockingNetworkRequest`` is documented as safe to call from a worker
thread, which is exactly where this runs -- inside ``QgsTask.run()``.
"""

from __future__ import annotations

import threading

from qgis.core import (
    QgsBlockingNetworkRequest,
    QgsFeedback,
    QgsNetworkAccessManager,
)
from qgis.PyQt.QtCore import QUrl
from qgis.PyQt.QtNetwork import QNetworkReply, QNetworkRequest

from nasa_power.core.errors import PowerHTTPError
from nasa_power.core.fetcher import ALLOWED_HOST, DEFAULT_TIMEOUT, USER_AGENT, check_url

#: HTTP status is carried on the reply attribute rather than the error code,
#: so a 422 arrives as ``ServerExceptionError`` and the real number has to be
#: read separately. Without it every API validation failure would surface as a
#: generic "server error" and POWER's own diagnosis would be thrown away.
_STATUS_ATTRIBUTE = QNetworkRequest.Attribute.HttpStatusCodeAttribute

_USER_AGENT_LOCK = threading.Lock()
_USER_AGENT_PREPROCESSOR_ID = ""


def _append_user_agent(request: QNetworkRequest) -> None:
    """Add the plugin's token to QGIS's own ``User-Agent``, for POWER only.

    Runs for every request QGIS makes anywhere in the process, so it returns
    immediately for any other host and can never raise.
    """
    try:
        if request.url().host() != ALLOWED_HOST:
            return
        existing = bytes(request.rawHeader(b"User-Agent")).decode("latin-1")
        if USER_AGENT in existing:
            # A plugin reload leaves the previous module's preprocessor
            # registered alongside the new one, so this can run twice on one
            # request; appending twice would misreport the client.
            return
        merged = f"{existing} {USER_AGENT}".strip()
        request.setRawHeader(b"User-Agent", merged.encode("latin-1"))
    except Exception:  # pragma: no cover - a global hook must never raise
        pass


def register_user_agent() -> None:
    """Ensure requests to POWER identify this plugin. Idempotent, thread-safe.

    ``QgsNetworkAccessManager::createRequest`` assigns the ``User-Agent`` raw
    header unconditionally, so the ``setHeader()`` below is discarded before
    the request leaves: measured on QGIS 4.2.2, POWER saw
    ``Mozilla/5.0 QGIS/40202/macOS Tahoe (26.6.2)`` and nothing about this
    client. Request preprocessors run *after* that assignment -- also measured
    -- which makes this the only hook left. POWER publishes no rate limit but
    asks that clients be identifiable, which is unmet if every QGIS in the
    world looks the same to it.
    """
    global _USER_AGENT_PREPROCESSOR_ID
    with _USER_AGENT_LOCK:
        if _USER_AGENT_PREPROCESSOR_ID:
            return
        _USER_AGENT_PREPROCESSOR_ID = QgsNetworkAccessManager.setRequestPreprocessor(
            _append_user_agent
        )


def unregister_user_agent() -> None:
    """Remove the preprocessor. Call from ``plugin.unload()``.

    A request preprocessor is registered process-wide and outlives the module
    that installed it, so a reload without this leaves the old function bound
    to a module that no longer exists -- and it keeps running on every network
    request QGIS makes anywhere. That is the same leak class as a stray toolbar
    icon, except it is in the network path.
    """
    global _USER_AGENT_PREPROCESSOR_ID
    with _USER_AGENT_LOCK:
        if not _USER_AGENT_PREPROCESSOR_ID:
            return
        try:
            QgsNetworkAccessManager.removeRequestPreprocessor(_USER_AGENT_PREPROCESSOR_ID)
        except (KeyError, ValueError):  # pragma: no cover - already gone
            pass
        _USER_AGENT_PREPROCESSOR_ID = ""


class QgisFetcher:
    """Fetch through QGIS's network stack, honouring its proxy and auth config.

    Parameters
    ----------
    feedback
        Cancellation channel for this fetcher's requests only. Give each
        concurrent task its own instance.
    auth_cfg
        A QGIS authentication configuration id. POWER needs no credentials, so
        this exists only for users who must reach it through an authenticating
        proxy.
    """

    def __init__(self, feedback: QgsFeedback | None = None, auth_cfg: str = "") -> None:
        self.feedback = feedback
        self.auth_cfg = auth_cfg

    def fetch(self, url: str, timeout: float = DEFAULT_TIMEOUT) -> bytes:
        check_url(url)

        if self.feedback is not None and self.feedback.isCanceled():
            # Getting here means the task was cancelled between scheduling and
            # sending. Raising keeps the caller from writing anything.
            raise PowerHTTPError(0, "Cancelled before the request was sent.", url)

        # QGIS overwrites the User-Agent set below; the preprocessor puts the
        # plugin's token back. See register_user_agent().
        register_user_agent()

        request = QNetworkRequest(QUrl(url))
        request.setHeader(QNetworkRequest.KnownHeaders.UserAgentHeader, USER_AGENT)
        request.setTransferTimeout(int(timeout * 1000))

        blocking = QgsBlockingNetworkRequest()
        if self.auth_cfg:
            blocking.setAuthCfg(self.auth_cfg)

        # forceRefresh=True: QGIS's own network cache must not answer for us.
        # This client has a content-addressed disk cache of its own, and two
        # caches with different expiry rules produce results neither can explain.
        code = blocking.get(request, True, self.feedback)

        reply = blocking.reply()
        status = reply.attribute(_STATUS_ATTRIBUTE)
        status = int(status) if status is not None else 0

        if code != QgsBlockingNetworkRequest.ErrorCode.NoError:
            body = bytes(reply.content()).decode("utf-8", errors="replace")
            if not body:
                body = blocking.errorMessage() or reply.errorString() or "no detail"
            raise PowerHTTPError(status, body, url)

        body_bytes = bytes(reply.content())
        if not body_bytes:
            # The dangerous case. A cancelled QgsBlockingNetworkRequest can
            # return NoError with an empty body -- and because a cache hit
            # never re-fetches, a zero-byte file written here would be read as
            # valid data forever. Refuse it, and say which of the two causes
            # it was.
            if self.feedback is not None and self.feedback.isCanceled():
                raise PowerHTTPError(status, "Cancelled during the request.", url)
            raise PowerHTTPError(
                status,
                "POWER returned an empty body with no error. Refusing to cache it.",
                url,
            )

        if reply.error() != QNetworkReply.NetworkError.NoError:
            # Belt and braces: a transport error that did not set the QGIS
            # error code. Never seen, but caching a partial body is exactly
            # the failure this module exists to prevent.
            raise PowerHTTPError(status, reply.errorString(), url)

        return body_bytes


def describe_proxy() -> str:
    """One line about the proxy QGIS will use, for the QA panel.

    Worth surfacing: when POWER is unreachable the first question is whether
    the request is even leaving the building, and the answer lives in a QGIS
    options page most users have never opened.
    """
    from qgis.core import QgsNetworkAccessManager

    try:
        proxy = QgsNetworkAccessManager.instance().proxy()
        host = proxy.hostName()
        if not host:
            return "No proxy configured; connecting directly."
        return f"Using QGIS's configured proxy: {host}:{proxy.port()}"
    except Exception:  # pragma: no cover - diagnostics must never raise
        return "Could not determine the proxy configuration."
