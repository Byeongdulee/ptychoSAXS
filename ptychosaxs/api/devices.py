"""Which hardware is open, who still needs it, and who may touch it.

Two jobs.

**Lifetime.** Hardware is opened when a client that needs it connects and
closed when the last such client disconnects. Each GUI declares the groups it
requires, every group carries a reference count, and a group is built on the
first acquire and torn down on the last release. Several of these are shared
today -- rungui and the CRL panel both drive the 12idc:3820 scaler, three
panels drive the X-ray eye -- and each currently opens its own handle. Under
one server they open once.

**Serialisation.** Nothing in the device layer takes a lock, and the PI C-887
accepts exactly one TCP connection. Running rungui and optics_motors as two
processes already races it; a single server with explicit per-stage locks is
the first time that access is actually ordered.

The lock protects **one driver command, not a whole operation**. A move sends
MOV and releases; completion is then established by polling position, each
poll taking the lock for a millisecond. Holding it for the duration of a
30-second hexapod move would stall the position publisher -- and therefore
every connected panel's readout -- for the whole move.
"""

import threading
from contextlib import contextmanager
from dataclasses import dataclass

# Fixed acquisition order, so a coordinated move that needs two stages can
# never deadlock against one that needs them the other way round.
STAGE_ORDER = ("hexapod", "phi", "gonio")


@dataclass(frozen=True, slots=True)
class AxisInfo:
    name: str
    unit: str
    stage: str          # hexapod | phi | gonio
    index: int          # index within that stage's controller
    connected: bool

    def describe(self) -> dict:
        return {"name": self.name, "unit": self.unit, "stage": self.stage,
                "connected": self.connected}


@dataclass(frozen=True, slots=True)
class GroupStatus:
    name: str
    open: bool
    refs: int
    detail: str = ""

    def describe(self) -> dict:
        return {"name": self.name, "open": self.open, "refs": self.refs,
                "detail": self.detail}


class DeviceGroup:
    """One independently-openable piece of the instrument.

    `open` must not raise. The device layer's own convention is that a stage
    whose driver cannot be built falls back to a simulated stand-in and says
    why (motions_ver2.build_controller), because a machine that can reach
    only some of the hardware should still come up. A group that raised here
    would take the whole server down with it.
    """

    def __init__(self, name, description, opener, closer=None):
        self.name = name
        self.description = description
        self._opener = opener
        self._closer = closer
        self.obj = None
        self.refs = 0
        self.detail = ""

    @property
    def is_open(self):
        return self.obj is not None

    def status(self):
        return GroupStatus(self.name, self.is_open, self.refs, self.detail)


