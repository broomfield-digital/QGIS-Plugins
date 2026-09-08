"""Background fetching, so a tiled request does not freeze the QGIS window.

A continental extent is dozens of HTTP calls. Done on the main thread that is
a minutes-long hang with a spinning cursor; done here it is a progress bar and
a working Cancel button.

Three rules the QGIS task API enforces, each learned the hard way:

* **``addSubTask()`` must be called before ``taskManager().addTask()``.** After
  the manager has the parent, adding a subtask is ignored.
* **Keep a Python reference to the task.** C++ takes ownership, but the Python
  wrapper is separate: if it is garbage collected the bound methods and
  closures go with it.
* **Nothing that lives on the main thread may be touched from ``run()``.** No
  layers, no widgets, no ``QgsProject``. Only ``QgsMessageLog`` (thread-safe)
  and ``QgsBlockingNetworkRequest`` (documented thread-safe). Results cross
  back as plain dataclasses.

Deliberately **not** ``QgsTask.fromFunction``: it is pure Python
(``qgis/core/additions/qgstaskwrapper.py``) and its ``if self.returned_values:``
check calls the completion callback with a different arity when a task returns
``0``, ``[]`` or ``{}`` -- which an all-cached fetch, returning an empty
failure list, does exactly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from qgis.core import Qgis, QgsFeedback, QgsMessageLog, QgsTask

from nasa_power.core.api import INTER_REQUEST_DELAY, PowerRequest, fetch_to_cache
from nasa_power.core.errors import PowerError, PowerHTTPError
from nasa_power.qgis_bridge.net import QgisFetcher

LOG_TAG = "NASA POWER"


@dataclass
class FetchOutcome:
    """What became of one request. Crosses the thread boundary as plain data."""

    request: PowerRequest
    path: Path | None = None
    was_cached: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.path is not None


@dataclass
class FetchJob:
    """Everything a fetch needs, assembled on the main thread before it starts."""

    requests: Sequence[PowerRequest]
    cache_dir: Path
    force: bool = False
    #: Concurrency. POWER publishes no rate limit but asks not to be hammered.
    concurrency: int = 4
    #: Set by the tasks as they run; read in ``finished()`` on the main thread.
    outcomes: list[FetchOutcome] = field(default_factory=list)

    @property
    def cached_count(self) -> int:
        return sum(1 for o in self.outcomes if o.was_cached)

    @property
    def failures(self) -> list[FetchOutcome]:
        return [o for o in self.outcomes if not o.ok]


def split(items: Sequence[PowerRequest], parts: int) -> list[list[PowerRequest]]:
    """Deal ``items`` round-robin into at most ``parts`` non-empty chunks.

    Round-robin rather than contiguous slices so that when tiles differ in
    size -- an edge tile of a bbox is smaller than an interior one -- the work
    still lands evenly.
    """
    parts = max(1, min(parts, len(items) or 1))
    chunks: list[list[PowerRequest]] = [[] for _ in range(parts)]
    for index, item in enumerate(items):
        chunks[index % parts].append(item)
    return [c for c in chunks if c]


class PowerChunkTask(QgsTask):
    """Fetches one slice of the job's requests, serially, on a worker thread."""

    def __init__(self, name: str, job: FetchJob, requests: Sequence[PowerRequest]) -> None:
        super().__init__(name, QgsTask.Flag.CanCancel)
        self.job = job
        self.requests = list(requests)
        # One feedback per task. A shared one would let a cancel on any task
        # abort every other task's in-flight request.
        self.feedback = QgsFeedback()
        self._outcomes: list[FetchOutcome] = []

    def cancel(self) -> None:
        self.feedback.cancel()
        super().cancel()

    def run(self) -> bool:
        fetcher = QgisFetcher(self.feedback)
        total = len(self.requests)

        for index, request in enumerate(self.requests):
            if self.isCanceled():
                return False
            try:
                path, was_cached = fetch_to_cache(
                    request,
                    self.job.cache_dir,
                    fetcher,
                    force=self.job.force,
                    # Retries happen inside; the delay below is the politeness
                    # gap between distinct requests.
                    sleep=self._sleep,
                )
                self._outcomes.append(FetchOutcome(request, path, was_cached))
                if not was_cached:
                    QgsMessageLog.logMessage(
                        f"Fetched {request.temporal}/{request.mode} "
                        f"{','.join(request.params)} -> {path.name}",
                        LOG_TAG,
                        Qgis.MessageLevel.Info,
                    )
            except PowerHTTPError as exc:
                # One tile failing must not lose the others. Record it and
                # carry on; the report names every failure with its URL.
                self._outcomes.append(FetchOutcome(request, error=str(exc)))
                QgsMessageLog.logMessage(str(exc), LOG_TAG, Qgis.MessageLevel.Warning)
            except (PowerError, OSError) as exc:
                # OSError too: the cache write can fail on a full or read-only
                # disk, and an exception escaping run() on a task-pool thread
                # takes QGIS down rather than failing one tile.
                self._outcomes.append(FetchOutcome(request, error=str(exc)))
                QgsMessageLog.logMessage(str(exc), LOG_TAG, Qgis.MessageLevel.Warning)

            self.setProgress(100.0 * (index + 1) / total)

            # Politeness gap between requests, skipped after the last one and
            # skipped entirely on a cache hit (which issued no request).
            if index + 1 < total and not self._outcomes[-1].was_cached:
                self._sleep(INTER_REQUEST_DELAY)

        # Published only once the slice is done, so the main thread never reads
        # a list being appended to on a worker thread.
        self.job.outcomes.extend(self._outcomes)
        return not self.isCanceled()

    def _sleep(self, seconds: float) -> None:
        """Sleep in short slices so a cancel is noticed promptly."""
        import time

        deadline = seconds
        step = 0.05
        while deadline > 0 and not self.isCanceled():
            time.sleep(min(step, deadline))
            deadline -= step


class PowerFetchTask(QgsTask):
    """Parent task. Owns the job and aggregates its subtasks' results.

    Does no fetching itself -- the subtasks do that. Its ``run()`` exists so
    the manager has something to complete once they are all done, and
    ``finished()`` is where the main thread picks the results up.
    """

    def __init__(self, description: str, job: FetchJob) -> None:
        super().__init__(description, QgsTask.Flag.CanCancel)
        self.job = job
        self._chunks: list[PowerChunkTask] = []
        #: Assigned by the caller before ``addTask``; invoked from
        #: ``finished()`` on the main thread with ``(job, cancelled)``.
        self.on_complete = None

    def build_subtasks(self) -> None:
        """Create and attach the chunk tasks. Call before ``addTask()``."""
        for index, chunk in enumerate(split(self.job.requests, self.job.concurrency), start=1):
            subtask = PowerChunkTask(f"NASA POWER fetch {index}", self.job, chunk)
            self._chunks.append(subtask)
            # ParentDependsOnSubTask: the parent aggregates in finished(), so
            # it must not complete until every chunk has.
            self.addSubTask(subtask, [], QgsTask.SubTaskDependency.ParentDependsOnSubTask)

    def cancel(self) -> None:
        for chunk in self._chunks:
            chunk.cancel()
        super().cancel()

    def run(self) -> bool:
        return not self.isCanceled()

    def finished(self, result: bool) -> None:
        """Runs on the **main thread**. The only place layers may be built."""
        if self.on_complete is not None:
            self.on_complete(self.job, self.isCanceled())
