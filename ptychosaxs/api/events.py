"""Events the session publishes, and the bus that fans them out.

No Qt. The device layer currently publishes positions through
motions_ver2.motorSignals, a QObject with pyqtSignals, which means a headless
process needs PyQt5 installed to read a motor position. An EventBus with
plain callables does the same job and lets ptychosaxs.client.qt be the single
place where any of this becomes a pyqtSignal.

Conflation matters more than it looks. Positions are published at 10 Hz to
every connected client; a client that stops draining must not accumulate a
backlog of stale snapshots it will render one by one when it recovers. The
publisher replaces a pending same-key event instead of queueing a second one,
which is the same "lose data, never lose time" rule qds_bridge applies to its
sample stream.
"""

import threading
import time
from dataclasses import asdict, dataclass, field


@dataclass(frozen=True, slots=True)
class Event:
    """What actually travels. `name` is dotted; `data` is JSON-ready."""
    name: str
    data: dict
    ts: float = field(default_factory=time.time)


class _Payload:
    """Mixin: every event dataclass converts to an Event the same way."""
    EVENT = "event"
    CONFLATE_KEY = None     # set to a string to make this event conflatable

    def to_event(self) -> Event:
        return Event(self.EVENT, asdict(self))

    @property
    def conflate_key(self):
        return self.CONFLATE_KEY


@dataclass(frozen=True, slots=True)
class ServerState(_Payload):
    """Startup progress, so a client can show why it is waiting.

    The daemon binds its socket before it builds any hardware, so a GUI can
    connect during startup and render "building hexapod" instead of sitting
    on a blind timeout.
    """
    ready: bool
    phase: str
    simulated: bool
    EVENT = "server.state"
    CONFLATE_KEY = "server.state"


@dataclass(frozen=True, slots=True)
class MotionPositions(_Payload):
    positions: dict
    moving: dict
    connected: dict
    EVENT = "motion.positions"
    CONFLATE_KEY = "motion.positions"


@dataclass(frozen=True, slots=True)
class OpticsPositions(_Payload):
    positions: dict
    EVENT = "optics.positions"
    CONFLATE_KEY = "optics.positions"


@dataclass(frozen=True, slots=True)
class OpticsStatus(_Payload):
    """Whether each optics block is in, out, or neither.

    Published less often than positions: classifying re-reads the saved
    definitions and compares every block, and a beamstop does not move
    between one 100 ms tick and the next.
    """
    blocks: dict            # block key -> in | out | between | undefined
    good_for_ptycho: bool
    problems: tuple = ()
    EVENT = "optics.status"
    CONFLATE_KEY = "optics.status"


@dataclass(frozen=True, slots=True)
class PvValues(_Payload):
    """Records the panels bind line edits to.

    One event for the whole subscribed set rather than one per record: a
    panel binds a dozen fields and a dozen events per tick would be twelve
    times the wakeups for the same information.
    """
    values: dict
    EVENT = "pv.values"
    CONFLATE_KEY = "pv.values"


@dataclass(frozen=True, slots=True)
class MessagePosted(_Payload):
    """What a QMessageBox used to be, when it was only telling you something.

    A message that asks a question is not this -- that is a Guard, answered
    over the ask/ans channel, because the control logic has to wait for the
    answer.
    """
    level: str          # "info" | "warning" | "error"
    text: str
    source: str = ""
    EVENT = "message.posted"


@dataclass(frozen=True, slots=True)
class ClientsChanged(_Payload):
    clients: tuple
    EVENT = "clients.changed"
    CONFLATE_KEY = "clients.changed"


@dataclass(frozen=True, slots=True)
class FocusRequested(_Payload):
    """Someone launched a second copy of a GUI that is already open.

    Delivered only to the connection holding that slot, which raises its
    window. The second process prints a line and exits.
    """
    gui_type: str
    EVENT = "client.focus_requested"


@dataclass(frozen=True, slots=True)
class JobStarted(_Payload):
    job_id: str
    kind: str
    origin: str
    params: dict
    cancellable: bool
    EVENT = "job.started"


@dataclass(frozen=True, slots=True)
class JobProgress(_Payload):
    job_id: str
    done: int
    total: int
    phase: str = ""
    EVENT = "job.progress"

    @property
    def conflate_key(self):
        # Per job: two jobs' progress must not displace each other.
        return f"job.progress:{self.job_id}"


@dataclass(frozen=True, slots=True)
class JobFinished(_Payload):
    job_id: str
    state: str          # "done" | "failed" | "aborted"
    error: str = ""
    result: dict = field(default_factory=dict)
    EVENT = "job.finished"


class EventBus:
    """Fan-out that never blocks the publisher.

    Subscribers run on the publishing thread, so a subscriber must do nothing
    but hand the event off -- the server's only subscriber puts it on a
    bounded queue and counts drops. A subscriber that raises is unsubscribed
    rather than allowed to take down the thread that was publishing, because
    that thread is usually in the middle of driving a motor.
    """

    def __init__(self, log=None):
        self._subs = []
        self._lock = threading.Lock()
        self._log = log

    def subscribe(self, fn):
        with self._lock:
            self._subs.append(fn)

        def unsubscribe():
            with self._lock:
                if fn in self._subs:
                    self._subs.remove(fn)
        return unsubscribe

    def publish(self, payload) -> None:
        event = payload.to_event() if hasattr(payload, "to_event") else payload
        with self._lock:
            subs = list(self._subs)
        for fn in subs:
            try:
                fn(event)
            except Exception:
                with self._lock:
                    if fn in self._subs:
                        self._subs.remove(fn)
                if self._log:
                    self._log.exception(
                        "event subscriber failed on %s; unsubscribed",
                        event.name)

    def __len__(self):
        with self._lock:
            return len(self._subs)
