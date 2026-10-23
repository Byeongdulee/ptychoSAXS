"""Long-running operations, and the rules about interrupting them.

A scan takes minutes. The client cannot block on it, and a second panel
should be able to watch one the first panel started, so every long operation
becomes a job: submit returns an id immediately and progress arrives as
events broadcast to everyone.

Cancellation is cooperative, because that is what the hardware allows. It
mirrors what the GUI already does -- ScanHandler.stopscan sets
isStopScanIssued and the scan executors check it between steps, which is why
the status bar says "Stop requested - finishing current step". A step is the
smallest unit that can be abandoned without leaving the shutter open and the
detectors armed, so stopping is a request, never a kill.

**A fly scan cannot be interrupted at all.** Its trajectory lives in the
C-887's wave generator and runs autonomously once started; there is no flag
for the host to set. So fly jobs are submitted with cancellable=False, and
everything that would otherwise interrupt one -- including the server
shutting down when the last GUI closes -- has to wait it out instead.
"""

import itertools
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from ptychosaxs.api.events import JobFinished, JobProgress, JobStarted

MOVE = "move"
STEP_SCAN = "step_scan"
FLY_SCAN = "fly_scan"

SCAN_KINDS = frozenset({STEP_SCAN, FLY_SCAN})

RUNNING = "running"
DONE = "done"
FAILED = "failed"
ABORTED = "aborted"


class JobAborted(Exception):
    """Raised inside a job body when it observes a stop request."""


@dataclass
class JobInfo:
    job_id: str
    kind: str
    origin: str
    params: dict
    cancellable: bool
    state: str = RUNNING
    started_at: float = field(default_factory=time.time)
    error: str = ""

    def describe(self) -> dict:
        return {"job_id": self.job_id, "kind": self.kind,
                "origin": self.origin, "state": self.state,
                "cancellable": self.cancellable,
                "started_at": self.started_at,
                "elapsed_s": round(time.time() - self.started_at, 1),
                "error": self.error}


class StopToken:
    """Handed to a job body so it can notice it has been asked to stop.

    The body polls `stop_requested` between steps and raises JobAborted (or
    returns) when it is set. Nothing here can force a stop -- see the module
    docstring.
    """

    def __init__(self, job_id, cancellable):
        self.job_id = job_id
        self.cancellable = cancellable
        self._event = threading.Event()

    @property
    def stop_requested(self) -> bool:
        return self._event.is_set()

    def request_stop(self) -> bool:
        if not self.cancellable:
            return False
        self._event.set()
        return True

    def check(self):
        if self._event.is_set():
            raise JobAborted(f"{self.job_id} stopped on request")


class JobHandle:
    """Server-side handle. The client gets the job id and the event stream."""

    def __init__(self, info, token, future):
        self.info = info
        self.token = token
        self.future = future

    @property
    def job_id(self):
        return self.info.job_id

    def result(self, timeout=None):
        return self.future.result(timeout=timeout)


class JobManager:
    """Runs jobs and answers the questions the lifecycle rules need.

    Two executors. Scans get a single worker, because the instrument has one
    set of stages and one detector chain and two concurrent scans is not a
    thing. Moves get a small pool so a jog on one axis is not stuck behind a
    long move on another.
    """

    def __init__(self, bus, log):
        self._bus = bus
        self._log = log
        self._lock = threading.Lock()
        self._jobs = {}
        self._tokens = {}
        self._ids = itertools.count(1)
        self._scans = ThreadPoolExecutor(max_workers=1,
                                         thread_name_prefix="ptys-scan")
        self._moves = ThreadPoolExecutor(max_workers=4,
                                         thread_name_prefix="ptys-move")

    # -- submission ----------------------------------------------------------

    def submit(self, kind, fn, *, origin="", params=None, cancellable=None):
        """Run `fn(token, progress)` on a worker thread.

        `fn` receives a StopToken and a progress callback. A fly scan is
        never cancellable, whatever the caller passes.
        """
        if cancellable is None:
            cancellable = kind != FLY_SCAN
        if kind == FLY_SCAN:
            cancellable = False

        job_id = f"job-{next(self._ids)}"
        info = JobInfo(job_id, kind, origin, dict(params or {}), cancellable)
        token = StopToken(job_id, cancellable)

        with self._lock:
            self._jobs[job_id] = info
            self._tokens[job_id] = token

        self._bus.publish(JobStarted(job_id, kind, origin, info.params,
                                     cancellable))

        pool = self._scans if kind in SCAN_KINDS else self._moves

        def progress(done, total, phase=""):
            self._bus.publish(JobProgress(job_id, int(done), int(total), phase))

        def run():
            try:
                result = fn(token, progress)
            except JobAborted as exc:
                self._finish(info, ABORTED, str(exc))
                return None
            except Exception as exc:
                self._log.exception("job %s (%s) failed", job_id, kind)
                self._finish(info, FAILED, f"{type(exc).__name__}: {exc}")
                raise
            self._finish(info, DONE, "", result if isinstance(result, dict) else {})
            return result

        return JobHandle(info, token, pool.submit(run))

    def _finish(self, info, state, error="", result=None):
        with self._lock:
            info.state = state
            info.error = error
        self._bus.publish(JobFinished(info.job_id, state, error, result or {}))

    # -- queries the lifecycle rules ask -------------------------------------

    def active(self):
        with self._lock:
            return [i for i in self._jobs.values() if i.state == RUNNING]

    def any_active(self) -> bool:
        return bool(self.active())

    def fly_active(self) -> bool:
        return any(i.kind == FLY_SCAN for i in self.active())

    def step_scan_active(self) -> bool:
        return any(i.kind == STEP_SCAN for i in self.active())

    def describe_active(self) -> str:
        act = self.active()
        if not act:
            return "nothing"
        return ", ".join(f"{i.kind} {i.job_id}" for i in act)

    def info(self, job_id):
        with self._lock:
            return self._jobs.get(job_id)

    def snapshot(self):
        with self._lock:
            return [i.describe() for i in self._jobs.values()
                    if i.state == RUNNING]

    # -- stopping ------------------------------------------------------------

    def request_stop(self, job_id=None) -> list:
        """Ask one job, or every cancellable job, to stop.

        Returns the ids actually asked. A fly scan is never in that list, so
        a caller can tell the difference between "stopping" and "you will
        have to wait".
        """
        asked = []
        for handle_id, token in self._live_tokens(job_id):
            if token.request_stop():
                asked.append(handle_id)
        if asked:
            self._log.info("stop requested for %s", ", ".join(asked))
        return asked

    def _live_tokens(self, job_id):
        with self._lock:
            if job_id:
                info = self._jobs.get(job_id)
                ids = [job_id] if info and info.state == RUNNING else []
            else:
                ids = [i.job_id for i in self._jobs.values()
                       if i.state == RUNNING]
            return [(jid, self._tokens[jid]) for jid in ids
                    if jid in self._tokens]

    def wait_idle(self, timeout) -> bool:
        """Block until no job is running, or the deadline passes."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.any_active():
                return True
            time.sleep(0.05)
        return not self.any_active()

    def shutdown(self, wait=True):
        self._scans.shutdown(wait=wait)
        self._moves.shutdown(wait=wait)
