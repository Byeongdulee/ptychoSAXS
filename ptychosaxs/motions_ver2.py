
import os
import sys
import time
import numpy as np
from PyQt5.QtCore import QObject, pyqtSignal

# ==========================================================================
# Per-controller debug flags
# ==========================================================================
# These mirror the editable DEBUG_* booleans at the top of gui/rungui.py,
# which exports them into the environment before importing this module. When
# a flag is on, that controller's hardware library is never imported and a
# simulated stand-in is used instead, so nothing is connected to.
#
#   PTYCHOSAXS_DEBUG_HEXAPOD   pihexapod.gcs   axes: X, Y, Z, U, V, W
#   PTYCHOSAXS_DEBUG_ACS       acspy           axes: phi
#   PTYCHOSAXS_DEBUG_GONIO     epics_gonio     axes: trans1, trans2, tilt1, tilt2
DEBUG_HEXAPOD = os.environ.get("PTYCHOSAXS_DEBUG_HEXAPOD") == "1"
DEBUG_ACS = os.environ.get("PTYCHOSAXS_DEBUG_ACS") == "1"
DEBUG_GONIO = os.environ.get("PTYCHOSAXS_DEBUG_GONIO") == "1"

# The hexapod's axes and units live with the rig config in
# ptychosaxs/hexapod.py, which is also where they are read from the IOC.

# How many times mv() may re-enable the servo and retry before giving up.
# This used to be unbounded, which hung the scan thread with no message when
# an axis would not come back.
MOVE_RECOVERY_ATTEMPTS = 3

# Ceiling on one blocking move, matching the poll loop in mv(). Without it a
# controller that blocks on put-completion would use its own default of 300 s
# and, with the retries above, could hold a scan thread for twenty minutes.
MOVE_TIMEOUT_S = 10
# Must track MOTOR_SLOTS in epics_gonio.py.
GONIO_AXES = ["trans1", "trans2", "tilt1", "tilt2"]
GONIO_UNITS = ["mm", "mm", "deg", "deg"]

acsIP = "10.54.122.157"

# Absolute, not relative, and deliberately so. This module is importable
# both as ptychosaxs.motions_ver2 and as a bare motions_ver2, and the
# builders below reach the hexapod through `from ptychosaxs.hexapod import
# ...`. Under the bare import a relative-then-bare shim here would bind
# `transport.TransportUnavailable` while hexapod.py raises
# `ptychosaxs.transport.TransportUnavailable` - two different classes, so
# the except clause in build_controller would not catch its own exception.
from ptychosaxs.transport import (
    EPICS, SIMULATED, TransportUnavailable, describe, resolve,
)

# acspy is imported and the controller connected inside _build_phi, not
# here. At module scope it meant importing motions_ver2 at all - from a
# test, or a machine with no ACS crate - opened a network connection as a
# side effect, and made the module unimportable without the library.
_acs_controller = None


def _acs_connect():
    """Connect to the ACS controller once, on first use."""
    global _acs_controller
    if _acs_controller is None:
        from acspy.control import Controller

        controller = Controller("ethernet", 1)
        controller.connect(acsIP)
        _acs_controller = controller
    return _acs_controller


class _SimulatedController:
    """Stand-in for a controller whose library is in debug mode.

    Positions are kept in-process so the motor panel still reads back and the
    tweak buttons still move something, but no hardware is touched. Scans are
    disabled GUI-side whenever any controller is simulated, so only the
    position/move surface the motor panel uses is implemented here.

    Used by phi and the goniometer. The hexapod does not need one: it runs
    the production controller over FakeMotorPV in debug mode, so the move
    logic exercised offline is the code that runs on the beamline.
    """

    def __init__(self, names, units):
        self.motornames = list(names)
        self.motorunits = list(units)
        self.connected = [True] * len(names)
        self._pos = {name: 0.0 for name in names}

    def ismoving(self, axis=None):
        return False

    def set_pos(self, axis, pos=0):
        self._pos[axis] = float(pos)

    def connect(self):
        pass

    def disconnect(self):
        pass

class motorSignals(QObject):
    AxisNameSignal = pyqtSignal(str)
    AxisPosSignal = pyqtSignal(float)


def generate_raster_scan_positions(size):
    x_positions = []
    y_positions = []
    
    for i in range(size):
        if i % 2 == 0:  # Even rows
            for j in range(size):
                x_positions.append(j)
                y_positions.append(i)
        else:  # Odd rows
            for j in range(size-1, -1, -1):  # Reverse iteration for odd rows
                x_positions.append(j)
                y_positions.append(i)
    return np.array(x_positions), np.array(y_positions)

