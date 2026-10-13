"""
The goniometer's "direct" transport: the SmarAct MCS2 SDK.

ptychosaxs/smaract_gonio.py drives the MCS2 through SmarAct's own library.
It predates the move to EPICS (it was removed in commit e9f219a and restored
for this transport) and it is a module of bare functions rather than a
class, which is how the old motions.py used it - as the controller object
itself.

It is very nearly a drop-in: mv, mvr, get_pos, set_pos, ismoving, stop,
get_speed and set_speed all already take either an axis name or a channel
index, and motornames/motorunits match the EPICS rig. This adapter exists
for the three places it does not line up, and so that the vendor module
stays byte-identical to the version in history:

  - no connect()/disconnect(), which motions_ver2 calls;
  - get_pos() returns None when the SDK raises, where every other
    controller here returns nan - motors.get_pos rounds the result, and
    round(None) raises;
  - channel_names and units are filled in at import from the hardware, so
    they are empty when the device did not open.

Deliberately no mv_many: the MCS2 has no coordinated-move primitive here,
so motors.can_coordinate reports False and a 2-D scan moves the two axes
one after the other, which is what this rig did before.

NOT exercised on this machine - the SmarAct SDK is not installed here, so
importing smaract_gonio fails and the transport falls back to a simulated
stand-in. This code runs on the machine wired to the MCS2.
"""

GONIO_AXES = ["trans1", "trans2", "tilt1", "tilt2"]
GONIO_UNITS = ["mm", "mm", "deg", "deg"]


class SmaractGonioController:
    """Adapts the smaract_gonio function module to the controller surface."""

    def __init__(self, module=None):
        if module is None:
            from ptychosaxs import smaract_gonio

            module = smaract_gonio
        self._m = module

    # -- names and units -------------------------------------------------------

    @property
    def names(self):
        return list(getattr(self._m, "motornames", GONIO_AXES))

    motornames = names

    @property
    def channel_names(self):
        """What the hardware reported at import, falling back to the
        configured names when the device did not open."""
        return list(getattr(self._m, "channel_names", None) or self.names)

    @property
    def motorunits(self):
        return list(getattr(self._m, "motorunits", GONIO_UNITS))

    @property
    def units(self):
        return list(getattr(self._m, "units", None) or self.motorunits)

    # -- connection ------------------------------------------------------------

    def connect(self) -> None:
        """The module opens the device at import; nothing to do here."""

    def disconnect(self) -> None:
        close = getattr(self._m.ctl, "Close", None)
        handle = getattr(self._m, "smaract", None)
        if close is not None and handle is not None:
            close(handle)

    def isconnected(self, ax=-1):
        """One axis's state for a slot index, every axis's as a list for -1.

        Unlike the module's motion functions this one takes only an int -
        its body is `if ax > -1`, which raises against a string - so a name
        is resolved to a channel here first.
        """
        if _is_index(ax) and ax < 0:
            return self._m.isconnected(-1)
        return bool(self._m.isconnected(self._channel(ax)))

    @property
    def connected(self) -> list:
        return self.isconnected()

    def is_axis_connected(self, axis) -> bool:
        return bool(self._m.isconnected(self._channel(axis)))

    # -- position --------------------------------------------------------------

    def get_pos(self, axis) -> float:
        """nan rather than None when the SDK raises, so callers that round
        and format the result do not blow up on a bad read."""
        pos = self._m.get_pos(self._channel(axis))
        return float("nan") if pos is None else float(pos)

    def set_pos(self, axis, position=0.0) -> float:
        pos = self._m.set_pos(self._channel(axis), position)
        return float("nan") if pos is None else float(pos)

    # -- motion ----------------------------------------------------------------

    def mv(self, axis, target, wait=True) -> None:
        self._m.mv(self._channel(axis), target, wait=wait)

    def mvr(self, axis, delta, wait=True) -> None:
        self._m.mvr(self._channel(axis), delta, wait=wait)

    def stop(self, axis) -> None:
        self._m.stop(self._channel(axis))

    def is_moving(self, axis) -> bool:
        return bool(self._m.ismoving(self._channel(axis)))

    ismoving = is_moving

    def tweak(self, axis, step, forward=True) -> None:
        self.mvr(axis, abs(step) if forward else -abs(step))

    # -- speed -----------------------------------------------------------------

    def get_speed(self, axis) -> tuple:
        """(velocity, acceleration) - this rig really does report both,
        unlike the EPICS one, which has no acceleration field."""
        return self._m.get_speed(self._channel(axis))

    def set_speed(self, axis, vel=5, acc=10) -> None:
        self._m.set_speed(self._channel(axis), vel, acc)

    # -- internals -------------------------------------------------------------

    def _channel(self, axis) -> int:
        """Always a channel index.

        The motion functions accept a name too, but isconnected does not,
        so resolving here keeps one rule for the whole adapter. Channel n
        is names[n] - the module's own motornames order.
        """
        if _is_index(axis):
            return axis
        return self.names.index(axis)


def _is_index(axis) -> bool:
    return isinstance(axis, int) and not isinstance(axis, bool)
