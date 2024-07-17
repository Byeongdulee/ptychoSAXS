from .motions import Hexapod, Axis, hexapodIP, acsIP, hexapod, phi, acscontroller, acsc, motorSignals
from .motions import gonio
from .interferometers import qds
from .interferometers import plot_position
import time
import numpy as np
import matplotlib.pyplot as plt

class instruments(object):
    def __init__(self):
        self.hexapod = hexapod
        self.phi = phi
        self.qds = qds
        self.gonio = gonio
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
        print("now in is moving")
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
        if axis == "phi":
            return self.phi.vel, self.phi.acc
        if axis in self.hexapod.axes:
            return self.hexapod.get_speed(), None
        if axis in self.gonio.channel_names:
            ch = self.gonio.channel_names.index(axis)
            vel,acc = self.gonio.get_speed(ch)
            return vel, acc
    
    def set_speed(self, axis, vel=1, acc=1):
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
        self.qds.disconnect()

    def connect(self):
        self.hexapod = Hexapod(hexapodIP)
        self.phi.controller.connect(acsIP)
        self.qds.connect()
        #self.phi = Axis(acscontroller, 0)
    
    def isconnected(self, axis = 'X'):
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
        
    def scan(self, axis, start_pos, end_pos, step, col=[0,1]):
        '''step-scan motor axis and read interferometer positions. '''
        pos = np.arange(start_pos, end_pos+step, step)
        rpos = []
        plt.ion()
        print(f"{axis} is moving to {start_pos}.....")
        for i, value in enumerate(pos):
            # move to the position
            if axis == "phi":
                unit = "deg"
                self.mvphi(value)
                ismoving = True
                time.sleep(0.1)
                while ismoving:
                    ismoving = self.ismoving(axis)
                    time.sleep(0.1)
                    r, a = self.qds.get_position()
            if axis in self.hexapod.axes:
                unit = "mm"
                self.mv(axis, value)
                time.sleep(0.1)
                while not self.hexapod.isattarget(axis):
                    time.sleep(0.1)
                    r, a = self.qds.get_position()
            if axis in self.gonio.channel_names:
                ax = self.gonio.channel_names.index(axis)
                unit = self.gonio.units[ax]
                self.mv(axis, value)
                time.sleep(0.1)
                while self.gonio.ismoving(ax):
                    time.sleep(0.1)
                    r, a = self.qds.get_position()
            # read a qds value
            r, a = self.qds.get_position()
            r = r[0]
            rpos.append([r[0], r[1], r[2]])

            # plot data
            plt.gca().cla()
            r = np.asarray(rpos)
            if type(col) == type([]):
                plt.plot(pos[0:i], r[0:i,col[0]]/1000, 'r', 
                         pos[0:i], r[0:i,col[1]]/1000, 'b', 
                         pos[0:i], r[0:i,col[2]]/1000, 'k')
            else:
                plt.plot(pos[0:i], r[0:i,col]/1000, 'b')
            plt.ylabel('Positions (um)')
            plt.xlabel(f"{axis} ({unit})")
            plt.draw()
            plt.pause(0.1)
        print("Scan done.")
        rpos = np.asarray(rpos)
        plt.show(block=True)

        return pos, rpos
        
    def fly_test(self, sec=0, dev=0):
       
        t0 = time.time()
        t_point = []
        t = 0
        relpos = []
        #abspos = []
        t_point = []
        k = 0
        if sec<=0:
            sec = self.hexapod.scantime + 1
        while (t - t0) < sec:
            rel, a = self.qds.get_position()
            r = rel[0]
            relpos.append([r[0], r[1], r[2]])
            #abspos.append([abs[0], abs[1], abs[2]])
            time.sleep(0.0001)
            t = time.time()
            t_point.append(t)
            k = k +1
            if k==10:
                self.hexapod.run_traj()
        relpos = np.asarray(relpos)
        #abspos = np.asarray(abspos)
        t_point = np.asarray(t_point)
        t_point = t_point-t0
        self.rpos = relpos
        self.tpos = t_point
        #return relpos, t_point

    def plotdata(self, t, r, col = 0):
        plt.plot(t, r[:,col])
        plt.show()

    def plot_qds(self, *args):
        plot_position(args)

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

    def plot_qds_hex(self, col=0, axis = 'X', timeshift=0, filename=""):
    #    global rpos
    #    global tpos

        t = self.tpos
        r = self.rpos
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
        x = t*1000+timeshift
        y = r[:, col]/1000-r[-1, col]/1000+data[axis][0][-1]*1000
        if len(filename)>0:
            dt = np.column_stack((x, y))
            np.savetxt(filename+"_qds"+".dat", dt, fmt='%1.8e %1.8e')
        plt.plot(x, y, 'k')        
        plt.ylabel('Positions (um)')
        plt.xlabel(f"Time (/{data['Sample Time']} s)")
        plt.show()

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

ptychosaxs = instruments()