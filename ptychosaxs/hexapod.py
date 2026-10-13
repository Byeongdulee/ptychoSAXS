"""
The PI C-887 hexapod, as the rest of ptychoSAXS sees it.

One object with two collaborators behind it:

  - step motion, readback, servo and coordinated moves go to the EPICS motor
    records at 12idbHXP (epics_hexapod.HexapodAxisController);
  - fly-scan trajectories go to the controller's wave generator over GCS
    (hexapod_traj), because no EPICS trajectory interface exists on this IOC.

This is a facade, not a subclass. It used to inherit from pihexapod's
Hexapod, with `Hexapod = object` substituted when the import failed - which
made every inherited method vanish silently, so the first symptom was a bare
AttributeError from inside a running scan. Composition also lets the two
halves come from different transports, which inheritance cannot.

The trajectory half is resolved lazily, on first use: a session that only
step-scans never opens a second connection to the controller. Callers that
need to know in advance - the GUI, deciding whether to offer a fly scan - ask
can_fly, which answers without connecting.
"""

try:
    from .epics_hexapod import HexapodAxisController
    from .gcs_hexapod import GcsAxisController
    from .transport import DIRECT, EPICS, SIMULATED, TransportUnavailable
    from .hexapod_traj import (
        PiHexapodTrajectory,
        TRAJ_ATTRS,
        TRAJ_METHODS,
        SimulatedTrajectory,
        TrajectoryUnavailable,
        make_trajectory_backend,
    )
except ImportError:  # imported as a bare module, not as part of the package
    from epics_hexapod import HexapodAxisController
    from gcs_hexapod import GcsAxisController
    from transport import DIRECT, EPICS, SIMULATED, TransportUnavailable
    from hexapod_traj import (
        PiHexapodTrajectory,
        TRAJ_ATTRS,
        TRAJ_METHODS,
        SimulatedTrajectory,
        TrajectoryUnavailable,
        make_trajectory_backend,
    )

# The C-887's own address, for the direct GCS connection. pihexapod carries a
# default too, but it has been wrong before; this is the verified one.
HEXAPOD_IP = "10.54.122.145"

# Base PV for pihexapod's GCS-over-EPICS passthrough, used only as a fallback
# when the direct connection fails. See hexapod_traj.AsynGcsTrajectory for why
# that path is experimental.
HEXAPOD_IOC = "12idbHXP"

# TrajectoryUnavailable is re-exported: a caller holding a facade catches it
# from here rather than reaching into hexapod_traj.
__all__ = [
    "HexapodFacade", "TrajectoryUnavailable", "TransportUnavailable",
    "make_hexapod",
]


HEXAPOD_AXES = ["X", "Y", "Z", "U", "V", "W"]
HEXAPOD_UNITS = ["mm", "mm", "mm", "deg", "deg", "deg"]


