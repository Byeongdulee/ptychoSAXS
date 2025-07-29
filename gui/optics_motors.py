
from PyQt5 import uic, QtCore, QtGui
from PyQt5.QtWidgets import QMainWindow, QApplication, QPushButton, QFileDialog, QWidget, QFormLayout
from PyQt5.QtWidgets import QLabel, QLineEdit, QMessageBox, QInputDialog, QDialog, QDialogButtonBox
from PyQt5.QtCore import QTimer, QObject, pyqtSlot, pyqtSignal, QRunnable, QThreadPool, QSize
from threading import Lock
import sys
import re
import os

# try:
#     import ptychosaxs.tw_galil as gl
#     MotorControlAvailable = True
# except:
#     MotorControlAvailable = False
#     print("Galil is not working")

#MotorControlAvailable = False
sys.path.append('../ptychosaxs')
import tw_galil as gl
MotorControlAvailable = True
try:
    from optics import ptyoptics
    from newport_piezo import newport
    MotorControlAvailable = True
except:
    MotorControlAvailable = False
    print("Piezo is NOT available.")

class motor_control(QMainWindow):
#    resized = QtCore.pyqtSignal()

    MOTOR_PREC = "%0.3f"
    def __init__(self):
        super(motor_control, self).__init__()
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose)
        guiName = "motorGUI.ui"
        self.ui = uic.loadUi(guiName)
        
        # list all possible motors
        # this should came from the pts.
        #controller = ['galil', 'smarAct', 'newport']
        self.control = {}
        self.control["galil"]= gl
        self.control["galil"].turn_on()
        self.control["newport"]= newport()
        self.control["optics"]= ptyoptics()

        self.motornames = ['OSAv','OSAh']
        self.controller = ['galil','galil']
        self.motorindices = [6,7]
        self.motorunits = ['step', 'step']
        
        for i, m in enumerate(self.control["optics"].motors):
            self.motornames.append(m.DESC)
            self.motorunits.append(m.EGU)
            self.controller.append('optics')
            self.motorindices.append(i)
        for i, m in enumerate(self.control["newport"].motors):
            self.motornames.append(m.DESC)
            self.motorunits.append(m.EGU)
            self.controller.append('newport')
            self.motorindices.append(i)
        print(self.motornames)
        self.lock = Lock()
        self.threadpool = QThreadPool.globalInstance()
        enable = True
        for i, name in enumerate(self.motornames):
            n = i+1
            self.enable_motors(n, enable)

        # update GUI
        for i, name in enumerate(self.motornames):
            n = i+1
            controller = self.control[self.controller[i]]
            axisname = controller.motornames[self.motorindices[i]]
            self.ui.findChild(QLabel, "lb%i"%n).setText(name)
            self.ui.findChild(QLabel, "lb_%i"%(i+1)).setText(str(controller.get_pos(axisname)))
            self.ui.findChild(QPushButton, "pb_tweak%iL"%n).clicked.connect(lambda: self.mvr(-1, -1))
            self.ui.findChild(QPushButton, "pb_tweak%iR"%n).clicked.connect(lambda: self.mvr(-1, 1))
            self.ui.findChild(QLineEdit, "ed_%i"%n).returnPressed.connect(lambda: self.mv(-1, None))
            self.ui.findChild(QLineEdit, "ed_reset_%i"%n).returnPressed.connect(lambda: self.reset(-1))
            self.ui.findChild(QPushButton, "pb_lup_%i"%n).setText("Stop")
            self.ui.findChild(QPushButton, "pb_lup_%i"%n).clicked.connect(lambda: self.stop(-1))
                         
        
        # menu
        self.ui.actionSmarAct_3.triggered.connect(self.enable_ptyoptics)
        self.ui.actionNewport.triggered.connect(self.enable_galil)
        self.ui.actionNewport_Piezo.triggered.connect(self.enable_newport)

        if os.name == 'nt':
            self.timer = QTimer()
            self.timer.timeout.connect(self.updatepos)
            self.timer.start(100)        
        self.ui.show()
        #self.resized.connect(self.resizeFunction)

    def enable_motors(self, n, enable):
        self.ui.findChild(QLabel, "lb%i"%n).setEnabled(enable)
        self.ui.findChild(QLabel, "lb_%i"%n).setEnabled(enable)
        self.ui.findChild(QPushButton, "pb_tweak%iL"%n).setEnabled(enable)
        self.ui.findChild(QPushButton, "pb_tweak%iR"%n).setEnabled(enable)
        self.ui.findChild(QPushButton, "pb_lup_%i"%n).setEnabled(enable)
        self.ui.findChild(QPushButton, "pb_SAXSscan_%i"%n).setEnabled(False)
        self.ui.findChild(QLineEdit, "ed_%i"%n).setEnabled(enable)   
        self.ui.findChild(QLineEdit, "ed_%i_tweak"%n).setEnabled(enable)               
        self.ui.findChild(QLineEdit, "ed_lup_%i_L"%n).setEnabled(False)               
        self.ui.findChild(QLineEdit, "ed_lup_%i_R"%n).setEnabled(False)               
        self.ui.findChild(QLineEdit, "ed_lup_%i_N"%n).setEnabled(False)               
        self.ui.findChild(QLineEdit, "ed_lup_%i_t"%n).setEnabled(False)  
        self.ui.findChild(QLineEdit, "ed_reset_%i"%n).setEnabled(enable) 

    
    def set_ui_enability(self, controller="smarAct", enable=True):
        for i, con in enumerate(self.controller):
            if con == controller:
                self.enable_motors(i+1, enable)

    # def enable_smarAct(self):
    #     if self.ui.actionSmarAct_3.isChecked():
    #         enable = True
    #     else:
    #         enable = False
    #     self.set_ui_enability('smarAct', enable=enable)

    def enable_galil(self):
        if self.ui.actionGalil.isChecked():
            enable = True
        else:
            enable = False
        self.set_ui_enability('galil', enable=enable)

    def enable_newport(self):
        if self.ui.actionNewport_Piezo.isChecked():
            enable = True
        else:
            enable = False
        self.set_ui_enability('newport', enable=enable)

    def enable_ptyoptics(self):
        if self.ui.actionOptics.isChecked():
            enable = True
        else:
            enable = False
        self.set_ui_enability('ptyoptics', enable=enable)

    def stop(self, motornumber=-1):
        if motornumber<0:
            pb = self.sender()
            objname = pb.objectName()
            val_text = pb.text()
            n = int(re.findall(r'\d+', objname)[0])
            #n = [int(s) for s in objname.split('_') if s.isdigit()][0]
            motornumber = n-1
        
        controller = self.control[self.controller[motornumber]]
        axis = controller.motornames[self.motorindices[motornumber]]
        controller.stop(axis)

    def reset(self, motornumber=-1):
        if motornumber<0:
            pb = self.sender()
            objname = pb.objectName()
            val_text = pb.text()
            n = int(re.findall(r'\d+', objname)[0])
            #n = [int(s) for s in objname.split('_') if s.isdigit()][0]
            motornumber = n-1
        
        controller = self.control[self.controller[motornumber]]
        axis = controller.motornames[self.motorindices[motornumber]]
        val = int(val_text)
        with self.lock:
            controller.set_pos(axis, val)

    def mv(self, motornumber=-1, val=None):
        if motornumber<0:
            pb = self.sender()
            objname = pb.objectName()
            val_text = pb.text()
            n = int(re.findall(r'\d+', objname)[0])
            motornumber = n-1

        controller = self.control[self.controller[motornumber]]
        axis = controller.motornames[self.motorindices[motornumber]]
        self.signalmotor = axis
        self.signalmotorunit = controller.motorunits[self.motorindices[motornumber]]
        self.set_ui_enability(controller, False)
        if type(val)==type(None):
            try:
                val = float(val_text)
            except:
                print('Text box is empty.')
                return
        with self.lock:
            controller.mv(axis, val, wait=False)
        self.set_ui_enability(controller, True)

    def mvr(self, motornumber=-1, sign=1, val=0):
        if motornumber ==-1:
            pb = self.sender()
            objname = pb.objectName()
            n = int(re.findall(r'\d+', objname)[0])
            #n = [int(s) for s in objname.split('_') if s.isdigit()][0]
            motornumber = n-1
        #print("motornumber is ", motornumber)
        controller = self.control[self.controller[motornumber]]
        axis = controller.motornames[self.motorindices[motornumber]]
        self.signalmotor = axis
        #print("axis is ", axis)
        #print("sign is ", sign)
        self.signalmotorunit = controller.motorunits[self.motorindices[motornumber]]
        self.set_ui_enability(controller, False)
        if val==0:
            val = float(self.ui.findChild(QLineEdit, "ed_%i_tweak"%n).text())
        #print(f"Move {axis} by {sign*val}")

        controller.mvr(axis, sign*val, wait=False)
        self.set_ui_enability(controller, True)

    def updatepos(self, axis = "", val=None):
        # done = False
        # timeout = 10
        # ct0 = time.time()
        if len(axis)==0:
            for i, name in enumerate(self.motornames):
                controller = self.control[self.controller[i]]
                axis = controller.motornames[self.motorindices[i]]
                if val is None:
                    with self.lock:
                        val = controller.get_pos(axis)
                self.ui.findChild(QLabel, "lb_%i"%(i+1)).setText(self.MOTOR_PREC%val)
                val = None
        else:
            motornumber = self.motornames.index(axis)
            controller = self.control[self.controller[motornumber]]
            axis = controller.motornames[self.motorindices[motornumber]]
            if val is None:
                with self.lock:
                    print(axis, " This is in line 3042")
                    val = controller.get_pos(axis)
            i = motornumber
            self.ui.findChild(QLabel, "lb_%i"%(i+1)).setText("%0.6f"%val)


app = QApplication(sys.argv)


def main():
    # non-server option
    app = QApplication(sys.argv)
    motor_panel = motor_control()
    sys.exit(app.exec_())

if __name__ == "__main__":
    # server option included..
    main()