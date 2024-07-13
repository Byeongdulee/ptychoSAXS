# -*- coding: utf-8 -*-
"""
Created on Thu Oct 27 16:42:18 2016

@author: Byeongdu Lee
@Date: Nov. 1. 2016
"""

import sys 
import os
import asyncio
from asyncqt import QEventLoop
from server_json import UDPserver, create_server
import json

from PyQt5 import uic, QtCore, QtGui
from PyQt5.QtWidgets import QMainWindow, QApplication, QPushButton, QFileDialog, QWidget
from PyQt5.QtWidgets import QLabel, QLineEdit, QMessageBox, QInputDialog
from PyQt5.QtCore import QTimer, QObject, pyqtSlot, pyqtSignal, QRunnable, QThreadPool, QSize

import time

sys.path.append('..')

from ptychosaxs import pts

from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.backends.backend_qt5agg import NavigationToolbar2QT as NavigationToolbar
import matplotlib.pyplot as plt
import numpy as np

#from tools.panda import get_pandadata
from tools.softglue import sgz_pty
s12softglue = sgz_pty()

#Delay generator
import tools.dg645 as dg645
dg645_12ID = dg645.dg645_12ID.open_from_uri(dg645.ADDRESS_12IDC)

# detectors
from tools.detectors import pilatus
import re
import analysis.planeeqn as eqn

HEXAPOD_FLYMODE_WAVELET = 0
HEXAPOD_FLYMODE_STANDARD = 1
QDS_UNIT_NM = 0
QDS_UNIT_UM = 1
QDS_UNIT_MM = 2
QDS_UNIT_DEFAULT = QDS_UNIT_UM  # default QDS output is um
DEFAULTS = {'xmotor':0, 'ymotor':1, 'phimotor':6}

def showerror(msg):
    dlg = QMessageBox()
    dlg.setIcon(QMessageBox.Warning)
    dlg.setText(msg)
    dlg.setWindowTitle("Error")
    dlg.setStandardButtons(QMessageBox.Ok | QMessageBox.Cancel)
    result = dlg.exec_()
    return result


class workerSignals(QObject):
    finished = pyqtSignal(bool)
    progress = pyqtSignal(int)

# Step 1: Create a worker class
class Worker(QRunnable):

    def __init__(self, fn, *args, **kwargs):
        super(Worker, self).__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signal = workerSignals()
    
    @pyqtSlot()
    def run(self):
        """Long-running task."""
        #print("Worker:", QThread.currentThread())
        self.fn(*self.args, **self.kwargs)
        self.signal.finished.emit(True)

# Step 1: Create a move class
class move(QRunnable):

    def __init__(self, pts, axis, pos):
        super(move, self).__init__()
        self.pts = pts
        self.axis = axis
        self.pos = pos
        self.signal = workerSignals()
    
    @pyqtSlot()
    def run(self):
        """Long-running task."""
        self.pts.mv(self.axis, self.pos)
        self.signal.finished.emit(True)

# Step 1: Create a move class
class mover(QRunnable):

    def __init__(self, pts, axis, pos):
        super(mover, self).__init__()
        self.pts = pts
        self.axis = axis
        self.pos = pos
        self.signal = workerSignals()
    
    @pyqtSlot()
    def run(self):
        """Long-running task."""
        self.pts.mvr(self.axis, self.pos)
        self.signal.finished.emit(True)

