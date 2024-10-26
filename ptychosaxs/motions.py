try:
    from pihexapod.gcs import Hexapod, plot_record, IP

    acsIP = "164.54.122.5"
    hexapodIP = IP
    hexapod = Hexapod(IP)
    hexapod.connected = [True,True,True,True,True,True]
except:
    hexapod = Hexapod
    hexapod.connected = [False,False,False,False,False,False]

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

try:
    #from ptychosaxs.smaract_gonio import ctl
    import ptychosaxs.smaract_gonio as gonio
    gonio.connected = gonio.isconnected()
except:
    class gonio:
        pass
    gonio.connected = [False,False,False,False]

from PyQt5.QtCore import QObject, pyqtSignal

class motorSignals(QObject):
    AxisNameSignal = pyqtSignal(str)
    AxisPosSignal = pyqtSignal(float)

import time
import numpy as np
import matplotlib.pyplot as plt
from epics import Motor

class motors(object):
    def __init__(self):
        self.hexapod = hexapod
        self.phi = phi
        self.gonio = gonio
        self.newport_piezo = []
        self.newport_piezo.append(Motor("12idcUC8:m1"))
        self.newport_piezo.append(Motor("12idcUC8:m3"))
        self.newport_piezo.append(Motor("12idcUC8:m5"))
        self.signals = motorSignals()

    def commutate_phi(self):
        acsc.commutate(acscontroller.hc, self.phi.axisno, wait=acsc.SYNCHRONOUS)

    def mvphi(self, target, relative=False, wait=True):
        try:
            if self.phi.enabled == False:
                self.phi.enable()
            if relative:
                c = "relative"
            else:
                c = "absolute"        
            if wait:
                wait = acsc.SYNCHRONOUS
            else:
                wait = acsc.ASYNCHRONOUS
            self.phi.ptp(target=target, coordinates=c, wait=wait)
        except acsc.AcscError as Err:
            if '3261:' in Err:
                print("phi was not commutated, and is being commutated. Please wait.")
                self.commutate_phi()

    def mvrphi(self, val, wait=True):
        self.mvphi(val, relative=True, wait=wait)

    def mvx(self, target, relative=False):
        if relative:
            pos = self.hexapod.get_pos()
            target += pos['X']
        self.hexapod.mv('X', target)

    def mvrx(self, target):
        self.mvx(target, relative=True)

    def ismoving(self, axis):
#        print("now in is moving")
        if 'newport_piezo' in axis:
            if axis == "newport_piezo1":
                n = 0
            if axis == "newport_piezo3":
                n = 1
            if axis == "newport_piezo5":
                n = 2
            dmov = self.newport_piezo[n].get('DMOV')
            if dmov==0:
                ismoving = True
            if dmov==1:
                ismoving = False
        if axis == "phi":
            ismoving = not self.phi.in_position
        if axis in self.hexapod.axes:
            ismoving = not self.hexapod.isattarget(axis)
        if axis in self.gonio.channel_names:
            ch = self.gonio.channel_names.index(axis)
            ismoving = self.gonio.ismoving(ch)
        return ismoving

    def get_pos(self, axis):
        #print(axis, " this is the name of axis")
        if 'newport_piezo' in axis:
            if axis == "newport_piezo1":
                n = 0
            if axis == "newport_piezo3":
                n = 1
            if axis == "newport_piezo5":
                n = 2
            pos = self.newport_piezo[n].get('VAL')
            return pos
        if axis == "phi":
            return float(self.posphi)
        if axis in self.hexapod.axes:
            pos = self.hexapod.get_pos()
            return float(pos[axis])
        if axis in self.gonio.channel_names:
            ch = self.gonio.channel_names.index(axis)
            pos = self.gonio.get_pos(ch)
            return pos
        
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
        if 'newport_piezo' in axis:
            if axis == "newport_piezo1":
                n = 0
            if axis == "newport_piezo3":
                n = 1
            if axis == "newport_piezo5":
                n = 2
            print(n, " in newport_piezo")
            self.newport_piezo[n].move(target)
            isstarted = False
            if not isstarted:
                isstarted = self.ismoving(axis)
                time.sleep(0.01)

            if wait:
                ismoving = True
                time.sleep(0.01)
                while ismoving:
                    self.signals.AxisPosSignal.emit(self.newport_piezo[n].VAL)
                    ismoving = self.ismoving(axis)
                    time.sleep(0.01)

        if axis in self.hexapod.axes:
            self.hexapod.mv(axis, target)
            prevpos = target-1
#            print(wait)
            if wait:
                time.sleep(0.01)
#                print("aaa")
                while True:
                    pos = self.hexapod.get_pos()
                    #print(self.hexapod.isattarget())
                    pos = float(pos[axis])
                    self.signals.AxisPosSignal.emit(pos)
