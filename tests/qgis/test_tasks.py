"""What the background fetch machinery must do, with no socket anywhere.

The task manager is deliberately absent. Adding a ``QgsTask`` to
``QgsApplication.taskManager()`` needs a running event loop and hands control
to a thread pool, so a headless test of it would be a test of QGIS's scheduler
rather than of this code. Instead ``run()`` is called directly -- it is a plain
method, and everything it is allowed to touch (no layers, no widgets, no
``QgsProject``) is exactly what makes that legitimate.

The one thing calling ``run()`` directly cannot check is that
``addSubTask()`` precedes ``addTask()``, so :class:`BuildSubtasksTest` checks
what it can: that ``build_subtasks()`` produces the chunks, that each is
attached with ``ParentDependsOnSubTask``, and that cancelling the parent
reaches every child's feedback.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from unittest import mock

from qgis.core import QgsTask

from nasa_power.core.api import (
    INTER_REQUEST_DELAY,
    PowerRequest,
    build_power_url,
    cache_path,
)
from nasa_power.core.errors import PowerHTTPError, PowerValidationError
from nasa_power.core.fetcher import DEFAULT_TIMEOUT, StubFetcher
from nasa_power.qgis_bridge import tasks
from nasa_power.qgis_bridge.tasks import (
    FetchJob,
    FetchOutcome,
    PowerChunkTask,
    PowerFetchTask,
    split,
)
from tests.qgis.qgis_case import QgisTestCase


def make_request(index: int) -> PowerRequest:
    """A distinct, API-legal point request. Distinct URL, so distinct cache path."""
    latitude = 40.0 + index / 10.0
    url = build_power_url(
        "daily",
        "point",
        ["T2M"],
        start="2024-02-01",
        end="2024-02-03",
        latitude=latitude,
        longitude=-105.27,
    )
    return PowerRequest(
        url=url,
        temporal="daily",
        mode="point",
        params=("T2M",),
        community="RE",
        start="20240201",
        end="20240203",
        site=f"site{index}",
        latitude=latitude,
        longitude=-105.27,
    )


class ScriptedFetcher(StubFetcher):
    """A :class:`StubFetcher` that can also be told to fail for one URL.

    Inherits the "unexpected URL raises AssertionError" contract, which is what
    makes "a cache hit issued no request" an assertion rather than a hope.
    """

    def __init__(self, responses=None, failures=None) -> None:
        super().__init__(responses)
        self.failures = dict(failures or {})

    def fetch(self, url: str, timeout: float = DEFAULT_TIMEOUT) -> bytes:
        if url in self.failures:
            self.calls.append(url)
            raise self.failures[url]
        return super().fetch(url, timeout)


class SplitTest(QgisTestCase):
    """``split`` decides how the work is dealt out. Empty chunks would be tasks
    that fetch nothing, and a lost item would be a tile missing from the map."""

    def test_it_deals_round_robin_not_in_slices(self):
        # Round-robin because tiles differ in size -- an edge tile of a bbox is
        # smaller than an interior one -- so contiguous slices land unevenly.
        items = [make_request(i) for i in range(5)]
        self.assertEqual(
            split(items, 2),
            [[items[0], items[2], items[4]], [items[1], items[3]]],
        )

    def test_order_within_a_chunk_is_preserved(self):
        items = [make_request(i) for i in range(9)]
        for chunk in split(items, 4):
            self.assertEqual(chunk, sorted(chunk, key=items.index))

    def test_every_item_appears_exactly_once(self):
        items = [make_request(i) for i in range(11)]
        for parts in (1, 2, 3, 5, 11, 20):
            with self.subTest(parts=parts):
                dealt = [item for chunk in split(items, parts) for item in chunk]
                self.assertEqual(sorted(dealt, key=items.index), items)
                self.assertEqual(len(dealt), len(items))

    def test_more_parts_than_items_gives_one_chunk_per_item(self):
        items = [make_request(i) for i in range(3)]
        chunks = split(items, 10)
        self.assertEqual(len(chunks), 3)
        self.assertEqual([len(c) for c in chunks], [1, 1, 1])

    def test_no_chunk_is_ever_empty(self):
        items = [make_request(i) for i in range(4)]
        for parts in range(1, 12):
            with self.subTest(parts=parts):
                self.assertTrue(all(chunks for chunks in split(items, parts)))

    def test_a_nonsense_part_count_still_yields_one_chunk(self):
        items = [make_request(i) for i in range(3)]
        for parts in (0, -1):
            with self.subTest(parts=parts):
                self.assertEqual(split(items, parts), [items])

    def test_no_items_means_no_chunks(self):
        # Not one empty chunk: a task with nothing to fetch would still be
        # scheduled, and would report a division by zero in setProgress.
        self.assertEqual(split([], 4), [])


class FetchJobAccountingTest(QgisTestCase):
    """What the completion callback reports, over a mixed set of outcomes."""

    def setUp(self):
        super().setUp()
        self.requests = [make_request(i) for i in range(4)]
        self.job = FetchJob(self.requests, Path("/nonexistent"))
        self.job.outcomes = [
            FetchOutcome(self.requests[0], Path("/c/a.json"), was_cached=True),
            FetchOutcome(self.requests[1], Path("/c/b.json"), was_cached=False),
            FetchOutcome(self.requests[2], Path("/c/c.json"), was_cached=True),
            FetchOutcome(self.requests[3], error="POWER API returned HTTP 422"),
        ]

    def test_cached_count_counts_only_the_hits(self):
        self.assertEqual(self.job.cached_count, 2)

    def test_failures_are_the_outcomes_with_no_path(self):
        self.assertEqual(self.job.failures, [self.job.outcomes[3]])

    def test_an_outcome_is_ok_exactly_when_it_has_a_path(self):
        self.assertTrue(FetchOutcome(self.requests[0], Path("/c/a.json")).ok)
        self.assertFalse(FetchOutcome(self.requests[0]).ok)

    def test_an_empty_job_reports_nothing_rather_than_failing(self):
        # The all-cached, nothing-to-do case -- an empty failure list is
        # exactly the falsy return that rules out QgsTask.fromFunction.
        empty = FetchJob([], Path("/nonexistent"))
        self.assertEqual(empty.cached_count, 0)
        self.assertEqual(empty.failures, [])


class ChunkTaskCase(QgisTestCase):
    """Shared wiring: a temp cache directory and a stubbed-out fetcher."""

    def setUp(self):
        super().setUp()
        self.cache_dir = Path(tempfile.mkdtemp(prefix="power-tasks-"))
        self.addCleanup(self._remove_cache_dir)
        self.feedbacks: list[object] = []

    def _remove_cache_dir(self) -> None:
        import shutil

        shutil.rmtree(self.cache_dir, ignore_errors=True)

    def patch_fetcher(self, fetcher) -> None:
        """Make ``PowerChunkTask.run`` build ``fetcher`` instead of a real one.

        The task constructs its own ``QgisFetcher`` -- deliberately, so each
        one carries its own feedback -- so the class is what has to be
        replaced. The feedback it is handed is recorded, because a shared one
        would let a cancel on any task abort every other task's request.
        """

        def factory(feedback=None, auth_cfg=""):
            self.feedbacks.append(feedback)
            return fetcher

        patcher = mock.patch.object(tasks, "QgisFetcher", factory)
        patcher.start()
        self.addCleanup(patcher.stop)


class ChunkTaskRunTest(ChunkTaskCase):
    """``run()`` on a worker thread: what reaches the job, and what does not."""

    def test_a_successful_chunk_populates_the_job_and_finishes_the_progress(self):
        requests = [make_request(0), make_request(1)]
        stub = ScriptedFetcher({r.url: b'{"ok": true}' for r in requests})
        self.patch_fetcher(stub)

        job = FetchJob(requests, self.cache_dir)
        task = PowerChunkTask("chunk", job, requests)
        self.assertTrue(task.run())

        self.assertEqual(len(job.outcomes), 2)
        self.assertTrue(all(o.ok for o in job.outcomes))
        self.assertEqual(job.cached_count, 0)
        self.assertEqual(task.progress(), 100.0)
        for outcome in job.outcomes:
            self.assertEqual(outcome.path.read_bytes(), b'{"ok": true}')

    def test_the_fetcher_is_given_this_task_s_own_feedback(self):
        requests = [make_request(0)]
        self.patch_fetcher(ScriptedFetcher({requests[0].url: b"{}"}))

        task = PowerChunkTask("chunk", FetchJob(requests, self.cache_dir), requests)
        task.run()
        self.assertEqual(self.feedbacks, [task.feedback])

    def test_a_cache_hit_issues_no_request_at_all(self):
        # The stub raises on any URL it was not given, so an empty stub turns
        # "a hit never re-fetches" into an assertion. That is a correctness
        # property, not a speed one: POWER may block a client that keeps asking
        # for the same location.
        request = make_request(0)
        cached = cache_path(self.cache_dir, request)
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(b'{"cached": true}')

        stub = ScriptedFetcher()
        self.patch_fetcher(stub)

        job = FetchJob([request], self.cache_dir)
        self.assertTrue(PowerChunkTask("chunk", job, [request]).run())

        self.assertEqual(stub.calls, [])
        self.assertEqual(job.cached_count, 1)
        self.assertEqual(job.outcomes[0].path, cached)

    def test_one_failed_request_does_not_lose_the_rest_of_the_chunk(self):
        requests = [make_request(i) for i in range(3)]
        stub = ScriptedFetcher(
            responses={requests[0].url: b"{}", requests[2].url: b"{}"},
            # 422, not 500: a validation failure is not retryable, so this
            # fails once rather than sleeping through three attempts.
            failures={requests[1].url: PowerHTTPError(422, "{}", requests[1].url)},
        )
        self.patch_fetcher(stub)

        job = FetchJob(requests, self.cache_dir)
        self.assertTrue(PowerChunkTask("chunk", job, requests).run())

        self.assertEqual([o.ok for o in job.outcomes], [True, False, True])
        self.assertEqual(job.failures[0].request, requests[1])
        self.assertIn("422", job.failures[0].error)
        # The third URL was still requested: the loop did not abort.
        self.assertEqual(stub.calls, [r.url for r in requests])

    def test_a_validation_failure_is_recorded_rather_than_raised(self):
        # PowerValidationError never reaches the wire, but it can still come
        # out of fetch_to_cache -- and an exception escaping run() on a worker
        # thread loses every outcome the chunk had already collected.
        request = make_request(0)
        stub = ScriptedFetcher(
            failures={request.url: PowerValidationError("refused before sending")}
        )
        self.patch_fetcher(stub)

        job = FetchJob([request], self.cache_dir)
        self.assertTrue(PowerChunkTask("chunk", job, [request]).run())
        self.assertEqual(len(job.failures), 1)

    def test_a_failed_request_writes_nothing_to_the_cache(self):
        # A zero-byte or partial file would be read as valid data forever,
        # because a hit never re-fetches.
        request = make_request(0)
        stub = ScriptedFetcher(failures={request.url: PowerHTTPError(422, "{}", request.url)})
        self.patch_fetcher(stub)

        PowerChunkTask("chunk", FetchJob([request], self.cache_dir), [request]).run()
        self.assertFalse(cache_path(self.cache_dir, request).exists())
        self.assertEqual(list(self.cache_dir.rglob("*.partial")), [])

    def test_outcomes_reach_the_job_only_once_the_chunk_is_done(self):
        # The main thread reads job.outcomes in finished(); a list appended to
        # from a worker thread mid-run is what that ordering avoids.
        requests = [make_request(0), make_request(1)]
        job = FetchJob(requests, self.cache_dir)
        task = PowerChunkTask("chunk", job, requests)

        seen: list[int] = []

        class Watching(ScriptedFetcher):
            def fetch(self, url, timeout=DEFAULT_TIMEOUT):
                seen.append(len(job.outcomes))
                return super().fetch(url, timeout)

        self.patch_fetcher(Watching({r.url: b"{}" for r in requests}))
        task.run()

        self.assertEqual(seen, [0, 0])
        self.assertEqual(len(job.outcomes), 2)


class PolitenessGapTest(ChunkTaskCase):
    """When the delay between requests is spent, and when it is not.

    POWER publishes no rate limit but its docs give repeated identical
    requests as a reason to block a client, so the gap has to be there. It
    also has to be *absent* where it buys nothing: a cache hit issued no
    request, so sleeping after one is pure dead time on the worker thread with
    nothing on the other end to be polite to. On a 200-tile all-cached re-open
    that is 50 s of a progress bar crawling for no reason.

    ``_sleep`` is replaced by a recorder rather than timed, so the assertion is
    on the number of gaps rather than on a wall clock a loaded machine can
    move.
    """

    def _gaps(self, task: PowerChunkTask) -> list[float]:
        """Run ``task`` with its sleeper recorded instead of executed."""
        recorded: list[float] = []
        # fetch_to_cache is handed `self._sleep` too, so a retry backoff would
        # land here as well -- the stub never fails, so every entry is a gap.
        task._sleep = recorded.append
        task.run()
        return recorded

    def test_a_gap_is_spent_between_fetched_requests(self):
        requests = [make_request(i) for i in range(3)]
        self.patch_fetcher(ScriptedFetcher({r.url: b"{}" for r in requests}))

        job = FetchJob(requests, self.cache_dir)
        gaps = self._gaps(PowerChunkTask("chunk", job, requests))

        # Three requests, two gaps: between 1-2 and 2-3, not after the third.
        self.assertEqual(gaps, [INTER_REQUEST_DELAY, INTER_REQUEST_DELAY])

    def test_no_gap_follows_the_last_request(self):
        requests = [make_request(0)]
        self.patch_fetcher(ScriptedFetcher({requests[0].url: b"{}"}))

        job = FetchJob(requests, self.cache_dir)
        self.assertEqual(self._gaps(PowerChunkTask("chunk", job, requests)), [])

    def test_a_wholly_cached_chunk_never_sleeps(self):
        # The re-open case: every tile is already on disk, so no request left
        # the machine and there is nothing to be polite about.
        requests = [make_request(i) for i in range(3)]
        for request in requests:
            cached = cache_path(self.cache_dir, request)
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(b'{"cached": true}')

        stub = ScriptedFetcher()  # raises on any URL, so a hit is provable
        self.patch_fetcher(stub)

        job = FetchJob(requests, self.cache_dir)
        gaps = self._gaps(PowerChunkTask("chunk", job, requests))

        self.assertEqual(stub.calls, [])
        self.assertEqual(job.cached_count, 3)
        self.assertEqual(gaps, [])

    def test_a_hit_between_two_misses_costs_no_gap_of_its_own(self):
        # Mixed is the ordinary case for a re-run after a partial failure.
        # Only the two requests that actually went out earn a gap.
        requests = [make_request(i) for i in range(3)]
        hit = cache_path(self.cache_dir, requests[1])
        hit.parent.mkdir(parents=True, exist_ok=True)
        hit.write_bytes(b'{"cached": true}')

        self.patch_fetcher(
            ScriptedFetcher({requests[0].url: b"{}", requests[2].url: b"{}"})
        )
        job = FetchJob(requests, self.cache_dir)
        gaps = self._gaps(PowerChunkTask("chunk", job, requests))

        # Request 1 fetched -> a gap; request 2 was a hit -> none; request 3
        # is last -> none.
        self.assertEqual(job.cached_count, 1)
        self.assertEqual(gaps, [INTER_REQUEST_DELAY])


class ChunkTaskCancellationTest(ChunkTaskCase):
    """Cancel must stop the work, and must reach the fetcher's feedback."""

    def test_cancelling_before_run_fetches_nothing_and_returns_false(self):
        requests = [make_request(i) for i in range(3)]
        stub = ScriptedFetcher({r.url: b"{}" for r in requests})
        self.patch_fetcher(stub)

        job = FetchJob(requests, self.cache_dir)
        task = PowerChunkTask("chunk", job, requests)
        task.cancel()

        self.assertFalse(task.run())
        self.assertEqual(stub.calls, [])
        self.assertEqual(job.outcomes, [])

    def test_cancel_reaches_the_feedback_the_fetcher_holds(self):
        # QgsTask.cancel() alone only sets the task's own flag; the in-flight
        # QgsBlockingNetworkRequest is watching the feedback, so cancelling one
        # without the other leaves the request running to completion.
        task = PowerChunkTask("chunk", FetchJob([], self.cache_dir), [])
        self.assertFalse(task.feedback.isCanceled())
        task.cancel()
        self.assertTrue(task.feedback.isCanceled())
        self.assertTrue(task.isCanceled())

    def test_a_chunk_can_always_be_cancelled(self):
        # Weak by construction, and left in as documentation rather than as a
        # guard: measured on this build, QgsTask's default flags already grant
        # CanCancel, so dropping the explicit flag from the constructor leaves
        # this green. What it does pin is that nothing has removed the flag.
        task = PowerChunkTask("chunk", FetchJob([], self.cache_dir), [])
        self.assertTrue(task.canCancel())

    def test_the_politeness_gap_is_short_enough_to_stay_responsive(self):
        # _sleep() polls isCanceled() every 50 ms, so the gap must be a small
        # multiple of that or Cancel feels dead. This pins the constant only;
        # the polling itself is the next two tests, because asserting on
        # INTER_REQUEST_DELAY alone never executes _sleep at all.
        self.assertLessEqual(INTER_REQUEST_DELAY, 1.0)
        self.assertGreater(INTER_REQUEST_DELAY, 0)

    def test_a_cancelled_sleep_returns_long_before_its_deadline(self):
        # _sleep is what fetch_to_cache is handed for both the politeness gap
        # and the retry backoff, and a retry backoff can be seconds long. A
        # plain time.sleep() there would leave Cancel visibly dead for that
        # whole window. Mutation-checked: dropping the isCanceled() term from
        # the loop left every other test in this file green.
        task = PowerChunkTask("chunk", FetchJob([], self.cache_dir), [])
        task.cancel()

        started = time.monotonic()
        task._sleep(5.0)
        self.assertLess(time.monotonic() - started, 0.5)

    def test_an_uncancelled_sleep_actually_waits(self):
        # The other half: a _sleep that returned immediately would turn the
        # politeness gap into no gap at all, which is the behaviour POWER's
        # docs give as a reason to block a client.
        task = PowerChunkTask("chunk", FetchJob([], self.cache_dir), [])

        started = time.monotonic()
        task._sleep(0.3)
        self.assertGreaterEqual(time.monotonic() - started, 0.25)

    def test_a_cancel_during_the_last_request_still_reports_failure(self):
        # The loop's own cancel check is at the TOP of each iteration, so a
        # cancel during the final request is never seen by it -- the chunk
        # falls out of the loop and only the closing `return not
        # self.isCanceled()` is left to report it. A `return True` there would
        # tell the task manager a cancelled chunk succeeded, and the parent's
        # completion handler would build a layer from a partial fetch.
        requests = [make_request(0)]
        job = FetchJob(requests, self.cache_dir)
        task = PowerChunkTask("chunk", job, requests)

        class CancelDuringFetch(ScriptedFetcher):
            def fetch(self, url, timeout=DEFAULT_TIMEOUT):
                task.cancel()
                return super().fetch(url, timeout)

        self.patch_fetcher(CancelDuringFetch({requests[0].url: b"{}"}))

        self.assertFalse(task.run())
        # The bytes it did get are still cached and still accounted for: the
        # cancel is reported, not pretended away.
        self.assertTrue(cache_path(self.cache_dir, requests[0]).exists())

    def test_a_cancel_between_requests_stops_the_chunk_where_it_is(self):
        # Cancel is checked at the top of each request, so a chunk stops after
        # the one already in flight rather than mid-write. Outcomes are
        # published only for a completed slice, so a cancelled chunk reports
        # none of them -- the bytes it did fetch are in the cache and a re-run
        # picks them up as hits, which is why discarding the records is safe.
        requests = [make_request(i) for i in range(3)]
        job = FetchJob(requests, self.cache_dir)
        task = PowerChunkTask("chunk", job, requests)

        class CancelAfterFirst(ScriptedFetcher):
            def fetch(self, url, timeout=DEFAULT_TIMEOUT):
                body = super().fetch(url, timeout)
                task.cancel()
                return body

        stub = CancelAfterFirst({r.url: b"{}" for r in requests})
        self.patch_fetcher(stub)

        self.assertFalse(task.run())
        # One request went out; the loop refused to start the second.
        self.assertEqual(stub.calls, [requests[0].url])
        self.assertEqual(job.outcomes, [])
        # And what it did fetch is on disk, so nothing was wasted.
        self.assertTrue(cache_path(self.cache_dir, requests[0]).exists())


