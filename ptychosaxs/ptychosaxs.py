from .motions import Hexapod, Axis, hexapodIP, acsIP, hexapod, phi, acscontroller
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

    def mvphi(self, target, relative=False):
        if relative:
            c = "relative"
        else:
            c = "absolute"        
        self.phi.ptp(target=target, coordinates=c)

    def mvrphi(self, val):
        self.phimv(val, relative=True)

    def mvx(self, target, relative=False):
        if relative:
            pos = self.hexapod.get_pos()
            target += pos['X']
        self.hexapod.mv('X', target)

    def mvrx(self, target):
        self.xmv(target, relative=True)

    def disconnect(self):
        self.hexapod.disconnect()
        self.phi.controller.disconnect()
        self.qds.disconnect()

    def connect(self):
        self.hexapod = Hexapod(hexapodIP)
        self.phi.controller.connect(acsIP)
        self.qds.connect()
        #self.phi = Axis(acscontroller, 0)

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