class _PhiDebug(_SimulatedController):
    """Simulated ACS phi axis. Scalar get_pos() and target-only mv(), as the
    real one has."""

    def __init__(self):
        super().__init__(["phi"], ["deg"])
        self.axisno = 0

    def isconnected(self, axis="phi"):
        """Reports as not connected, on purpose.

        phi is optional hardware and the motor panel already has a path for
        it being absent: the slot stays (so the stages after it do not
        shift), the row is disabled, and a warning names it. A simulated phi
        takes that same path rather than disabling every scan, which is what
        treating it like the other motors used to do.
        """
        return False

    def get_pos(self, axis=0):
        return round(self._pos["phi"], 3)

    def mv(self, target, relative=False):
        base = self._pos["phi"] if relative else 0.0
        self._pos["phi"] = base + float(target)

    def mvr(self, val, **kwargs):
        self.mv(val, relative=True)

    def commutate(self):
        pass

    def get_speed(self, axis=None):
        return 1.0, 1.0

    def set_speed(self, axis, vel=1, acc=1):
        pass

    def set_pos(self, axis, pos=0):
        self._pos["phi"] = float(pos)


class _GonioDebug(_SimulatedController):
    """Simulated EPICS goniometer. Per-axis get_pos() and list-returning
    isconnected(), as the real one has."""

    def __init__(self):
        super().__init__(GONIO_AXES, GONIO_UNITS)
        self.channel_names = self.motornames
        self.units = self.motorunits

    def isconnected(self, ax=-1):
        if isinstance(ax, int) and ax > -1:
            return True
        return [True] * len(self.motornames)

    def _name(self, axis):
        return axis if isinstance(axis, str) else self.motornames[axis]

    def get_pos(self, axis):
        return self._pos.get(self._name(axis), 0.0)

    def mv(self, axis, target, wait=True):
        self._pos[self._name(axis)] = float(target)
        return True

    def mv_many(self, targets, wait=True, timeout=None):
        for axis, target in targets:
            self._pos[self._name(axis)] = float(target)
        return True

    def get_speed(self, axis):
        return (1.0, None)

    def set_speed(self, axis, vel=1, acc=None):
        pass


def _build_hexapod(transport):
    """The hexapod: six axes of motion plus the fly-scan trajectory.

    "epics" drives the 12idbHXP motor records and reaches the wave
    generator over GCS separately; "direct" uses one GCS connection for
    both. See ptychosaxs/hexapod.py.
    """
    from ptychosaxs.hexapod import make_hexapod

    return make_hexapod(transport)


def _build_gonio(transport):
    """The goniometer's four stages.

    Two drivers, chosen by rungui's GONIO_USE_SMARACT: the 12ideMCS1 motor
    records, or the SmarAct MCS2 SDK through ptychosaxs/smaract_gonio.py
    (restored from commit e9f219a^ for the machine wired to the crate).
    Nothing is tunnelled - when the SDK is selected EPICS is not involved
    at all. See direct_gonio.py for what the adapter covers.
    """
    if transport == SIMULATED:
        return _GonioDebug()
    if transport == EPICS:
        from ptychosaxs.epics_gonio import GonioAxisController

        gonio = GonioAxisController()
        gonio.connect()
        return gonio
    from ptychosaxs.direct_gonio import SmaractGonioController

    try:
        # smaract_gonio opens the MCS2 at import time, so constructing the
        # adapter is where the connection actually happens.
        return SmaractGonioController()
    except Exception as exc:
        raise TransportUnavailable(
            f"the SmarAct SDK or the MCS2 is not reachable here: "
            f"{type(exc).__name__}: {exc}"
        ) from exc