class BuildSubtasksTest(QgisTestCase):
    """The parent task. ``addSubTask()`` must precede ``addTask()``, so the
    chunks are built by an explicit call the caller makes first."""

    def setUp(self):
        super().setUp()
        self.requests = [make_request(i) for i in range(7)]

    def test_it_creates_one_subtask_per_chunk(self):
        job = FetchJob(self.requests, Path("/nonexistent"), concurrency=3)
        parent = PowerFetchTask("fetch", job)
        parent.build_subtasks()
        self.assertEqual(len(parent._chunks), 3)

    def test_concurrency_above_the_request_count_does_not_make_idle_tasks(self):
        job = FetchJob(self.requests[:2], Path("/nonexistent"), concurrency=8)
        parent = PowerFetchTask("fetch", job)
        parent.build_subtasks()
        self.assertEqual(len(parent._chunks), 2)

    def test_every_request_is_assigned_to_exactly_one_subtask(self):
        job = FetchJob(self.requests, Path("/nonexistent"), concurrency=4)
        parent = PowerFetchTask("fetch", job)
        parent.build_subtasks()

        assigned = [r for chunk in parent._chunks for r in chunk.requests]
        self.assertEqual(sorted(assigned, key=self.requests.index), self.requests)

    def test_no_requests_means_no_subtasks(self):
        parent = PowerFetchTask("fetch", FetchJob([], Path("/nonexistent")))
        parent.build_subtasks()
        self.assertEqual(parent._chunks, [])

    def test_every_subtask_gets_its_own_feedback(self):
        # A shared feedback would make a cancel on one chunk abort another
        # chunk's in-flight request.
        job = FetchJob(self.requests, Path("/nonexistent"), concurrency=3)
        parent = PowerFetchTask("fetch", job)
        parent.build_subtasks()

        feedbacks = [chunk.feedback for chunk in parent._chunks]
        self.assertEqual(len({id(f) for f in feedbacks}), 3)

    def test_cancelling_the_parent_cancels_every_child_s_feedback(self):
        # This asserts the end property, and the end property has two possible
        # causes. Measured on this build: QgsTask::cancel() already walks its
        # subtasks and the walk dispatches to PowerChunkTask.cancel(), so this
        # stays green with PowerFetchTask.cancel()'s explicit loop deleted --
        # mutation-checked. The loop is kept because relying on that
        # propagation would make cancellation depend on an implementation
        # detail QGIS does not document; this test cannot tell the two apart,
        # and says so rather than claiming the loop is what it proves.
        job = FetchJob(self.requests, Path("/nonexistent"), concurrency=3)
        parent = PowerFetchTask("fetch", job)
        parent.build_subtasks()
        parent.cancel()

        self.assertTrue(parent.isCanceled())
        for chunk in parent._chunks:
            self.assertTrue(chunk.feedback.isCanceled())
            self.assertTrue(chunk.isCanceled())

    def test_subtasks_are_attached_so_the_parent_waits_for_them(self):
        # ParentDependsOnSubTask, never SubTaskIndependent: the parent
        # aggregates in finished(), so if it may complete before its chunks do,
        # the completion handler reads job.outcomes while chunks are still
        # appending to it and builds a layer from part of the data.
        # QgsTask exposes no way to read a subtask relationship back, so the
        # call itself is what is observed.
        job = FetchJob(self.requests, Path("/nonexistent"), concurrency=3)
        parent = PowerFetchTask("fetch", job)

        seen: list[tuple] = []
        with mock.patch.object(
            PowerFetchTask,
            "addSubTask",
            lambda _self, task, deps, dependency: seen.append((task, deps, dependency)),
        ):
            parent.build_subtasks()

        self.assertEqual(len(seen), 3)
        for task, deps, dependency in seen:
            self.assertIsInstance(task, PowerChunkTask)
            self.assertEqual(deps, [])
            self.assertEqual(
                dependency, QgsTask.SubTaskDependency.ParentDependsOnSubTask
            )

    def test_the_parent_does_no_fetching_of_its_own(self):
        job = FetchJob(self.requests, Path("/nonexistent"), concurrency=3)
        parent = PowerFetchTask("fetch", job)
        parent.build_subtasks()
        # No fetcher is patched, so a parent that fetched would reach a real
        # QgisFetcher and a refused URL.
        self.assertTrue(parent.run())
        self.assertEqual(job.outcomes, [])


