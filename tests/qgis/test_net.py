"""What :class:`~nasa_power.qgis_bridge.net.QgisFetcher` must do with a real socket.

Everything here talks to a ``http.server`` on 127.0.0.1, never to POWER. That
is the only way to exercise the transport at all: ``QgsBlockingNetworkRequest``
has no injectable reply, so the choice is a real socket or no coverage of the
one class in ring 2 that can put bytes on disk.

Two decisions worth stating outright:

* **``check_url`` is monkeypatched away in the transport tests**, not worked
  around by calling internals -- ``fetch()`` has no seam below the check, so
  patching is what keeps the tests driving the public entry point. The patch
  records its calls, so those tests still prove the check ran; and
  :class:`CheckUrlIsEnforcedTest` leaves it alone entirely and proves a bad URL
  dies before a socket is opened.
* The empty-body guard gets **two** tests, because the wire cannot produce both
  halves of it. See :class:`EmptyBodyTest`.
"""

from __future__ import annotations

import http.server
import json
import threading
import time
from unittest import mock

from qgis.core import QgsBlockingNetworkRequest, QgsFeedback
from qgis.PyQt.QtCore import QUrl
from qgis.PyQt.QtNetwork import QNetworkProxy, QNetworkReply, QNetworkRequest

from nasa_power.core.errors import PowerHTTPError, PowerValidationError
from nasa_power.core.fetcher import USER_AGENT
from nasa_power.qgis_bridge import net
from tests.qgis.qgis_case import QgisTestCase, load_bytes

#: Seconds ``/slow`` withholds its body for. Long enough that a 0.4 s cancel
#: and a 0.5 s transfer timeout both land inside the window on a loaded
#: machine, short enough that a wedged test is not a wedged suite.
SLOW_SECONDS = 2.0


