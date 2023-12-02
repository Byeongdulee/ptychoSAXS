from .motions import hexapod, phi, acscontroller
from .interferometers import qds
import time
import numpy as np
import matplotlib.pyplot as plt

def disconnect():
    hexapod.disconnect()
    acscontroller.disconnect()
    qds.disconnect()

def connect(self):
    self.hexapod = motions.Hexapod(motions.hexapodIP)
    self.acscontroller.connect(motions.acsIP)
    self.phi = motions.Axis(acscontroller, 0)

def fly_test(sec=2, dev=0):
    
    t0 = time.time()
    t_point = []
    t = 0
    relpos = []
    #abspos = []
    t_point = []
    k = 0
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
    return relpos, t_point

def plotdata(t, r, col = 0):
    plt.plot(t, r[:,col])
    plt.show()

def plot_qds_hex(t, r, col=0, axis = 'X', timeshift=0, filename=""):
    data = hexapod.get_records()
    if isinstance(data, type({})):
        l_data = [data]
    else:
        l_data = data
    for data in l_data:
        ndata = data[axis][0].size
        plt.plot(range(0, ndata), data[axis][1]*1000, 'b')
        plt.plot(range(0, ndata), data[axis][0]*1000, 'r')
    plt.plot(t*1000+timeshift, r[:, col]/1000-r[-1, col]/1000+data[axis][0][-1]*1000, 'k')        
    plt.ylabel('Positions (um)')
    plt.xlabel(f"Time (/{data['Sample Time']} s)")
    plt.show()