class FinishedTest(QgisTestCase):
    """``finished()`` is the only place that runs on the main thread."""

    def setUp(self):
        super().setUp()
        self.job = FetchJob([make_request(0)], Path("/nonexistent"))
        self.parent = PowerFetchTask("fetch", self.job)
        self.calls: list[tuple] = []

    def test_it_hands_the_job_and_the_cancelled_flag_to_on_complete(self):
        self.parent.on_complete = lambda job, cancelled: self.calls.append((job, cancelled))
        self.parent.finished(True)
        self.assertEqual(self.calls, [(self.job, False)])

    def test_a_cancelled_run_says_so(self):
        self.parent.on_complete = lambda job, cancelled: self.calls.append((job, cancelled))
        self.parent.cancel()
        self.parent.finished(False)
        self.assertEqual(self.calls, [(self.job, True)])

    def test_no_callback_is_not_an_error(self):
        # The dock assigns on_complete before addTask, but a task built and
        # dropped must not raise into QGIS's task manager.
        self.parent.finished(True)

    def test_the_callback_is_reached_with_an_empty_failure_list(self):
        # The arity trap: QgsTask.fromFunction's `if self.returned_values:`
        # calls back differently when a task returns [] -- which is what an
        # all-cached fetch produces. This subclass must not care.
        self.parent.on_complete = lambda job, cancelled: self.calls.append((job.failures, cancelled))
        self.parent.finished(True)
        self.assertEqual(self.calls, [([], False)])


if __name__ == "__main__":
    import unittest

    unittest.main()
