try:
    from pihexapod.gcs import Hexapod, plot_record, IP, WaveGenID

    acsIP = "10.54.122.157"
    hexapodIP = IP
    hexapod = Hexapod(IP)
    hexapod.connected = [True,True,True,True,True,True]
    hexapod.WaveGenID = WaveGenID
except:
    hexapod = Hexapod
    hexapod.connected = [False,False,False,False,False,False]
    hexapod.WaveGenID = WaveGenID

ishexpodavailable = True
try:
    hexapod.get_pos()
except:
    ishexpodavailable = False
#hexapod.axis_names = ["X","Y","Z","U","V","W"]

try:
    from acspy.control import Controller, Axis
    from acspy import acsc
    from types import MethodType

    acscontroller = Controller("ethernet", 1)
    acscontroller.connect(acsIP)

    phi = Axis(acscontroller, 0)
    phi.connected = True
    acsc.commutate(phi.controller.hc, phi.axisno)
except:
    phi = Axis
    phi.connected = False

from PyQt5.QtCore import QObject, pyqtSignal

class motorSignals(QObject):
    AxisNameSignal = pyqtSignal(str)
    AxisPosSignal = pyqtSignal(float)

import time
import numpy as np
import matplotlib.pyplot as plt
from epics import Motor


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


import smaract_gonio

class gonio(object):

    def __init__(self):
        self.connected = smaract_gonio.isconnected()
        self.motornames = smaract_gonio.channel_names
        self.motorunits = smaract_gonio.units
        

    def ismoving(self, axis):
        ch = self.channel_names.index(axis)
        ismoving = smaract_gonio.ismoving(ch)
        return ismoving

    def get_pos(self, axis):
        ch = smaract_gonio.channel_names.index(axis)
        pos = smaract_gonio.get_pos(ch)
        return pos

    def mv(self, axis, target):
        ch = smaract_gonio.channel_names.index(axis)
        smaract_gonio.mv(ch, target, wait=False)

    def mvr(self, axis, target):
        ch = smaract_gonio.channel_names.index(axis)
        smaract_gonio.mvr(ch, target, wait=False)

    def get_speed(self, axis):
        ch = smaract_gonio.channel_names.index(axis)
        vel,acc = smaract_gonio.get_speed(ch)
        return vel, acc
    
    def set_speed(self, axis, vel=1, acc=1):
        ch = smaract_gonio.channel_names.index(axis)
        smaract_gonio.set_speed(ch, vel, acc)
    
    def set_pos(self, axis, pos=0):
        ch = smaract_gonio.channel_names.index(axis)
        smaract_gonio.set_pos(ch, pos)
        
    def isconnected(self, axis = 'X'):
        ax = smaract_gonio.channel_names.index(axis)
        return self.connected[ax]

class hexa(object):

    def __init__(self):
        self.motornames = hexapod.axes
        self.motorunits = ["mm","mm","mm","deg","deg","deg"]

    def mvx(self, target, relative=False):
        if relative:
            pos = hexapod.get_pos()
            target += pos['X']
        hexapod.mv('X', target)

    def mvrx(self, target):
        self.mvx(target, relative=True)

    def ismoving(self, axis):
        ismoving = not hexapod.isattarget(axis)
        return ismoving

    def get_pos(self, axis=""):
        pos = hexapod.get_pos()
        if len(axis) == 0:
            return pos
        else:
            return float(pos[axis])
    
    def mv(self, *args):
        status = hexapod.mv(*args)
        return status

    def mvr(self, axis, target):
        pos = hexapod.get_pos()
        prevpos = pos[axis]
        abstarget = prevpos+target
        return hexapod.mv(axis, abstarget)

    def get_speed(self, axis=0):
        return hexapod.get_speed(), None
    
    def set_speed(self, axis=0, vel=1, acc=1):
        hexapod.set_speed(vel)
    
    def set_pos(self, axis, pos=0):
        pass        

    
