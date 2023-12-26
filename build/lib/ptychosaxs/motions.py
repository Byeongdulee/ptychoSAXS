try:
    from pihexapod.gcs import Hexapod, plot_record, IP

    acsIP = "164.54.122.5"
    hexapodIP = IP
    hexapod = Hexapod(IP)
    hexapod.connected = [True,True,True,True,True,True]
except:
    hexapod = Hexapod
    hexapod.connected = [False,False,False,False,False,False]

#hexapod.axis_names = ["X","Y","Z","U","V","W"]

try:
    from acspy.control import Controller, Axis
    from acspy import acsc
    from types import MethodType

    acscontroller = Controller("ethernet", 1)
    acscontroller.connect(acsIP)

    phi = Axis(acscontroller, 0)
    phi.connected = True
except:
    phi = Axis
    phi.connected = False

try:
    import ptychosaxs.smaract_gonio as gonio
    # Read the version of the library
    gonio.channel_names = []
    gonio.connected = gonio.isconnected()
    trnum=0
    tinum=0
    k = 0
    for unit in gonio.units:
        if unit == 'mm':
            name0 = 'trans'
            trnum += 1
            n = trnum
        elif unit == "deg":
            name0 = 'tilt'
            tinum += 1
            n = tinum
        else:
            name0 = 'None'
            k = k+1
            n = k
        gonio.channel_names.append("%s%i"%(name0, n))
        
except:
    gonio.connected = [False,False,False,False]

from PyQt5.QtCore import QObject, pyqtSignal

class motorSignals(QObject):
    AxisNameSignal = pyqtSignal(str)
    AxisPosSignal = pyqtSignal(float)