class tweakmotors(QMainWindow):
#    resized = QtCore.pyqtSignal()

    def __init__(self):
        super(tweakmotors, self).__init__()
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose)
        guiName = "motorGUI.ui"
        self.pts = pts
        self.ui = uic.loadUi(guiName)
        
        # list all possible motors
        # this should came from the pts.
        motornames = ['X', 'Y', 'Z', 'U', 'V', 'W', 'phi']
        motorunits = ['mm', 'mm', 'mm', 'deg', 'deg', 'deg', 'deg']
        self.hexapod_flymode = HEXAPOD_FLYMODE_STANDARD
        self._qds_unit = QDS_UNIT_DEFAULT
        self._qds_x_sensor = 0
        self._qds_y_sensor = 1
        self.is_selfsaved = False
        if not hasattr(self.pts.gonio, 'channel_names'):
            self.pts.gonio.channel_names = [""]
            self.pts.gonio.units = [""]
        for i, name in enumerate(self.pts.gonio.channel_names):
            if len(name)>0:
                motornames.append(name)
        for unit in self.pts.gonio.units:
            if len(unit)>0:
                motorunits.append(unit)

        enable = False
        for i, name in enumerate(motornames):
            n = i+1
            self.ui.findChild(QLabel, "lb%i"%n).setEnabled(enable)
            self.ui.findChild(QPushButton, "pb_tweak%iL"%n).setEnabled(enable)
            self.ui.findChild(QPushButton, "pb_tweak%iR"%n).setEnabled(enable)
            self.ui.findChild(QPushButton, "pb_lup_%i"%n).setEnabled(enable)
            self.ui.findChild(QPushButton, "pb_SAXSscan_%i"%n).setEnabled(enable)
            self.ui.findChild(QLineEdit, "ed_%i"%n).setEnabled(enable)   
            self.ui.findChild(QLineEdit, "ed_%i_tweak"%n).setEnabled(enable)               
            self.ui.findChild(QLineEdit, "ed_lup_%i_L"%n).setEnabled(enable)               
            self.ui.findChild(QLineEdit, "ed_lup_%i_R"%n).setEnabled(enable)               
            self.ui.findChild(QLineEdit, "ed_lup_%i_N"%n).setEnabled(enable)               
            self.ui.findChild(QLineEdit, "ed_lup_%i_t"%n).setEnabled(enable)  
        # checking only the connected motors.. 
        # if not done, later it will try to update the position of disconnected motors    
        self.motornames = []
        self.motorunits = []
        for i, name in enumerate(motornames):
            try:
                if self.pts.isconnected(name):
                    self.motornames.append(name)
                    self.motorunits.append(motorunits[i])
            except:
                print(f"{name} is not connected.")
                pass
        # motors for 2d and 3d scans.....
        xm = self.motornames.index('X')
        ym = self.motornames.index('Y')
        phim = self.motornames.index('phi')
        # update GUI
        for i, name in enumerate(self.motornames):
            n = i+1
            self.ui.findChild(QLabel, "lb%i"%n).setText(name)
            self.ui.findChild(QPushButton, "pb_tweak%iL"%n).clicked.connect(lambda: self.mvr(-1, -1))
            self.ui.findChild(QPushButton, "pb_tweak%iR"%n).clicked.connect(lambda: self.mvr(-1, 1))
            self.ui.findChild(QPushButton, "pb_lup_%i"%n).clicked.connect(lambda: self.stepscan(-1))
            self.ui.findChild(QPushButton, "pb_SAXSscan_%i"%n).clicked.connect(lambda: self.fly(-1))
            self.ui.findChild(QLineEdit, "ed_%i"%n).returnPressed.connect(lambda: self.mv(-1, None))
            if self.pts.isconnected(name):
                enable = True
            else:
                enable = False
            self.ui.findChild(QLabel, "lb%i"%n).setEnabled(enable)
            self.ui.findChild(QPushButton, "pb_tweak%iL"%n).setEnabled(enable)
            self.ui.findChild(QPushButton, "pb_tweak%iR"%n).setEnabled(enable)
            self.ui.findChild(QPushButton, "pb_lup_%i"%n).setEnabled(enable)
            self.ui.findChild(QPushButton, "pb_SAXSscan_%i"%n).setEnabled(enable)
            self.ui.findChild(QLineEdit, "ed_%i"%n).setEnabled(enable)               
            self.ui.findChild(QLineEdit, "ed_%i_tweak"%n).setEnabled(enable)               
            self.ui.findChild(QLineEdit, "ed_lup_%i_L"%n).setEnabled(enable)               
            self.ui.findChild(QLineEdit, "ed_lup_%i_R"%n).setEnabled(enable)               
            self.ui.findChild(QLineEdit, "ed_lup_%i_N"%n).setEnabled(enable)               
            self.ui.findChild(QLineEdit, "ed_lup_%i_t"%n).setEnabled(enable)               
        
        self.ui.actionRun.triggered.connect(self.timescan)
        self.ui.actionStop.triggered.connect(self.timescanstop)
        self.ui.actionClear.triggered.connect(self.clearplot)
        self.ui.actionEnable_fly_with_controller.setCheckable(True)
        self.ui.actionEnable_fly_with_controller.setChecked(False)
        self.ui.actionEnable_fly_with_controller.triggered.connect(self.select_flymode) # hexapod flyscan type.
        self.ui.actionSet_the_default_vel_acc.triggered.connect(self.sethexapodvel_default)  # hexapod set vel acc into default
        self.ui.actionSet_default_speed.triggered.connect(self.setphivel_default)
        self.ui.actionSave.triggered.connect(self.savescan)
        self.ui.actionSave_flyscan_result.triggered.connect(self.fly_result)
        self.ui.actionFit_QDS_phi.setEnabled(False)
        self.ui.actionFit_QDS_phi.triggered.connect(self.fit_wobble_eccentricity)
        self.ui.actionLoad_eccentricity_data.triggered.connect(self.load_plot_eccentricity)
        self.ui.actionLoad_wobble_data.triggered.connect(self.load_plot_wobble)
        self.ui.actionSave_scan.triggered.connect(self.savescan)
        self.ui.actionLoad_scan.triggered.connect(self.loadscan)
        self.ui.actionSelect_units.triggered.connect(self.select_qds_units)
        self.ui.actionSelect_QDS_for_X.triggered.connect(self.select_qds_x)
        self.ui.actionSelect_QDS_for_Y.triggered.connect(self.select_qds_y)
        self.ui.actionCalibrate.triggered.connect(self.smaract_calibrate)
        self.ui.actionFindReference.triggered.connect(self.smaract_findreference)
        self.ui.actionSet_gonio_default_vel_acc.triggered.connect(self.smaract_set_defaultspeed)
        self.ui.actionScanStop.triggered.connect(self.stopscan)
        self.isStopScanIssued = False
        self.ui.action2D_scan.triggered.connect(lambda: self.fly2d(xm, ym))
        self.ui.action3D_scan.triggered.connect(lambda: self.fly3d(xm, ym, phim))
        self.ui.actionSelect_time_intervals.triggered.connect(self.select_timeintervals)
        self.ui.actionTrigout.triggered.connect(lambda: self.set_softglue_in(1))
        self.ui.actionDetout.triggered.connect(lambda: self.set_softglue_in(2))
        self.ui.actionPrint_flyscan_settings.triggered.connect(lambda: self.print_fly_settings(0))
        self.ui.actionSAXS.triggered.connect(lambda: self.select_detectors(1))
        self.ui.actionWAXS.triggered.connect(lambda: self.select_detectors(2))
        self.ui.actionReset_to_Fly_mode.triggered.connect(self.reset_det_flymode)
        self.ui.actionChannels_to_record.triggered.connect(self.choose_softglue_channels)
        self.ui.actionSave_current_results.triggered.connect(self.save_softglue)
        self.pts.signals.AxisPosSignal.connect(self.update_motorpos)
        self.pts.signals.AxisNameSignal.connect(self.update_motorname)
        self.ui.actionTestFly.triggered.connect(self.scantest)
        self.softglue_channels = ['B', 'C', 'D']
        self.ui.ed_workingfolder.returnPressed.connect(self.update_workingfolder)
        self.ui.ed_scanname.returnPressed.connect(self.update_scanname)
        if os.name != 'nt':
            self.ui.menuQDS.setDisabled(True)
        # set default softglue collection freq. 10 micro seconds.
        s12softglue.set_count_freq(10)

        self.rpos = []
        self.mpos = []
        self.threadpool = QThreadPool.globalInstance()
        self.working_folder = ""
        # qds
        self.ref_X = 0
        self.ref_Z = 0
        self.ref_Z2 = 0
        self.isscan = False
        self.isfly = False
        self.ui.pb_resetx.clicked.connect(self.reset_qdsX)
        self.ui.pb_resetz.clicked.connect(self.reset_qdsZ)
        self.ui.pb_resetz_2.clicked.connect(self.reset_qdsZ2)

        self.ui.pb_recordx1.clicked.connect(lambda: self.record_qdsX(1))
        self.ui.pb_recordx2.clicked.connect(lambda: self.record_qdsX(2))
        self.ui.pb_recordx3.clicked.connect(lambda: self.record_qdsX(3))

        self.ui.pb_recordz1.clicked.connect(lambda: self.record_qdsZ(1))
        self.ui.pb_recordz2.clicked.connect(lambda: self.record_qdsZ(2))
        self.ui.pb_recordz3.clicked.connect(lambda: self.record_qdsZ(3))

        self.ui.pb_recordz1_2.clicked.connect(lambda: self.record_qdsZ(4))
        self.ui.pb_recordz2_2.clicked.connect(lambda: self.record_qdsZ(5))
        self.ui.pb_recordz3_2.clicked.connect(lambda: self.record_qdsZ(6))
        
        self._qds_time_interval = 0.1

        # figure to plot
        # a figure instance to plot on
        self.figure = plt.figure()

        #self.ui.
        self.px = 1/plt.rcParams['figure.dpi']  # pixel in inches
        # print(plt.rcParams['figure.subplot.left'])
        # print(plt.rcParams['figure.subplot.bottom'] )
        # print(plt.rcParams['figure.subplot.right'] )
        # print(plt.rcParams['figure.subplot.top'])

        # this is the Canvas Widget that displays the `figure`
        # it takes the `figure` instance as a parameter to __init__
        self.canvas = FigureCanvas(self.figure)
        self.figure.set_tight_layout(True)

        # this is the Navigation widget
        # it takes the Canvas widget and a parent
        self.toolbar = NavigationToolbar(self.canvas, self)

        # set the layout
        
        self.ui.verticalLayout_2.addWidget(self.toolbar)
        self.ui.verticalLayout_2.addWidget(self.canvas)
        self.figure.clear()

        # create an axis
        self.ax = self.figure.add_subplot(131)
        self.ax2 = self.figure.add_subplot(132)
        self.ax3 = self.figure.add_subplot(133)
        self.canvas.mpl_connect('button_press_event', self.onclick)
        self.canvas.draw()

        self.updatepos()

        # detectors
        self.detector = [None]*2

        if os.name == 'nt':
            self.timer = QTimer()
            self.timer.timeout.connect(self.update_qds)
            self.timer.start(100)        
        self.ui.show()
        #self.resized.connect(self.resizeFunction)

    def update_workingfolder(self, folder=""):
        if len(folder) == 0:
            self.working_folder = self.ui.ed_workingfolder.text()
        else:
            self.ui.ed_workingfolder.setText(self.working_folder)

    def update_scanname(self):
        txt = self.ui.ed_scanname.text()
        for det in self.detector:
            if det is not None:
                det.filePut('FileName', txt)

    def choose_softglue_channels(self):
        strv = ''
        for i, ch in enumerate(self.softglue_channels):
            if i==0:
                strv = ch
            else:
                strv = "%s, %s"% (strv, ch)
        text, okPressed = QInputDialog.getText(self, "Channels of SoftGlueZinq to Record","Channels:", QLineEdit.Normal, strv)
        if okPressed:
            self.softglue_channels = [x.strip() for x in text.split(',')]
            print(self.softglue_channels)

    def reset_det_flymode(self):
        for det in self.detector:
            if det is not None:
                det.set_fly_configuration()

    def set_softglue_in(self, val):
        if val==1:
            self.ui.actionDetout.setChecked(False)
            self.ui.actionTrigout.setChecked(True)
            s12softglue.set_count_freq(10)
        if val==2:
            self.ui.actionDetout.setChecked(True)
            self.ui.actionTrigout.setChecked(False)
            s12softglue.set_count_freq(100)
            
    def stopscan(self):
        self.isStopScanIssued = True

    def onclick(self, event):
