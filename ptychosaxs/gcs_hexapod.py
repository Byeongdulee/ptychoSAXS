"""
The hexapod's "direct" motion transport: GCS straight at the C-887.

Presents the same surface as epics_hexapod.HexapodAxisController, so
HexapodFacade can hold either one without caring which. The two differ in
more than plumbing, and the differences are documented at each method rather
than smoothed over - a GCS pose is one round trip for all six axes, where
the motor records are six cached reads; GCS MOV is already atomic across
axes, where the records need the IOC's deferred-moves flag to be.

Used when rungui's HEXAPOD_TRANSPORT is "direct". In that mode the same
device object also serves the trajectory half, so a session opens one
connection to the controller rather than two.
"""

HEXAPOD_AXES = ["X", "Y", "Z", "U", "V", "W"]
HEXAPOD_UNITS = ["mm", "mm", "mm", "deg", "deg", "deg"]


class GcsAxisController:
    """Motion half of the hexapod, over the vendor's GCS library."""

    def __init__(self, device, axes=None, units=None):
        self._dev = device
        self._axes = list(axes or getattr(device, "axes", None) or HEXAPOD_AXES)
        self._units = list(units or HEXAPOD_UNITS)

    # -- names and units -------------------------------------------------------

    @property
    def names(self):
        return list(self._axes)

    axes = names
    motornames = names
    channel_names = names

    @property
    def motorunits(self):
        return list(self._units)

    units = motorunits

    # -- connection ------------------------------------------------------------

    def connect(self) -> None:
        """The device connects in its own constructor; this is for parity."""
        connect = getattr(self._dev, "connect", None)
        if connect is not None:
            connect()

    def disconnect(self) -> None:
        disconnect = getattr(self._dev, "disconnect", None)
        if disconnect is not None:
            disconnect()

    def is_axis_connected(self, axis) -> bool:
        """There is one socket for all six axes, so this is per-device.

        A failed read means the link is gone, which is the only kind of
        "not connected" this transport has.
        """
        try:
            return bool(self._dev.get_pos())
        except Exception:
            return False

    def isconnected(self, ax=-1):
        live = self.is_axis_connected(None)
        if isinstance(ax, int) and ax > -1:
            return live
        return [live] * len(self._axes)

    @property
    def connected(self) -> list:
        return self.isconnected()

    def ioc_link_ok(self) -> bool:
        """Always true: there is no IOC in the middle to lose."""
        return True

    def asyn_passthrough_status(self):
        """Not applicable - nothing is being tunnelled through an IOC."""
        return False, "the hexapod is on its direct GCS connection, not an IOC"

    # -- position --------------------------------------------------------------

    def get_pos(self, axis) -> float:
        """One axis, as a float.

        GCS answers for all six at once, so this costs a full POS? round
        trip per call where the motor records cost a cached read. Callers
        wanting the whole pose should use the facade's bare get_pos().
        """
        try:
            pose = self._dev.get_pos()
        except Exception:
            return float("nan")
        name = self._resolve(axis)
        try:
            return float(pose[name])
        except (KeyError, TypeError, ValueError):
            return float("nan")

    def set_pos(self, axis, pos=0) -> None:
        """Not supported, as on the EPICS side: a hexapod position is a pose
        in a coordinate system, not a per-axis offset."""
        return None

    # -- motion ----------------------------------------------------------------

    def move_coordinated(self, targets, wait=True, timeout=None) -> bool:
        """One MOV for every axis in `targets`.

        Already atomic - the whole point of the GCS form - so unlike the
        EPICS path there is no deferred-moves flag to set. `timeout` is
        accepted for a common signature; the library applies its own.
        """
        argv = []
        for axis, target in targets:
            argv += [self._resolve(axis), target]
        if not argv:
            return True
        return bool(self._dev.mv(*argv, wait=wait))

    def mv_many(self, targets, wait=True, timeout=None) -> bool:
        return self.move_coordinated(targets, wait=wait, timeout=timeout)

    def stop(self, axis=None) -> None:
        """GCS #24 (stop all) - there is no per-axis stop, so the argument is
        accepted and ignored."""
        self._dev.pidev.send_command("#24")

    def tweak(self, axis, step, forward=True) -> None:
        name = self._resolve(axis)
        delta = abs(step) if forward else -abs(step)
        self.move_coordinated([(name, self.get_pos(name) + delta)])

    def is_moving(self, axis) -> bool:
        return not self.isattarget(self._resolve(axis))

    ismoving = is_moving

    def isattarget(self, axis=""):
        return self._dev.isattarget(self._resolve(axis) if axis else "")

    # -- servo -----------------------------------------------------------------

    def is_servo_on(self, axis="") -> bool:
        """Fails open, matching the EPICS side: the library raises when the
        read comes back empty, and "cannot tell" is not "off"."""
        try:
            return bool(self._dev.is_servo_on(self._resolve(axis) if axis else ""))
        except Exception:
            return True

    def enable_servo(self, axis, on=True) -> None:
        self._dev.pidev.SVO()

    def handle_error(self) -> bool:
        """The library's own recovery.

        Note this is *not* the servo-only recovery the EPICS path performs:
        gcs.Hexapod.handle_error re-references the platform with FRF, which
        drives all six legs, then restores the pose. That has always been
        this transport's behaviour; it is called out here because the two
        transports differ in how much they will do unattended.
        """
        return bool(self._dev.handle_error())

    # -- speed -----------------------------------------------------------------

    def get_speed(self, axis=None) -> tuple:
        """(velocity, None). VLS is one platform-wide speed, so the axis
        argument is accepted and ignored."""
        return (self._dev.get_speed(), None)

    def set_speed(self, axis=None, vel=1.0, acc=None) -> None:
        self._dev.set_speed(vel)

    # -- internals -------------------------------------------------------------

    def _resolve(self, axis):
        if isinstance(axis, int):
            return self._axes[axis]
        return axis
