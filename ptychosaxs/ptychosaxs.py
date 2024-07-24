from .motions import motors
from .interferometers import qds
from .interferometers import plot_position
import time
import numpy as np
import matplotlib.pyplot as plt

class instruments(motors):
    def __init__(self):
        self.qds = qds

    def disconnect(self):
        motors.disconnect()
        self.qds.disconnect()

    def connect(self):
        motors.connect()
        self.qds.connect()
        #self.phi = Axis(acscontroller, 0)
        
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

ptychosaxs = instruments()