def _build_phi(transport):
    """The tomography rotation stage.

    acspy is the only driver - no IOC exists for it - so there is no switch
    for this one. When an IOC appears, add EPICS to CHOICES["phi"] in
    transport.py and give it a branch here.
    """
    if transport == SIMULATED:
        return _PhiDebug()

    try:
        from acspy.control import Axis
        from acspy import acsc
    except ImportError as exc:
        raise TransportUnavailable(f"acspy is not installed ({exc})") from exc
    controller = _acs_connect()


    class phi(Axis):
        def __init__(self):
            super().__init__(controller, 0)
            self.motornames = ["phi"]
            self.motorunits = ["deg"]
            self.axisno = 0
            self.controller = controller

        def commutate(self):
            acsc.commutate(controller.hc, self.axisno, wait=acsc.SYNCHRONOUS)

        def mv(self, target, relative=False):
            try:
                if self.enabled == False:
                    self.enable()
                if relative:
                    c = "relative"
                else:
                    c = "absolute"
                self.ptp(target=target, coordinates=c)
            except acsc.AcscError as Err:
                if '3261:' in Err:
                    print("phi was not commutated, and is being commutated. Please wait.")
                    self.commutate()

        def mvr(self, val, **kwargs):
            self.mv(val, relative=True, **kwargs)

        def ismoving(self, axis):
            ismoving = not self.in_position
            return ismoving

        def get_pos(self, axis=0):
            return round(float(self.fpos), 3)

        def get_speed(self, axis):
            return self.vel, self.acc

        def set_speed(self, axis, vel=1, acc=1):
            self.vel = vel
            self.acc = acc

        def set_pos(self, axis, pos=0):
            acsc.setRPosition(self.controller.hc, self.axisno, pos)

        def disconnect(self):
            self.controller.disconnect()

        def connect(self):
            self.controller.connect(acsIP)
            #self.control["phi"] = Axis(controller, 0)

        def isconnected(self, axis = 'X'):
            return acsc.getMotorEnabled(controller.hc, 0)

    return phi()


_BUILDERS = {
    "hexapod": _build_hexapod,
    "phi": _build_phi,
    "gonio": _build_gonio,
}


def build_controller(name, simulated=False):
    """(controller, transport) for one stage, never raising.

    Resolves the configured transport, builds it, and on failure falls back
    to a simulated stand-in with the reason printed. The fallback is the
    point: a machine that can only reach some of the hardware should still
    bring up the GUI and drive the rest.
    """
    transport, why = resolve(name, simulated=simulated)
    if why:
        print(f"{name}: {why}; using a simulated stand-in.")
    try:
        return _BUILDERS[name](transport), transport
    except TransportUnavailable as exc:
        print(f"{name}: {transport} transport unavailable - {exc}. "
              f"Using a simulated stand-in.")
        return _BUILDERS[name](SIMULATED), SIMULATED