class rotation(object):
    def __init__(self):
        self.motornames = ["phi"]
        self.motorunits = ["deg"]
        self.axisno = 0
    
    def commutate(self):
        acsc.commutate(acscontroller.hc, self.axisno, wait=acsc.SYNCHRONOUS)

    def mv(self, target, relative=False, **kwargs):
        try:
            if phi.enabled == False:
                phi.enable()
            if relative:
                c = "relative"
            else:
                c = "absolute"        
            if wait:
                wait = acsc.SYNCHRONOUS
            else:
                wait = acsc.ASYNCHRONOUS
            self.control["phi"].ptp(target=target, coordinates=c, **kwargs)
        except acsc.AcscError as Err:
            if '3261:' in Err:
                print("phi was not commutated, and is being commutated. Please wait.")
                self.commutate()

    def mvr(self, val, **kwargs):
        self.mv(val, relative=True, **kwargs)

    def ismoving(self, axis):
        ismoving = not phi.in_position
        return ismoving

    def get_pos(self, axis=0):
        return float(self.pos)
    
    def get_speed(self, axis):
        return phi.vel, phi.acc
    
    def set_speed(self, axis, vel=1, acc=1):
        phi.vel = vel
        phi.acc = acc
    
    def set_pos(self, axis, pos=0):
        acsc.setRPosition(phi.controller.hc, self.axisno, pos)
        
    def disconnect(self):
        phi.controller.disconnect()

    def connect(self):
        phi.controller.connect(acsIP)
        #self.control["phi"] = Axis(acscontroller, 0)
    
    def isconnected(self, axis = 'X'):
        phi.connected

class motors(object):
    def __init__(self):

        self.control = {}
        self.control["hexapod"]= hexapod
        self.control["phi"]= phi
        self.control["gonio"]= gonio
        #self.control["beamstop"]= beamstop()
        self.motornames = []
        self.motorunits = []
        self.motorindices = []
        self.controller = []
        for i, m in enumerate(self.control["hexapod"].axes):
            self.motornames.append(m)
            if i<3:
                unit = 'mm'
            else:
                unit = 'deg'
            self.motorunits.append(unit)
            self.controller.append('hexapod')
            self.motorindices.append(i)
        
        for i in range(1):
            self.motornames.append('phi')
            self.motorunits.append('deg')
            self.controller.append('phi')
            self.motorindices.append(i)
        
        for i, m in enumerate(self.control["gonio"].motornames):
            self.motornames.append(m)
            self.motorunits.append(self.control["gonio"].motorunits[i])
            self.controller.append('gonio')
            self.motorindices.append(i)
        # for i, m in enumerate(self.control["beamstop"].motors):
        #     self.motornames.append(m.DESC)
        #     self.motorunits.append(m.EGU)
        #     self.controller.append('beamstop')
        #     self.motorindices.append(i)

        self.signals = motorSignals()

    def commutate_phi(self):
        acsc.commutate(acscontroller.hc, self.control["phi"].axisno, wait=acsc.SYNCHRONOUS)

    def mvphi(self, target, relative=False, wait=True):
        try:
            if self.control["phi"].enabled == False:
                self.control["phi"].enable()
            if relative:
                c = "relative"
            else:
                c = "absolute"        
            if wait:
                wait = acsc.SYNCHRONOUS
            else:
                wait = acsc.ASYNCHRONOUS
            self.control["phi"].ptp(target=target, coordinates=c, wait=wait)
        except acsc.AcscError as Err:
            if '3261:' in Err:
                print("phi was not commutated, and is being commutated. Please wait.")
                self.commutate_phi()

    def mvrphi(self, val, wait=True):
        self.mvphi(val, relative=True, wait=wait)

    def mvx(self, target, relative=False):
        if relative:
            pos = self.control["hexapod"].get_pos()
            target += pos['X']
        self.control["hexapod"].mv('X', target)

    def mvrx(self, target):
        self.mvx(target, relative=True)

    def ismoving(self, axis):