#        xu = self.ui.width()
#        yu = self.ui.height()
        xf = self.ui.frame.width()
        yf = self.ui.frame.height()
        xs = xf*self.px
        ys = (yf-500)*self.px
        #self.ui.frame.setGeometry(0, 0, xu, yu)
        #print(f"xsize is {xu}, ysize is {yu}, xf is {xf}, yf is {yf}")
        self.figure.set_size_inches(xs, ys)
        self.canvas.draw()
        # print('%s click: button=%d, x=%d, y=%d, xdata=%f, ydata=%f' %
        #   ('double' if event.dblclick else 'single', event.button,
        #    event.x, event.y, event.xdata, event.ydata))
        
    def eventFilter(self, myself, event):
#        print(event.type())
#        print(self.figure.get_size_inches())
        if (event.type() == QtCore.QEvent.Resize):
            xs = self.ui.width()*self.px
            ys = (self.ui.height()-350)*self.px
            print(f"xsize is {xs}, ysize is {ys}")
            self.figure.set_size_inches(xs, ys)
            self.canvas.draw()
            #self.resize.emit(1)
        return True
    
    def select_qds_units(self):
        text, ok = QInputDialog().getItem(self, "Select QDS units",
                                            "Units:", ('nm', 'um', 'mm'), current=1, editable=False)
        if text =="nm":
            self._qds_unit = QDS_UNIT_NM
        if text =="um":
            self._qds_unit = QDS_UNIT_UM
        if text =="mm":
            self._qds_unit = QDS_UNIT_MM
    
    def select_timeintervals(self):
        val, ok = QInputDialog().getDouble(self, "QDS acqusition time intervals", "time intervals(s)", self._qds_time_interval)
        self._qds_time_interval = val

    def select_qds_x(self):
        text, ok = QInputDialog().getItem(self, "Select QDS units",
                                            "Units:", ('0', '1', '2'), current=1, editable=False)
        self._qds_x_sensor = int(text)

    def select_qds_y(self):
        text, ok = QInputDialog().getItem(self, "Select QDS units",
                                            "Units:", ('0', '1', '2'), current=1, editable=False)
        self._qds_y_sensor = int(text)

    def scantest(self):
        if self.ui.actionTestFly.isChecked():
            self.ui.actionTestFly.setChecked(True)
        else:
            self.ui.actionTestFly.setChecked(False)
            self.detector[1] = None
    def fit_wobble_eccentricity(self):
        tp = np.asarray(self.mpos)
        rp = np.asarray(self.rpos)
        self.fitdata(xd=tp, yd=rp[:,self._qds_x_sensor], dtype="eccent")
        self.fitdata(xd=tp, yd=rp[:,self._qds_y_sensor], dtype="wob")

    def loadscan(self):
        w = QWidget()
        w.resize(320, 240)
        # Set window title
        w.setWindowTitle("Load phi vs QDS[0,1,2] Data")
        fn = QFileDialog.getOpenFileName(w, 'Open File', '', 'Text (*.txt *.dat)',None, QFileDialog.DontUseNativeDialog)
        filename = fn[0]
        if filename == "":
            return 0
        self.fitdata(filename=filename, datacolumn=self._qds_x_sensor+1, dtype="eccent")
        self.fitdata(filename=filename, datacolumn=self._qds_y_sensor+1, dtype="wob")

        self.canvas.draw()

    def fitdata(self, filename="", datacolumn=2, xd = [], yd = [], dtype="wobble"):
        if self._qds_unit == QDS_UNIT_MM:
            eqn.POSITION_UNIT = eqn.POSITION_UNIT_MM
        if self._qds_unit == QDS_UNIT_UM:
            eqn.POSITION_UNIT = eqn.POSITION_UNIT_UM
        if self._qds_unit == QDS_UNIT_NM:
            eqn.POSITION_UNIT = eqn.POSITION_UNIT_NM
        if len(filename)>0:
            xd, yd = eqn.loadata(filename=filename, datacolumn=datacolumn)
        else:
            xd, yd = eqn.loadata(xdata=xd, ydata=yd)

        if dtype in "eccentricity":
            popt, pconv = eqn.fit_eccentricity(xd, yd)
            cv, lb = eqn.get_eccen_fitcurve(xd, popt)
            self.plotfits(xd, yd, cv, lb, ax=1)    
        if dtype in "wobble":
            popt, pconv = eqn.fit_wobble(xd, yd)
            cv, lb = eqn.get_wobble_fitcurve(xd, popt)
            self.plotfits(xd, yd, cv, lb, ax=2) 

    def load_plot_eccentricity(self):
        # this is multiple column data where the last column is the sensor data.
        w = QWidget()
        w.resize(320, 240)
        # Set window title
        w.setWindowTitle("Load phi vs QDS Eccentricity(X) Data")
        fn = QFileDialog.getOpenFileName(w, 'Open File', '', 'Text (*.txt *.dat)',None, QFileDialog.DontUseNativeDialog)
        filename = fn[0]
        if filename == "":
            return 0
        self.fitdata(filename=filename, datacolumn=-1, dtype="eccent")    

    def load_plot_wobble(self):
        # this is multiple column data where the last column is the sensor data.
        w = QWidget()
        w.resize(320, 240)
        # Set window title
        w.setWindowTitle("Load phi vs QDS Wobble(Y) Data")
        fn = QFileDialog.getOpenFileName(w, 'Open File', '', 'Text (*.txt *.dat)',None, QFileDialog.DontUseNativeDialog)
        filename = fn[0]
        if filename == "":
            return 0
        self.fitdata(filename=filename, datacolumn=-1, dtype="wob")        

    def plotfits(self, xd, yd, curve, lbl, ax=2):
        # dt should be two colum data [phi, pos]
        # where phi is the phi angle in radian and pos is the QDS position in mm.
        #plt.figure()
        if ax==2:
            ax = self.ax2
        if ax==1:
            ax = self.ax
        ax.cla()
        ax.plot(np.rad2deg(xd), yd, 'b', np.rad2deg(xd), curve, 'g--')
        ax.set_title(lbl)
