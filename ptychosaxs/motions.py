from pihexapod.gcs import Hexapod, plot_record, IP
from acspy.control import Controller, Axis
from acspy import acsc
from types import MethodType
from PyQt5.QtCore import QObject, pyqtSignal
import smaract.ctl as ctl

acsIP = "164.54.122.5"
hexapodIP = IP
hexapod = Hexapod(IP)
acscontroller = Controller("ethernet", 1)
acscontroller.connect(acsIP)

class motorSignals(QObject):
    AxisNameSignal = pyqtSignal(str)
    AxisPosSignal = pyqtSignal(float)

phi = Axis(acscontroller, 0)


# Read the version of the library
# Note: this is the only function that does not require the library to be initialized.
version = ctl.GetFullVersionString()
print("SmarActCTL library version: '{}'.".format(version))

# Find available MCS2 devices
smaractstage = 'network:sn:MCS2-00012316'
try:
    buffer = ctl.FindDevices()
    if not (smaractstage in buffer):
        print("MCS2 no devices found.")
except:
    pass

d_handle = None
try:
    # Open the first MCS2 device from the list
    smaract = ctl.Open(smaractstage)
    print("MCS2 opened {}.".format(smaractstage))
except:
    pass
