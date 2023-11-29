from .motions import hexapod, phi, acscontroller
from .interferometers import qds

def disconnect():
    hexapod.disconnect()
    acscontroller.disconnect()
    qds.disconnect()

def connect(self):
    self.hexapod = motions.Hexapod(motions.hexapodIP)
    self.acscontroller.connect(motions.acsIP)
    self.phi = motions.Axis(acscontroller, 0)    