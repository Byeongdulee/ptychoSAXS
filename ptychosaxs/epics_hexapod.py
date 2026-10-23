"""
EPICS device config for the PI C-887 hexapod: X/Y/Z (linear, mm) and U/V/W
(rotary, deg) on one IOC.

This is the single place to edit when the hardware wiring changes:
  - IOC_PREFIX: the IOC's EPICS prefix.
  - MOTOR_SLOTS: change each motor record suffix to match which physical
    positioner is which. The unit in each tuple is only a fallback - the
    IOC's own .EGU wins once it can be read.

Built on the generic, reusable driver in epics_motor.py, and shaped like
epics_gonio.py so motions_ver2.py reaches every stage the same way.

This covers step motion only. The controller's wave generator - which is what
a fly scan drives - has no EPICS interface on this IOC: the motorPIGCS2
driver stubs out profileMove()/triggerProfile(), loads no profile-move
records, and implements no WAV/WSL/WGC/WGO/DRC/DRR at all. Trajectories stay
on GCS; see hexapod_traj.py.
"""

import time

from .epics_motor import (
    CONNECT_TIMEOUT_S,
    MOVE_TIMEOUT_S,
    READ_TIMEOUT_S,
    EpicsMotorController,
)

# ---------------------------------------------------------------------
# EDIT THESE FOR YOUR SETUP
# ---------------------------------------------------------------------
IOC_PREFIX = "12idbHXP"

# Ordered list of (logical_name, pv_prefix, fallback_unit). The order defines
# the slot index each axis answers to when addressed by number.
MOTOR_SLOTS = [
    ("X", f"{IOC_PREFIX}:m1", "mm"),
    ("Y", f"{IOC_PREFIX}:m2", "mm"),
    ("Z", f"{IOC_PREFIX}:m3", "mm"),
    ("U", f"{IOC_PREFIX}:m4", "deg"),
    ("V", f"{IOC_PREFIX}:m5", "deg"),
    ("W", f"{IOC_PREFIX}:m6", "deg"),
]

# Controller-wide records, from $(MOTOR)/db/PI_SupportCtrl.db. DEFER is bound
# to the driver's MOTOR_DEFER_MOVES parameter: with it set, each .VAL write is
# captured instead of sent, and releasing it makes processDeferredMoves()
# coalesce them into a single "MOV X <x> Z <z>" on the wire.
DEFER_PV = f"{IOC_PREFIX}:DEFER"
DEFER_ON = 1
DEFER_OFF = 0

# The asyn record's connection state. The IOC answers for its records whether
# or not it can reach the C-887, so without this an unplugged controller looks
# like six axes sitting at 0.000000.
ASYN_CNCT_PV = f"{IOC_PREFIX}:asyn_1.CNCT"
ASYN_CONNECTED = 1

# The asyn record's read buffer, which bounds any GCS reply tunnelled through
# it. IMAX is declared special(SPC_NOMOD) in asynRecord.dbd, so this is fixed
# at IOC boot and cannot be raised from here.
#
# This used to demand 4096 on the grounds that a DRR? record dump would not
# fit. That conflated two different things. IMAX bounds one *read*, and with
# IFMT=Hybrid each read returns one line, so what has to fit is the longest
# single reply line - 130 bytes for a six-axis POS?, 124 for a twelve-column
# DRR? row (both measured on 12idbHXP, Oct 2026). 256 carries every command
# a fly scan sends.
#
# What 256 does rule out is bulk readback: draining thousands of DRR? lines
# one Channel Access round trip at a time is slow and took the IOC down once.
# So the gate passes, and get_records() is the thing that refuses - see
# hexapod_traj.PiHexapodTrajectory.get_records.
ASYN_IMAX_PV = f"{IOC_PREFIX}:asyn_1.IMAX"
ASYN_MIN_IMAX = 256

# EPICS alarm severity. A record at INVALID did not successfully read its
# device, so its value is whatever it was last left at.
INVALID_ALARM = 3

DEFAULT_VELOCITY = 1.0