#        ax.plot(xd, yd, 'b', label='data')
#        ax.plot(xd, curve, 'g--', label=lbl)
        if 'xc=' in lbl:
            ylbl = 'x-x_mean (mm)'
        else:
            ylbl = 'y-y_mean (mm)'
        ax.set_xlabel('phi (deg)')
        ax.set_ylabel(ylbl)
        #ax.legend()
        self.canvas.draw()

    def get_motorpos(self, axis):
        return self.pts.get_pos(axis)
        
    def updatepos(self, axis = "", val=None):
        if len(axis)==0:
            for i, name in enumerate(self.motornames):
                if val is None:
                    val = self.pts.get_pos(name)
                #self.ui.findChild(QLineEdit, "ed_%i"%(i+1)).setText("%0.4f"%val)
                self.ui.findChild(QLabel, "lb_%i"%(i+1)).setText("%0.4f"%val)
                val = None
        else:
            if val is None:
                val = self.pts.get_pos(axis)
            i = self.motornames.index(axis)
            #self.ui.findChild(QLineEdit, "ed_%i"%(i+1)).setText("%0.4f"%val)
            self.ui.findChild(QLabel, "lb_%i"%(i+1)).setText("%0.4f"%val)

    def update_motorpos(self, value):
        self.updatepos(self.signalmotor, value)

    def update_motorname(self, axis):
        self.signalmotor = axis
    
    def smaract_set_defaultspeed(self):
        for i, connected in enumerate(self.pts.gonio.connected):
            if connected:
                self.pts.gonio.set_speed(i)

    def smaract_calibrate(self):
        for i, connected in enumerate(self.pts.gonio.connected):
            if connected:
                self.pts.gonio.calibrate(i)
        print("MCS2 calibration done..")

    def smaract_findreference(self):
        for i, connected in enumerate(self.pts.gonio.connected):
            if connected:
                self.pts.gonio.findReference(i)
        print("MCS2 finding references done..")
                        
    def setphivel_default(self):
#        print(self.pts.phi.vel, " This was vel value")
        self.pts.phi.vel = 36
        time.sleep(0.1)
        self.pts.phi.acc = self.pts.phi.vel*10
#        self.pts.set_speed()

    def sethexapodvel_default(self):
