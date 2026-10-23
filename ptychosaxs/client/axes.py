"""An EpicsMotorController-shaped view of a rig owned by the server.

The CRL panel and its scalar-scan window were written against
EpicsMotorController directly -- get_pos, mv, tweak, set_speed and so on.
Rather than rewrite both against a facade, this presents the same surface
over the link, so the windows keep their logic and simply stop touching
Channel Access.

Positions come from the mirror the server pushes when one is attached, so
a readback is a dict lookup rather than a round trip. Everything that
changes the hardware is an RPC, which is the right way round: reads are
frequent and writes are deliberate.

This is a staging post, not the destination. The scalar scan is a *scan*,
and when it moves to a server job it will stop driving axes from the client
one step at a time. Until then this keeps it off EPICS.
"""


class RemoteAxisController:
    """The subset of EpicsMotorController that the CRL windows use."""

    def __init__(self, facade, mirror=None):
        self._facade = facade
        self._mirror = mirror          # dict of axis -> position, or None
        self._axes = None

    # -- introspection -------------------------------------------------------

    def _info(self):
        if self._axes is None:
            self._axes = self._facade.axes()
        return self._axes

    @property
    def names(self):
        return [a["name"] for a in self._info()]

    def get_unit(self, axis):
        for a in self._info():
            if a["name"] == axis:
                return a["unit"]
        return ""

    def is_axis_connected(self, axis):
        for a in self._info():
            if a["name"] == axis:
                return bool(a["connected"])
        return False

    def disconnected_axes(self):
        return [a["name"] for a in self._info() if not a["connected"]]

    def is_connected(self):
        return bool(self._info()) and not self.disconnected_axes()

    def connect(self):
        """The server owns the connection; this just refreshes what we know."""
        self._axes = None
        return self._info()

    def disconnect(self):
        self._axes = None

    # -- reads ---------------------------------------------------------------

    def get_pos(self, axis):
        if self._mirror is not None and axis in self._mirror:
            return self._mirror[axis]
        return self._facade.position(axis)

    def get_speed(self, axis):
        return self._facade.get_speed(axis)

    def is_moving(self, axis):
        # Not pushed, and the CRL windows only ask while settling a move.
        return False

    # -- writes --------------------------------------------------------------

    def mv(self, axis, target, wait=False):
        self._facade.move(axis, float(target), wait=wait)

    def mvr(self, axis, delta):
        self._facade.move(axis, self.get_pos(axis) + float(delta))

    def tweak(self, axis, step, forward=True):
        self._facade.tweak(axis, float(step), forward=forward)

    def set_tweak_step(self, axis, step):
        self._facade.set_tweak_step(axis, float(step))

    def set_speed(self, axis, vel):
        self._facade.set_speed(axis, float(vel))

    def set_pos(self, axis, value=0.0):
        self._facade.set_position(axis, float(value))

    def stop(self, axis=None):
        self._facade.stop(axis)
