from PyQt5.QtCore import QObject, pyqtSignal

class motorSignals(QObject):
    AxisNameSignal = pyqtSignal(str)
    AxisPosSignal = pyqtSignal(float)

import time
from epics import Motor
from epics import PV

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


class epicsslit(object):
    # axis number counts from 1.
    def __init__(self, pvlist, motornames=[], motorunits=[]):
        self.motors = []
        for i, pv in enumerate(pvlist):
            self.motors.append(slit(pv, motornames[i], motorunits[i]))
        self.motornames = []
        self.motorunits = []
        for i, name in enumerate(motornames):
            self.motornames.append(name)
            self.motorunits.append(motorunits[i])
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
        dmov = self.motors[n].ismoving()
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
        pos = self.motors[n].get_pos()
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
        self.motors[n].mv(target)
        isstarted = False
        if not isstarted:
            isstarted = self.motors[n].ismoving()
            time.sleep(0.01)

        if wait:
            ismoving = True
            time.sleep(0.01)
            while ismoving:
                self.signals.AxisPosSignal.emit(self.motors[n].get_pos())
                ismoving = self.motors[n].ismoving()
                time.sleep(0.01)

    def mvr(self, axis, target, wait=True):
        if type(axis)==str:
            n = self.motornames.index(axis)
        if type(axis)==int:
            n = axis-1
        self.motors[n].mv(self.get_pos(axis) + target)
        if wait:
            ismoving = True
            time.sleep(0.01)
            while ismoving:
                self.signals.AxisPosSignal.emit(self.motors[n].get_pos())
                ismoving = self.motors[n].ismoving()
                time.sleep(0.01)                    

    def get_speed(self, axis):
        if type(axis)==str:
            n = self.motornames.index(axis)
        if type(axis)==int:
            n = axis-1
        return self.motors[n].get_speed()
    
    def set_speed(self, axis, vel=1, acc=1):
        if type(axis)==str:
            n = self.motornames.index(axis)
        if type(axis)==int:
            n = axis-1
        self.motors[n].set_speed()

class slit():
    def __init__(self, pv_master = "12idc:CL_SlitH", name = "", units = ""):
        self.VAL_RBV = PV("%st2.C"%pv_master)
        self.VAL = PV("%ssize.VAL"%pv_master)
        self.Tweak_Pos = PV("%ssize_tweak.A"%pv_master)
        self.Tweak_Neg = PV("%ssize_tweak.B"%pv_master)
        self.TweakVal = PV("%ssize_twealVal.VAL"%pv_master)
        self.name = name
        self.units = units

    def stop(self):
        pass
    
    def ismoving(self):
        v = self.VAL_RBV.get()
        time.sleep(0.05)
        v2 = self.VAL_RBV.get()
        return v != v2

    def get_pos(self):
        pos = self.VAL_RBV.get()
        return pos
    
    def set_position(self):
        pass
    def get_speed(self):
        pass
    def set_speed(self):
        pass
    def mv(self, target, wait=True):
        self.VAL.put(target)
        isstarted = False
        if not isstarted:
            isstarted = self.ismoving()
            time.sleep(0.01)

        if wait:
            ismoving = True
            time.sleep(0.01)
            while ismoving:
                #self.signals.AxisPosSignal.emit(self.VAL)
                ismoving = self.ismoving()
                time.sleep(0.01)

    def mvr(self, rpos, wait=True):
        self.TweakVal.put(abs(rpos))
        if rpos>0:
            self.Tweak_Pos.put(1)
        else:
            self.Tweak_Neg.put(1)
        if wait:
            ismoving = True
            time.sleep(0.01)
            while ismoving:
                #self.signals.AxisPosSignal.emit(self.get_pos())
                ismoving = self.ismoving()
                time.sleep(0.01)                    
