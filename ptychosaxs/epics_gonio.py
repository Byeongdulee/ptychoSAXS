"""
EPICS device config for the goniometer stages: trans1/trans2 (linear, mm) and
tilt1/tilt2 (rotary, deg) on one IOC.

This is the single place to edit when the hardware wiring changes:
  - IOC_PREFIX: the IOC's EPICS prefix.
  - MOTOR_SLOTS: change each motor record suffix to match which physical
    positioner is which. The unit in each tuple is only a fallback - the
    IOC's own .EGU wins once it can be read.

Built on the generic, reusable driver in epics_motor.py. Beyond the rig
config, GonioAxisController presents the attribute/method names the rest of
the GUI already uses for these stages (motornames, isconnected() returning a
list, ismoving, a two-element get_speed), so motions_ver2.py, rungui.py and
scan_handler.py reach them exactly as before.
"""

try:
    from .epics_motor import EpicsMotorController
except ImportError:
    from epics_motor import EpicsMotorController

# ---------------------------------------------------------------------
# EDIT THESE FOR YOUR SETUP
# ---------------------------------------------------------------------
IOC_PREFIX = "12ideMCS1"

# Ordered list of (logical_name, pv_prefix, fallback_unit). The order defines
# the slot index each axis answers to when addressed by number.
MOTOR_SLOTS = [
    ("trans1", f"{IOC_PREFIX}:m1", "mm"),
    ("trans2", f"{IOC_PREFIX}:m2", "mm"),
    ("tilt1", f"{IOC_PREFIX}:m3", "deg"),
    ("tilt2", f"{IOC_PREFIX}:m4", "deg"),
]

# Writing .SET puts the record in "set" mode, where a write to .VAL redefines
# the current position instead of commanding a move.
SET_MODE_SET = 1
SET_MODE_USE = 0

DEFAULT_VELOCITY = 1.0


class GonioAxisController(EpicsMotorController):
    """The goniometer's four stages, named 'trans1'/'trans2'/'tilt1'/'tilt2'."""

    # .SET is only needed by this rig's set_pos; the base map is otherwise unchanged.
    FIELDS = {**EpicsMotorController.FIELDS, "set_mode": ".SET"}

    def __init__(self, motor_slots=MOTOR_SLOTS, pv_class=None):
        super().__init__(motor_slots, pv_class=pv_class)
        self._units = [unit for _name, _prefix, unit in self.motor_slots]

    def connect(self) -> None:
        super().connect()
        self._units = [self.get_unit(name) for name in self.names]

    # -- names and units -------------------------------------------------------

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

    def isconnected(self, ax=-1):
        """One axis's state when given a slot index, every axis's as a list
        otherwise."""
        if isinstance(ax, int) and ax > -1:
            return self.is_axis_connected(ax)
        return [self.is_axis_connected(name) for name in self.names]

    def ismoving(self, axis) -> bool:
        return self.is_moving(axis)

    def get_pos(self, axis) -> float:
        """Position in the axis's own unit, or nan when the IOC is not
        answering for it.

        Always a float: callers round it, format it with %f and do arithmetic
        on it without checking, so nan propagates visibly through all of those
        where None would raise. Use is_axis_connected() to test reachability.
        """
        pos = super().get_pos(axis)
        return float("nan") if pos is None else pos

    # -- speed -----------------------------------------------------------------

    def get_speed(self, axis) -> tuple:
        """(velocity, acceleration), with acceleration reported as None - this
        IOC exposes no acceleration field. Callers already handle a None here
        for the hexapod."""
        return (super().get_speed(axis), None)

    def set_speed(self, axis, vel: float = DEFAULT_VELOCITY, acc=None) -> None:
        """acc is accepted so existing call sites keep working, and ignored."""
        super().set_speed(axis, vel)

    # -- position redefinition ----------------------------------------------------

    def set_pos(self, axis, position: float = 0.0) -> float:
        """Redefine the current physical position as `position` without moving.

        The record only treats a .VAL write as a redefinition while .SET says
        so, so set mode is restored even if the write fails - leaving it on
        would turn every later move into another silent redefinition.
        """
        set_pv = self._pv(axis, "set_mode")
        try:
            set_pv.put(SET_MODE_SET, wait=True)
            self._pv(axis, "drive").put(position, wait=True)
        finally:
            set_pv.put(SET_MODE_USE, wait=True)
        # Bypass the monitor cache: the redefined .RBV has not been pushed yet,
        # so the cached value here is still the old position.
        readback = self._pv(axis, "readback")
        if not readback.connected:
            return float("nan")
        return readback.get(use_monitor=False)
