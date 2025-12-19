
from PyQt5.QtCore import QObject, pyqtSignal
from pihexapod.gcs import Hexapod, plot_record, IP, WaveGenID
acsIP = "10.54.122.157"
from acspy.control import Controller, Axis
from acspy import acsc
acscontroller = Controller("ethernet", 1)
acscontroller.connect(acsIP)
import sys
import time
import numpy as np

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

class hexapod(Hexapod):

    def __init__(self):
        super().__init__(IP)
        self.motornames = self.axes
        self.motorunits = ["mm","mm","mm","deg","deg","deg"]
        self.connected = [True,True,True,True,True,True]
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

    # def get_pos(self, axis=""):
    #     pos = self.get_pos()
    #     if len(axis) == 0:
    #         return pos
    #     else:
    #         return float(pos[axis])
    
    def mvr(self, axis, target):
        pos = self.get_pos()
        prevpos = pos[axis]
        abstarget = prevpos+target
        return self.mv(axis, abstarget)

    # def get_speed(self, axis=0):
    #     return self.get_speed(), None
    
    # def set_speed(self, axis=0, vel=1, acc=1):
    #     self.set_speed(vel)
    
    def set_pos(self, axis, pos=0):
        pass        

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
        
        import smaract_gonio as gonio

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
        #controller = self.controller[indx]
        #con = self.control[controller]
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