class DeviceRegistry:
    """Owns the device groups and the locks that guard them."""

    def __init__(self, profile, bus, log):
        self.profile = profile
        self._bus = bus
        self._log = log
        self._lock = threading.Lock()
        self._groups = {}
        self._stage_locks = {s: threading.RLock() for s in STAGE_ORDER}
        # Last seen position per axis, refreshed by the publisher. Reads of a
        # direct-driver axis go through a lock, so a cache lets a status call
        # answer without queueing behind a move.
        self._positions = {}
        self._register_builtin()

    # -- group table ---------------------------------------------------------

    def _register_builtin(self):
        self.register(DeviceGroup(
            "motion",
            "hexapod, phi and goniometer axes",
            self._open_motion,
            self._close_motion,
        ))
        self.register(DeviceGroup(
            "optics",
            "upstream optics: beamstop, zone plate, OSA, SAXS Z",
            self._open_optics,
            self._close_optics,
        ))
        # Still to come, each in the phase that migrates the GUI owning it,
        # so a device's construction leaves gui/rungui.py module scope
        # exactly once: qds, softglue, dg645, shutter, scaler, detectors,
        # scan_number, crl.

    def register(self, group: DeviceGroup):
        with self._lock:
            self._groups[group.name] = group

    def known_groups(self):
        with self._lock:
            return sorted(self._groups)

    # -- refcounting ---------------------------------------------------------

    def acquire(self, names):
        """Open every named group that is not open, and add a reference."""
        opened = []
        with self._lock:
            for name in names:
                group = self._groups.get(name)
                if group is None:
                    self._log.warning("unknown device group %r requested", name)
                    continue
                if group.refs == 0 and not group.is_open:
                    opened.append(group)
                group.refs += 1
        for group in opened:
            self._open(group)
        return self.status()

    def release(self, names):
        """Drop a reference; close any group that reaches zero."""
        closing = []
        with self._lock:
            for name in names:
                group = self._groups.get(name)
                if group is None:
                    continue
                group.refs = max(0, group.refs - 1)
                if group.refs == 0 and group.is_open:
                    closing.append(group)
        for group in closing:
            self._close(group)
        return self.status()

    def _open(self, group):
        self._log.info("opening device group %r (%s)", group.name,
                       group.description)
        try:
            group.obj = group._opener()
            group.detail = ""
        except Exception as exc:
            # Never fatal: see the class docstring.
            group.obj = None
            group.detail = f"{type(exc).__name__}: {exc}"
            self._log.exception("device group %r failed to open", group.name)

    def _close(self, group):
        self._log.info("closing device group %r", group.name)
        try:
            if group._closer and group.obj is not None:
                group._closer(group.obj)
        except Exception:
            self._log.exception("device group %r failed to close cleanly",
                                group.name)
        finally:
            group.obj = None

    def close_all(self):
        with self._lock:
            groups = [g for g in self._groups.values() if g.is_open]
            for g in self._groups.values():
                g.refs = 0
        for g in groups:
            self._close(g)

    def get(self, name):
        with self._lock:
            group = self._groups.get(name)
        return group.obj if group else None

    def status(self):
        with self._lock:
            return [g.status() for g in self._groups.values()]

    def any_open(self):
        with self._lock:
            return any(g.is_open for g in self._groups.values())

    # -- the motion group ----------------------------------------------------

    def _open_motion(self):
        # Imported here, not at module scope: importing motions_ver2 builds
        # nothing, but it does pull in the vendor libraries, and a process
        # that never acquires this group should not pay for them.
        from ptychosaxs.motions_ver2 import motors
        obj = motors()
        degraded = tuple(
            stage for stage, t in getattr(obj, "transports", {}).items()
            if t == "simulated"
        )
        if degraded and not self.profile.simulated:
            self._log.warning(
                "stages running simulated because their driver could not be "
                "built: %s", ", ".join(degraded))
        object.__setattr__(self.profile, "degraded", degraded)
        return obj

    def _close_motion(self, obj):
        """Disconnect each stage controller once.

        motors.disconnect() takes an *axis* and resolves it to its
        controller, so driving it from the axis list would disconnect the
        hexapod six times. Each stage is closed independently: one that
        refuses must not strand the other two still holding their sockets.
        """
        for stage, controller in getattr(obj, "control", {}).items():
            close = getattr(controller, "disconnect", None)
            if close is None:
                continue
            try:
                close()
            except Exception:
                self._log.exception("stage %r did not disconnect cleanly",
                                    stage)

    @property
    def motion(self):
        return self.get("motion")

    # -- the optics group ----------------------------------------------------

    def _open_optics(self):
        from ptychosaxs.api import optics as optics_api
        built = optics_api.open_controllers(self.profile, self._log)
        missing = [g for g in ("opticsbox", "OSA", "saxs_z", "beamstop")
                   if g not in built]
        if missing:
            self._log.warning("optics groups unavailable: %s",
                              ", ".join(missing))
        return built

    def _close_optics(self, obj):
        from ptychosaxs.api import optics as optics_api
        optics_api.close_controllers(obj)

    @property
    def optics(self):
        return self.get("optics")

    # -- locking -------------------------------------------------------------

    def stage_lock(self, stage):
        return self._stage_locks.setdefault(stage, threading.RLock())

    @contextmanager
    def exclusive(self, *stages):
        """Hold the named stages' locks, always in STAGE_ORDER.

        Wrap one driver command, not a whole move.
        """
        ordered = [s for s in STAGE_ORDER if s in stages]
        ordered += [s for s in stages if s not in STAGE_ORDER]
        acquired = []
        try:
            for stage in ordered:
                lock = self.stage_lock(stage)
                lock.acquire()
                acquired.append(lock)
            yield
        finally:
            for lock in reversed(acquired):
                lock.release()

    def _needs_lock(self, stage):
        """EPICS reads come from pyepics' monitor cache and are thread-safe.

        A direct driver is one socket or SDK handle with no reentrancy, so
        every call to it is serialised, reads included.
        """
        motion = self.motion
        transports = getattr(motion, "transports", {}) if motion else {}
        return transports.get(stage) == "direct"

    # -- reads ---------------------------------------------------------------

    def axes(self):
        motion = self.motion
        if motion is None:
            return []
        return [
            AxisInfo(name, motion.motorunits[i], motion.controller[i],
                     motion.motorindices[i], bool(motion.connected[i]))
            for i, name in enumerate(motion.motornames)
        ]

    def stage_of(self, axis):
        for info in self.axes():
            if info.name == axis:
                return info.stage
        return None

    def position(self, axis):
        motion = self.motion
        if motion is None:
            raise RuntimeError("the motion group is not open")
        stage = self.stage_of(axis)
        if stage is None:
            raise KeyError(f"unknown axis {axis!r}")
        if self._needs_lock(stage):
            with self.exclusive(stage):
                value = motion.get_pos(axis)
        else:
            value = motion.get_pos(axis)
        self._positions[axis] = value
        return value

    def positions(self):
        """Every connected axis. A read that fails leaves the last value.

        One unreachable axis must not blank the whole panel, so failures are
        logged at debug and the cached value stands.
        """
        out = {}
        for info in self.axes():
            if not info.connected:
                continue
            try:
                out[info.name] = self.position(info.name)
            except Exception:
                self._log.debug("could not read %s", info.name, exc_info=True)
                if info.name in self._positions:
                    out[info.name] = self._positions[info.name]
        return out

    def moving(self):
        motion = self.motion
        if motion is None:
            return {}
        out = {}
        for info in self.axes():
            if not info.connected:
                continue
            try:
                if self._needs_lock(info.stage):
                    with self.exclusive(info.stage):
                        out[info.name] = bool(motion.ismoving(info.name))
                else:
                    out[info.name] = bool(motion.ismoving(info.name))
            except Exception:
                self._log.debug("could not read moving for %s", info.name,
                                exc_info=True)
        return out
