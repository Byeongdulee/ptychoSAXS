# -*- coding: utf-8 -*-
"""
Created on Thu Oct 27 16:42:18 2016

@author: Byeongdu Lee
@Date: Nov. 1. 2016
"""

import sys 
import os

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

# panda box...
import asyncio
#import sys
from pandablocks.blocking import BlockingClient
from pandablocks.commands import Put
pandaip = "164.54.122.90"
from pandablocks.asyncio import AsyncioClient
from pandablocks.commands import Put
from pandablocks.hdf import write_hdf_files
import h5py
import re
import analysis.planeeqn as eqn

HEXAPOD_FLYMODE_WAVELET = 0
HEXAPOD_FLYMODE_STANDARD = 1
QDS_UNIT_NM = 0
QDS_UNIT_UM = 1
QDS_UNIT_MM = 2
QDS_UNIT_DEFAULT = QDS_UNIT_UM  # default QDS output is um

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

def disarm_panda():

    with BlockingClient(pandaip) as client:
        client.send(Put("BITS.A", 0))


pandafn = "C:/Users/s12idc/Documents/GitHub/panda-capture.h5"

async def arm_and_hdf():
    # Create a client and connect the control and data ports
    async with AsyncioClient(pandaip) as client:
        try:
            # Put to 2 fields simultaneously
            await asyncio.gather(
                client.send(Put("BITS.A", 1)),
            )
            # Listen for data, arming the PandA at the beginning
            
            await write_hdf_files(client, file_names=iter((pandafn,)), arm=True)
        except:
            pass

def get_pandadata():
    h = h5py.File(pandafn, "r")
    d = h["INENC2.VAL.Value"][()]
    return d

