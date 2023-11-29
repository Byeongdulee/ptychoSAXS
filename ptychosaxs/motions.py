from pihexapod.gcs import Hexapod, plot_record, IP
from acspy.control import Controller, Axis

acsIP = "164.54.122.5"
hexapodIP = IP
hexapod = Hexapod(IP)
acscontroller = Controller("ethernet", 1)
acscontroller.connect(acsIP)
phi = Axis(acscontroller, 0)

# class stage:
#     acsIP = "164.54.122.5"
#     hexapodIP = IP
#     def __init__(self):
#         self.hexapod = Hexapod(IP)
#         c = Controller("ethernet", 1)
#         c.connect(self.acsIP)
#         self.phi = Axis(c, 0)