class motors(object):
    def __init__(self):

        # Each stage independently: its configured transport when that is
        # usable here, a simulated stand-in when it is not. self.transports
        # records what each one actually ended up on, so the GUI can say.
        self.control = {}
        self.transports = {}
        for name, simulated in (
            ("hexapod", DEBUG_HEXAPOD),
            ("phi", DEBUG_ACS),
            ("gonio", DEBUG_GONIO),
        ):
            controller, transport = build_controller(name, simulated=simulated)
            self.control[name] = controller
            self.transports[name] = transport
            print(f"{name}: {describe(name, transport)}")
        #self.control["beamstop"]= beamstop()
        self.motornames = []
        self.motorunits = []
        self.motorindices = []
        self.controller = []
        self.connected = []
        for i, m in enumerate(self.control["hexapod"].motornames):
            self.motornames.append(m)
            self.motorunits.append(self.control["hexapod"].motorunits[i])
            self.controller.append('hexapod')
            self.motorindices.append(i)
            # Channel Access reachability, not servo state: an axis whose
            # servo is off is still a real axis and belongs in the panel.
            # Seeding this from is_servo_on() made startup fail outright.
            self.connected.append(self.control["hexapod"].isconnected(i))
        
        for i, m in enumerate(self.control['phi'].motornames):
            self.motornames.append(m)
            self.motorunits.append(self.control["phi"].motorunits[i])
            self.controller.append('phi')
            self.motorindices.append(i)
            self.connected.append(self.control["phi"].isconnected())

        iscon = self.control["gonio"].isconnected()
        for i, m in enumerate(self.control["gonio"].motornames):
            self.motornames.append(m)
            self.motorunits.append(self.control["gonio"].motorunits[i])
            self.controller.append('gonio')
            self.motorindices.append(i)
            self.connected.append(iscon[i])
        # for i, m in enumerate(self.control["beamstop"].motors):
        #     self.motornames.append(m.DESC)
        #     self.motorunits.append(m.EGU)
        #     self.controller.append('beamstop')
        #     self.motorindices.append(i)

        self.signals = motorSignals()

    def ismoving(self, axis):
        indx = self.motornames.index(axis)
        controller = self.controller[indx]
        return self.control[controller].ismoving(axis)

    def get_pos(self, axis):
        indx = self.motornames.index(axis)
        controller = self.controller[indx]
        con = self.control[controller]
        if controller == 'phi':
            pos = con.get_pos()  # single axis, so it takes no name
        else:
            pos = con.get_pos(axis)
        return round(pos, 6)
    
    def mv(self, axis, target, wait=True):
        indx = self.motornames.index(axis)
        controller = self.controller[indx]
        con = self.control[controller]
        self.signals.AxisNameSignal.emit(axis)
        if controller == 'hexapod':
            status = con.mv(axis, target, wait=wait, timeout=MOVE_TIMEOUT_S)
            for _ in range(MOVE_RECOVERY_ATTEMPTS):
                if status:
                    break
                print("Hexapod error, trying to servo back on.")
                if not con.handle_error():
                    break  # recovery itself failed - retrying cannot help
                status = con.mv(axis, target, wait=wait, timeout=MOVE_TIMEOUT_S)
            if not status:
                raise RuntimeError(
                    f"Hexapod could not move {axis} to {target} after "
                    f"{MOVE_RECOVERY_ATTEMPTS} recovery attempts."
                )
        if controller == "phi":
            con.mv(target)
        if controller == "gonio":
            con.mv(axis, target, wait=wait)
        TIMEOUT = 10
        t0 = time.time()
        if wait:
            ismoving = True
            time.sleep(0.01)
            while ismoving:
                pos = self.get_pos(axis)
                self.signals.AxisPosSignal.emit(pos)
                ismoving = self.ismoving(axis)
                time.sleep(0.01)
                if (time.time()-t0) > TIMEOUT:
                    raise TimeoutError

    def mvr(self, axis, target, wait=True):
        pos = self.get_pos(axis)
        self.mv(axis, pos+target, wait=wait)

    def controller_for(self, axis):
        """The controller object that owns this axis."""
        return self.control[self.controller[self.motornames.index(axis)]]

    def can_coordinate(self, xaxis, yaxis) -> bool:
        """Whether these two axes can be started as one motion."""
        xcon = self.controller_for(xaxis)
        return xcon is self.controller_for(yaxis) and hasattr(xcon, "mv_many")

    def mv_xy(self, xaxis, xp, yaxis, yp, wait=True, timeout=None):
        """Move a scan's two grid axes to one point.

        One coordinated move when both axes are on a controller that can
        start them together, two sequential moves otherwise. Moving them one
        after the other walks the stage through a corner on the way to every
        point and pays two settle times, so the coordinated path is worth
        taking wherever it exists.

        Which path applies is the controller's business, not the scan
        engine's - that is what lets the scan axes be swapped.

        timeout bounds the coordinated path only; the sequential one is
        bounded by mv()'s own poll loop.
        """
        if self.can_coordinate(xaxis, yaxis):
            mv_many = self.controller_for(xaxis).mv_many
            self.signals.AxisNameSignal.emit(xaxis)
            targets = [(xaxis, xp), (yaxis, yp)]
            if timeout is None:
                return mv_many(targets, wait=wait)
            return mv_many(targets, wait=wait, timeout=timeout)
        self.mv(xaxis, xp, wait=wait)
        self.mv(yaxis, yp, wait=wait)
        return True

    def get_speed(self, axis):
        indx = self.motornames.index(axis)
        controller = self.controller[indx]
        con = self.control[controller]
        vel, acc = con.get_speed(axis)
        return vel, acc
    
    def set_speed(self, axis, vel=1, acc=1):
        indx = self.motornames.index(axis)
        controller = self.controller[indx]
        con = self.control[controller]
        con.set_speed(axis, vel, acc)
    
    def set_pos(self, axis, pos=0):
        indx = self.motornames.index(axis)
        controller = self.controller[indx]
        con = self.control[controller]
        con.set_pos(axis, pos)
        
    def disconnect(self, axis):
        indx = self.motornames.index(axis)
        controller = self.controller[indx]
        con = self.control[controller]
        con.disconnect()

    def connect(self, axis=None):
        """Re-read every axis's reachability into self.connected.

        Previously appended to self.isconnected, which is a method, so this
        raised AttributeError on its first iteration and left self.connected
        empty. It now rebuilds the list isconnected() indexes into.
        """
        connected = []
        hexapod_con = self.control["hexapod"]
        for i, _m in enumerate(hexapod_con.motornames):
            connected.append(hexapod_con.isconnected(i))

        for _i, _m in enumerate(self.control["phi"].motornames):
            connected.append(self.control["phi"].isconnected())

        iscon = self.control["gonio"].isconnected()
        for i, _m in enumerate(self.control["gonio"].motornames):
            connected.append(iscon[i])

        self.connected = connected
        return self.connected
    
    def isconnected(self, axis = 'X'):
        indx = self.motornames.index(axis)
        return self.connected[indx]
    
    @property
    def hexapod(self):
        return self.control["hexapod"]    
    @property
    def phi(self):
        return self.control["phi"]    
    @property
    def gonio(self):
        return self.control["gonio"]