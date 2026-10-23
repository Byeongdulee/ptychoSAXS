"""One instrument, one session object.

This is what replaces gui.rungui.ptyco_main_control as the thing every
control action reaches through. The difference is that the window became a
service locator by accident -- handlers were constructed as Handler(window)
and kept self.w -- whereas this holds only the things that are actually
state. The 894 `self.w.*` references in scan_handler resolve, in order of
frequency, to parameters, detectors, messages, motors, axis names and a
dozen flags. All of those belong here. The ~180 genuine widget reads do not,
and become explicit arguments to the methods that need them.

Built once per server process. Readiness is a state machine rather than a
constructor, because the daemon binds its socket before any of this exists:
a panel that connects during startup is told "building hexapod" instead of
timing out against a socket that is not there yet.
"""

import threading
import time

from ptychosaxs.api.devices import DeviceRegistry
from ptychosaxs.api.events import EventBus, MessagePosted, ServerState
from ptychosaxs.api.jobs import JobManager
from ptychosaxs.api.optics import OpticsService
from ptychosaxs.api.profile import Profile
from ptychosaxs.api.pvs import PVCache
from ptychosaxs.api.scans import ScanService

PHASE_NEW = "new"
PHASE_DEVICES = "building devices"
PHASE_READY = "ready"
PHASE_CLOSING = "closing"
PHASE_FAILED = "failed"


class BeamlineSession:
    """Everything a control action needs, and nothing that draws."""

    def __init__(self, profile: Profile = None, bus: EventBus = None, log=None):
        import logging
        self.log = log or logging.getLogger("ptychosaxs.session")
        self.profile = profile or Profile.from_env()
        self.bus = bus or EventBus(self.log)
        self.started_at = time.time()

        # Before anything can import the device layer. motions_ver2 and
        # interferometers read their flags at module scope, so this has to
        # happen first or the profile is decoration.
        self.profile.apply_to_env(warn=self.log.warning)

        self._lock = threading.Lock()
        self._phase = PHASE_NEW
        self._ready = False

        self.devices = DeviceRegistry(self.profile, self.bus, self.log)
        self.jobs = JobManager(self.bus, self.log)
        self.pvs = PVCache(self.profile, self.log)
        self.optics = OpticsService(self.devices, self.pvs, self.log)
        self.scans = ScanService(self, self.log)

        # Phase 5 moves these off the GUI window and onto the session:
        #   parameters  -- SafeIni over gui/ini/pty-co-saxs.ini (124 refs)
        #   detectors   -- the detector[] inventory and mode (117 refs)
        #   messages    -- the message log behind _ErrorDict (94 refs)
        #   flags       -- isscan / isfly / is_ptychomode / ... 
        # They are not stubbed here: an attribute that exists but does
        # nothing is worse than one that is absent, because it silently
        # accepts writes that go nowhere.

    # -- readiness -----------------------------------------------------------

    @property
    def phase(self):
        with self._lock:
            return self._phase

    @property
    def ready(self):
        with self._lock:
            return self._ready

    def _set_phase(self, phase, ready=None):
        with self._lock:
            self._phase = phase
            if ready is not None:
                self._ready = ready
        self.bus.publish(ServerState(self.ready, phase,
                                     self.profile.simulated))

    # -- lifetime ------------------------------------------------------------

    def start(self):
        """Bring the session up. Never raises: a stage that cannot be built
        falls back to a simulated stand-in, the same contract
        motions_ver2.build_controller already honours."""
        self._set_phase(PHASE_DEVICES)
        self.log.info("session starting: %s", self.profile.summary())
        self._set_phase(PHASE_READY, ready=True)
        self.log.info("session ready")
        return self

    def close(self):
        """Idempotent. Runs from the daemon's signal handler, so it must not
        assume start() completed or that it is called only once."""
        with self._lock:
            if self._phase == PHASE_CLOSING:
                return
        self._set_phase(PHASE_CLOSING, ready=False)
        try:
            self.jobs.shutdown(wait=False)
        except Exception:
            self.log.exception("job manager did not shut down cleanly")
        try:
            self.pvs.close_all()
        except Exception:
            self.log.exception("PV cache did not close cleanly")
        try:
            self.devices.close_all()
        except Exception:
            self.log.exception("devices did not close cleanly")
        self.log.info("session closed")

    # -- client-scoped device lifetime ---------------------------------------

    def acquire_for(self, groups):
        """Open what a connecting client needs."""
        return self.devices.acquire(groups)

    def release_for(self, groups):
        """Give back what a disconnecting client needed."""
        return self.devices.release(groups)

    # -- messages ------------------------------------------------------------

    def post(self, level, text, source=""):
        """What a QMessageBox used to be when it only told you something.

        A message that needs an answer is not this: that is a Guard, asked
        over the ask/ans channel, because the control logic has to wait.
        """
        self.log.log({"error": 40, "warning": 30}.get(level, 20),
                     "%s: %s", source or "session", text)
        self.bus.publish(MessagePosted(level, text, source))

    # -- status --------------------------------------------------------------

    def status(self):
        return {
            "ready": self.ready,
            "phase": self.phase,
            "simulated": self.profile.simulated,
            "profile": self.profile.describe(),
            "uptime_s": round(time.time() - self.started_at, 1),
            "groups": [g.describe() for g in self.devices.status()],
            "jobs": self.jobs.snapshot(),
        }
