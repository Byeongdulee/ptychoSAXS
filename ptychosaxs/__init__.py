from .motions import hexapod, phi, acscontroller
from .interferometers import qds
from .interferometers import plot_position as plot_qds
import time
import numpy as np
import matplotlib.pyplot as plt
global rpos
global tpos

def disconnect():
    hexapod.disconnect()
    acscontroller.disconnect()
    qds.disconnect()

def connect(self):
    self.hexapod = motions.Hexapod(motions.hexapodIP)
    self.acscontroller.connect(motions.acsIP)
    self.phi = motions.Axis(acscontroller, 0)

def fly_test(sec=0, dev=0):
    global rpos
    global tpos
    
    t0 = time.time()
    t_point = []
    t = 0
    relpos = []
    #abspos = []
    t_point = []
    k = 0
    if sec<=0:
        sec = hexapod.scantime + 1
    while (t - t0) < sec:
        rel, a = qds.get_position()
        r = rel[0]
        relpos.append([r[0], r[1], r[2]])
        #abspos.append([abs[0], abs[1], abs[2]])
        time.sleep(0.0001)
        t = time.time()
        t_point.append(t)
        k = k +1
        if k==10:
            hexapod.run_traj()
    relpos = np.asarray(relpos)
    #abspos = np.asarray(abspos)
    t_point = np.asarray(t_point)
    t_point = t_point-t0
    rpos = relpos
    tpos = t_point
    #return relpos, t_point

def plotdata(t, r, col = 0):
    plt.plot(t, r[:,col])
    plt.show()

def plot_qds_hex(col=0, axis = 'X', timeshift=0, filename=""):
#    global rpos
#    global tpos

    t = tpos
    r = rpos
    print("Getting records from Hexapod.")
    data = hexapod.get_records()
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