class HexapodFacade:
    """Step motion over EPICS, trajectories over GCS, behind one name."""

    axes = list(HEXAPOD_AXES)

    def __init__(self, axis_controller=None, traj=None, pv_class=None,
                 address=HEXAPOD_IP, ioc_prefix=HEXAPOD_IOC, connect=True,
                 traj_preference="auto"):
        self._axes_controller = axis_controller or HexapodAxisController(pv_class=pv_class)
        if connect:
            self._axes_controller.connect()
        self._traj = traj
        self._traj_preference = traj_preference
        self._address = address
        self._ioc_prefix = ioc_prefix
        self.motornames = list(self.axes)
        self.motorunits = list(HEXAPOD_UNITS)

    # -- trajectory transport --------------------------------------------------

    @property
    def traj(self):
        """The trajectory backend, resolved on first use and then cached."""
        if self._traj is None:
            self._traj = make_trajectory_backend(
                self._address, self._ioc_prefix, prefer=self._traj_preference,
                epics_ready=self._axes_controller.asyn_passthrough_status,
            )
        return self._traj

    @property
    def WaveGenID(self) -> dict:
        """The controller's wave-table ids, keyed by axis.

        Empty rather than raising when there is no transport: unlike the
        trajectory methods, this is read while deciding what to do, so a
        caller that has not checked can_fly should get a falsy answer, not
        an exception.
        """
        traj = self.traj
        return traj.WaveGenID if traj.available else {}

    @property
    def traj_resolved(self) -> bool:
        """Whether the trajectory transport has been looked up yet. Lets a
        caller check without triggering the connection."""
        return self._traj is not None

    @property
    def can_fly(self) -> bool:
        """Whether a fly scan can run. Resolves the transport if it has not
        been resolved - the answer is not knowable otherwise - but that
        happens once, at startup, not per scan."""
        return self.traj.available

    @property
    def traj_state(self) -> str:
        return self.traj.state

    @property
    def traj_reason(self) -> str:
        return self.traj.reason

    # -- connection ------------------------------------------------------------

    def connect(self) -> None:
        self._axes_controller.connect()

    def disconnect(self) -> None:
        self._axes_controller.disconnect()
        if self._traj is not None:
            disconnect = getattr(self._traj, "disconnect", None)
            if disconnect is not None:
                disconnect()

    def isconnected(self, axis="X"):
        return self._axes_controller.isconnected(axis)

    @property
    def connected(self) -> list:
        return self._axes_controller.connected

    def ioc_link_ok(self) -> bool:
        """False when the IOC is up but cannot reach the C-887 - in which
        case every readback is undefined rather than a position."""
        return self._axes_controller.ioc_link_ok()

    # -- position --------------------------------------------------------------

    def get_pos(self, axis=None):
        """Every axis as a dict when called bare, one axis as a float when
        named.

        The bare form is the older contract and is still what motions_ver2
        and ptychosaxs.scan() use, so both shapes are supported rather than
        one of them being migrated.
        """
        if axis is None:
            return {name: self._axes_controller.get_pos(name) for name in self.axes}
        return self._axes_controller.get_pos(axis)

    def set_pos(self, axis, pos=0) -> None:
        return self._axes_controller.set_pos(axis, pos)

    # -- motion ----------------------------------------------------------------

    def mv(self, *argv, wait=True, timeout=None) -> bool:
        """Move one or more axes: mv('X', 1.0) or mv('X', 1.0, 'Z', 2.0).

        Returns True on success. The bool is load-bearing - motions_ver2's
        mv() retries on a falsy return - so this must never return None.

        timeout bounds the wait. Leave it unset only outside a scan: the
        controller's own default is 300 s, long enough that a stalled axis
        would hold a scan thread rather than losing one point.
        """
        if len(argv) % 2:
            raise TypeError(
                f"mv() takes alternating axis and position arguments, got {argv!r}"
            )
        targets = list(zip(argv[0::2], argv[1::2]))
        if not targets:
            return True
        if timeout is None:
            return self._axes_controller.move_coordinated(targets, wait=wait)
        return self._axes_controller.move_coordinated(
            targets, wait=wait, timeout=timeout
        )

    def mv_many(self, targets, wait=True, timeout=None) -> bool:
        """Coordinated move from a list of (axis, target) pairs.

        Present so motions_ver2.mv_xy can treat this rig and the goniometer
        the same way.
        """
        if timeout is None:
            return self._axes_controller.move_coordinated(targets, wait=wait)
        return self._axes_controller.move_coordinated(targets, wait=wait, timeout=timeout)

    def mvr(self, axis, delta, wait=True, timeout=None) -> bool:
        pos = self._axes_controller.get_pos(axis)
        return self.mv(axis, pos + delta, wait=wait, timeout=timeout)

    def mvx(self, target, relative=False) -> bool:
        if relative:
            return self.mvr("X", target)
        return self.mv("X", target)

    def mvrx(self, target) -> bool:
        return self.mvx(target, relative=True)

    def stop(self, axis=None) -> None:
        for name in ([axis] if axis else self.axes):
            self._axes_controller.stop(name)

    def tweak(self, axis, step, forward=True) -> None:
        self._axes_controller.tweak(axis, step, forward=forward)

    def ismoving(self, axis) -> bool:
        return self._axes_controller.ismoving(axis)

    def isattarget(self, axis=""):
        return self._axes_controller.isattarget(axis)

    # -- servo -----------------------------------------------------------------

    def is_servo_on(self, axis="") -> bool:
        return self._axes_controller.is_servo_on(axis)

    def handle_error(self) -> bool:
        return self._axes_controller.handle_error()

    # -- speed -----------------------------------------------------------------

    def get_speed(self, axis=None) -> tuple:
        return self._axes_controller.get_speed(axis)

    def set_speed(self, axis=None, vel=1.0, acc=None) -> None:
        self._axes_controller.set_speed(axis, vel, acc)


