from threading import Thread
from epics import caget, caput, PV
import time
from PyQt5 import QtCore
from PyQt5.QtCore import QObject

beamline = '12IDC'

def keepshutteropen(beamline='12IDC'):
    if beamline.lower() == '12idb':
        station = 'B'
    if beamline.lower() == '12idc':
        station = 'C'
    pv_a_shutter = f"PB:12ID:STA_{station}_S{station}S_CLSD_PL.VAL"
    pv_exp_shutter = f"12ida2:rShtr{station}:Open"
    val = caget(pv_a_shutter)
    if val == 0:
        caput(pv_exp_shutter, 1)

## shutter stuff.. Could be eventually separated out.
def open_shutter(beamline='12IDC'):
    if beamline.lower() == '12idb':
        station = 'B'
    if beamline.lower() == '12idc':
        station = 'C'
    pv_exp_shutter = f"12ida2:rShtr{station}:Open"
    caput(pv_exp_shutter, 1)

def close_shutter(beamline='12IDC'):
    if beamline.lower() == '12idb':
        station = 'B'
    if beamline.lower() == '12idc':
        station = 'C'
    pv_exp_shutter = f"12ida2:rShtr{station}:Close"
    caput(pv_exp_shutter, 1)

class beamstatus(QObject):
    onChange = QtCore.pyqtSignal()
    def __init__(self, beamline="12IDC"):
        if beamline.lower() == '12idb':
            station = 'B'
        if beamline.lower() == '12idc':
            station = 'C'
        # A station shutter..
        self.shutter_val = PV('PB:12ID:STA_A_FES_CLSD_PL', callback=self.check_A_shutter)
        self.shutterA = PV('12ida2:rShtrA:Open')
        self.shutterC_open = PV(f'12ida2:rShtr{station}:Open')
        self.shutterC_close = PV(f'12ida2:rShtr{station}:Close')

    def check_A_shutter(self, value, **kws):
        if value == 0:
            self.signal.onChange.emit(False)
        if value == 1:
            self.signal.onChange.emit(True)

    def open_shutter(self):
        self.shutterA.put(1)

#    def close_shutter(self):
#        self.shutterA.put(0)

    def open_shutterC(self):
        self.shutterC_open.put(1)

    def close_shutterC(self):
        self.shutterC_close.put(1)

class shutter():
    def __init__(self, beamline="12IDC"):
        if beamline.lower() == '12idb':
            station = 'B'
        if beamline.lower() == '12idc':
            station = 'C'
        self.shutterC_open = PV(f'12ida2:rShtr{station}:Open')
        self.shutterC_close = PV(f'12ida2:rShtr{station}:Close')
        self.shutterA_open = PV('12ida2:rShtrA:Open')
        self.status = PV(f'PB:12ID:STA_{station}_S{station}S_CLSD_PL.VAL')
    def get_status(self):
        if self.status.get() == 1:
            # shutter is in close status
            return False
        else:
            # shutter is in open status
            return True
    @property
    def isClosed(self):
        return not self.get_status()
    
    def open(self):
        timout = 5
        self.shutterC_open.put(1)
        t0 = time.time()
        while not self.get_status():
            time.sleep(0.1)
            self.shutterC_open.put(1)
            if time.time()-t0>timout:
                print("Shutter won't open in timeout (5s).")
                break
    
    def close(self):
        self.shutterC_close.put(1)
        
    def open_A(self):
        self.shutterA_open.put(1)


class keepshopenThread(Thread):
    def run(self):
        while self.needOn:
            keepshutteropen()
    def stop(self):
        self.needOn = False