#        print(self.pts.phi.vel, " This was vel value")
        self.pts.set_speed(self.pts.hexapod.axes[0], 5, None)

    def scandone(self, value):
        self.isscan = False
        self.updatepos()
        print("scan done.......")
    
    def select_detectors(self, N):
        if N==1:
            if self.ui.actionSAXS.isChecked():
                self.ui.actionSAXS.setChecked(True)
                self.detector[0] = pilatus('S12-PILATUS1:')
            else:
                self.ui.actionSAXS.setChecked(False)
                self.detector[0] = None
        if N==2:
            if self.ui.actionWAXS.isChecked():
                self.ui.actionWAXS.setChecked(True)
                self.detector[1] = pilatus('12idcPIL:')
            else:
                self.ui.actionWAXS.setChecked(False)
                self.detector[1] = None
        
    def select_flymode(self):
        if self.ui.actionEnable_fly_with_controller.isChecked():  # when checked, this value is False
            self.ui.actionEnable_fly_with_controller.setChecked(True)
            self.hexapod_flymode = HEXAPOD_FLYMODE_WAVELET
        else:
            self.hexapod_flymode = HEXAPOD_FLYMODE_STANDARD
            self.ui.actionEnable_fly_with_controller.setChecked(False)

    def save_softglue(self):
        # read softglue data
        foldername = self.ui.ed_workingfolder.text()
        if len(foldername) == 0:
            return
            #foldername = os.getcwd()
        N_cnt = 0
        if hasattr(self.pts.hexapod, "pulse_number"):
            N_cnt = self.pts.hexapod.pulse_number
        t = []
        timeout = 2
        ct0 = time.time()
        while len(t)<N_cnt:
            #s12softglue.PROC = 1
            #time.sleep(0.1)
            try:
                t, dt = s12softglue.get_arrays(self.softglue_channels)
            except:
                t = []
            print(f"length of t is {len(t)}, and N_pulses is {N_cnt}")
            if (time.time()-ct0 > timeout):
                print("timeout")
                break
        # save softglue data
        filename = ""
        for det in self.detector:
            if det is not None:
                fn = det.File_FullFileName_RBV
                fn = fn.tobytes()
                filename = os.path.basename(fn.decode('utf-8')).rstrip('\x00')
                filename = filename.rstrip('.h5')
        if len(filename) ==0:
            print("****** Error: No detector was triggered.")
            return
        else:
            print(f"Total {len(t)} data will be saved under {foldername}.")
        for i, td in enumerate(t):
            scanname = '%s_%i.dat' % (filename, i)
            dt2 = np.column_stack((td, dt[0][i], dt[1][i], dt[2][i]))
            np.savetxt(os.path.join(foldername, scanname), dt2, fmt="%1.8e %1.8e %1.8e %1.8e")

    def flydone(self, value):
        print("fly done.......")
        isTestRun = self.ui.actionTestFly.isChecked()
        if isTestRun:
            return
        self.isscan = False
        self.updatepos()
        s12softglue.flush()
        #if self.signalmotor not in self.pts.hexapod.axes:        
        #    self.pts.set_speed(self.signalmotor, self._prev_vel, self._prev_acc)
        try:
            self.save_softglue()
        except:
            print("Error in softglue saving....")
    
    def timescanstop(self):
        self.isscan = False

    def timescan(self):
        if self.ui.actionckTime_reset_before_scan.isChecked():
            s12softglue.ckTime_reset()
        if not self.ui.cb_keepprevscan.isChecked():
            self.clearplot()
        #if self.isscan:
        #    print("Stop the scan first.")
        #    return
        self.t0 = time.time()
        self.signalmotor = "Time"
        self.signalmotorunit = "s"

        self.mpos = []
        self.rpos = []

        self.isscan = True
        # self.thread = self.createtimescanthread()
        # self.thread.start()
        self.is_selfsaved = False
        w = Worker(self.timescan0)
        w.signal.finished.connect(self.scandone)
        self.threadpool.start(w)
        
    def fly2d(self, xmotor=0, ymotor=1, scanname = ""):
        if self.ui.actionckTime_reset_before_scan.isChecked():
            s12softglue.ckTime_reset()
        motor = [xmotor, ymotor]
        for m in motor:
            n = m+1
            try:
                st = float(self.ui.findChild(QLineEdit, "ed_lup_%i_L"%n).text())
                fe = float(self.ui.findChild(QLineEdit, "ed_lup_%i_R"%n).text())
                tm = float(self.ui.findChild(QLineEdit, "ed_lup_%i_t"%n).text())
            except:
                showerror("Check scan paramters.")
                return 0
        
        self.isscan = True
        w = Worker(self.fly2d0, xmotor, ymotor, scanname=scanname)
        w.signal.finished.connect(self.flydone)
        self.threadpool.start(w)

    def fly3d(self, xmotor=0, ymotor=1, phimotor=6, scanname=""):
        if self.ui.actionckTime_reset_before_scan.isChecked():
            s12softglue.ckTime_reset()
        motor = [xmotor, ymotor, phimotor]
        for m in motor:
            n = m+1
            try:
                st = float(self.ui.findChild(QLineEdit, "ed_lup_%i_L"%n).text())
                fe = float(self.ui.findChild(QLineEdit, "ed_lup_%i_R"%n).text())
                tm = float(self.ui.findChild(QLineEdit, "ed_lup_%i_t"%n).text())
            except:
                showerror("Check scan paramters.")
                return 0
        
        self.isscan = True
        w = Worker(self.fly3d0, xmotor, ymotor, phimotor, scanname=scanname)
        w.signal.finished.connect(self.flydone)
        self.threadpool.start(w)

    def fly(self, motornumber=-1):
        if self.ui.actionckTime_reset_before_scan.isChecked():
            s12softglue.ckTime_reset()

        if motornumber<0:
            pb = self.sender()
            objname = pb.objectName()
            n = int(re.findall(r'\d+', objname)[0])
            motornumber = n-1
        else:
            n = motornumber + 1

        try:
            st = float(self.ui.findChild(QLineEdit, "ed_lup_%i_L"%n).text())
            fe = float(self.ui.findChild(QLineEdit, "ed_lup_%i_R"%n).text())
            tm = float(self.ui.findChild(QLineEdit, "ed_lup_%i_t"%n).text())
        except:
            showerror("Check scan paramters.")
            return 0
        
        self.isscan = True
        w = Worker(self.fly0, motornumber)
        w.signal.finished.connect(self.flydone)
        self.threadpool.start(w)

    def stepscan(self, motornumber=-1):
        if motornumber<0:
            pb = self.sender()
            objname = pb.objectName()
            n = int(re.findall(r'\d+', objname)[0])
            motornumber = n-1
        else:
            n = motornumber + 1
        
        try:
            st = float(self.ui.findChild(QLineEdit, "ed_lup_%i_L"%n).text())
            fe = float(self.ui.findChild(QLineEdit, "ed_lup_%i_R"%n).text())
            #tm = float(self.ui.findChild(QLineEdit, "ed_lup_%i_t"%n).text())
            step = float(self.ui.findChild(QLineEdit, "ed_lup_%i_N"%n).text())
        except:
            showerror("Check scan parameters.")
            return 0
        
        self.isscan = True
        
        w = Worker(self.stepscan0, motornumber)
        w.signal.finished.connect(self.scandone)
        self.threadpool.start(w)
    
    # def createflyscanthread(self, motornumber, type):
    #     thread = QThread()
    #     w = Worker()
    #     w.moveToThread(thread)
    #     thread.started.connect(lambda: self.fly0(motornumber, type))
    #     w.progress.connect(self.update_graph)
    #     w.finished.connect(thread.quit)
    #     w.finished.connect(w.deleteLater)
    #     thread.finished.connect(thread.deleteLater)
    #     return thread
    
    def update_graph(self):
        print("update_graph called")
        pass

    def timescan0(self):
        self.tempfilename = "C:\TEMP\_qds_temporary.txt"
        N_selfsave_points = 1024
        k = 0
        while self.isscan:
            r = self.get_qds_pos()
            self.rpos.append([r[0], r[1], r[2]])
            t = time.time()-self.t0
            self.mpos.append(t)
            time.sleep(self._qds_time_interval)
            if len(self.mpos)>N_selfsave_points:
                self.save_qds(self.tempfilename, "a")
                k = k + 1
                self.mpos = []
                self.rpos = []
                print(f"Previous {N_selfsave_points} data points are saved in {self.tempfilename}")
                self.is_selfsaved = True

    def get_qds_pos(self, isrefavailable = True):
        if os.name == 'nt':
            r, a = self.pts.qds.get_position()
            r = r[0]
        else:
            r = self.pts.qds.get_position(self.softglue_channels)

        if isrefavailable:
            r = [r[0]/1000-self.ref_X, r[1]/1000-self.ref_Z, r[2]/1000-self.ref_Z2]
        else:
            r = [r[0]/1000, r[1]/1000, r[2]/1000]
        return r
    
    def stepscan0(self, motornumber):
        axis = self.motornames[motornumber]
        self.signalmotor = axis
        self.signalmotorunit = self.motorunits[motornumber]
        self.rpos = []
        self.mpos = []
        pos = self.pts.get_pos(axis)
        self.isfly = False
        n = motornumber+1

        if not self.ui.cb_keepprevscan.isChecked():
            self.clearplot()

        st = float(self.ui.findChild(QLineEdit, "ed_lup_%i_L"%n).text())
        fe = float(self.ui.findChild(QLineEdit, "ed_lup_%i_R"%n).text())
        #tm = float(self.ui.findChild(QLineEdit, "ed_lup_%i_t"%n).text())
        step = float(self.ui.findChild(QLineEdit, "ed_lup_%i_N"%n).text())

        # disable fit menu
        self.ui.actionFit_QDS_phi.setEnabled(False)
        # enable fit menu
        if axis == "phi":
            self.ui.actionFit_QDS_phi.setEnabled(True)
        if st>fe:
            step = -1*abs(step)
        if st<fe:
            step = abs(step)
        if self.ui.cb_reversescandir.isChecked():
            if abs(st-pos)>abs(fe-pos):
                t = fe
                fe = st
                st = t 
                step = -step
        self.pts.mv(axis, st)
        pos = np.arange(st, fe+step, step)
        for i, value in enumerate(pos):
            if self.isStopScanIssued:
                return
            self.pts.mv(axis, value)
            r = self.get_qds_pos()
            self.rpos.append([r[0], r[1], r[2]])
            #pos = self.get_motorpos(self.signalmotor)
            self.mpos.append(value)

    def fly3d0(self, xmotor=0, ymotor=1, phimotor=6, scanname = ""):
        # xmotor is for flying
        # ymotor is for stepping
        # phimotor is for rotation
        axis = self.motornames[phimotor]
        self.signalmotor3 = axis
        self.signalmotorunit3 = self.motorunits[phimotor]
#        self.rpos3 = []
#        self.mpos3 = []
        pos = self.pts.get_pos(axis)
        self.isfly3 = False
        n = phimotor+1
        st = float(self.ui.findChild(QLineEdit, "ed_lup_%i_L"%n).text())
        fe = float(self.ui.findChild(QLineEdit, "ed_lup_%i_R"%n).text())
        #tm = float(self.ui.findChild(QLineEdit, "ed_lup_%i_t"%n).text())
        step = float(self.ui.findChild(QLineEdit, "ed_lup_%i_N"%n).text())

        if st>fe:
            step = -1*abs(step)
        if st<fe:
            step = abs(step)
        if self.ui.cb_reversescandir.isChecked():
            if abs(st-pos)>abs(fe-pos):
                t = fe
                fe = st
                st = t 
                step = -step
        self.pts.mv(axis, st)
        pos = np.arange(st, fe+step, step)
        if len(scanname):
            scanname=axis
        else:
#            print(scanname, axis)
            scanname=f"{scanname}{axis}"        
        for i, value in enumerate(pos):
            if self.isStopScanIssued:
                return
            self.pts.mv(axis, value)
            # fly here
            scan="%s%0.3d"%(scanname, i)
            self.fly2d0(xmotor=xmotor, ymotor=ymotor, scanname=scan)
