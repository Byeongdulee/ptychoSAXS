from .motions import Hexapod, Axis, hexapodIP, acsIP, hexapod, phi, acscontroller, acsc, motorSignals
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
        self.signals = motorSignals()

    def mvphi(self, target, relative=False, wait=True):
        if relative:
            c = "relative"
        else:
            c = "absolute"        
        if wait:
            wait = acsc.SYNCHRONOUS
        else:
            wait = acsc.ASYNCHRONOUS
        self.phi.ptp(target=target, coordinates=c, wait=wait)

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
        if axis == "phi":
            ismoving = not self.phi.in_position
        else:
            ismoving = not self.hexapod.isattarget()
        return ismoving
    
    def mv(self, axis, target, wait=True):
        self.signals.AxisNameSignal.emit(axis)
        if axis == "phi":
            self.mvphi(target)
            if wait:
                ismoving = True
                time.sleep(0.1)
                while ismoving:
                    self.signals.AxisPosSignal.emit(float(self.posphi))
                    ismoving = self.ismoving(axis)
                    time.sleep(0.1)
        if axis in ["X","Y","Z","U","V","W"]:
            self.hexapod.mv(axis, target)
            if wait:
                time.sleep(0.02)
                while not self.hexapod.isattarget():
                    pos = self.hexapod.get_pos()
                    self.signals.AxisPosSignal.emit(float(pos[axis]))
                    time.sleep(0.1)
        if axis in ["trans1","trans2","tilt1","tilt2"]:
            self.gonio.mv(axis, target, wait=False)
            if wait:
                time.sleep(0.02)
                while not self.gonio.ismoving():
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
        else:
            pos = self.hexapod.get_pos()
            self.hexapod.mv(axis, pos[axis]+target)
            if wait:
                time.sleep(0.02)
                while not self.hexapod.isattarget():
                    pos = self.hexapod.get_pos()
                    try:
                        self.signals.AxisPosSignal.emit(pos[axis])
                    except:
                        print(axis)
                        print(pos)
                    time.sleep(0.02)
                    
    def disconnect(self):
        self.hexapod.disconnect()
        self.phi.controller.disconnect()
        self.qds.disconnect()

    def connect(self):
        self.hexapod = Hexapod(hexapodIP)
        self.phi.controller.connect(acsIP)
        self.qds.connect()
        #self.phi = Axis(acscontroller, 0)
    
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
            else:
                unit = "mm"
                self.mv(axis, value)
                time.sleep(0.1)
                while not self.hexapod.isattarget():
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