#        print("now in is moving")
        if 'epicsmotors' in axis:
            if axis == "epicsmotors1":
                n = 0
            if axis == "epicsmotors2":
                n = 1
            if axis == "epicsmotors3":
                n = 2
            dmov = self.epicsmotors[n].get('DMOV')
            if dmov==0:
                ismoving = True
            if dmov==1:
                ismoving = False
        if axis == "phi":
            ismoving = not self.control["phi"].in_position
        if axis in self.control["hexapod"].axes:
            ismoving = not self.control["hexapod"].isattarget(axis)
        if axis in self.gonio.channel_names:
            ch = self.gonio.channel_names.index(axis)
            ismoving = self.gonio.ismoving(ch)
        return ismoving

    def get_pos(self, axis):
        print(axis, " this is the name of axis")
        if 'epicsmotors' in axis:
            if axis == "epicsmotors1":
                n = 0
            if axis == "epicsmotors2":
                n = 1
            if axis == "epicsmotors3":
                n = 2
            pos = self.epicsmotors[n].get('VAL')
            return pos
        if axis == "phi":
            return float(self.posphi)
        if axis in self.control["hexapod"].axes:
            if ishexpodavailable:
                pos = self.control["hexapod"].get_pos()
            else:
                pos = {"X":-999,"Y":-999,"Z":-999,"U":-999,"V":-999,"W":-999}
            return float(pos[axis])
        if axis in self.gonio.channel_names:
            ch = self.gonio.channel_names.index(axis)
            pos = self.gonio.get_pos(ch)
            return pos
    
    def mv_hex(self, *args, wait=True):
        self.control["hexapod"].mv(*args)
        if wait:
            time.sleep(0.01)
            while True:
                if self.control["hexapod"].isattarget():
                    break
                time.sleep(0.01)

    def mv(self, axis, target, wait=True):
        self.signals.AxisNameSignal.emit(axis)
        t0 = time.time()
        if axis == "phi":
            self.mvphi(target)
            if wait:
                ismoving = True
                time.sleep(0.01)
                while ismoving:
                    self.signals.AxisPosSignal.emit(float(self.posphi))
                    ismoving = self.ismoving(axis)
                    time.sleep(0.01)
        if 'epicsmotors' in axis:
            if axis == "epicsmotors1":
                n = 0
            if axis == "epicsmotors2":
                n = 1
            if axis == "epicsmotors3":
                n = 2
            print(n, " in epicsmotors")
            self.epicsmotors[n].move(target)
            isstarted = False
            if not isstarted:
                isstarted = self.ismoving(axis)
                time.sleep(0.01)

            if wait:
                ismoving = True
                time.sleep(0.01)
                while ismoving:
                    self.signals.AxisPosSignal.emit(self.epicsmotors[n].VAL)
                    ismoving = self.ismoving(axis)
                    time.sleep(0.01)

        if axis in self.control["hexapod"].axes:
            status = False
            while not status:
                status = self.control["hexapod"].mv(axis, target)
                if not status:
                    status = self.control["hexapod"].handle_error()
                    print("Hexapod error, trying to servo back on.")
                    
            prevpos = target-1
#            print(wait)
            if wait:
                time.sleep(0.01)
#                print("aaa")
                while True:
                    pos = self.control["hexapod"].get_pos()
                    #print(self.control["hexapod"].isattarget())
                    pos = float(pos[axis])
                    self.signals.AxisPosSignal.emit(pos)