#            r = self.get_qds_pos()
#            self.rpos3.append([r[0], r[1], r[2]])
#            self.mpos3.append(value)        

    def fly2d0(self, xmotor = 0, ymotor=1, scanname = ""):
        # xmotor is for flying
        # ymotor is for stepping
        axis = self.motornames[ymotor]
        self.signalmotor2 = axis
        self.signalmotorunit2 = self.motorunits[ymotor]
#        self.rpos2 = []
#        self.mpos2 = []
        pos = self.pts.get_pos(axis)
        self.isfly2 = False
        n = ymotor+1
        st = float(self.ui.findChild(QLineEdit, "ed_lup_%i_L"%n).text())
        fe = float(self.ui.findChild(QLineEdit, "ed_lup_%i_R"%n).text())
        #tm = float(self.ui.findChild(QLineEdit, "ed_lup_%i_t"%n).text())
        step = float(self.ui.findChild(QLineEdit, "ed_lup_%i_N"%n).text())

        if st>fe:
            step = -1*abs(step)
        if st<fe:
            step = abs(step)
        if self.ui.cb_reversescandir.isChecked():
            if abs(st-pos)>abs(fe-pos):
                t = fe
                fe = st
                st = t 
                step = -step
        self.pts.mv(axis, st)
        pos = np.arange(st, fe+step, step)
        print(pos)
        if len(scanname):
            scanname=axis
        else:
            scanname=f"{scanname}{axis}"
        for i, value in enumerate(pos):
            if self.isStopScanIssued:
                return
            self.pts.mv(axis, value)
            # fly here
            self.fly0(xmotor)
            filename = "%s%0.3d"%(scanname, i)
            self.save_qds(filename=filename)
#            r = self.get_qds_pos()
#            self.rpos2.append([r[0], r[1], r[2]])
#            self.mpos2.append(value)

    def fly0(self, motornumber):
        axis = self.motornames[motornumber]
        self.signalmotor = axis
        self.signalmotorunit = self.motorunits[motornumber]
        self.rpos = []
        self.mpos = []
        
        print("")
        isTestRun = self.ui.actionTestFly.isChecked()
        if isTestRun:
            print("**** Test Run:")
        self.isfly = True
        if self.ui.actionMemory_clear_before_scan.isChecked():
            try:
                s12softglue.memory_clear()
            except TimeoutError:
                print("softglue memory_clear timeout")
        # disable fit menu
        self.ui.actionFit_QDS_phi.setEnabled(False)

        n = motornumber+1

        if not self.ui.cb_keepprevscan.isChecked():
            self.clearplot()
        st = float(self.ui.findChild(QLineEdit, "ed_lup_%i_L"%n).text())
        fe = float(self.ui.findChild(QLineEdit, "ed_lup_%i_R"%n).text())
        tm = float(self.ui.findChild(QLineEdit, "ed_lup_%i_t"%n).text())
        try:
            step = float(self.ui.findChild(QLineEdit, "ed_lup_%i_N"%n).text())
        except:
            step = 0.1
            self.ui.findChild(QLineEdit, "ed_lup_%i_N"%n).setText("%0.3f"%step)
        if step<0.05:
            showerror('step time should equal or greater than 0.05 seconds.')
            return        
        pos = self.pts.get_pos(axis)
        if axis in self.pts.hexapod.axes:
            if self.ui.cb_reversescandir.isChecked():
                if abs(st-pos)>abs(fe-pos):
                    t = fe
                    fe = st
                    st = t 
                    step = -step
#            print(self.hexapod_flymode, "fly mode")
            if (self.hexapod_flymode==HEXAPOD_FLYMODE_WAVELET):
#            if (self.hexapod_flymode==HEXAPOD_FLYMODE_WAVELET) and (axis == "X"):
#                print("Running the fly scan with controller")
                direction = int(step)/abs(step)
                self.pts.hexapod.set_traj(axis, tm, fe-st, st, direction, abs(step), 50)
                #expt = np.around(self.pts.hexapod.scantime/self.pts.hexapod.pulse_number*0.75, 3)
                period = self.pts.hexapod.scantime/self.pts.hexapod.pulse_number
                expt = period-0.015
                # set the delay generator
                if expt != dg645_12ID._exposuretime:
                    dg645_12ID.set_pilatus_fly(expt)
                N_counts = s12softglue.number_acquisition(expt, self.pts.hexapod.pulse_number)
                print(f"Total {np.round(N_counts/self.pts.hexapod.pulse_number)} encoder positions will be collected per a shot.")
                if N_counts>100000:
                    print(f"******** CAUTION: Number of softglue counts: {N_counts} is larger than 100E3. Slow down the clock speed.")

                for det in self.detector:
                    if det is not None:
                        print(f"Exposure time set to %0.3f seconds for {det._prefix}."% expt)
                        try:
                            det.fly_ready(expt, self.pts.hexapod.pulse_number, period=period, isTest=isTestRun)
                        except TimeoutError:
                            print(f"Detector, {det._prefix}, hasnt started yet.")
                            #showerror("Detector timeout.")
                            return
                if not isTestRun:
                    self.pts.hexapod.run_traj(axis)
                while self.pts.ismoving(axis):
                    time.sleep(0.01)

            if (self.hexapod_flymode==HEXAPOD_FLYMODE_STANDARD):
#            if (self.hexapod_flymode==HEXAPOD_FLYMODE_STANDARD) or (axis != "X"):
                print(" Running the fly scan without controller")
                self.pts.mv(axis, st, wait=True)
                self._prev_vel,self._prev_acc = self.pts.get_speed(axis)
                print("     prev speed was ", self._prev_vel)
                print("     speed should be ", abs(fe-st)/tm)
                self.pts.set_speed(axis, abs(fe-st)/tm, None)
                time.sleep(0.02)
                self.pts.mv(axis, fe, wait=True)
                #print("Should be in run.")

        if motornumber >=6:
            # st = float(self.ui.ed_lup_7_L.text())
            # fe = float(self.ui.ed_lup_7_R.text())
            # tm = float(self.ui.ed_lup_7_t.text())
            if self.ui.cb_reversescandir.isChecked():
                if abs(st-pos)>abs(fe-pos):
                    t = fe
                    fe = st
                    st = t 
            if motornumber ==6:
                # enable fit menu
                self.ui.actionFit_QDS_phi.setEnabled(True)
                # scan start
                self._prev_vel, self._prev_acc = self.pts.get_speed(axis)
                self.pts.set_speed(axis, 36/2, 36/2*10)
#                print(f"Speed of phi is set to {self.pts.phi.vel}.")
                self.pts.mv('phi', st, wait=True)
                time.sleep(0.2)
                self.pts.set_speed(axis, abs(fe-st)/tm,abs(fe-st)/tm*10)
