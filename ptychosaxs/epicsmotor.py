from PyQt5.QtCore import QObject, pyqtSignal

class motorSignals(QObject):
    AxisNameSignal = pyqtSignal(str)
    AxisPosSignal = pyqtSignal(float)

import time
from epics import Motor

class epicsmotor(object):
    # axis number counts from 1.
    def __init__(self, pvlist, motornames=[]):
        self.motors = []
        for pv in pvlist:
            self.motors.append(Motor(pv))
        self.motornames = []
        self.motorunits = []
        if len(motornames)==0:
            for i, name in enumerate(pvlist):
                self.motornames.append(self.motors[i].DESC)
                self.motorunits.append(self.motors[i].EGU)
        else:
            for i, name in enumerate(motornames):
                self.motornames.append(name)
                self.motorunits.append(self.motors[i].EGU)
        self.signals = motorSignals()

    def stop(self, axis):
        if type(axis)==str:
            n = self.motornames.index(axis)
        if type(axis)==int:
            n = axis-1
        self.motors[n].stop()

    def ismoving(self, axis):
        if type(axis)==str:
            n = self.motornames.index(axis)
        if type(axis)==int:
            n = axis-1
        dmov = self.motors[n].get('DMOV')
        if dmov==0:
            ismoving = True
        if dmov==1:
            ismoving = False
        return ismoving

    def get_pos(self, axis):
        if type(axis)==str:
            n = self.motornames.index(axis)
        if type(axis)==int:
            n = axis-1
        pos = self.motors[n].get('RBV')
        return pos

    def set_pos(self, axis, pos):
        if type(axis)==str:
            n = self.motornames.index(axis)
        if type(axis)==int:
            n = axis-1
        self.motors[n].set_position(pos)
        pos = self.get_pos(axis)
        return pos
        
    def mv(self, axis, target, wait=True):
        self.signals.AxisNameSignal.emit(axis)
        if type(axis)==str:
            n = self.motornames.index(axis)
        if type(axis)==int:
            n = axis-1
        self.motors[n].move(target)
        isstarted = False
        if not isstarted:
            isstarted = self.ismoving(axis)
            time.sleep(0.01)

        if wait:
            ismoving = True
            time.sleep(0.01)
            while ismoving:
                self.signals.AxisPosSignal.emit(self.motors[n].VAL)
                ismoving = self.ismoving(axis)
                time.sleep(0.01)

    def mvr(self, axis, target, wait=True):
        if type(axis)==str:
            n = self.motornames.index(axis)
        if type(axis)==int:
            n = axis-1
        self.motors[n].move(self.get_pos(axis) + target)
        if wait:
            ismoving = True
            time.sleep(0.01)
            while ismoving:
                self.signals.AxisPosSignal.emit(self.motors[n].VAL)
                ismoving = self.ismoving(axis)
                time.sleep(0.01)                    

    def get_speed(self, axis):
        if type(axis)==str:
            n = self.motornames.index(axis)
        if type(axis)==int:
            n = axis-1
        return self.motors[n].get('VBAS')
    
    def set_speed(self, axis, vel=1, acc=1):
        if type(axis)==str:
            n = self.motornames.index(axis)
        if type(axis)==int:
            n = axis-1
        self.motors[n].put('VBAS', vel)