class tweakmotors(QMainWindow):
    resized = QtCore.pyqtSignal()

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
        for i, name in enumerate(self.pts.gonio.channel_names):
            #if self.pts.gonio.connected[i]:
            motornames.append(name)
        for name in self.pts.gonio.units:
            motorunits.append(name)

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
            if self.pts.isconnected(name):
                self.motornames.append(name)
                self.motorunits.append(motorunits[i])
        # motors for 2d and 3d scans.....
        xm = self.motornames.index('X')
        ym = self.motornames.index('Y')
        phim = self.motornames.index('phi')
        # update GUI
        for i, name in enumerate(self.motornames):
            n = i+1
            self.ui.findChild(QLabel, "lb%i"%n).setText(name)
            self.ui.findChild(QPushButton, "pb_tweak%iL"%n).clicked.connect(lambda: self.mvr(None, -1))
            self.ui.findChild(QPushButton, "pb_tweak%iR"%n).clicked.connect(lambda: self.mvr(None, 1))
            self.ui.findChild(QPushButton, "pb_lup_%i"%n).clicked.connect(lambda: self.stepscan(None))
            self.ui.findChild(QPushButton, "pb_SAXSscan_%i"%n).clicked.connect(lambda: self.fly(None))
            self.ui.findChild(QLineEdit, "ed_%i"%n).returnPressed.connect(lambda: self.mv(None))
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
        # self.ui.pb_tweak1L.clicked.connect(lambda: self.mvr(0, -1))
        # self.ui.pb_tweak1R.clicked.connect(lambda: self.mvr(0, 1))
        # self.ui.pb_tweak2L.clicked.connect(lambda: self.mvr(1, -1))
        # self.ui.pb_tweak2R.clicked.connect(lambda: self.mvr(1, 1))
        # self.ui.pb_tweak3L.clicked.connect(lambda: self.mvr(2, -1))
        # self.ui.pb_tweak3R.clicked.connect(lambda: self.mvr(2, 1))
        # self.ui.pb_tweak4L.clicked.connect(lambda: self.mvr(3, -1))
        # self.ui.pb_tweak4R.clicked.connect(lambda: self.mvr(3, 1))
        # self.ui.pb_tweak5L.clicked.connect(lambda: self.mvr(4, -1))
        # self.ui.pb_tweak5R.clicked.connect(lambda: self.mvr(4, 1))
        # self.ui.pb_tweak6L.clicked.connect(lambda: self.mvr(5, -1))
        # self.ui.pb_tweak6R.clicked.connect(lambda: self.mvr(5, 1))
        # self.ui.pb_tweak7L.clicked.connect(lambda: self.mvr(6, -1))
        # self.ui.pb_tweak7R.clicked.connect(lambda: self.mvr(6, 1))
        # self.ui.ed_1.returnPressed.connect(lambda: self.mv(0))
        # self.ui.ed_2.returnPressed.connect(lambda: self.mv(1))
        # self.ui.ed_3.returnPressed.connect(lambda: self.mv(2))
        # self.ui.ed_4.returnPressed.connect(lambda: self.mv(3))
        # self.ui.ed_5.returnPressed.connect(lambda: self.mv(4))
        # self.ui.ed_6.returnPressed.connect(lambda: self.mv(5))
        # self.ui.ed_7.returnPressed.connect(lambda: self.mv(6))

        # self.ui.pb_SAXSscan_1.setEnabled(True)
        # self.ui.pb_SAXSscan_2.setEnabled(False)
        # self.ui.pb_SAXSscan_3.setEnabled(False)
        # self.ui.pb_SAXSscan_4.setEnabled(False)
        # self.ui.pb_SAXSscan_5.setEnabled(False)
        # self.ui.pb_SAXSscan_6.setEnabled(False)
        # self.ui.pb_SAXSscan_7.setEnabled(True)

        # self.ui.pb_lup_1.clicked.connect(lambda: self.stepscan(0))
        # self.ui.pb_lup_2.clicked.connect(lambda: self.stepscan(1))
        # self.ui.pb_lup_3.clicked.connect(lambda: self.stepscan(2))
        # self.ui.pb_lup_4.clicked.connect(lambda: self.stepscan(3))
        # self.ui.pb_lup_5.clicked.connect(lambda: self.stepscan(4))
        # self.ui.pb_lup_6.clicked.connect(lambda: self.stepscan(5))
        # self.ui.pb_lup_7.clicked.connect(lambda: self.stepscan(6))
        # self.ui.pb_SAXSscan_1.clicked.connect(lambda: self.fly(0))
        # self.ui.pb_SAXSscan_7.clicked.connect(lambda: self.fly(6))
        self.ui.actionRun.triggered.connect(self.timescan)
        self.ui.actionStop.triggered.connect(self.timescanstop)
        self.ui.actionClear.triggered.connect(self.clearplot)
        self.ui.actionEnable_fly_with_controller.setCheckable(True)
        self.ui.actionEnable_fly_with_controller.setChecked(False)
        self.ui.actionEnable_fly_with_controller.triggered.connect(self.select_flymode) # hexapod flyscan type.
        self.ui.actionSet_the_default_vel_acc.triggered.connect(self.sethexapodvel_default)  # hexapod set vel acc into default
        self.ui.actionSet_default_speed.triggered.connect(self.setphivel_default)
        self.ui.actionSave.triggered.connect(self.save_qds)
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
 
        self.ui.action2D_scan.triggered.connect(lambda: self.fly2d(xm, ym))
        self.ui.action3D_scan.triggered.connect(self.fly3d(xm, ym, phim))
        
        self.pts.signals.AxisPosSignal.connect(self.update_motorpos)
        self.pts.signals.AxisNameSignal.connect(self.update_motorname)

        self.rpos = []
        self.mpos = []
        self.threadpool = QThreadPool.globalInstance()
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
        
        # figure to plot
        # a figure instance to plot on
        self.figure = plt.figure()

        #self.ui.
        self.px = 1/plt.rcParams['figure.dpi']  # pixel in inches
        print(plt.rcParams['figure.subplot.left'])
        print(plt.rcParams['figure.subplot.bottom'] )
        print(plt.rcParams['figure.subplot.right'] )
        print(plt.rcParams['figure.subplot.top'])

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

        self.updatepos()

        #self.ui.installEventFilter(self)
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_qds)
        self.timer.start(100)        
        self.ui.show()
        #self.resized.connect(self.resizeFunction)

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
    
    def select_qds_x(self):
        text, ok = QInputDialog().getItem(self, "Select QDS units",
                                            "Units:", ('0', '1', '2'), current=1, editable=False)
        self._qds_x_sensor = int(text)

    def select_qds_y(self):
        text, ok = QInputDialog().getItem(self, "Select QDS units",
                                            "Units:", ('0', '1', '2'), current=1, editable=False)
        self._qds_y_sensor = int(text)

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
    
    def select_flymode(self):
        if self.ui.actionEnable_fly_with_controller.isChecked():  # when checked, this value is False
            self.ui.actionEnable_fly_with_controller.setChecked(True)
            self.hexapod_flymode = HEXAPOD_FLYMODE_WAVELET
        else:
            self.hexapod_flymode = HEXAPOD_FLYMODE_STANDARD
            self.ui.actionEnable_fly_with_controller.setChecked(False)

    def flydone(self, value):
        self.isscan = False
        self.updatepos()
        if self.signalmotor not in self.pts.hexapod.axes:        
            self.pts.set_speed(self.signalmotor, self._prev_vel, self._prev_acc)
        print("fly done.......")
    
    def timescanstop(self):
        self.isscan = False

    def timescan(self):
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
        w = Worker(self.timescan0)
        w.signal.finished.connect(self.scandone)
        self.threadpool.start(w)
        

    # def createtimescanthread(self):
    #     thread = QThread()
    #     w = Worker()
    #     w.moveToThread(thread)
    #     thread.started.connect(self.timescan0)
    #     w.progress.connect(self.update_graph)
    #     w.finished.connect(thread.quit)
    #     w.finished.connect(w.deleteLater)
    #     thread.finished.connect(thread.deleteLater)
    #     return thread

    def fly2d(self, xmotor=0, ymotor=1):
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
        w = Worker(self.fly2d0, xmotor, ymotor)
        w.signal.finished.connect(self.flydone)
        self.threadpool.start(w)

    def fly3d(self, xmotor=0, ymotor=1, phimotor=6):
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
        w = Worker(self.fly3d0, xmotor, ymotor, phimotor)
        w.signal.finished.connect(self.flydone)
        self.threadpool.start(w)

    def fly(self, motornumber=-1):
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
        while self.isscan:
            r = self.get_qds_pos()
            self.rpos.append([r[0], r[1], r[2]])
            t = time.time()-self.t0
            self.mpos.append(t)
            time.sleep(0.1)

    def get_qds_pos(self, isrefavailable = True):
        r, a = self.pts.qds.get_position()
        r = r[0]
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
            scanname="%s_%s"%(scanname, axis)        
        for i, value in enumerate(pos):
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
        if len(scanname):
            scanname=axis
        else:
            scanname="%s_%s"%scanname
        for i, value in enumerate(pos):
            self.pts.mv(axis, value)
            # fly here
            self.fly0(xmotor)
            self.save_qds(filename="%s%0.3d"%(scanname, i))
