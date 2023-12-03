from pihexapod.gcs import Hexapod, plot_record, IP
from acspy.control import Controller, Axis
from acspy import acsc

acsIP = "164.54.122.5"
hexapodIP = IP
hexapod = Hexapod(IP)
acscontroller = Controller("ethernet", 1)
acscontroller.connect(acsIP)

class phiaxis(Axis):
    def __init__(self, contrl, motoraxis):
        super().__init__(controller=contrl, axisno=motoraxis)

    def mv(self, val, relative=False, wait=False):
        """Performs a point to point move in either relative or absolute
        (default) coordinates."""
       
        if wait:
            wait=acsc.SYNCHRONOUS
        else:
            wait=acsc.ASYNCHRONOUS
            
        if relative == True:
            flags = acsc.AMF_RELATIVE
        else:
            flags = None
        acsc.toPoint(self.controller.hc, flags, self.axisno, val, wait=wait)

    def mvr(self, val, wait=False):
        self.mv(val, relative=True, wait=wait)

phi = phiaxis(acscontroller, 0)

# class stage:
#     acsIP = "164.54.122.5"
#     hexapodIP = IP
#     def __init__(self):
#         self.hexapod = Hexapod(IP)
#         c = Controller("ethernet", 1)
#         c.connect(self.acsIP)
#         self.phi = Axis(c, 0)
