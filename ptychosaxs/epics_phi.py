"""
EPICS device config for the tomography rotation stage, phi: one rotary axis
(deg) on its own IOC.

This is the single place to edit when the hardware wiring changes:
  - IOC_PREFIX: the IOC's EPICS prefix.
  - MOTOR_SLOTS: the motor record suffix for the rotation. The unit in the
    tuple is only a fallback - the IOC's own .EGU wins once it can be read.

Built on the generic, reusable driver in epics_motor.py. Beyond the rig
config, PhiAxisController presents the call surface the rest of the GUI
already uses for phi - the one the acspy driver in motions_ver2._build_phi
established, which differs from every other stage here because phi is a
single axis: get_pos() takes no axis name, and mv() takes a target and a
relative flag rather than (axis, target). motions_ver2.get_pos/mv special-
case phi on exactly that shape, so this class keeps it.
"""

import time

try:
    from .epics_motor import EpicsMotorController
except ImportError:
    from epics_motor import EpicsMotorController

# ---------------------------------------------------------------------
# EDIT THESE FOR YOUR SETUP
# ---------------------------------------------------------------------
IOC_PREFIX = "12idePtycho"

# Ordered list of (logical_name, pv_prefix, fallback_unit). One entry: the
# record is 12idePtycho:m1, described by the IOC as "Tomo Rot".
MOTOR_SLOTS = [
    ("phi", f"{IOC_PREFIX}:m1", "deg"),
]

# Writing .SET puts the record in "set" mode, where a write to .VAL redefines
# the current position instead of commanding a move.
SET_MODE_SET = 1
SET_MODE_USE = 0

DEFAULT_VELOCITY = 36.0  # deg/s, what rungui asks for at startup
DEFAULT_ACCELERATION = 360.0  # deg/s^2

# How long after a move is commanded ismoving() keeps saying "moving" even
# though .DMOV still reads done.
#
# mv() does not block - the GUI's move loop polls ismoving() and emits each
# position as it goes, which a put-completion wait would hide. But .DMOV
# only drops once the IOC has processed the .VAL write, so for the first
# few milliseconds after the put the record still reads "done" and that
# loop would exit immediately, reporting a move as finished while the stage
# is still turning. This window covers the gap; the record's own .DMOV
# takes over as soon as it is meaningful.
MOVE_START_GRACE_S = 0.25


class PhiAxisController(EpicsMotorController):
    """The tomography rotation stage, named 'phi'."""

    AXIS = "phi"

    # .SET is needed by set_pos and .ACCL by the speed pair; the base map is
    # otherwise unchanged.
    FIELDS = {
        **EpicsMotorController.FIELDS,
        "set_mode": ".SET",
        "accel_time": ".ACCL",
    }

    def __init__(self, motor_slots=MOTOR_SLOTS, pv_class=None):
        super().__init__(motor_slots, pv_class=pv_class)
        self._unit = motor_slots[0][2]
        self._move_until = 0.0

    def connect(self) -> None:
        super().connect()
        self._unit = self.get_unit(self.AXIS)

    # -- names and units -------------------------------------------------------

    @property
    def motornames(self) -> list:
        return self.names

    @property
    def motorunits(self) -> list:
        return [self._unit]

    @property
    def units(self) -> list:
        return self.motorunits

    # -- status ----------------------------------------------------------------

    def isconnected(self, axis=AXIS) -> bool:
        """A bare bool, not a list.

        phi is one axis and every caller treats it that way - motions_ver2
        appends `isconnected()` straight into its per-motor list, and rungui
        disables the row when it is False.
        """
        return self.is_axis_connected(self.AXIS)

    def ismoving(self, axis=None) -> bool:
        return self.is_moving(self.AXIS) or time.monotonic() < self._move_until

    def get_pos(self, axis=0) -> float:
        """Position in deg, or nan when the IOC is not answering.

        Takes no axis in practice - the argument exists only because some
        callers pass a slot index positionally. Always a float, for the same
        reason as the goniometer's: callers round and format it without
        checking, and nan propagates visibly where None would raise.
        """
        pos = super().get_pos(self.AXIS)
        return float("nan") if pos is None else pos

    # -- motion ----------------------------------------------------------------

    def mv(self, target: float, relative: bool = False) -> None:
        """Start a move and return; the caller polls ismoving().

        Signature deliberately differs from the base class's
        mv(axis, target): phi has a single axis, and this is the shape
        motions_ver2 calls it with.
        """
        if relative:
            self._pv(self.AXIS, "relative").put(float(target))
        else:
            self._pv(self.AXIS, "drive").put(float(target))
        self._move_until = time.monotonic() + MOVE_START_GRACE_S

    def mvr(self, val: float, **kwargs) -> None:
        self.mv(val, relative=True)

    def stop(self, axis=None) -> None:
        super().stop(self.AXIS)
        self._move_until = 0.0

    # -- speed ------------------------------------------------------------------

    def get_speed(self, axis=None) -> tuple:
        """(velocity in deg/s, acceleration in deg/s^2).

        The record stores acceleration as .ACCL, the time to reach .VELO
        from rest, so the pair the GUI uses is derived from the two. An
        unreachable record, or an .ACCL of zero, gives None for the
        acceleration rather than a fabricated number - callers already
        handle a None here for the goniometer.
        """
        vel = self._read(self.AXIS, "velocity")
        accl = self._read(self.AXIS, "accel_time")
        if vel is None:
            return (None, None)
        if not accl:
            return (vel, None)
        return (vel, vel / accl)

    def set_speed(self, axis=None, vel: float = DEFAULT_VELOCITY,
                  acc: float = DEFAULT_ACCELERATION) -> None:
        """Set velocity in deg/s and acceleration in deg/s^2.

        .ACCL is a time, so the acceleration is written as the seconds that
        ramp takes at the velocity being set. acc=None leaves the record's
        own ramp time alone.
        """
        super().set_speed(self.AXIS, vel)
        if acc:
            self._pv(self.AXIS, "accel_time").put(abs(float(vel)) / abs(float(acc)))

    # -- position redefinition ----------------------------------------------------

    def set_pos(self, axis=None, pos: float = 0.0) -> float:
        """Redefine the current physical position as `pos` without moving.

        The record only treats a .VAL write as a redefinition while .SET
        says so, so set mode is restored even if the write fails - leaving
        it on would turn every later move into another silent redefinition.
        """
        set_pv = self._pv(self.AXIS, "set_mode")
        try:
            set_pv.put(SET_MODE_SET, wait=True)
            self._pv(self.AXIS, "drive").put(float(pos), wait=True)
        finally:
            set_pv.put(SET_MODE_USE, wait=True)
        # Bypass the monitor cache: the redefined .RBV has not been pushed
        # yet, so the cached value here is still the old position.
        readback = self._pv(self.AXIS, "readback")
        if not readback.connected:
            return float("nan")
        return readback.get(use_monitor=False)

    # -- ACS compatibility --------------------------------------------------------

    def commutate(self) -> None:
        """No-op. Commutation is an ACS servo concern; over EPICS the IOC
        owns it. Kept so callers written against the acspy phi still work."""