#                    if (abs(pos-target)<0.00001) or (abs(pos-prevpos)<0.00001):
                    if self.hexapod.isattarget(axis):
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
                    b = self.phi.motor_state
                    self.signals.AxisPosSignal.emit(self.posphi)
                    ismoving = b['moving']
                    time.sleep(0.02)
        if 'newport_piezo' in axis:
            if axis == "newport_piezo1":
                n = 0
            if axis == "newport_piezo3":
                n = 1
            if axis == "newport_piezo5":
                n = 2
            self.newport_piezo[n].move(self.get_pos(axis) + target)
            if wait:
                ismoving = True
                time.sleep(0.01)
                while ismoving:
                    self.signals.AxisPosSignal.emit(self.newport_piezo[n].VAL)
                    ismoving = self.ismoving(axis)
                    time.sleep(0.01)                    
        if axis in self.hexapod.axes:
            pos = self.hexapod.get_pos()
            prevpos = pos[axis]
            abstarget = prevpos+target
            self.hexapod.mv(axis, abstarget)
            if wait:
                time.sleep(0.02)
                while True:
                    pos = self.hexapod.get_pos()
                    pos = float(pos[axis])
                    self.signals.AxisPosSignal.emit(pos)
                    #if (abs(pos-abstarget)<0.00001) or (abs(pos-prevpos)<0.00001):
                    if self.hexapod.isattarget(axis):
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
        if 'newport_piezo' in axis:
            if axis == "newport_piezo1":
                n = 0
            if axis == "newport_piezo3":
                n = 1
            if axis == "newport_piezo5":
                n = 2
            return self.newport_piezo[n].get('VBAS')
        if axis == "phi":
            return self.phi.vel, self.phi.acc
        if axis in self.hexapod.axes:
            return self.hexapod.get_speed(), None
        if axis in self.gonio.channel_names:
            ch = self.gonio.channel_names.index(axis)
            vel,acc = self.gonio.get_speed(ch)
            return vel, acc
    
    def set_speed(self, axis, vel=1, acc=1):
        if 'newport_piezo' in axis:
            if axis == "newport_piezo1":
                n = 0
            if axis == "newport_piezo3":
                n = 1
            if axis == "newport_piezo5":
                n = 2
            self.newport_piezo[n].put('VBAS', vel)
        if axis == "phi":
            self.phi.vel = vel
            self.phi.acc = acc
        if axis in self.hexapod.axes:
            self.hexapod.set_speed(vel)
        if axis in self.gonio.channel_names:
            ch = self.gonio.channel_names.index(axis)
            self.gonio.set_speed(ch, vel, acc)
        
    def disconnect(self):
        self.hexapod.disconnect()
        self.phi.controller.disconnect()

    def connect(self):
        self.hexapod = Hexapod(hexapodIP)
        self.phi.controller.connect(acsIP)
        #self.phi = Axis(acscontroller, 0)
    
    def isconnected(self, axis = 'X'):
        #print(axis, " This is in motions.py")
        if "newport" in axis:
        #    print(axis, " 22222 This is in motions.py")
            return True
        if axis in self.gonio.channel_names:
            ax = self.gonio.channel_names.index(axis)
            return self.gonio.connected[ax]
        if axis in self.hexapod.axes:
            ax = self.hexapod.axes.index(axis)
            return self.hexapod.connected[ax]
        if axis == "phi":
            return self.phi.connected

    def disp(self):
        while self.phi.moving:
            print(self.phi.fpos)
            time.sleep(1)
        
    def plot_hex(self, axis = 'X', filename=""):
        print("Getting records from Hexapod.")
        data = self.hexapod.get_records()
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
        pos = self.hexapod.get_pos()
        return pos['X']
    @posx.setter
    def posx(self, value):
        self.hexapod.mv('X', value)
    
    @property
    def posy(self):
        pos = self.hexapod.get_pos()
        return pos['Y']
    @posy.setter
    def posy(self, value):
        self.hexapod.mv('Y', value)

    @property
    def posz(self):
        pos = self.hexapod.get_pos()
        return pos['Z']
    @posz.setter
    def posz(self, value):
        self.hexapod.mv('Z', value)
    
    @property
    def posu(self):
        pos = self.hexapod.get_pos()
        return pos['U']
    @posu.setter
    def posu(self, value):
        self.hexapod.mv('U', value)

    @property
    def posv(self):
        pos = self.hexapod.get_pos()
        return pos['V']
    @posv.setter
    def posv(self, value):
        self.hexapod.mv('V', value)

    @property
    def posw(self):
        pos = self.hexapod.get_pos()
        return pos['W']
    @posw.setter
    def posw(self, value):
        self.hexapod.mv('W', value)

    @property
    def posphi(self):
        return self.phi.fpos
    @posphi.setter
    def posphi(self, value):
        self.mvphi(value)