#                print(f"Speed of phi is set to {self.pts.phi.vel}.")
                self.pts.mv('phi', fe, wait=True)
                print("Should be in run.")
            else:
                self.pts.mv(axis, st, wait=True)
                #ax = self.pts.gonio.channel_names.index(axis)
                self._prev_vel,self._prev_acc = self.pts.get_speed(axis)
                print("prev speed was ", self._prev_vel)
                print("speed should be ", abs(fe-st)/tm)
                self.pts.set_speed(axis, abs(fe-st)/tm, abs(fe-st)/tm*10)
                time.sleep(0.02)
                self.pts.mv(axis, fe, wait=True)
                print("Should be in run.")
        #self.isscan = False

        # check if data collections are all done..
        for det in self.detector:
            if det is not None:
                det.ForceStop(2)

    def print_fly_settings(self, motornumber):
        print('')
        print("Currently, the flyscan only works for X axis of the hexapod.")
        print('==========================================================')
        print('')
        axis = self.motornames[motornumber]
        self.signalmotor = axis
        self.signalmotorunit = self.motorunits[motornumber]
        
        self.isfly = True
        n = motornumber+1
        st = float(self.ui.findChild(QLineEdit, "ed_lup_%i_L"%n).text())
        fe = float(self.ui.findChild(QLineEdit, "ed_lup_%i_R"%n).text())
        tm = float(self.ui.findChild(QLineEdit, "ed_lup_%i_t"%n).text())
        try:
            step = float(self.ui.findChild(QLineEdit, "ed_lup_%i_N"%n).text())
        except:
            step = 0.1
            self.ui.findChild(QLineEdit, "ed_lup_%i_N"%n).setText("%0.3f"%step)
        pos = self.pts.get_pos(axis)
        if axis in self.pts.hexapod.axes:
            if self.ui.cb_reversescandir.isChecked():
                if abs(st-pos)>abs(fe-pos):
                    t = fe
                    fe = st
                    st = t 
                    step = -step
            if (self.hexapod_flymode==HEXAPOD_FLYMODE_WAVELET) and (axis == "X"):
                direction = int(step)/abs(step)
                self.pts.hexapod.set_traj(axis, tm, fe-st, st, direction, abs(step), 50)
            else:
                print("Currently, the flyscan only works for X axis.")
        else:
            print("Currently, the flyscan only works for X axis of the hexapod.")
        print('==========================================================')
        print('')
        print('')

    def getfilename(self):
        w = QWidget()
        w.resize(320, 240)
        # Set window title
        w.setWindowTitle("Save QDS Data As")
        fn = QFileDialog.getSaveFileName(w, 'Save File', '', 'Text (*.txt *.dat)',None, QFileDialog.DontUseNativeDialog)
        filename = fn[0]
        if filename == "":
            return 0
        if ".txt" not in filename:
            filename = filename + ".txt"
        d = os.path.dirname(filename)
        if len(d) == 0:
            filename = os.path.join(self.working_folder, filename)
        else:
            self.working_folder = d
        return filename
    
    def save_qds(self, filename = '', saveoption = "w"):
        if type(filename)==bool:
            fn = ""
        if type(filename)==str:
            if len(filename) == 0:
                fn = ""
            else:
                fn = filename
        if len(fn) == 0:
            filename = self.getfilename()
        # data unit and data
        if self._qds_unit == QDS_UNIT_MM:
            self.rpos = self.rpos/1E3
        if self._qds_unit == QDS_UNIT_UM:
            pass
        if self._qds_unit == QDS_UNIT_NM:
            self.rpos = self.rpos*1E3
        self.save_list(filename, self.mpos, self.rpos, col=[0,1,2], option=saveoption)
        #self.pts.savedata(filename, self.mpos, self.rpos, col=[0,1,2])

    def save_list(self, filename, mpos, rpos, col, option="w"):
        with open(filename, option) as f:
            for i, m in enumerate(mpos):
                strv = ""
                for cind in col:
                    strv = "%s    %0.5e"%(strv, rpos[i][cind])
                f.write("%0.5e%s\n"%(m, strv))

    def savescan(self, filename=""):
        if self.is_selfsaved:
            self.save_qds(self.tempfilename, "a")
            filename = self.getfilename()
            os.rename(self.tempfilename, filename)
        else:
            self.save_qds(filename=filename)
        if self.is_selfsaved:
            self.is_selfsaved = False

    def fly_result(self, filename=""):
        if len(filename)==0:
            w = QWidget()
            w.resize(320, 240)
            # Set window title
            w.setWindowTitle("Save QDS Data As")
            fn = QFileDialog.getSaveFileName(w, 'Save File', '', 'Text (*.txt *.dat)',None, QFileDialog.DontUseNativeDialog)
            filename = fn[0]
            if filename == "":
                return 0
        # filename handling
        if ".txt" not in filename:
            filename = filename + ".txt"
        d = os.path.dirname(filename)
        if len(d) == 0:
            filename = os.path.join(self.working_folder, filename)
        else:
            self.working_folder = d
        # rea
        data = self.pts.hexapod.get_records()
        print("Done.. Preparing to plot.")
        if isinstance(data, type({})):
            l_data = [data]
        else:
            l_data = data
        try:
            qds_data = s12softglue.get_pos_array()
        except:
            showerror("PanDA is needed.")
            return
        qds_data = qds_data/1000
        #print(self.pts.hexapod.wave_start, " wave_start position")
        #qds_data = qds_data[-1]-qds_data-self.pts.hexapod.wave_start*1000
        axis = "X"
        for data in l_data:
            #ndata = data[axis][0].size
            #x = range(0, ndata)
            if len(filename)>0:
                target = data[axis][0]*1000
                encoded = data[axis][1]*1000
                target = target[self.pts.hexapod.pulse_positions_index]
                encoded = encoded[self.pts.hexapod.pulse_positions_index]
                try:
                    dt2 = np.column_stack((target, encoded, qds_data))
                    np.savetxt(filename, dt2, fmt="%1.8e %1.8e %1.8e")
                except:
                    print(target.shape, " encoded data")
                    print(encoded.shape, " encoded data")
                    print(qds_data.shape, " qds data")

    def clearplot(self):
        #self.isscan = False
        ax = self.figure.get_axes()
        for a in ax:
            a.clear()
        #lt.show()
        self.canvas.draw()

    def mv(self, motornumber=-1, val=None):
        if motornumber<0:
            pb = self.sender()
            objname = pb.objectName()
            val_text = pb.text()
            n = int(re.findall(r'\d+', objname)[0])
            #n = [int(s) for s in objname.split('_') if s.isdigit()][0]
            motornumber = n-1

        #print("motor number is ", motornumber)
        axis = self.motornames[motornumber]
        self.signalmotor = axis
        self.signalmotorunit = self.motorunits[motornumber]
        if type(val)==type(None):
            try:
                val = float(val_text)
            except:
                showerror('Text box is empty.')
                return
        #print(f"Move {axis} to {val}")
        w = move(self.pts, axis, val)
        #w.signal.finished.connect(self.scandone)
        self.threadpool.start(w)
        self.updatepos(axis)

    def mvr(self, motornumber=-1, sign=1, val=0):
        if motornumber ==-1:
            pb = self.sender()
            objname = pb.objectName()
            n = int(re.findall(r'\d+', objname)[0])
            #n = [int(s) for s in objname.split('_') if s.isdigit()][0]
            motornumber = n-1
        #print("motornumber is ", motornumber)
        axis = self.motornames[motornumber]
        self.signalmotor = axis
        #print("axis is ", axis)
        #print("sign is ", sign)
        self.signalmotorunit = self.motorunits[motornumber]
        if val==0:
            val = float(self.ui.findChild(QLineEdit, "ed_%i_tweak"%n).text())
        #print(f"Move {axis} by {sign*val}")

        w = mover(self.pts, axis, sign*val)
        self.threadpool.start(w)
        self.updatepos(axis)
    
    def update_qds(self):
        r = self.get_qds_pos()