#                    if (abs(pos-target)<0.00001) or (abs(pos-prevpos)<0.00001):
                    if self.control["hexapod"].isattarget(axis):
                        break
                    prevpos = pos
                    time.sleep(0.01)
        if axis in self.gonio.channel_names:
            ch = self.gonio.channel_names.index(axis)
            self.gonio.mv(ch, target, wait=False)
            if wait:
                time.sleep(0.02)
                while self.gonio.ismoving(ch):
                    pos = self.gonio.get_pos(ch)
                    self.signals.AxisPosSignal.emit(pos)
                    time.sleep(0.01)

    def mvr(self, axis, target, wait=True):
        if axis == "phi":
            self.mvphi(self.posphi + target)
            #print(self.posphi, " before move")
            if wait:
                ismoving = True
                time.sleep(0.2)
                while ismoving:
                    b = self.control["phi"].motor_state
                    self.signals.AxisPosSignal.emit(self.posphi)
                    ismoving = b['moving']
                    time.sleep(0.02)
        if 'epicsmotors' in axis:
            if axis == "epicsmotors1":
                n = 0
            if axis == "epicsmotors2":
                n = 1
            if axis == "epicsmotors3":
                n = 2
            self.epicsmotors[n].move(self.get_pos(axis) + target)
            if wait:
                ismoving = True
                time.sleep(0.01)
                while ismoving:
                    self.signals.AxisPosSignal.emit(self.epicsmotors[n].VAL)
                    ismoving = self.ismoving(axis)
                    time.sleep(0.01)                    
        if axis in self.control["hexapod"].axes:
            pos = self.control["hexapod"].get_pos()
            prevpos = pos[axis]
            abstarget = prevpos+target
            self.control["hexapod"].mv(axis, abstarget)
            if wait:
                time.sleep(0.02)
                while True:
                    pos = self.control["hexapod"].get_pos()
                    pos = float(pos[axis])
                    self.signals.AxisPosSignal.emit(pos)
                    #if (abs(pos-abstarget)<0.00001) or (abs(pos-prevpos)<0.00001):
                    if self.control["hexapod"].isattarget(axis):
                        break
                    prevpos = pos
                    time.sleep(0.01)
        if axis in self.gonio.channel_names:
            ch = self.gonio.channel_names.index(axis)
            self.gonio.mvr(ch, target, wait=False)
            if wait:
                time.sleep(0.02)
                while self.gonio.ismoving(ch):
                    pos = self.gonio.get_pos(ch)
                    self.signals.AxisPosSignal.emit(pos)
                    time.sleep(0.01)

    def get_speed(self, axis):
        if 'epicsmotors' in axis:
            if axis == "epicsmotors1":
                n = 0
            if axis == "epicsmotors2":
                n = 1
            if axis == "epicsmotors3":
                n = 2
            return self.epicsmotors[n].get('VBAS')
        if axis == "phi":
            return self.control["phi"].vel, self.control["phi"].acc
        if axis in self.control["hexapod"].axes:
            return self.control["hexapod"].get_speed(), None
        if axis in self.gonio.channel_names:
            ch = self.gonio.channel_names.index(axis)
            vel,acc = self.gonio.get_speed(ch)
            return vel, acc
    
    def set_speed(self, axis, vel=1, acc=1):
        if 'epicsmotors' in axis:
            if axis == "epicsmotors1":
                n = 0
            if axis == "epicsmotors2":
                n = 1
            if axis == "epicsmotors3":
                n = 2
            self.epicsmotors[n].put('VBAS', vel)
        if axis == "phi":
            self.control["phi"].vel = vel
            self.control["phi"].acc = acc
        if axis in self.control["hexapod"].axes:
            self.control["hexapod"].set_speed(vel)
        if axis in self.gonio.channel_names:
            ch = self.gonio.channel_names.index(axis)
            self.gonio.set_speed(ch, vel, acc)
    
    def set_pos(self, axis, pos=0):
        if 'epicsmotors' in axis:
            if axis == "epicsmotors1":
                n = 0
            if axis == "epicsmotors2":
                n = 1
            if axis == "epicsmotors3":
                n = 2
            self.epicsmotors[n].put('VBAS', pos)
        if axis == "phi":
            acsc.setRPosition(self.control["phi"].controller.hc, self.control["phi"].axisno, pos)
        if axis in self.gonio.channel_names:
            ch = self.gonio.channel_names.index(axis)
            self.gonio.set_pos(ch, pos)
        
    def disconnect(self):
        self.control["hexapod"].disconnect()
        self.control["phi"].controller.disconnect()

    def connect(self):
        self.hexapod = Hexapod(hexapodIP)
        self.control["phi"].controller.connect(acsIP)
        #self.control["phi"] = Axis(acscontroller, 0)
    
    def isconnected(self, axis = 'X'):
        #print(axis, " This is in motions.py")