#            r = self.get_qds_pos()
#            self.rpos2.append([r[0], r[1], r[2]])
#            self.mpos2.append(value)

    def fly0(self, motornumber):
        axis = self.motornames[motornumber]
        self.signalmotor = axis
        self.signalmotorunit = self.motorunits[motornumber]
        self.rpos = []
        self.mpos = []
        
        self.isfly = True
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
        pos = self.pts.get_pos(axis)
        if axis in self.pts.hexapod.axes:
            if self.ui.cb_reversescandir.isChecked():
                if abs(st-pos)>abs(fe-pos):
                    t = fe
                    fe = st
                    st = t 
                    step = -step
#            print(self.hexapod_flymode, "fly mode")
            if (self.hexapod_flymode==HEXAPOD_FLYMODE_WAVELET) and (axis == "X"):
                print("Run the fly scan with controller")
                self.pts.hexapod.set_traj(tm, fe-st, st, 50, step)
                self.pts.hexapod.run_traj()
                while self.pts.ismoving(axis):
                    time.sleep(0.01)

            if (self.hexapod_flymode==HEXAPOD_FLYMODE_STANDARD) or (axis != "X"):
                print("Run the fly scan without controller")
                self.pts.mv(axis, st, wait=True)
                self._prev_vel,self._prev_acc = self.pts.get_speed(axis)
                print("prev speed was ", self._prev_vel)
                print("speed should be ", abs(fe-st)/tm)
                self.pts.set_speed(axis, abs(fe-st)/tm, None)
                time.sleep(0.02)
                self.pts.mv(axis, fe, wait=True)
                print("Should be in run.")

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

    def save_qds(self, filename = ''):
        if len(filename) == 0:
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
        if self._qds_unit == QDS_UNIT_MM:
            self.rpos = self.rpos/1E3
        if self._qds_unit == QDS_UNIT_UM:
            pass
        if self._qds_unit == QDS_UNIT_NM:
            self.rpos = self.rpos*1E3
        self.pts.savedata(filename, self.mpos, self.rpos, col=[0,1,2])

    def savescan(self, filename=""):
        self.save_qds(filename=filename)

    def fly_result(self):
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


        data = self.pts.hexapod.get_records()
        print("Done.. Preparing to plot.")
        if isinstance(data, type({})):
            l_data = [data]
        else:
            l_data = data
        try:
            qds_data = get_pandadata()
        except:
            showerror("PanDA is needed.")
            return
        qds_data = qds_data/1000
        print(self.pts.hexapod.wave_start, " wave_start position")
        qds_data = qds_data[-1]-qds_data-self.pts.hexapod.wave_start*1000
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
        self.isscan = False
        ax = self.figure.get_axes()
        for a in ax:
            a.clear()
        #lt.show()
        self.canvas.draw()

    def mv(self, motornumber):
        pb = self.sender()
        objname = pb.objectName()
        n = int(re.findall(r'\d+', objname)[0])
        #n = [int(s) for s in objname.split('_') if s.isdigit()][0]
        motornumber = n-1
        #print("motor number is ", motornumber)
        axis = self.motornames[motornumber]
        self.signalmotor = axis
        self.signalmotorunit = self.motorunits[motornumber]
        try:
            val = float(pb.text())
        except:
            showerror('Text box is empty.')
            return
        #print(f"Move {axis} to {val}")
        w = move(self.pts, axis, val)
        #w.signal.finished.connect(self.scandone)
        self.threadpool.start(w)
        self.updatepos(axis)

    def mvr(self, motornumber, sign):
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
#        print(self.isscan, " isscan...")
        if self.isscan:
            if self.isfly:
                self.rpos.append([r[0], r[1], r[2]])
                self.mpos.append(self.get_motorpos(self.signalmotor))
            self.updatepos()
            self.plot()

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
        
        #import random
        ''' plot some random stuff '''
        # random data
        #data = [random.random() for i in range(10)]

        # discards the old graph
        # ax.hold(False) # deprecated, see above


        r = np.asarray(self.rpos)
        pos = np.asarray(self.mpos)
        xl = f"{self.signalmotor} ({self.signalmotorunit})"

        if not self.ui.cb_keepprevscan.isChecked():
            self.ax.cla()
            self.ax2.cla()
            self.ax3.cla()
        try:
            self.ax.plot(pos, r[:,0], 'r')
            self.ax.set_xlabel(xl)
            yl = 'X position (um)'
            self.ax.set_ylabel(yl)
            self.ax2.plot(pos, r[:,1], 'b')
            self.ax2.set_xlabel(xl)
            self.ax3.plot(pos, r[:,2], 'k')
            self.ax3.set_xlabel(xl)
            yl = 'Z position (um)'
            self.ax2.set_ylabel(yl)
            self.ax3.set_ylabel(yl)
        except:
            print("There was error in the plot")
            pass
        #plt.show()
        self.canvas.draw()



if __name__ == "__main__":
#    import logging
#    logging.basicConfig(level=logging.INFO)
    from PyQt5.QtWidgets import QMainWindow
    app = QApplication(sys.argv)
    a = tweakmotors()
    sys.exit(app.exec_())