def _traj_forward(name):
    def method(self, *args, **kwargs):
        return getattr(self.traj, name)(*args, **kwargs)
    method.__name__ = name
    method.__doc__ = (
        f"Forwards to the trajectory backend's {name}(). Raises "
        f"TrajectoryUnavailable when no transport could be resolved."
    )
    return method


def _traj_forward_attr(name):
    def getter(self):
        return getattr(self.traj, name)
    return property(getter, doc=f"Reads the trajectory backend's {name}.")


# Explicit forwards rather than __getattr__: these are greppable, they keep
# TrajectoryUnavailable (not AttributeError) as the failure, and __getattr__
# would break hasattr() probes in surprising ways.
for _name in TRAJ_METHODS:
    setattr(HexapodFacade, _name, _traj_forward(_name))
for _name in TRAJ_ATTRS:
    if _name != "WaveGenID":  # set when the transport resolves
        setattr(HexapodFacade, _name, _traj_forward_attr(_name))
del _name


def make_hexapod(transport=EPICS, pv_class=None, address=HEXAPOD_IP,
                 ioc_prefix=HEXAPOD_IOC, connect=True):
    """Build the hexapod on the given transport.

    "epics"     - motion through the 12idbHXP motor records, and the wave
                  generator resolved separately when a fly scan first asks
                  for it. Needs pyepics and an IOC, nothing else.
    "direct"    - one GCS connection serving both halves, as this code did
                  before the IOC existed. Needs pihexapod and a route to
                  the C-887.
    "simulated" - the production facade and axis controller over fake PVs,
                  so the move, tweak and deferred-move logic exercised
                  offline is the same code that runs on the beamline.

    Raises TransportUnavailable if "direct" is asked for and the library or
    the controller is not there; the caller decides whether to fall back.
    """
    if transport == SIMULATED:
        if pv_class is None:
            from debug_stubs import FakeMotorPV  # debug/ is on sys.path in debug mode
            pv_class = FakeMotorPV
        return HexapodFacade(pv_class=pv_class, traj=SimulatedTrajectory(),
                             connect=connect)

    if transport == DIRECT:
        device = _open_gcs_device(address)
        return HexapodFacade(
            axis_controller=GcsAxisController(device),
            traj=PiHexapodTrajectory(device, _wave_gen_id(), transport="direct",
                                     address=address),
            connect=connect,
        )

    return HexapodFacade(address=address, ioc_prefix=ioc_prefix, connect=connect)


def _open_gcs_device(address):
    """The vendor library's device object, or TransportUnavailable."""
    try:
        from pihexapod.gcs import Hexapod
    except ImportError as exc:
        raise TransportUnavailable(f"pihexapod is not installed ({exc})") from exc
    try:
        return Hexapod(address)
    except Exception as exc:
        raise TransportUnavailable(
            f"could not reach the C-887 at {address}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc


def _wave_gen_id():
    from pihexapod.gcs import WaveGenID

    return WaveGenID