#        print(axis)
        if "newport" in axis:
        #    print(axis, " 22222 This is in motions.py")
            print("Newport is connected")
            return True
#        else:
#            print("Newport not connected")
        if axis in self.gonio.channel_names:
            ax = self.gonio.channel_names.index(axis)
            return self.gonio.connected[ax]
        if axis in self.control["hexapod"].axes:
            ax = self.control["hexapod"].axes.index(axis)
            return self.control["hexapod"].connected[ax]
        if axis == "phi":
            return self.control["phi"].connected

    def disp(self):
        while self.control["phi"].moving:
            print(self.control["phi"].fpos)
            time.sleep(1)
        
    def plot_hex(self, axis = 'X', filename=""):
        print("Getting records from Hexapod.")
        data = self.control["hexapod"].get_records()
        print("Done.. Preparing to plot.")
        if isinstance(data, type({})):
            l_data = [data]
        else:
            l_data = data
        for data in l_data:
            ndata = data[axis][0].size
            x = range(0, ndata)
            plt.plot(x, data[axis][1]*1000, 'b')
            plt.plot(x, data[axis][0]*1000, 'r')
            if len(filename)>0:
                dt2 = np.column_stack((x, data[axis][0]*1000, data[axis][1]*1000))
                np.savetxt(filename+"_hexapod"+".dat", dt2, fmt="%1.8e %1.8e %1.8e")

    def savedata(self, filename, t, r, col=[0,1,2]):
        tp = np.asarray(t)
        rp = np.asarray(r)
        if len(filename)>0:
            if type(col)==type([]):
                dt = tp
                myfmt = '%1.8e'
                for i in col:
                    dt = np.column_stack((dt, rp[:,i]))
                    myfmt = '%s %s' % (myfmt, '%1.8e')
            else:
                dt = np.column_stack((tp, rp[:,col]))
                myfmt = '%1.8e %1.8e'
            np.savetxt(filename, dt, fmt=myfmt)

    #pos = hexapod.get_pos()
    @property
    def posx(self):
        pos = self.control["hexapod"].get_pos()
        return pos['X']
    @posx.setter
    def posx(self, value):
        self.control["hexapod"].mv('X', value)
    
    @property
    def posy(self):
        pos = self.control["hexapod"].get_pos()
        return pos['Y']
    @posy.setter
    def posy(self, value):
        self.control["hexapod"].mv('Y', value)

    @property
    def posz(self):
        pos = self.control["hexapod"].get_pos()
        return pos['Z']
    @posz.setter
    def posz(self, value):
        self.control["hexapod"].mv('Z', value)
    
    @property
    def posu(self):
        pos = self.control["hexapod"].get_pos()
        return pos['U']
    @posu.setter
    def posu(self, value):
        self.control["hexapod"].mv('U', value)

    @property
    def posv(self):
        pos = self.control["hexapod"].get_pos()
        return pos['V']
    @posv.setter
    def posv(self, value):
        self.control["hexapod"].mv('V', value)

    @property
    def posw(self):
        pos = self.control["hexapod"].get_pos()
        return pos['W']
    @posw.setter
    def posw(self, value):
        self.control["hexapod"].mv('W', value)

    @property
    def posphi(self):
        return self.control["phi"].fpos
    @posphi.setter
    def posphi(self, value):
        self.mvphi(value)