class HexapodAxisController(EpicsMotorController):
    """The hexapod's six axes, named 'X','Y','Z','U','V','W'."""

    # One field added, deliberately. .CNEN reaches the driver's
    # motorClosedLoop_ parameter, which issues the GCS "SVO" command.
    #
    # PI_Support.db also offers $(P)$(R):SERVO, a dedicated "SVO?" readback,
    # and it is tempting - but it is SCAN="I/O Intr" off the asyn port, so
    # when the controller is unreachable it never processes and reads 0 with
    # an INVALID severity, which is indistinguishable from a real "servo
    # off" unless you check the severity. .MSTA is likewise all zeros while
    # the driver is not polling. Neither is worth the cost: connect() builds
    # a PV per field per axis and is_axis_connected() requires all of them,
    # so every extra field is another way for all six axes to be reported
    # dead.
    FIELDS = {
        **EpicsMotorController.FIELDS,
        "servo": ".CNEN",
    }

    def __init__(self, motor_slots=MOTOR_SLOTS, pv_class=None):
        super().__init__(motor_slots, pv_class=pv_class)
        self._units = [unit for _name, _prefix, unit in self.motor_slots]
        self._defer = None
        self._asyn_cnct = None
        self._asyn_imax = None

    def connect(self) -> None:
        super().connect()
        self._units = [self.get_unit(name) for name in self.names]
        # Controller-wide records live outside the per-axis table the base
        # class builds, so create them here - and wait for them. The base
        # class's connection budget covers only the per-axis PVs, so without
        # this these are read the instant they are created: link_down() then
        # reads None, concludes "cannot tell", and lets handle_error() write
        # .CNEN to a controller that is not there.
        self._defer = self._pv_class(DEFER_PV)
        self._asyn_cnct = self._pv_class(ASYN_CNCT_PV, auto_monitor=True)
        self._asyn_imax = self._pv_class(ASYN_IMAX_PV)
        deadline = time.monotonic() + CONNECT_TIMEOUT_S
        for pv in (self._defer, self._asyn_cnct, self._asyn_imax):
            wait = getattr(pv, "wait_for_connection", None)
            remaining = deadline - time.monotonic()
            if wait is None or remaining <= 0:
                break
            wait(timeout=remaining)

    def disconnect(self) -> None:
        for pv in (self._defer, self._asyn_cnct, self._asyn_imax):
            disconnect = getattr(pv, "disconnect", None)
            if disconnect is not None:
                disconnect()
        self._defer = None
        self._asyn_cnct = None
        self._asyn_imax = None
        super().disconnect()

    # -- names and units -------------------------------------------------------

    @property
    def axes(self) -> list:
        return self.names

    @property
    def motornames(self) -> list:
        return self.names

    @property
    def channel_names(self) -> list:
        return self.names

    @property
    def motorunits(self) -> list:
        return list(self._units)

    @property
    def units(self) -> list:
        return list(self._units)

    # -- status ----------------------------------------------------------------

    @property
    def connected(self) -> list:
        """Per-axis Channel Access reachability, in slot order.

        Reachability, not readiness: an axis whose servo is off is still
        listed, so it appears in the motor panel instead of making startup
        fail.

        Channel Access reachability only - deliberately not folded together
        with link_down(). A hexapod whose controller is unreachable would
        then report every axis down, and rungui treats that as fatal, so an
        unplugged C-887 would stop the GUI starting at all. The link is
        reported through ioc_link_ok() and the IOC info dialog instead.
        """
        return [self.is_axis_connected(name) for name in self.names]

    def isconnected(self, ax=-1):
        """One axis's state when given a slot index, every axis's as a list
        otherwise."""
        if isinstance(ax, int) and ax > -1:
            return self.is_axis_connected(ax)
        return [self.is_axis_connected(name) for name in self.names]

    def ismoving(self, axis) -> bool:
        return self.is_moving(axis)

    def isattarget(self, axis=""):
        """Per-axis settled state: one bool for a named axis, a dict of all
        six otherwise. Mirrors the GCS library's ONT? shape."""
        if axis:
            return not self.is_moving(axis)
        return {name: not self.is_moving(name) for name in self.names}

    def link_down(self) -> bool:
        """True only when the IOC positively reports its socket to the C-887
        as down.

        Deliberately one-sided. "Cannot tell" - no record, not connected -
        is not "down": this gates whether the hexapod's rows are usable at
        all, so an IOC laid out slightly differently must not be able to
        grey out six working axes.
        """
        if self._asyn_cnct is None or not self._asyn_cnct.connected:
            return False
        value = self._asyn_cnct.get(timeout=READ_TIMEOUT_S)
        return value is not None and value != ASYN_CONNECTED

    def ioc_link_ok(self) -> bool:
        """True when the IOC's socket to the C-887 is up, or unknown.

        Distinct from is_axis_connected(), which only says the IOC is
        answering. When the link is down every readback is undefined rather
        than a position, so the GUI should say so rather than show zeros.
        """
        return not self.link_down()

    def asyn_passthrough_status(self):
        """Whether GCS can usefully be tunnelled through this IOC's asyn
        record: (ok, reason).

        Two independent things have to hold. The record's socket to the
        C-887 must be up - otherwise commands go nowhere and the failure
        looks like a hang. And its read buffer must be big enough for the
        replies, which it currently is not on this IOC.
        """
        if self._asyn_cnct is None:
            return False, "connect() has not been called"
        if not self._asyn_cnct.connected:
            return False, f"{ASYN_CNCT_PV} is unreachable"
        if self._asyn_cnct.get() != ASYN_CONNECTED:
            return False, (
                f"the IOC's socket to the C-887 is down ({ASYN_CNCT_PV} reads "
                f"Disconnect) - check the controller address in the IOC's "
                f"iocsh/hexapod.cmd"
            )
        imax = self._asyn_imax.get() if self._asyn_imax is not None else None
        if imax is None:
            return False, f"{ASYN_IMAX_PV} is unreachable"
        if imax < ASYN_MIN_IMAX:
            return False, (
                f"{ASYN_IMAX_PV} is {imax}, below the {ASYN_MIN_IMAX} needed to "
                f"read a GCS reply; IMAX is fixed at IOC boot, so this needs an "
                f"edit to the IOC's iocsh/hexapod.cmd"
            )
        return True, ""

    def get_pos(self, axis) -> float:
        """Position in the axis's own unit, or nan when the IOC is not
        answering for it.

        Always a float: callers round it, format it with %f and do arithmetic
        on it without checking, so nan propagates visibly through all of those
        where None would raise. Use is_axis_connected() to test reachability.
        """
        pos = super().get_pos(axis)
        return float("nan") if pos is None else pos

    # -- servo -----------------------------------------------------------------

    def _servo_state(self, axis):
        """(known, on) for one axis - `known` is False when the record
        cannot be trusted.

        Severity matters as much as the value here. A record whose device
        support could not read the controller holds whatever it was last
        left at and raises its severity to INVALID; taking that number at
        face value reads as "servo off" and invites a caller to go and
        "fix" a controller that is not even connected.
        """
        pv = self._pv(axis, "servo")
        if not pv.connected:
            return False, False
        value = pv.get(timeout=READ_TIMEOUT_S)
        if value is None:
            return False, False
        if getattr(pv, "severity", 0) >= INVALID_ALARM:
            return False, False
        return True, bool(value)

    def is_servo_on(self, axis="") -> bool:
        """Closed-loop state: one axis's when named, every axis's ANDed
        otherwise.

        Fails open: an axis whose state cannot be read is not reported as
        off. Reporting "cannot tell" as off would make the startup check in
        rungui.py try to re-enable the servo on every launch - writing to
        six records on a controller that may not even be reachable.
        """
        for name in ([axis] if axis else self.names):
            known, on = self._servo_state(name)
            if known and not on:
                return False
        return True

    def enable_servo(self, axis, on: bool = True) -> None:
        self._pv(axis, "servo").put(1 if on else 0)

    def handle_error(self) -> bool:
        """Re-enable closed-loop control on every axis, then report the truth.

        Servo only, and deliberately so. The GCS library's handle_error()
        also calls move_ref() -> FRF, which re-references the platform by
        driving all six legs unattended; it is reached from retry loops that
        can fire during a scan. Nothing here initiates a referencing move - no
        .HOMF, no .HOMR.

        One pass, not a retry loop: a caller that wants to retry can, and
        returning False honestly is what lets it stop.

        Does nothing when the IOC cannot reach the controller. Writing .CNEN
        then changes six records and commands nothing, which looks like a
        successful recovery right up until the link returns and the driver
        pushes the stale request through.
        """
        if not self.ioc_link_ok():
            print(
                "hexapod: not touching the servo - the IOC cannot reach the "
                f"controller ({ASYN_CNCT_PV} reads Disconnect)."
            )
            return False
        for name in self.names:
            try:
                self.enable_servo(name, True)
            except Exception as exc:  # a single bad record must not mask the rest
                print(f"hexapod: could not enable servo on {name}: {exc}")
        return self.is_servo_on()

    # -- motion ----------------------------------------------------------------

    def move_coordinated(self, targets, wait: bool = True,
                         timeout: float = MOVE_TIMEOUT_S) -> bool:
        """Move several axes as one motion, via the IOC's deferred-moves flag.

        With DEFER set the driver captures each .VAL write instead of sending
        it, and releasing DEFER emits a single "MOV X <x> Z <z>". That is one
        straight path; separate moves walk the platform through a corner and
        pay two settle times.

        DEFER is released in a finally for the same reason epics_gonio.set_pos
        restores .SET: left set, it silently swallows every later move from
        every EPICS client, not just this one.

        Note the driver skips its velocity/acceleration block on the deferred
        branch, so a .VELO change does not apply to a deferred move.

        Falls back to concurrent puts when the DEFER record is unreachable,
        and returns False rather than raising - callers during a scan prefer
        to log and carry on.
        """
        targets = list(targets)
        if not targets:
            return True
        # One axis is not a coordinated move, and taking the deferred path
        # for it would cost something: the driver skips its velocity and
        # acceleration block on that branch, so a .VELO set beforehand would
        # not apply.
        if len(targets) == 1:
            return super().mv_many(targets, wait=wait, timeout=timeout)
        if self._defer is None or not self._defer.connected:
            return super().mv_many(targets, wait=wait, timeout=timeout)

        self._defer.put(DEFER_ON, wait=True)
        try:
            done_events = self._start_many(targets)
        finally:
            self._defer.put(DEFER_OFF, wait=True)
        if done_events is None:
            return False
        if not wait:
            return True
        return self._wait_many(done_events, timeout)

    def mv_many(self, targets, wait: bool = True,
                timeout: float = MOVE_TIMEOUT_S) -> bool:
        """Coordinated by default on this rig - see move_coordinated."""
        return self.move_coordinated(targets, wait=wait, timeout=timeout)

    def set_pos(self, axis, pos: float = 0.0) -> None:
        """Not supported on the hexapod, and deliberately a no-op.

        A hexapod position is a pose in a coordinate system, not an axis
        offset: redefining one axis's zero through the motor record's .SET
        would desynchronise the record from the controller's own kinematics.
        Use the coordinate-system API to move the origin.
        """
        return None

    # -- speed -----------------------------------------------------------------

    def get_speed(self, axis=None) -> tuple:
        """(velocity, acceleration), with acceleration reported as None.

        The velocity is the whole platform's, not this axis's: the driver maps
        .VELO to the C-887's VLS/VLS?, which is a single system-wide speed. An
        axis argument is accepted for a uniform call shape and otherwise only
        picks which record to read.
        """
        return (super().get_speed(axis or self.names[0]), None)

    def set_speed(self, axis=None, vel: float = DEFAULT_VELOCITY, acc=None) -> None:
        """Set the platform velocity. acc is accepted and ignored.

        One write is enough - .VELO is VLS, which applies to all six axes -
        but writing every record keeps the readbacks consistent rather than
        leaving five of them showing the old value.
        """
        for name in self.names:
            super().set_speed(name, vel)
