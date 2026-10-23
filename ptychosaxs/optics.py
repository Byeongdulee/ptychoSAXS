"""The upstream optics motors, on the modern EPICS motor-record driver.

These were the last users of ptychosaxs/epicsmotor.py -- the original
epics.Motor wrapper, with 1-based axis indices, busy-wait moves and a
PyQt5 import in the device layer. Keeping them on it meant a headless
server could not read an optics position without Qt installed.

They are the same four groups, in the same order, driving the same records:
the panel addresses a motor as (group, index) and those indices have not
moved. What changed underneath is the driver -- EpicsMotorController reads
through pyepics monitors instead of polling, waits on put-completion
instead of sleeping in 10 ms steps, and takes an injectable PV class so the
whole thing is testable with no Channel Access at all.

Record layout lives in ptychosaxs/optics_config.py, shared with
gui/optics_status.py so the two cannot disagree about which motor is which.
"""

from ptychosaxs import optics_config
from ptychosaxs.epics_motor import EpicsMotorController


class _OpticsGroup(EpicsMotorController):
    """One optics controller, with the surface the optics panel expects.

    The panel reaches for `motornames[index]` and then calls get_pos, mv and
    mvr with that name, so those keep working unchanged. `wait` is accepted
    on mvr because the panel passes it; EpicsMotorController.mvr is already
    non-blocking, which is what the panel asks for.
    """

    GROUP = None

    def __init__(self, pv_class=None, connect=True):
        super().__init__(optics_config.slots_for(self.GROUP), pv_class=pv_class)
        if connect:
            self.connect()

    @property
    def motornames(self):
        return self.names

    @property
    def motorunits(self):
        return [self.get_unit(name) for name in self.names]

    def mvr(self, axis, delta, wait=False):
        super().mvr(axis, delta)
        if wait:
            self._settle(axis)

    def mv(self, axis, target, wait=False):
        super().mv(axis, target, wait=wait)

    def _settle(self, axis, timeout=60.0):
        """Block until the axis reports done.

        Only used when a caller explicitly asks mvr to wait. The panel does
        not; it polls positions on its own.
        """
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.is_moving(axis):
                return True
            time.sleep(0.02)
        return False


class opticsbox(_OpticsGroup):
    """Beamstop and zone-plate translations, plus the shared Z stage."""
    GROUP = "opticsbox"


class OSA(_OpticsGroup):
    """Order-sorting aperture: X, Z, Y."""
    GROUP = "OSA"


class saxs_z(_OpticsGroup):
    """SAXS detector Z -- the sample-to-detector distance.

    NeXus records this as /entry/instrument/detector/distance. Not the
    optical camera, which is a different stage entirely.
    """
    GROUP = "saxs_z"


class beamstop(_OpticsGroup):
    """SAXS beamstop, horizontal and vertical."""
    GROUP = "beamstop"


#: Every optics record, for callers that want the flat list.
ALL_MOTORS = list(optics_config.PV_BASES.values())

# Removed with the move off epicsmotor: ptyoptics, camera, gentry, slit and
# slit_CRL. None of them was imported anywhere -- only opticsbox, OSA,
# saxs_z and beamstop are. The two slit classes additionally could not move
# to a motor-record driver at all: they drove "<base>size.VAL" and
# "<base>size_tweak.A/B", which are not motor records.
