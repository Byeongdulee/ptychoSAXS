"""
Generic, reusable controller for a set of EPICS motor records.

Knows nothing about how many axes exist, what they are named, or what they
drive - that is the caller's concern (see epics_crl3dprint.py for a
device-specific config built on top of this). A rig whose PV layout differs
from the stock motor record overrides FIELDS rather than subclassing the
motion logic.

Every readable field is created with auto_monitor=True, so get_pos(),
is_moving() and get_speed() read pyepics' locally cached monitor value
instead of making a Channel Access round trip. That makes them cheap enough
to call from a GUI timer, and safe to call from any thread.
"""

import threading
import time

try:
    from epics import PV
except ImportError:
    PV = None


# PV name suffixes, appended to an axis's prefix (e.g. "12ideMCS2:m1").
# Override on a subclass for a rig whose records are laid out differently.
FIELDS = {
    "readback": ".RBV",
    "drive": ".VAL",
    "done": ".DMOV",
    "velocity": ".VELO",
    "set_velocity": "_vCh.A",
    "unit": ".EGU",
    "tweak_step": ".TWV",
    "tweak_forward": ".TWF",
    "tweak_reverse": ".TWR",
    "relative": ".RLV",
    "stop": ".SPMG",
}

# Fields worth a monitor: read often, and pushed by the IOC when they change.
# Everything else is write-only and gets a plain PV.
MONITORED = ("readback", "done", "velocity", "unit")

# .SPMG enumeration. Stopping latches the axis - it ignores further moves
# until SPMG returns to Go - so stop() restores Go immediately afterwards.
SPMG_STOP = 0
SPMG_GO = 3

CONNECT_TIMEOUT_S = 2.0  # one-time bounded wait for a new PV's first connection
MOVE_TIMEOUT_S = 300.0  # put-completion ceiling for a single blocking move

# Ceiling on a single field read. A monitored value is served from cache and
# returns instantly; this only bounds the case where the monitor has not
# delivered yet, so that a GUI-thread read can never stall the window.
READ_TIMEOUT_S = 0.5