#        print(r)
        self.ui.lcd_X.display("%0.3f" % (r[0]))     
        self.ui.lcd_Z.display("%0.3f" % (r[1]))
        self.ui.lcd_Z_2.display("%0.3f" % (r[2]))
        #self.rpos = []
        #self.mpos = []
        if self.isscan:
            if self.isfly:
                self.rpos.append([r[0], r[1], r[2]])
                self.mpos.append(self.get_motorpos(self.signalmotor))
            self.updatepos()
            self.plot()
        else:
            self.updatepos()

    def reset_qdsX(self):
        r = self.get_qds_pos(False)
        self.ref_X = r[0]
        #self.ref_X = self.ui.lcd_X.value()  

    def reset_qdsZ(self):
        r = self.get_qds_pos(False)
        self.ref_Z = r[1]
#        self.ref_Z = self.ui.lcd_Z.value()

    def reset_qdsZ2(self):
        r = self.get_qds_pos(False)
        self.ref_Z2 = r[2]
#        self.ref_Z = self.ui.lcd_Z.value()

    def record_qdsX(self, value):
        txt = str(self.ui.lcd_X.value())
        if value==1:
            self.ui.x1.setText(txt)
        if value==2:
            self.ui.x2.setText(txt)
        if value==3:
            self.ui.x3.setText(txt)

    def record_qdsZ(self, value):
        txt = str(self.ui.lcd_Z.value())
        if value==1:
            self.ui.z1.setText(txt)
        if value==2:
            self.ui.z2.setText(txt)
        if value==3:
            self.ui.z3.setText(txt)
        if value>3:
            txt = str(self.ui.lcd_Z_2.value())
            if value==1:
                self.ui.z1_2.setText(txt)
            if value==2:
                self.ui.z2_2.setText(txt)
            if value==3:
                self.ui.z3_2.setText(txt)

    def plot(self):
        
        r = np.asarray(self.rpos)
        pos = np.asarray(self.mpos)
        try:
            xl = f"{self.signalmotor} ({self.signalmotorunit})"
        except:
            xl = ""

        try:
            self.ax.clear()
            self.ax.plot(pos, r[:,0], 'r')
            self.ax.set_xlabel(xl)
            yl = 'X position (um)'
            self.ax.set_ylabel(yl)
            self.ax2.clear()
            self.ax2.plot(pos, r[:,1], 'b')
            self.ax2.set_xlabel(xl)
            self.ax3.clear()
            self.ax3.plot(pos, r[:,2], 'k')
            self.ax3.set_xlabel(xl)
            yl = 'Z position (um)'
            self.ax2.set_ylabel(yl)
            self.ax3.set_ylabel(yl)
        except:
            print("There was error in the plot")
            pass
        self.canvas.draw()

    @QtCore.pyqtSlot(str, float, float, float, float)
    def set_data(self, axis, L, R, step, rt):
#        print(axis, L, R, rt, step)
        motornumber = self.motornames.index(axis)
        n = motornumber+1
        self.ui.findChild(QLineEdit, "ed_lup_%i_L"%n).setText(str(L))
        self.ui.findChild(QLineEdit, "ed_lup_%i_R"%n).setText(str(R))
        self.ui.findChild(QLineEdit, "ed_lup_%i_t"%n).setText(str(rt))
        self.ui.findChild(QLineEdit, "ed_lup_%i_N"%n).setText(str(step))
    
    @QtCore.pyqtSlot(dict)
    def run_json(self, json_message):
        #data = json.loads(json_message)
        # return_message = None
        cmd = json_message['command']
        scanname = ""
        try:
            data = json_message['data']
        except:
            data = {}

        try:
            xmotor = int(data['xmotor'])
        except:
            xmotor = DEFAULTS['xmotor']
        try:
            ymotor = int(data['ymotor'])
        except:
            ymotor = DEFAULTS['ymotor']
        try:
            phimotor = int(data['phimotor'])
        except:
            phimotor = DEFAULTS['phimotor']
        try:
            scanname = int(data['scanname'])
        except:
            scanname = ""
        try:
            folder = int(data['folder'])
        except:
            folder = ""

        if cmd == 'setrange':
            motornumber = self.motornames.index(data['axis'])
            n = motornumber+1
            for key, val in data.items():
                if key=='axis':
                    pass
                else:
                    self.ui.findChild(QLineEdit, "ed_lup_%i_%s"%(n, key)).setText(val)
        elif cmd == 'mv':
            for axis, pos in data.items():
                #self.set_mv(self, axis, float(pos))
                motornumber = self.motornames.index(axis)
                self.mv(motornumber=motornumber, val=float(pos))
        elif cmd == 'mvr':
            for axis, pos in data.items():
                motornumber = self.motornames.index(axis)
                self.mvr(motornumber=motornumber, val=float(pos))
        elif cmd == 'run2d':
            self.fly2d(xmotor=xmotor,ymotor=ymotor,scanname=scanname)
        elif cmd == 'run3d':
            self.fly3d(xmotor=xmotor,ymotor=ymotor,phimotor=phimotor,scanname=scanname)
        elif cmd == 'none':
            self.runRequested.emit(0)
        elif cmd == "toggle":
            try:
                val = data['controllerfly']
                if val == "on":
                    self.ui.actionEnable_fly_with_controller.setChecked(True)
                if val == "off":
                    self.ui.actionEnable_fly_with_controller.setChecked(False)
            except:
                pass
            try:
                val = data['keepprevscan']
                if val == "on":
                    self.ui.cb_keepprevscan.setChecked(True)
                if val == "off":
                    self.ui.cb_keepprevscan.setChecked(False)
            except:
                pass
            try:
                val = data['reversescan']
                if val == "on":
                    self.ui.cb_reversescandir.setChecked(True)
                if val == 'off':
                    self.ui.cb_reversescandir.setChecked(False)
            except:
                pass
        elif cmd == "setfolder":
            self.working_folder = folder
            self.update_workingfolder(self.working_folder)
        else:
            print(f"Invalid command {cmd} is recieved.")

    @QtCore.pyqtSlot(int)
    def run_cmd(self, n):
#        print("run_cmd is called")
        if n==0:
            print("None is sent from the client.")
        if n==2:
            self.fly2d()
        if n==3:
            self.fly3d()

    @QtCore.pyqtSlot(str, float)
    def set_mv(self, axis, pos):
        motornumber = self.motornames.index(axis)
        self.mv(motornumber=motornumber, val=pos)


def main():
#    run gui with server option
    app = QApplication(sys.argv)
    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)
    a = tweakmotors()

    with loop:
        _, protocol = loop.run_until_complete(create_server(loop))
        protocol.rangeChanged.connect(a.set_data)
        protocol.runRequested.connect(a.run_cmd)
        protocol.mvRequested.connect(a.set_mv)
        protocol.jsonReceived.connect(a.run_json)
        loop.run_forever()

def main_no_server():
    # non-server option
    app = QApplication(sys.argv)
    a = tweakmotors()
    sys.exit(app.exec_())

if __name__ == "__main__":
    # server option included..
    main()
