
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

HEXAPOD_AXES = ["X", "Y", "Z", "U", "V", "W"]
HEXAPOD_UNITS = ["mm", "mm", "mm", "deg", "deg", "deg"]
# Must track MOTOR_SLOTS in epics_gonio.py.
GONIO_AXES = ["trans1", "trans2", "tilt1", "tilt2"]
GONIO_UNITS = ["mm", "mm", "deg", "deg"]

acsIP = "10.54.122.157"

if not DEBUG_HEXAPOD:
    from pihexapod.gcs import Hexapod, plot_record, IP, WaveGenID

if not DEBUG_ACS:
    from acspy.control import Controller, Axis
    from acspy import acsc

    acscontroller = Controller("ethernet", 1)
    acscontroller.connect(acsIP)


class _SimulatedController:
    """Stand-in for a controller whose library is in debug mode.

    Positions are kept in-process so the motor panel still reads back and the
    tweak buttons still move something, but no hardware is touched. Scans are
    disabled GUI-side whenever any controller is simulated, so only the
    position/move surface the motor panel uses is implemented here.
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

class _HexapodDebug(_SimulatedController):
    """Simulated PI hexapod. get_pos() returns the whole axis dict, as the
    real one does."""

    def __init__(self):
        super().__init__(HEXAPOD_AXES, HEXAPOD_UNITS)
        self.axes = self.motornames
        self.WaveGenID = {}

    def is_servo_on(self, axis):
        return True

    def isconnected(self, axis="X"):
        return True

    def get_pos(self):
        return dict(self._pos)

    def mv(self, axis, target, wait=True):
        self._pos[axis] = float(target)
        return True

    def mvr(self, axis, target):
        return self.mv(axis, self._pos[axis] + float(target))

    def isattarget(self, axis=None):
        return True

    def handle_error(self):
        return True

    def get_speed(self, axis=None):
        return 1.0

    def set_speed(self, vel=1):
        pass

    def set_pos(self, axis, pos=0):
        pass


class _PhiDebug(_SimulatedController):
    """Simulated ACS phi axis. Scalar get_pos() and target-only mv(), as the
    real one has."""

    def __init__(self):
        super().__init__(["phi"], ["deg"])
        self.axisno = 0

    def isconnected(self, axis="phi"):
        return True

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


if DEBUG_HEXAPOD:
    hexapod = _HexapodDebug
else:

    class hexapod(Hexapod):

        def __init__(self):
            super().__init__(IP)
            self.motornames = self.axes
            self.motorunits = list(HEXAPOD_UNITS)
            self.connected = [True, True, True, True, True, True]
            self.WaveGenID = WaveGenID

        def mvx(self, target, relative=False):
            if relative:
                pos = self.get_pos()
                target += pos['X']
            self.mv('X', target)

        def mvrx(self, target):
            self.mvx(target, relative=True)

        def ismoving(self, axis):
            ismoving = not self.isattarget(axis)
            return ismoving

        def mvr(self, axis, target):
            pos = self.get_pos()
            prevpos = pos[axis]
            abstarget = prevpos+target
            return self.mv(axis, abstarget)

        def set_pos(self, axis, pos=0):
            pass


if DEBUG_ACS:
    phi = _PhiDebug
else:

    class phi(Axis):
        def __init__(self):
            super().__init__(acscontroller, 0)
            self.motornames = ["phi"]
            self.motorunits = ["deg"]
            self.axisno = 0
            self.controller = acscontroller

        def commutate(self):
            acsc.commutate(acscontroller.hc, self.axisno, wait=acsc.SYNCHRONOUS)

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
            #self.control["phi"] = Axis(acscontroller, 0)

        def isconnected(self, axis = 'X'):
            return acsc.getMotorEnabled(acscontroller.hc, 0)

class motors(object):
    def __init__(self):

        if DEBUG_GONIO:
            gonio = _GonioDebug()
        else:
            from ptychosaxs.epics_gonio import GonioAxisController

            gonio = GonioAxisController()
            gonio.connect()

        self.control = {}
        self.control["hexapod"]= hexapod()
        self.control["phi"]= phi()
        self.control["gonio"]= gonio
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
            self.connected.append(self.control["hexapod"].is_servo_on(m))
        
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
        if controller == 'hexapod':
            pos = con.get_pos()
            pos = float(pos[axis])
        if controller == 'phi':
            pos = con.get_pos()
        if controller == 'gonio':
            pos = con.get_pos(axis)
        return round(pos, 6)
    
    def mv(self, axis, target, wait=True):
        indx = self.motornames.index(axis)
        controller = self.controller[indx]
        con = self.control[controller]
        self.signals.AxisNameSignal.emit(axis)
        if controller == 'hexapod':
            status = False
            while not status:
                status = con.mv(axis, target)
                if not status:
                    status = con.handle_error()
                    print("Hexapod error, trying to servo back on.")
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

    def get_speed(self, axis):
        indx = self.motornames.index(axis)
        controller = self.controller[indx]
        con = self.control[controller]
        if controller == 'hexapod':
            vel = con.get_speed()
            return vel, None
        else:
            vel, acc = con.get_speed(axis)
        return vel, acc
    
    def set_speed(self, axis, vel=1, acc=1):
        indx = self.motornames.index(axis)
        controller = self.controller[indx]
        con = self.control[controller]
        if controller == 'hexapod':
            vel = con.set_speed(vel)
        else:
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

    def connect(self, axis):
        self.connected = []
        for i, m in enumerate(self.control["hexapod"].motornames):
            self.isconnected.append(self.control["hexapod"].is_servo_on(m))
        
        for i, m in enumerate(self.control['phi'].motornames):
            self.isconnected.append(self.control["phi"].isconnected())

        iscon = self.control["gonio"].isconnected()
        for i, m in enumerate(self.control["gonio"].motornames):
            self.isconnected.append(iscon[i])
    
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