class EpicsMotorController:
    """Name-based controller for one rig's worth of EPICS motor records.

    motor_slots: [(name, pv_prefix, default_unit), ...]. The default unit is
    only used until the IOC's own .EGU can be read, and as the fallback if
    that record is unreachable.

    pv_class: injected PV factory, defaulting to epics.PV. Passing a fake
    here (see debug/debug_stubs.py FakeMotorPV) exercises this class's real
    logic with no Channel Access traffic at all.
    """

    FIELDS = FIELDS
    MONITORED = MONITORED

    def __init__(self, motor_slots, pv_class=None):
        self.motor_slots = list(motor_slots)
        self._pv_class = pv_class or PV
        self._prefixes = {name: prefix for name, prefix, _unit in self.motor_slots}
        self._default_units = {name: unit for name, _prefix, unit in self.motor_slots}
        self._pvs = {}  # (axis_name, field) -> PV

    @property
    def names(self):
        return [name for name, _prefix, _unit in self.motor_slots]

    # -- connection lifecycle ------------------------------------------------

    def connect(self) -> None:
        """Create every PV and give the whole set a bounded chance to connect.

        The budget is shared across all of them rather than spent per PV:
        the searches run concurrently, so waiting on each in turn would turn
        an unreachable IOC into a startup freeze lasting the timeout times
        the number of records.
        """
        if self._pv_class is None:
            raise RuntimeError(
                "pyepics is not installed - cannot reach the EPICS motor records. "
                "Run with --debug_mode to use simulated motors instead."
            )
        for name in self.names:
            for field in self.FIELDS:
                self._pvs[(name, field)] = self._pv_class(
                    self._pv_name(name, field),
                    auto_monitor=field in self.MONITORED,
                )
        deadline = time.monotonic() + CONNECT_TIMEOUT_S
        for pv in self._pvs.values():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            pv.wait_for_connection(timeout=remaining)

    def disconnect(self) -> None:
        for pv in self._pvs.values():
            disconnect = getattr(pv, "disconnect", None)
            if disconnect is not None:
                disconnect()
        self._pvs = {}

    def is_connected(self) -> bool:
        """True when every axis is reachable over Channel Access."""
        return bool(self._pvs) and not self.disconnected_axes()

    def is_axis_connected(self, axis) -> bool:
        """True when every PV backing this axis has a live CA connection."""
        axis = self._resolve(axis)
        pvs = [pv for (name, _field), pv in self._pvs.items() if name == axis]
        return bool(pvs) and all(pv.connected for pv in pvs)

    def disconnected_axes(self) -> list:
        """Axis names the IOC is not currently answering for, in slot order."""
        return [name for name in self.names if not self.is_axis_connected(name)]

    # -- motion ----------------------------------------------------------------

    def get_pos(self, axis):
        """Current readback in the axis's own unit, or None if unreachable."""
        return self._read(axis, "readback")

    def mv(self, axis, target: float, wait: bool = False) -> None:
        """Move to an absolute position.

        wait=True uses Channel Access put-completion, which the motor record
        acknowledges only once the move has finished - no polling, and no
        race against .DMOV not having dropped yet.
        """
        pv = self._pv(axis, "drive")
        if wait:
            pv.put(target, wait=True, timeout=MOVE_TIMEOUT_S)
        else:
            pv.put(target)

    def mv_many(self, targets, wait: bool = True, timeout: float = MOVE_TIMEOUT_S) -> bool:
        """Start every (axis, target) move at once, then wait for all of them.

        Moving several axes with separate blocking mv() calls serialises them
        and walks the stage through an intermediate position on the way. Here
        every put is issued non-blocking with its own completion callback, so
        the axes travel together and the wait ends when the last one finishes.

        Each call gets fresh events rather than reading a per-PV completion
        flag: that flag survives between calls, so a completion left over from
        an earlier move - or a put dropped on a disconnected PV, which never
        clears it - would otherwise read as "already finished" and let a scan
        measure while the stage is still travelling.

        Returns True when every move completed, False if any put was refused
        or the wait timed out - callers during a scan prefer to log and carry
        on rather than lose the run.
        """
        done_events = []
        for axis, target in targets:
            pv = self._pv(axis, "drive")
            done = threading.Event()
            if pv.put(target, callback=lambda done=done, **kwargs: done.set()) is None:
                return False  # not connected: the put never went out
            done_events.append(done)
        if not wait:
            return True
        deadline = time.monotonic() + timeout
        for done in done_events:
            if not done.wait(max(0.0, deadline - time.monotonic())):
                return False
        return True

    def mvr(self, axis, delta: float) -> None:
        """Move by a relative amount."""
        self._pv(axis, "relative").put(delta)

    def tweak(self, axis, step: float, forward: bool = True) -> None:
        """Step by `step` using the record's own tweak fields.

        The step is written to .TWV every time rather than assumed, so a value
        changed by another EPICS client cannot silently produce a wrong move.
        """
        self.set_tweak_step(axis, step)
        self._pv(axis, "tweak_forward" if forward else "tweak_reverse").put(1)

    def set_tweak_step(self, axis, step: float) -> None:
        """Publish the step size to .TWV so other clients see the same value."""
        self._pv(axis, "tweak_step").put(abs(step))

    def stop(self, axis) -> None:
        self._pv(axis, "stop").put(SPMG_STOP)
        self._pv(axis, "stop").put(SPMG_GO)

    def is_moving(self, axis) -> bool:
        done = self._read(axis, "done")
        return done is not None and not done

    # -- speed ------------------------------------------------------------------

    def get_speed(self, axis):
        """Velocity in the axis's unit/s, or None if unreachable."""
        return self._read(axis, "velocity")

    def set_speed(self, axis, vel: float) -> None:
        self._pv(axis, "set_velocity").put(vel)

    # -- status -----------------------------------------------------------------

    def get_unit(self, axis) -> str:
        """The IOC's engineering unit, falling back to the configured default."""
        unit = self._read(axis, "unit")
        return str(unit) if unit else self._default_units[self._resolve(axis)]

    # -- internals ----------------------------------------------------------------

    def _resolve(self, axis) -> str:
        """Accept either a logical name or a slot index, as the per-axis
        callers in this codebase do interchangeably."""
        if isinstance(axis, int):
            return self.names[axis]
        return axis

    def _pv_name(self, axis, field) -> str:
        return self._prefixes[self._resolve(axis)] + self.FIELDS[field]

    def _pv(self, axis, field):
        name = self._resolve(axis)
        try:
            return self._pvs[(name, field)]
        except KeyError:
            raise RuntimeError(f"connect() has not been called (no PV for {name}.{field})")

    def _read(self, axis, field):
        """Monitor-cached read: None rather than a stale value when the IOC
        is not answering, so callers can show the axis as unavailable."""
        pv = self._pv(axis, field)
        if not pv.connected:
            return None
        return pv.get(timeout=READ_TIMEOUT_S)