class _Handler(http.server.BaseHTTPRequestHandler):
    """Serves the four responses POWER can produce, plus one that stalls."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # noqa: D102 - silence the stderr access log
        pass

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's spelling
        self.server.record(self.path, self.headers)

        if self.path == "/empty":
            # A 200 whose body is zero bytes: the response that must never be
            # allowed to reach the content-addressed cache.
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if self.path == "/slow":
            self.send_response(200)
            self.send_header("Content-Length", "5")
            self.end_headers()
            time.sleep(SLOW_SECONDS)
            try:
                self.wfile.write(b"hello")
            except OSError:  # the client hung up, which is the point
                pass
            return

        if self.path == "/422":
            status, body = 422, load_bytes("error_422_bbox.json")
        elif self.path == "/500":
            status, body = 500, b'{"messages": ["Internal Server Error"]}'
        else:
            status, body = 200, b'{"properties": {"parameter": {}}}'

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _Server(http.server.ThreadingHTTPServer):
    """Threading so ``/slow`` cannot block a concurrent request, and so a
    cancelled client leaves its handler to finish alone."""

    daemon_threads = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._lock = threading.Lock()
        self.requests_seen: list[str] = []
        self.user_agents: list[str] = []

    def record(self, path: str, headers) -> None:
        with self._lock:
            self.requests_seen.append(path)
            self.user_agents.append(headers.get("User-Agent", ""))

    def reset(self) -> None:
        with self._lock:
            self.requests_seen.clear()
            self.user_agents.clear()


class LocalServerCase(QgisTestCase):
    """One server for the whole module, on an ephemeral loopback port."""

    server: _Server
    base: str

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.server = _Server(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.server.reset()


class TransportCase(LocalServerCase):
    """Base for tests that need bytes to actually move.

    ``check_url`` only ever allows ``https://power.larc.nasa.gov``, so it is
    replaced by a recorder for the duration. Every subclass test can therefore
    still assert the fetcher called it.
    """

    def setUp(self):
        super().setUp()
        self.checked: list[str] = []
        patcher = mock.patch.object(net, "check_url", self.checked.append)
        patcher.start()
        self.addCleanup(patcher.stop)


class SuccessfulFetchTest(TransportCase):
    """The happy path, and what the request carries on the way out."""

    def test_a_200_returns_the_body_bytes(self):
        body = net.QgisFetcher().fetch(f"{self.base}/ok")
        self.assertEqual(json.loads(body), {"properties": {"parameter": {}}})
        self.assertEqual(self.server.requests_seen, ["/ok"])

    def test_the_url_went_through_check_url_first(self):
        net.QgisFetcher().fetch(f"{self.base}/ok")
        self.assertEqual(self.checked, [f"{self.base}/ok"])

    def test_a_feedback_that_is_not_cancelled_does_not_interfere(self):
        feedback = QgsFeedback()
        body = net.QgisFetcher(feedback).fetch(f"{self.base}/ok")
        self.assertTrue(body)
        self.assertFalse(feedback.isCanceled())


class UserAgentTest(TransportCase):
    """POWER asks that clients be identifiable, and QGIS makes that hard.

    Measured on QGIS 4.2.2: ``QgsNetworkAccessManager::createRequest`` assigns
    the ``User-Agent`` raw header unconditionally, so the ``setHeader()`` in
    ``fetch()`` never reaches the wire -- the server saw
    ``Mozilla/5.0 QGIS/40202/macOS Tahoe (26.6.2)`` and nothing else. Request
    preprocessors run after that assignment, so that is where the plugin's
    token goes back on.
    """

    def test_the_request_identifies_the_plugin(self):
        # ALLOWED_HOST is patched rather than the URL, because the preprocessor
        # must key on the real host in production and there is no POWER here.
        with mock.patch.object(net, "ALLOWED_HOST", "127.0.0.1"):
            net.QgisFetcher().fetch(f"{self.base}/ok")

        sent = self.server.user_agents[0]
        self.assertIn(USER_AGENT, sent)
        # QGIS's own identity is appended to, not replaced: it is what its
        # servers' operators filter on.
        self.assertIn("QGIS/", sent)

    def test_requests_to_other_hosts_are_left_alone(self):
        # The preprocessor is process-global -- it runs for every WMS tile and
        # every plugin-repository poll QGIS makes -- so it must be inert
        # everywhere but POWER. Registered explicitly so this does not quietly
        # pass by never having been installed at all.
        net.register_user_agent()
        net.QgisFetcher().fetch(f"{self.base}/ok")
        self.assertNotIn(USER_AGENT, self.server.user_agents[0])

    def test_appending_twice_does_not_duplicate_the_token(self):
        # A plugin reload leaves the old module's preprocessor registered
        # beside the new one, so the same request can pass through twice.
        request = QNetworkRequest(QUrl(f"https://{net.ALLOWED_HOST}/api/x"))
        request.setRawHeader(b"User-Agent", b"Mozilla/5.0 QGIS/40202")
        net._append_user_agent(request)
        net._append_user_agent(request)

        sent = bytes(request.rawHeader(b"User-Agent")).decode()
        self.assertEqual(sent.count(USER_AGENT), 1)

    def test_registration_happens_once_however_many_fetchers_run(self):
        # Registering per fetch would stack one preprocessor per tile, and
        # several chunk tasks call this concurrently from worker threads.
        net.register_user_agent()
        first = net._USER_AGENT_PREPROCESSOR_ID
        net.register_user_agent()
        self.assertTrue(first)
        self.assertEqual(net._USER_AGENT_PREPROCESSOR_ID, first)


class UnregisterUserAgentTest(TransportCase):
    """``plugin.unload()`` calls this, and nothing else covered it.

    A request preprocessor is registered process-wide and outlives the module
    that installed it. Reload strips the plugin from ``sys.modules``, so an
    un-removed hook keeps running on every network request QGIS makes anywhere
    -- for the rest of the session, bound to a module that no longer exists.
    That is the same leak class as a duplicate toolbar icon, except it is in
    the network path. Mutation-checked: making ``unregister_user_agent`` a
    no-op left the rest of the suite green.
    """

    def setUp(self):
        super().setUp()
        # Whatever these do to the process-global hook, leave it registered:
        # that is the state every other test in the file runs in.
        self.addCleanup(net.register_user_agent)

    def test_unregistering_clears_the_id_and_a_second_call_is_harmless(self):
        # unload() can run twice (reload, then close), and the second must not
        # raise into QGIS's plugin manager. Measured: removeRequestPreprocessor
        # raises KeyError for an id it does not know, which is why the module
        # guards on the stored id rather than calling unconditionally.
        net.register_user_agent()
        self.assertTrue(net._USER_AGENT_PREPROCESSOR_ID)
        net.unregister_user_agent()
        self.assertEqual(net._USER_AGENT_PREPROCESSOR_ID, "")
        net.unregister_user_agent()
        self.assertEqual(net._USER_AGENT_PREPROCESSOR_ID, "")

    def test_after_unregistering_the_token_stops_reaching_the_wire(self):
        # Driven through QgsBlockingNetworkRequest directly rather than through
        # fetch(), because fetch() re-registers on every call -- which is what
        # makes the plugin work again after a reload, and what makes this the
        # only way to observe the unregistered state on a real socket.
        with mock.patch.object(net, "ALLOWED_HOST", "127.0.0.1"):
            net.register_user_agent()
            self._raw_get("/before")
            self.assertIn(USER_AGENT, self.server.user_agents[-1])

            net.unregister_user_agent()
            self._raw_get("/after")
            sent = self.server.user_agents[-1]
            self.assertNotIn(USER_AGENT, sent)
            # QGIS's own header is still there: the hook was removed, not the
            # network stack's own behaviour.
            self.assertIn("QGIS/", sent)

    def test_a_reload_can_register_again_after_unloading(self):
        # unload() then a fresh initGui() is the ordinary reload cycle, and the
        # plugin has to identify itself again afterwards.
        net.unregister_user_agent()
        with mock.patch.object(net, "ALLOWED_HOST", "127.0.0.1"):
            net.QgisFetcher().fetch(f"{self.base}/ok")
        self.assertIn(USER_AGENT, self.server.user_agents[-1])

    def _raw_get(self, path: str) -> None:
        blocking = QgsBlockingNetworkRequest()
        blocking.get(QNetworkRequest(QUrl(f"{self.base}{path}")), True)


class EmptyBodyTest(TransportCase):
    """The most dangerous response in the design, from both directions.

    A zero-byte file in the cache is permanent damage: hits never re-fetch, so
    it would be read as valid data forever. The guard therefore has to hold
    whether QGIS reports the empty response as an error or as a success, and
    those two halves cannot both be produced from the wire:

    * Measured on QGIS 4.2.2: a 200 with ``Content-Length: 0`` comes back as
      ``ErrorCode == 3`` (``ServerExceptionError``) with ``errorMessage()``
      ``"empty response: Unknown error"`` -- so the *first* branch runs. A 204
      and a bodyless connection-close behave identically.
    * ``NoError`` with an empty body -- the documented trap -- is reachable
      over a socket only by cancelling mid-flight (see
      :class:`CancellationTest`). The uncancelled version of it is faked here,
      because no server response produces it on this build.
    """

    def test_an_empty_200_raises_rather_than_returning_empty_bytes(self):
        with self.assertRaises(PowerHTTPError) as caught:
            net.QgisFetcher().fetch(f"{self.base}/empty")
        self.assertEqual(caught.exception.url, f"{self.base}/empty")
        self.assertEqual(self.server.requests_seen, ["/empty"])

    def _fake_blocking(self, *, content=b"", reply_error=None):
        """A ``QgsBlockingNetworkRequest`` that reports success over ``content``.

        The wire cannot produce ``NoError`` with an empty body uncancelled, and
        it cannot produce a reply error that QGIS did not also report as an
        ErrorCode -- so both of ``fetch``'s remaining guards need this.
        """
        if reply_error is None:
            reply_error = QNetworkReply.NetworkError.NoError

        class _Reply:
            def attribute(self, _key):
                return 200

            def content(self):
                return content

            def error(self):
                return reply_error

            def errorString(self):  # noqa: N802 - QNetworkReply's spelling
                return "fake transport failure"

        class _Blocking:
            # net.py reads the enum off the class it was handed, so the fake
            # has to carry it too.
            ErrorCode = QgsBlockingNetworkRequest.ErrorCode

            def setAuthCfg(self, _cfg):
                pass

            def get(self, _request, _refresh, _feedback=None):
                return QgsBlockingNetworkRequest.ErrorCode.NoError

            def reply(self):
                return _Reply()

        return mock.patch.object(net, "QgsBlockingNetworkRequest", _Blocking)

    def test_no_error_with_an_empty_body_is_still_refused(self):
        # Faked because the wire cannot do it uncancelled -- see the class
        # docstring. Everything QgisFetcher.fetch touches on the reply is here.
        with self._fake_blocking():
            with self.assertRaises(PowerHTTPError) as caught:
                net.QgisFetcher().fetch(f"{self.base}/ok")
        self.assertEqual(caught.exception.status, 200)
        # Nothing was sent: the fake never opened a socket.
        self.assertEqual(self.server.requests_seen, [])

    def test_a_cancelled_fetch_with_a_clean_reply_is_still_refused(self):
        # The cancel arm of the same guard, and the reason it needs its own
        # test. On the wire a mid-flight cancel trips TWO branches: the
        # empty-body guard fires first, and reply.error() is
        # OperationCanceledError, so the belt-and-braces branch below it would
        # have caught the same response. Each masks the other, so
        # CancellationTest cannot pin either -- mutation-checked, replacing
        # `if not body_bytes:` with `if False:` leaves that test green. Here
        # the reply reports NoError, so only the guard is left to refuse it.
        feedback = QgsFeedback()
        feedback.cancel()
        with self._fake_blocking():
            with self.assertRaises(PowerHTTPError) as caught:
                # The pre-send check would catch a feedback cancelled this
                # early, so it is cancelled after fetch() has looked at it.
                fetcher = net.QgisFetcher()
                fetcher.feedback = _CancelOnRead(feedback)
                fetcher.fetch(f"{self.base}/ok")
        self.assertEqual(caught.exception.status, 200)

    def test_a_reply_error_under_a_clean_error_code_is_still_refused(self):
        # The branch after the guard: a body arrived, QGIS reported NoError,
        # and the reply itself says the transfer failed. Never seen on this
        # build -- QGIS pre-empts with an ErrorCode -- but caching a partial
        # body is the whole failure this module exists to prevent, and without
        # this test the branch is dead code: mutation-checked, replacing it
        # with `if False:` left the rest of test_net green.
        with self._fake_blocking(
            content=b'{"truncat',
            reply_error=QNetworkReply.NetworkError.RemoteHostClosedError,
        ):
            with self.assertRaises(PowerHTTPError) as caught:
                net.QgisFetcher().fetch(f"{self.base}/ok")
        self.assertEqual(caught.exception.status, 200)


class _CancelOnRead:
    """A feedback that passes ``fetch``'s pre-send check and is cancelled after.

    ``fetch()`` refuses an already-cancelled feedback before opening a socket,
    which is the right thing and also makes the *later* cancel branch
    unreachable from a test. This reports False once -- for the pre-send check
    -- and True afterwards, which is exactly what a real mid-flight cancel
    looks like to the two calls ``fetch`` makes.
    """

    def __init__(self, feedback):
        self._feedback = feedback
        self._reads = 0

    def isCanceled(self) -> bool:  # noqa: N802 - QgsFeedback's spelling
        self._reads += 1
        return self._reads > 1


class ErrorResponseTest(TransportCase):
    """HTTP failures, and the two facts the caller needs from each."""

    def test_a_422_carries_the_status_and_powers_own_message(self):
        with self.assertRaises(PowerHTTPError) as caught:
            net.QgisFetcher().fetch(f"{self.base}/422")

        exc = caught.exception
        # The status lives on a reply attribute, not on the QGIS error code: a
        # 422 arrives as ServerExceptionError, so reading the code alone would
        # turn every API validation failure into a generic "server error".
        self.assertEqual(exc.status, 422)

        message = json.loads(load_bytes("error_422_bbox.json"))["messages"][0]
        rendered = str(exc)
        self.assertIn(message, rendered)
        # Without the URL beside it, "provide at least a 2 degree range" cannot
        # be acted on: the user cannot tell which of six tiles was malformed.
        self.assertIn(f"{self.base}/422", rendered)

    def test_a_422_is_not_worth_retrying(self):
        with self.assertRaises(PowerHTTPError) as caught:
            net.QgisFetcher().fetch(f"{self.base}/422")
        self.assertFalse(caught.exception.is_retryable)

    def test_a_500_is_retryable(self):
        with self.assertRaises(PowerHTTPError) as caught:
            net.QgisFetcher().fetch(f"{self.base}/500")
        self.assertEqual(caught.exception.status, 500)
        self.assertTrue(caught.exception.is_retryable)

    def test_a_stalled_transfer_times_out_instead_of_hanging_the_task(self):
        # A fetch with no timeout hangs a QgsTask forever, and the task manager
        # has no way to reap it. 0.5 s against a 2 s stall.
        started = time.monotonic()
        with self.assertRaises(PowerHTTPError):
            net.QgisFetcher().fetch(f"{self.base}/slow", timeout=0.5)
        self.assertLess(time.monotonic() - started, SLOW_SECONDS)


class CancellationTest(TransportCase):
    """Cancelling must never produce bytes -- especially not zero of them."""

    def test_an_already_cancelled_feedback_stops_before_the_socket_opens(self):
        feedback = QgsFeedback()
        feedback.cancel()

        with self.assertRaises(PowerHTTPError) as caught:
            net.QgisFetcher(feedback).fetch(f"{self.base}/ok")

        # Status 0: there is no HTTP status because there was no HTTP.
        self.assertEqual(caught.exception.status, 0)
        self.assertEqual(self.server.requests_seen, [])

    def test_a_cancel_mid_request_raises_rather_than_returning_an_empty_body(self):
        # This is the trap itself. Measured: QgsBlockingNetworkRequest returns
        # ErrorCode.NoError with an empty reply here, and
        # HttpStatusCodeAttribute is already 200 -- so without a guard the
        # caller would cache a zero-byte file under a name that says the fetch
        # succeeded. What this test proves is the end-to-end property: a cancel
        # never yields bytes. It does NOT pin either individual guard, because
        # reply.error() is OperationCanceledError here so both of them catch
        # this response and each masks the other -- see
        # EmptyBodyTest.test_a_cancelled_fetch_with_a_clean_reply_is_still_refused.
        feedback = QgsFeedback()
        canceller = threading.Timer(0.4, feedback.cancel)
        canceller.start()
        self.addCleanup(canceller.cancel)

        with self.assertRaises(PowerHTTPError):
            net.QgisFetcher(feedback).fetch(f"{self.base}/slow")

        self.assertEqual(self.server.requests_seen, ["/slow"])
        self.assertTrue(feedback.isCanceled())


class CheckUrlIsEnforcedTest(LocalServerCase):
    """No patching here: the allow-list must stop these before any socket."""

    def test_plain_http_to_the_local_server_is_refused(self):
        with self.assertRaises(PowerValidationError):
            net.QgisFetcher().fetch(f"{self.base}/ok")
        self.assertEqual(self.server.requests_seen, [])

    def test_another_host_over_https_is_refused(self):
        with self.assertRaises(PowerValidationError):
            net.QgisFetcher().fetch("https://example.com/api/temporal/daily/point")
        self.assertEqual(self.server.requests_seen, [])

    def test_the_check_runs_before_the_cancellation_check(self):
        # Order matters only in that a bad URL must raise the *validation*
        # error, not be masked by a cancelled feedback's PowerHTTPError.
        feedback = QgsFeedback()
        feedback.cancel()
        with self.assertRaises(PowerValidationError):
            net.QgisFetcher(feedback).fetch(f"{self.base}/ok")


class AuthCfgTest(LocalServerCase):
    """The auth configuration has to reach the request, not just the fetcher.

    POWER itself needs no credentials; this exists for users who can only
    reach it through an authenticating proxy, and for them a silently
    unapplied ``authcfg`` is a fetch that fails with a 407 they cannot
    explain. A real ``setAuthCfg`` needs a QGIS auth database and a master
    password, so what is checked here is the wiring: the id the fetcher was
    built with is the id handed to ``QgsBlockingNetworkRequest``, and an empty
    one is not handed over at all.
    """

    def setUp(self):
        super().setUp()
        self.auth_cfgs: list[str] = []
        recorder = self.auth_cfgs

        class _Blocking:
            ErrorCode = QgsBlockingNetworkRequest.ErrorCode

            def setAuthCfg(self, cfg):  # noqa: N802 - QGIS's spelling
                recorder.append(cfg)

            def get(self, _request, _refresh, _feedback=None):
                return QgsBlockingNetworkRequest.ErrorCode.NoError

            def reply(self):
                return _StubReply(b'{"ok": true}')

        for target, replacement in (
            ("QgsBlockingNetworkRequest", _Blocking),
            ("check_url", lambda _url: None),
        ):
            patcher = mock.patch.object(net, target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_the_configured_id_is_applied_to_the_request(self):
        net.QgisFetcher(auth_cfg="power_proxy_cfg").fetch(f"{self.base}/ok")
        self.assertEqual(self.auth_cfgs, ["power_proxy_cfg"])

    def test_no_id_means_setAuthCfg_is_not_called(self):  # noqa: N802
        # Passing "" would ask QGIS to look up a configuration that does not
        # exist, which prompts for the master password on a session that never
        # needed one.
        net.QgisFetcher().fetch(f"{self.base}/ok")
        self.assertEqual(self.auth_cfgs, [])


class _StubReply:
    """The four things ``QgisFetcher.fetch`` reads off a reply."""

    def __init__(self, content: bytes) -> None:
        self._content = content

    def attribute(self, _key):
        return 200

    def content(self):
        return self._content

    def error(self):
        return QNetworkReply.NetworkError.NoError

    def errorString(self):  # noqa: N802 - QNetworkReply's spelling
        return ""


class DescribeProxyTest(QgisTestCase):
    """Diagnostics for "is the request even leaving the building?"."""

    def _line_for(self, host: str, port: int) -> str:
        """``describe_proxy()`` as it reads for a proxy of ``host``/``port``."""
        proxy = QNetworkProxy()
        proxy.setHostName(host)
        proxy.setPort(port)
        manager = mock.Mock()
        manager.proxy.return_value = proxy
        with mock.patch("qgis.core.QgsNetworkAccessManager.instance", return_value=manager):
            return net.describe_proxy()

    def test_it_returns_a_line_and_never_raises(self):
        line = net.describe_proxy()
        self.assertIsInstance(line, str)
        self.assertTrue(line)

    def test_a_configured_proxy_is_named_with_its_port(self):
        # Host and port are the whole content of this diagnostic: "there is a
        # proxy" is not actionable, "squid.example.org:3128" is.
        line = self._line_for("squid.example.org", 3128)
        self.assertIn("squid.example.org", line)
        self.assertIn("3128", line)

    def test_no_proxy_is_not_reported_as_a_proxy(self):
        # An unset QNetworkProxy has hostName() == "" and port() == 0, so a
        # branchless version of this renders the nonexistent proxy ":0" and
        # sends the user hunting for a machine that is not there. Measured:
        # this process has no proxy configured, so the same line is what the
        # unpatched call above returns.
        line = self._line_for("", 0)
        self.assertNotIn(":0", line)
        self.assertNotEqual(line, self._line_for("squid.example.org", 3128))

    def test_a_broken_network_manager_still_produces_a_line(self):
        # A diagnostic that raises is worse than useless: it hides the failure
        # it was added to explain.
        with mock.patch("qgis.core.QgsNetworkAccessManager.instance", side_effect=RuntimeError):
            self.assertTrue(net.describe_proxy())


if __name__ == "__main__":
    import unittest

    unittest.main()
