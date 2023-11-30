from .motions import hexapod, phi, acscontroller
from .interferometers import qds
import time
import numpy as np

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
        rel, abs = qds.get_position()
        relpos.append([rel[0], rel[1], rel[2]])
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