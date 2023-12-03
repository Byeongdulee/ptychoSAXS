from pihexapod.gcs import Hexapod, plot_record, IP
from acspy.control import Controller, Axis
from acspy import acsc
from types import MethodType

acsIP = "164.54.122.5"
hexapodIP = IP
hexapod = Hexapod(IP)
acscontroller = Controller("ethernet", 1)
acscontroller.connect(acsIP)

phi = Axis(acscontroller, 0)