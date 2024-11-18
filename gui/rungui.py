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
import epics

from PyQt5 import uic, QtCore, QtGui
from PyQt5.QtWidgets import QMainWindow, QApplication, QPushButton, QFileDialog, QWidget, QFormLayout
from PyQt5.QtWidgets import QLabel, QLineEdit, QMessageBox, QInputDialog, QDialog, QDialogButtonBox
from PyQt5.QtCore import QTimer, QObject, pyqtSlot, pyqtSignal, QRunnable, QThreadPool, QSize

import time

sys.path.append('..')

from ptychosaxs import instruments
pts = instruments()

from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.backends.backend_qt5agg import NavigationToolbar2QT as NavigationToolbar
import matplotlib.pyplot as plt

# plot of struck scaler.
import datetime
#import matplotlib.pyplot as plt
#import matplotlib.animation as animation
#import random


import numpy as np

#from tools.panda import get_pandadata
from tools.softglue import sgz_pty
s12softglue = sgz_pty()

#Delay generator
import tools.dg645 as dg645
dg645_12ID = dg645.dg645_12ID.open_from_uri(dg645.ADDRESS_12IDC)

# struck
from tools import struck

# detectors
from tools.detectors import pilatus
import re
import analysis.planeeqn as eqn
import py12inifunc

from typing import List

HEXAPOD_FLYMODE_WAVELET = 0
HEXAPOD_FLYMODE_STANDARD = 1
FRACTION_EXPOSURE_PERIOD = 0.2
DETECTOR_READOUTTIME = 0.02
QDS_UNIT_NM = 0
QDS_UNIT_UM = 1
QDS_UNIT_MM = 2
QDS_UNIT_DEFAULT = QDS_UNIT_UM  # default QDS output is um
DEFAULTS = {'xmotor':0, 'ymotor':2, 'phimotor':6}  #vertical stage is Z in the scan_gui, change 'ymotor' from 1 to 2, JD
inifilename = "pty-co-saxs.ini"

def rstrip_from_char(string, char):
    """Removes characters from the right of the string starting from the first occurrence of 'char'."""
#    print(f'{string=}')
#    print(f'{char=}')
    if char in string:
        index = string.rfind(char)
        return string[:index]
    return string

async def showerror(msg):
    dlg = QMessageBox()
    dlg.setIcon(QMessageBox.Warning)
    dlg.setText(msg)
    dlg.setWindowTitle("Error")
    dlg.setStandardButtons(QMessageBox.Ok | QMessageBox.Cancel)
    result = dlg.exec_()
    return result

class InputDialog(QDialog):
    def __init__(self, labels:List[str], parent=None):
        super().__init__(parent)
        
        buttonBox = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        layout = QFormLayout(self)
        
        self.inputs = []
        for lab in labels:
            self.inputs.append(QLineEdit(self))
            layout.addRow(lab, self.inputs[-1])
        
        layout.addWidget(buttonBox)
        
        buttonBox.accepted.connect(self.accept)
        buttonBox.rejected.connect(self.reject)
    
    def getInputs(self):
        return tuple(input.text() for input in self.inputs)

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

## shutter stuff.. Could be eventually separated out.
def open_shutter(self):
    epics.caput('12ida2:rShtrC:Open', 1)

def close_shutter(self):
    epics.caput('12ida2:rShtrC:Open', 0)


class beamstatus(QObject):
    onChange = QtCore.pyqtSignal()
    def __init__(self):
        # A station shutter..
        self.shutter_val = epics.PV('PB:12ID:STA_A_FES_CLSD_PL', callback=self.checkshutter)
        self.shutter = epics.PV('12ida2:rShtrA:Open')

    def checkshutter(self, value, **kws):
        if value == 0:
            self.signal.onChange.emit(False)
        if value == 1:
            self.signal.onChange.emit(True)

    def open_shutter(self):
        self.shutter.put(1)

    def close_shutter(self):
        self.shutter.put(0)

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
        self.hexapod_flymode = HEXAPOD_FLYMODE_WAVELET
#        self.hexapod_flymode = HEXAPOD_FLYMODE_STANDARD

        self.is_selfsaved = False
        self.parameters = py12inifunc.ini(inifilename)
        # When you need new field to inifile, edit the ini file first.
        try:
            self.parameters.readini()
        except:
            self.parameters._qds_unit = QDS_UNIT_DEFAULT
            self.parameters._qds_x_sensor = 0
            self.parameters._qds_y_sensor = 1
            self.parameters.countsperexposure = 0
            self.parameters.working_folder = ""
            self.parameters._ref_X = 0
            self.parameters._ref_Z = 0
            self.parameters._ref_Z2 = 0
            self.parameters._qds_time_interval = 0.1
            self.parameters._waittime_between_scans = 1 
            self.parameters._qds_R_vert = 10.0 # 10mm
            self.parameters._qds_th0_vert = -30.0 # degree
            self.parameters._qds_R_cyl = 50.0 # mm
            self.parameters.softglue_channels = ['B', 'C', 'D']
            self.parameters.logfilename = ""
            self.parameters.scan_number = 0
            self.parameters._ratio_exp_period = FRACTION_EXPOSURE_PERIOD

        self.isscan = False
        self.isfly = False

        if not hasattr(self.pts.gonio, 'channel_names'):
            self.pts.gonio.channel_names = [""]
            self.pts.gonio.units = [""]
        for i, name in enumerate(self.pts.gonio.channel_names):
            if len(name)>0:
                motornames.append(name)
        for unit in self.pts.gonio.units:
            if len(unit)>0:
                motorunits.append(unit)

        # append newport_piezo
        motornames.append('newport_piezo1')
        motornames.append('newport_piezo2')
        motornames.append('newport_piezo3')
        motorunits.append('mm')
        motorunits.append('mm')
        motorunits.append('mm')

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
#        print(motornames, " line 241")
        for i, name in enumerate(motornames):
            try:
#                print(name)
#                print(self.pts.isconnected(name))
                if self.pts.isconnected(name):
#                    print(name)
                    self.motornames.append(name)
                    self.motorunits.append(motorunits[i])
            except:
                print(f"{name} is not connected.")
                pass
#        print(motornames, " line 252")
        # motors for 2d and 3d scans.....
        #xm = self.motornames.index('X') #JD
        #ym = self.motornames.index('Y') #JD
        #Better get them from DEFAULTS instead of hard coded. 
        xm=DEFAULTS['xmotor']  #JD
        ym=DEFAULTS['ymotor']  #JD

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
#            print(name, " This is in tweakmtors .....")
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
        
        self.ui.actionSet_Log_Filename.triggered.connect(self.set_logfilename)
        self.ui.actionRun.triggered.connect(self.timescan)
        self.ui.actionStop.triggered.connect(self.timescanstop)
        self.ui.actionClear.triggered.connect(self.clearplot)
        self.ui.actionEnable_fly_with_controller.setCheckable(True)
        self.ui.actionEnable_fly_with_controller.setChecked(True)
        self.ui.actionEnable_fly_with_controller.triggered.connect(self.select_flymode) # hexapod flyscan type.
        self.ui.actionSet_the_default_vel_acc.triggered.connect(self.sethexapodvel_default)  # hexapod set vel acc into default
        self.ui.actionSet_default_speed.triggered.connect(self.setphivel_default)
        self.ui.actionSave.triggered.connect(self.savescan)
        self.ui.actionSave_flyscan_result.triggered.connect(self.fly_result)
        self.ui.actionFit_QDS_phi.setEnabled(False)
        self.ui.actionFit_QDS_phi.triggered.connect(self.fit_wobble_eccentricity)
        self.ui.actionSet_Interferometer_Param.triggered.connect(self.set_interferometer_params)
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
        self.ui.actionevery_10_millie_seconds.triggered.connect(lambda: self.set_softglue_in(3))
        self.ui.actionPrint_flyscan_settings.triggered.connect(lambda: self.print_fly_settings(0))
        self.ui.actionSAXS.triggered.connect(lambda: self.select_detectors(1))
        self.ui.actionWAXS.triggered.connect(lambda: self.select_detectors(2))
        self.ui.actionStruck.triggered.connect(lambda: self.select_detectors(3))
        self.ui.actionReset_to_Fly_mode.triggered.connect(self.reset_det_flymode)
        self.ui.actionChannels_to_record.triggered.connect(self.choose_softglue_channels)
        self.ui.actionSave_current_results.triggered.connect(self.save_softglue)
        self.pts.signals.AxisPosSignal.connect(self.update_motorpos)
        self.pts.signals.AxisNameSignal.connect(self.update_motorname)
        self.ui.actionTestFly.triggered.connect(self.scantest)
        self.ui.ed_workingfolder.setText(self.parameters.working_folder)
        self.ui.ed_workingfolder.returnPressed.connect(self.update_workingfolder)
        self.ui.ed_scanname.returnPressed.connect(lambda: self.update_scanname(False))
        self.ui.actionSet_waittime_between_scans.triggered.connect(self.set_waittime_between_scans)
        self.ui.actionMonitor_Beamline_Status.triggered.connect(self.set_monitor_beamline_status)
        self.ui.actionUse_hdf_plugin.triggered.connect(self.set_hdf_plugin_use)
        self.ui.le_scannumber.setText(str(int(self.parameters.scan_number)+1))
        self.ui.actionRatio_of_exptime_period_for_Flyscan.triggered.connect(self.set_exp_period_ratio)
#        self.ui.ed_scanname.returnPressed.connect(self.update_scannumber)
        self.use_hdf_plugin = True

        if os.name != 'nt':
            self.ui.menuQDS.setDisabled(True)

        # Struck
        self.isStruckCountNeeded = False

        # set default softglue collection freq. 10 micro seconds.
        s12softglue.set_count_freq(100)

        self.rpos = []
        self.mpos = []
        self.threadpool = QThreadPool.globalInstance()

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
        self.ui.progressBar.setValue(0)

        ## shutter control
#        self.shutter_status = epics.PV('12idc:scaler1.CNT', callback=self.checkshutter) # to test.
        self.shutter_status = epics.PV('PB:12ID:STA_C_SCS_CLSD_PL.VAL', callback=self.checkshutter)

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
        self.det_readout_time = 0.02 # detector minimum readout time.
        self.detector = [None]*2

        if os.name == 'nt':
            self.timer = QTimer()
            self.timer.timeout.connect(self.update_qds)
            self.timer.start(100)        
        self.ui.show()
        #self.resized.connect(self.resizeFunction)

    def set_hdf_plugin_use(self):
        if self.ui.actionUse_hdf_plugin.isChecked():
            self.ui.actionUse_hdf_plugin.setChecked(True)
            self.use_hdf_plugin = True
        else:
            self.ui.actionUse_hdf_plugin.setChecked(False)
            self.use_hdf_plugin = False

    def set_monitor_beamline_status(self):
        if self.ui.actionMonitor_Beamline_Status.isChecked():
            self.ui.actionMonitor_Beamline_Status.setChecked(True)
        else:
            self.ui.actionMonitor_Beamline_Status.setChecked(False)

    def checkshutter(self, value, **kws):
        if not self.ui.actionMonitor_Beamline_Status.isChecked():
            return
        shutter_events = {"time":time.time(), "state": value}
        #print(f"Value of the shutter is {value}")
        if value==0:
            self.isOK2run = False
            self.run_hold(shutter_events)
        else:
            self.isOK2run = True

    def run_hold(self, sevnt):
        print("run hold executed. This will hold the scan.")
        self.shutter_events = sevnt
        while self.isOK2run==False:
            time.sleep(10)
        
    def run_resume(self):
        pass

    def set_interferometer_params(self):
        #dialog = InputDialog(labels=["R0 for the top sensor(mm)","th0 for the top sensor(mm)"])
        value, okPressed = QInputDialog.getDouble(self, "The top sensor positions","R0 (mm):", self.parameters._qds_R_vert)
        if okPressed:
            self.parameters._qds_R_vert = value
        value, okPressed = QInputDialog.getDouble(self, "The top sensor positions","th (deg):", self.parameters._qds_th0_vert, -360.0, 360.0, 2)
        if okPressed:
            self.parameters._qds_th0_vert = value
        value, okPressed = QInputDialog.getDouble(self, "The horizontal sensor positions","R (mm):", self.parameters._qds_R_cyl)
        if okPressed:
            self.parameters._qds_R_cyl = value
        self.parameters.writeini()

    def set_logfilename(self):
        strv = self.parameters.logfilename
        if len(strv)>0:
            strv = os.path.basename(strv)
        text, okPressed = QInputDialog.getText(self, "Log file","Filename:", QLineEdit.Normal, strv)
        if okPressed:
            foldername = self.ui.ed_workingfolder.text()
            self.parameters.logfilename = os.path.join(foldername, text)
            self.parameters.scan_number = 0
            scaninfo = []
            scaninfo.append('#I logging started on')
            scaninfo.append(time.ctime())
            self.write_scaninfo_to_logfile(scaninfo)
        self.parameters.writeini()

    def set_waittime_between_scans(self):
        if hasattr(self.parameters, '_waittime_between_scans'):
            wtime = self.parameters._waittime_between_scans
        else:
            wtime = 1.0
        value, okPressed = QInputDialog.getDouble(self, "How long stay idle between scans?","sleep time (s):", wtime)
        if okPressed:
            self.parameters._waittime_between_scans = value
            self.parameters.writeini()
#            print(self.parameters.softglue_channels)

    def update_workingfolder(self, folder=""):
        if len(folder) == 0:
            self.parameters.working_folder = self.ui.ed_workingfolder.text()
            self.parameters.writeini()
        else:
            self.ui.ed_workingfolder.setText(self.parameters.working_folder)

    def update_scanname(self, update_detector = True):
        txt = self.ui.ed_scanname.text()
        self.parameters.scan_number = int(self.ui.le_scannumber.text())
        txt = "%s%0.3i"%(txt,self.parameters.scan_number)
        self.ui.lb_scanname.setText(txt)
        if update_detector:
            for det in self.detector:
                if det is not None:
                    det.filePut('FileName', txt)
                    det.FileName = txt 

    def choose_softglue_channels(self):
        strv = ''
        for i, ch in enumerate(self.parameters.softglue_channels):
            if i==0:
                strv = ch
            else:
                strv = "%s, %s"% (strv, ch)
        text, okPressed = QInputDialog.getText(self, "Channels of SoftGlueZinq to Record","Channels:", QLineEdit.Normal, strv)
        if okPressed:
            self.parameters.softglue_channels = [x.strip() for x in text.split(',')]
#            print(self.parameters.softglue_channels)

    def reset_det_flymode(self):
        for det in self.detector:
            if det is not None:
                det.set_fly_configuration()

    def set_softglue_in(self, val):
        if val==1:
            self.ui.actionevery_10_millie_seconds.setChecked(False)
            self.ui.actionDetout.setChecked(False)
            self.ui.actionTrigout.setChecked(True)
            s12softglue.set_count_freq(10)
        if val==2:
            self.ui.actionevery_10_millie_seconds.setChecked(False)
            self.ui.actionDetout.setChecked(True)
            self.ui.actionTrigout.setChecked(False)
            s12softglue.set_count_freq(100)
        if val==3:
            self.ui.actionevery_10_millie_seconds.setChecked(True)
            self.ui.actionDetout.setChecked(False)
            self.ui.actionTrigout.setChecked(False)
            s12softglue.set_count_freq(1000)
            
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
            self.parameters._qds_unit = QDS_UNIT_NM
        if text =="um":
            self.parameters._qds_unit = QDS_UNIT_UM
        if text =="mm":
            self.parameters._qds_unit = QDS_UNIT_MM
        self.parameters.writeini()
    
    def select_timeintervals(self):
        val, ok = QInputDialog().getDouble(self, "QDS acqusition time intervals", "time intervals(s)", self.parameters._qds_time_interval)
        self.parameters._qds_time_interval = val
        self.parameters.writeini()

    def set_exp_period_ratio(self):
        val, ok = QInputDialog().getDouble(self, "Exposuretime/Period for Flyscan", "Fraction", self.parameters._ratio_exp_period)
        self.parameters._ratio_exp_period = val
        self.parameters.writeini()

    def select_qds_x(self):
        text, ok = QInputDialog().getItem(self, "Select QDS units",
                                            "Units:", ('0', '1', '2'), current=self.parameters._qds_x_sensor, editable=False)
        self.parameters._qds_x_sensor = int(text)
        self.parameters.writeini()

    def select_qds_y(self):
        text, ok = QInputDialog().getItem(self, "Select QDS units",
                                            "Units:", ('0', '1', '2'), current=self.parameters._qds_y_sensor, editable=False)
        self.parameters._qds_y_sensor = int(text)
        self.parameters.writeini()

    def scantest(self):
        if self.ui.actionTestFly.isChecked():
            self.ui.actionTestFly.setChecked(True)
        else:
            self.ui.actionTestFly.setChecked(False)
            self.detector[1] = None
    def fit_wobble_eccentricity(self):
        tp = np.asarray(self.mpos)
        rp = np.asarray(self.rpos)
        self.fitdata(xd=tp, yd=rp[:,self.parameters._qds_x_sensor], dtype="eccent")
        self.fitdata(xd=tp, yd=rp[:,self.parameters._qds_y_sensor], dtype="wob")

    def loadscan(self):
        w = QWidget()
        w.resize(320, 240)
        # Set window title
        w.setWindowTitle("Load phi vs QDS[0,1,2] Data")
        fn = QFileDialog.getOpenFileName(w, 'Open File', '', 'Text (*.txt *.dat)',None, QFileDialog.DontUseNativeDialog)
        filename = fn[0]
        if filename == "":
            return 0
        self.fitdata(filename=filename, datacolumn=self.parameters._qds_x_sensor+1, dtype="eccent")
        self.fitdata(filename=filename, datacolumn=self.parameters._qds_y_sensor+1, dtype="wob")

        self.canvas.draw()

    def fitdata(self, filename="", datacolumn=2, xd = [], yd = [], dtype="wobble"):
        if self.parameters._qds_unit == QDS_UNIT_MM:
            eqn.POSITION_UNIT = eqn.POSITION_UNIT_MM
        if self.parameters._qds_unit == QDS_UNIT_UM:
            eqn.POSITION_UNIT = eqn.POSITION_UNIT_UM
        if self.parameters._qds_unit == QDS_UNIT_NM:
            eqn.POSITION_UNIT = eqn.POSITION_UNIT_NM
        if len(filename)>0:
            xd, yd = eqn.loadata(filename=filename, datacolumn=datacolumn)
        else:
            xd, yd = eqn.loadata(xdata=xd, ydata=yd)

        if dtype in "eccentricity":
            popt, pconv = eqn.fit_eccentricity(xd, yd, R=self.parameters._qds_R_cyl)
            cv, lb = eqn.get_eccen_fitcurve(xd, popt)
            self.plotfits(xd, yd, cv, lb, ax=1)    
        if dtype in "wobble":
            popt, pconv = eqn.fit_wobble(xd, yd, th0=self.parameters._qds_th0_vert, R=self.parameters._qds_R_vert)
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
        # get motor position from the label
        # i.e. axis = 'X'
        i = self.motornames.index(axis)
        return float(self.ui.findChild(QLabel, "lb_%i"%(i+1)).text())
        
        
    def updatepos(self, axis = "", val=None):
        if len(axis)==0:
            for i, name in enumerate(self.motornames):
                if val is None:
                    val = self.pts.get_pos(name)
                #self.ui.findChild(QLineEdit, "ed_%i"%(i+1)).setText("%0.4f"%val)
                self.ui.findChild(QLabel, "lb_%i"%(i+1)).setText("%0.6f"%val)
                val = None
        else:
            if val is None:
                val = self.pts.get_pos(axis)
            i = self.motornames.index(axis)
            #self.ui.findChild(QLineEdit, "ed_%i"%(i+1)).setText("%0.4f"%val)
            self.ui.findChild(QLabel, "lb_%i"%(i+1)).setText("%0.6f"%val)

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
        print("scan done")
        self.isscan = False
        self.updatepos()
        self.plot()
        if len(self.parameters.logfilename)>0:
            pos = np.asarray(self.mpos)
            r = np.asarray(self.rpos)
            self.save_list(self.parameters.logfilename, pos,r,[0,1,2],"a")
            scaninfo = []
            scaninfo.append('#I detector_filename')
            for det in self.detector:
                if det is not None:
                    if self.use_hdf_plugin:
                        fnum = det.fileGet('FileNumber_RBV')
                        fn = det.fileGet('FullFileName_RBV', as_string=True)
                        if str(fnum-1) not in fn:
                            fn = det.fileGet('FullFileName_RBV', as_string=True)
                    else:
                        fnum = det.FileNumber_RBV
                        fn = bytes(det.FullFileName_RBV).decode().strip('\x00')
                    filename = os.path.basename(fn)
                    scaninfo.append(filename)
            if len(scaninfo)>1:
                self.write_scaninfo_to_logfile(scaninfo)
            scaninfo = []
            scaninfo.append('#D')
            scaninfo.append(time.ctime())
        self.run_stop_issued()

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
        if N==3:
            if self.ui.actionStruck.isChecked():
                self.ui.actionStruck.setChecked(True)
                self.isStruckCountNeeded = True
                print("Struct in on")
#                self.detector[1] = pilatus('12idcPIL:')
            else:
                self.ui.actionStruck.setChecked(False)
                self.isStruckCountNeeded = False
                print("Struck is off")
#                self.detector[1] = None
        
    def select_flymode(self):
        if self.ui.actionEnable_fly_with_controller.isChecked():  # when checked, this value is False
            self.ui.actionEnable_fly_with_controller.setChecked(True)
            self.hexapod_flymode = HEXAPOD_FLYMODE_WAVELET
        else:
            self.hexapod_flymode = HEXAPOD_FLYMODE_STANDARD
            self.ui.actionEnable_fly_with_controller.setChecked(False)
    
    def read_softglue(self):
        # read softglue data
        foldername = self.ui.ed_workingfolder.text()
        if len(foldername) == 0:
            return
            #foldername = os.getcwd()
        N_cnt = 0
        if hasattr(self.pts.hexapod, "pulse_number"):
            N_cnt = self.pts.hexapod.pulse_number
        t = []
        #time.sleep(0.5)
        #timeout = 5
        # ct0 = time.time()
        # while s12softglue.VALI<N_cnt*self.parameters.countsperexposure:
        #     if (time.time()-ct0 > timeout):
        #         print("timeout")
        #         break
        #     time.sleep(0.1)
        ct0 = time.time()
#        count = 0
        s12softglue.PROC=1
        while len(t)<N_cnt:
            t, dt = s12softglue.get_arrays(self.parameters.softglue_channels)
#            print(f"count = {count}")
#            count += 1
        print(f"time to read softglue data = {time.time()-ct0}")
        return t,dt

    def save_softglue_new(self,t,dt):
        foldername = self.ui.ed_workingfolder.text()
        if len(foldername) == 0:
            return
        filename = ""
        for det in self.detector:
            if det is not None:
                if self.use_hdf_plugin:
                    fnum = det.fileGet('FileNumber_RBV')
                    fn = det.fileGet('FullFileName_RBV', as_string=True)
                else:
                    fnum = det.FileNumber_RBV
                    fn = bytes(det.FullFileName_RBV).decode().strip('\x00')
#                print(f'{fn=}')
                #if str(fnum-1) not in fn:
                #    fn = det.fileGet('FullFileName_RBV', as_string=True)
                filename = os.path.basename(fn)
#                print(filename)
#                print(rstrip_from_char(filename, "_"))
                filename = "%s_%0.5i" % (rstrip_from_char(filename, "_"), fnum-1)
#                print(filename)
                #filename = filename.rstrip('.h5')
        if len(filename) ==0:
            print("****** Error: detector ioc does not response.")
            filename = "temp%i"%int(time.time())

        print(f"Total {len(t)} data will be saved under {foldername} with names of {filename}.")

        for i, td in enumerate(t):
            scanname = '%s_%i.dat' % (filename, i)
            dt2 = np.column_stack((td, dt[0][i], dt[1][i], dt[2][i]))
            np.savetxt(os.path.join(foldername, scanname), dt2, fmt="%1.8e %1.8e %1.8e %1.8e")


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
        #time.sleep(0.5)
        #timeout = 5
        # ct0 = time.time()
        # while s12softglue.VALI<N_cnt*self.parameters.countsperexposure:
        #     if (time.time()-ct0 > timeout):
        #         print("timeout")
        #         break
        #     time.sleep(0.1)
        ct0 = time.time()
#        count = 0
        s12softglue.PROC=1
        while len(t)<N_cnt:
            t, dt = s12softglue.get_arrays(self.parameters.softglue_channels)
#            print(f"count = {count}")
#            count += 1
        print(f"time to read softglue data = {time.time()-ct0}")
        filename = ""
        for det in self.detector:
            if det is not None:
                if self.use_hdf_plugin:
                    fnum = det.fileGet('FileNumber_RBV')
                    fn = det.fileGet('FullFileName_RBV', as_string=True)
                else:
                    fnum = det.FileNumber_RBV
                    fn = bytes(det.FullFileName_RBV).decode().strip('\x00')
#                print(f'{fn=}')
                #if str(fnum-1) not in fn:
                #    fn = det.fileGet('FullFileName_RBV', as_string=True)
                filename = os.path.basename(fn)
#                print(filename)
#                print(rstrip_from_char(filename, "_"))
                filename = "%s_%0.5i" % (rstrip_from_char(filename, "_"), fnum-1)
#                print(filename)
                #filename = filename.rstrip('.h5')
        if len(filename) ==0:
            print("****** Error: detector ioc does not response.")
            filename = "temp%i"%int(time.time())

        print(f"Total {len(t)} data will be saved under {foldername} with names of {filename}.")

        for i, td in enumerate(t):
            if i>=N_cnt:
                continue
            scanname = '%s_%i.dat' % (filename, i)
            dt2 = np.column_stack((td, dt[0][i], dt[1][i], dt[2][i]))
            np.savetxt(os.path.join(foldername, scanname), dt2, fmt="%1.8e %1.8e %1.8e %1.8e")

    def flydone(self, return_motor=True):
        if return_motor:
            for key in self.motor_p0:
                self.mv(key, self.motor_p0[key])

        print("fly done.......")
#        ct0 = time.time()
#        pos = self.pts.get_pos('X')
#        print(f'X position is at {pos} in flydone.')
        isTestRun = self.ui.actionTestFly.isChecked()
        if isTestRun:
            return
        self.isscan = False
        try:
            self.plot()
            self.updatepos()
            s12softglue.flush()
        except:
            print("error here.....")
        #if self.signalmotor not in self.pts.hexapod.axes:        
        #    self.pts.set_speed(self.signalmotor, self._prev_vel, self._prev_acc)
#        print(f"elapsed time since done = {time.time()-ct0}")
        self.isfly = False
        if len(self.parameters.logfilename)>0:
            if self.isStruckCountNeeded:
                # save struck data.
                r = struck.read_mcs([0, 1, 2])
                pos = np.arange(len(r[0]))
                self.mpos = pos
                print("Number of MCS channels : ", len(r))
            else:
                # save qds data.
                pos = np.asarray(self.mpos)
                r = np.asarray(self.rpos)
            try:
                self.save_nparray(self.parameters.logfilename, pos,r,[0,1,2],"a")
            except:
                self.save_list(self.parameters.logfilename, pos,r,[0,1,2],"a")
            scaninfo = []
            scaninfo.append('#I detector_filename')
            for det in self.detector:
                if det is not None:
                    if self.use_hdf_plugin:
                        fnum = det.fileGet('FileNumber_RBV')
                        fn = det.fileGet('FullFileName_RBV', as_string=True)
                        if str(fnum-1) not in fn:
                            fn = det.fileGet('FullFileName_RBV', as_string=True)
                    else:
                        fnum = det.FileNumber_RBV
                        fn = bytes(det.FullFileName_RBV).decode().strip('\x00')
                        #fnum = det.fileGet('FileNumber_RBV')
                        #fn = det.fileGet('FullFileName_RBV', as_string=True)
                    filename = os.path.basename(fn)
                    scaninfo.append(filename)
                    # if no cpature, comment the two line out.
#                    while det.fileGet('WriteFile_RBV'):
#                        time.sleep(0.1)
            if len(scaninfo)>1:
                self.write_scaninfo_to_logfile(scaninfo)
            scaninfo = []
            scaninfo.append('#D')
            scaninfo.append(time.ctime())
            self.write_scaninfo_to_logfile(scaninfo)
#        print(f"elapsed time since done = {time.time()-ct0}")
        success=False
        timeout = 5
        cnt = 0
        while success == False:
            try:
                self.save_softglue()
                success = True
            except:
                print("error on softglue, it will be flushed again.")
                s12softglue.flush()
                cnt = cnt + 1
                if cnt>timeout:
                    break
        # if read softglue failed...
        if success == False:
            foldername = self.ui.ed_workingfolder.text()
            if len(foldername) == 0:
                return
            filename = ""
            for det in self.detector:
                if det is not None:
                    if self.use_hdf_plugin:
                        fnum = det.fileGet('FileNumber_RBV')
                        fn = det.fileGet('FullFileName_RBV', as_string=True)
                    else:
                        fnum = det.FileNumber_RBV
                        fn = bytes(det.FullFileName_RBV).decode().strip('\x00')
                    filename = os.path.basename(fn)
                    filename = "%s_%0.5i" % (rstrip_from_char(filename, "_"), fnum-1)
            if len(filename) ==0:
                print("****** Error: detector ioc does not response.")
                filename = "temp%i"%int(time.time())
            filename = f"{filename}.dat"
            print(f"\nSoftglue epics erorr.....Data read from usb will be saved in {filename}\n")
            try:
                self.save_nparray(filename, pos,r,[0,1,2],"a")
            except:
                self.save_list(filename, pos,r,[0,1,2],"a")
        self.rpos = []
        self.mpos = []
        #t = []
        #while len(t) == 0:
        #    try:
        #        #self.save_softglue()
        #        t,dt=self.read_softglue()
        #        time.sleep(1.5)
        #    except:
        #        print("Error in softglue saving....")

        #print(f"elapsed time since flydone = {time.time()-ct0}")
        #w1 = Worker(self.save_softglue_new,t,dt)
        #self.threadpool.start(w1)

    def flydone2d(self, value=0):
        for key in self.motor_p0:
            self.mv(key, self.motor_p0[key])
#            self.ui.findChild(QLineEdit, "ed_%i"%n).setText("%0.4f"%self.motor_p0[m])
        print("2D fly done.......")
        isTestRun = self.ui.actionTestFly.isChecked()
        if isTestRun:
            return
        self.isscan = False
        self.updatepos()
        try:
            self.save_scaninfo()
        except:
            print("save_scaninfo is empty yet. This will save phi angles......")
        self.isfly = False

    def flydone3d(self, value=0):
        for key in self.motor_p0:
            self.mv(key, self.motor_p0[key])        
        print("3D fly done.......")
        isTestRun = self.ui.actionTestFly.isChecked()
        if isTestRun:
            return
        self.isscan = False
        self.updatepos()
        self.isfly = False
    
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
        self.isStopScanIssued = False
        # for det in self.detector: #JD
        #     if det is not None:  #JD
        #         det.filePut('FileNumber', 1)  #JD
        #         det.FileNumber = 1

        if self.ui.actionckTime_reset_before_scan.isChecked():
            s12softglue.ckTime_reset()

        # reset the progress bar
        self.ui.progressBar.setValue(0)

        motor = [xmotor, ymotor]
        print(f'\n\nfly2d:{xmotor=}; {ymotor=}') #JD
                # logging
        scaninfo = []
        scaninfo.append('\n#S')
        scaninfo.append(self.parameters.scan_number)
        scaninfo.append('fly2d')
        initial_motorpos = {}
        for i, m in enumerate(motor):
            n = m+1
            try:
                scaninfo.append(n)
                #p0 = self.ui.findChild(QLineEdit, "ed_%i"%n).text()
                #if len(p0)==0:
                p0 = self.ui.findChild(QLabel, "lb_%i"%n).text()
                self.ui.findChild(QLineEdit, "ed_%i"%n).setText(p0)
                p0 = float(p0)
                initial_motorpos[m] = p0
                st = float(self.ui.findChild(QLineEdit, "ed_lup_%i_L"%n).text())
                fe = float(self.ui.findChild(QLineEdit, "ed_lup_%i_R"%n).text())
                tm = float(self.ui.findChild(QLineEdit, "ed_lup_%i_t"%n).text())
                step = float(self.ui.findChild(QLineEdit, "ed_lup_%i_N"%n).text())
                scaninfo.append(st+p0)
                scaninfo.append(fe+p0)
                scaninfo.append(tm)      
                scaninfo.append(step)      
            except:
                showerror("Check scan paramters.")
                return 0
            if i == 0:
                self.fly1d_p0 = p0
                self.fly1d_st = st
                self.fly1d_fe = fe
                self.fly1d_tm = tm
                self.fly1d_step = step
            if i == 1:
                self.fly2d_p0 = p0
                self.fly2d_st = st
                self.fly2d_fe = fe
                self.fly2d_tm = tm
                self.fly2d_step = step

        self.motor_p0 = initial_motorpos
        self.write_scaninfo_to_logfile(scaninfo)
        scaninfo = []
        scaninfo.append('#D')
        scaninfo.append(time.ctime())
        self.isscan = True
        w = Worker(self.fly2d0, xmotor, ymotor, scanname=scanname)
        w.signal.finished.connect(self.flydone2d)
        self.threadpool.start(w)

    def updateprogressbar(self, value):
        self.ui.progressBar.setValue(value)

    def fly3d(self, xmotor=0, ymotor=1, phimotor=6, scanname=""):
        self.isStopScanIssued = False
        if self.ui.actionckTime_reset_before_scan.isChecked():
            s12softglue.ckTime_reset()
        motor = [xmotor, ymotor, phimotor]
        print(motor)
        # logging
        scaninfo = []
        scaninfo.append('\n#S')
        scaninfo.append(self.parameters.scan_number)
        scaninfo.append('fly3d')
        initial_motorpos = {}
        for i, m in enumerate(motor):
            n = m+1
            try:
                scaninfo.append(n)
                #p0 = self.ui.findChild(QLineEdit, "ed_%i"%n).text()
                #if len(p0)==0:
                p0 = self.ui.findChild(QLabel, "lb_%i"%n).text()
                print(p0)
                self.ui.findChild(QLineEdit, "ed_%i"%n).setText(p0)
                print(p0)
                p0 = float(p0)
                print(p0)
                initial_motorpos[m] = p0
                st = float(self.ui.findChild(QLineEdit, "ed_lup_%i_L"%n).text())
                fe = float(self.ui.findChild(QLineEdit, "ed_lup_%i_R"%n).text())
                tm = float(self.ui.findChild(QLineEdit, "ed_lup_%i_t"%n).text())
                step = float(self.ui.findChild(QLineEdit, "ed_lup_%i_N"%n).text())
                scaninfo.append(st+p0)
                scaninfo.append(fe+p0)
                scaninfo.append(tm)
                scaninfo.append(step)
            except:
                showerror("Check scan paramters.")
                return 0
            if i == 0:
                self.fly1d_p0 = p0
                self.fly1d_st = st
                self.fly1d_fe = fe
                self.fly1d_tm = tm
                self.fly1d_step = step
            if i==1:
                self.fly2d_p0 = p0
                self.fly2d_st = st
                self.fly2d_fe = fe
                self.fly2d_tm = tm
                self.fly2d_step = step
            if i==2:
                self.fly3d_p0 = p0
                self.fly3d_st = st
                self.fly3d_fe = fe
                self.fly3d_tm = tm
                self.fly3d_step = step
            
        self.motor_p0 = initial_motorpos
        self.write_scaninfo_to_logfile(scaninfo)
        scaninfo = []
        scaninfo.append('#D')
        scaninfo.append(time.ctime())

        self.isscan = True
        w = Worker(self.fly3d0, xmotor, ymotor, phimotor, scanname=scanname)
        w.signal.finished.connect(self.flydone3d)
        self.threadpool.start(w)

    def fly(self, motornumber=-1):
        self.update_scanname()
        self.isStopScanIssued = False
        if motornumber<0:
            pb = self.sender()
            objname = pb.objectName()
            n = int(re.findall(r'\d+', objname)[0])
            motornumber = n-1
        else:
            n = motornumber + 1

        # logging
        scaninfo = []
        scaninfo.append('\n#S')
        scaninfo.append(self.parameters.scan_number)
        scaninfo.append('fly')
        scaninfo.append(n)    
        initial_motorpos = {}    

        # n = motornumber+1
        # p0 = self.ui.findChild(QLineEdit, "ed_%i"%n).text()
        # if len(p0)==0:
        #     p0 = self.ui.findChild(QLabel, "lb_%i"%n).text()
        #     self.ui.findChild(QLineEdit, "ed_%i"%n).setText(p0)
        # p0 = float(p0)
        # st = float(self.ui.findChild(QLineEdit, "ed_lup_%i_L"%n).text())+p0
        # fe = float(self.ui.findChild(QLineEdit, "ed_lup_%i_R"%n).text())+p0
        # tm = float(self.ui.findChild(QLineEdit, "ed_lup_%i_t"%n).text())
        # try:
        #     step = float(self.ui.findChild(QLineEdit, "ed_lup_%i_N"%n).text())
        # except:
        #     step = 0.1
        #     self.ui.findChild(QLineEdit, "ed_lup_%i_N"%n).setText("%0.3f"%step)
        # if abs(tm) <= 0.033:
        #     print(f"Note that Max speed of Pilatus2M is 30Hz.")
        #     print("******* Cannot run.")
        #     return

        try:
            #p0 = self.ui.findChild(QLineEdit, "ed_%i"%n).text()
            #if len(p0)==0:
            p0 = self.ui.findChild(QLabel, "lb_%i"%n).text()
            self.ui.findChild(QLineEdit, "ed_%i"%n).setText(p0)
            p0 = float(p0)
            initial_motorpos[motornumber] = p0
            st = float(self.ui.findChild(QLineEdit, "ed_lup_%i_L"%n).text())
            fe = float(self.ui.findChild(QLineEdit, "ed_lup_%i_R"%n).text())
            tm = float(self.ui.findChild(QLineEdit, "ed_lup_%i_t"%n).text())
            step = float(self.ui.findChild(QLineEdit, "ed_lup_%i_N"%n).text())
        except:
            showerror("Check scan paramters.")
            return 0
        self.motor_p0 = initial_motorpos
        self.fly1d_p0 = p0
        self.fly1d_st = st
        self.fly1d_fe = fe
        self.fly1d_tm = tm
        self.fly1d_step = step
        scaninfo.append(st+p0)
        scaninfo.append(fe+p0)
        scaninfo.append(tm)
        scaninfo.append(step)
        self.write_scaninfo_to_logfile(scaninfo)

        self.isscan = True
        w = Worker(self.fly0, motornumber)
        w.signal.finished.connect(self.flydone)

        self.threadpool.start(w)
        self.run_stop_issued()

    def write_scaninfo_to_logfile(self, strlist):
        if len(self.parameters.logfilename) == 0:
            return 0
        with open(self.parameters.logfilename, "a") as f:
            for i, m in enumerate(strlist):
                if i==0:
                    strv = "%s"%str(m)
                else:
                    strv = "%s    %s"%(strv, str(m))
            f.write("%s\n"%strv)

    def log_data(self, data_list):
        if len(self.parameters.logfilename) == 0:
            return 0
        strv = ""
        with open(self.parameters.logfilename, "a") as f:
            for i, m in enumerate(data_list):
                if i==0:
                    strv = "%0.8f"%m
                else:
                    strv = "%s    %0.8f"%(strv, m)
            f.write("%s\n"%strv)

    def stepscan(self, motornumber=-1):
        self.update_scanname()

        self.isStopScanIssued = False
        if motornumber<0:
            pb = self.sender()
            objname = pb.objectName()
            n = int(re.findall(r'\d+', objname)[0])
            motornumber = n-1
        else:
            n = motornumber + 1
        
        try:
            p0 = self.ui.findChild(QLineEdit, "ed_%i"%n).text()
            if len(p0)==0:
                p0 = self.ui.findChild(QLabel, "lb_%i"%n).text()
                self.ui.findChild(QLineEdit, "ed_%i"%n).setText(p0)
            p0 = float(p0)
            st = float(self.ui.findChild(QLineEdit, "ed_lup_%i_L"%n).text())
            fe = float(self.ui.findChild(QLineEdit, "ed_lup_%i_R"%n).text())
            tm = float(self.ui.findChild(QLineEdit, "ed_lup_%i_t"%n).text())
            step = float(self.ui.findChild(QLineEdit, "ed_lup_%i_N"%n).text())
        except:
            showerror("Check scan parameters.")
            return 0
        
        # logging
        scaninfo = []
        scaninfo.append('\n#S')
        scaninfo.append(self.parameters.scan_number)
        scaninfo.append('step_scan')
        scaninfo.append(self.motornames[motornumber])
        scaninfo.append(st+p0)
        scaninfo.append(fe+p0)
        scaninfo.append(tm)
        scaninfo.append(step)
        self.write_scaninfo_to_logfile(scaninfo)
        scaninfo = []
        scaninfo.append('#D')
        scaninfo.append(time.ctime())
        self.write_scaninfo_to_logfile(scaninfo)
        # logging datatype
        scaninfo = []
        scaninfo.append('#H')
        if self.isStruckCountNeeded:
            scaninfo.append(self.motornames[motornumber])
            scaninfo.append(struck.strk.scaler.NM2)
            scaninfo.append(struck.strk.scaler.NM3)
            scaninfo.append(struck.strk.scaler.NM4)
        else:
            scaninfo.append(self.motornames[motornumber])
            scaninfo.append('QDS1')
            scaninfo.append('QDS2')
            scaninfo.append('QDS3')
        self.write_scaninfo_to_logfile(scaninfo)    
        # start the scan
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
            time.sleep(self.parameters._qds_time_interval)
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
            r = self.pts.qds.get_position(self.parameters.softglue_channels)

        if isrefavailable:
            r = [r[0]/1000-self.parameters._ref_X, r[1]/1000-self.parameters._ref_Z, r[2]/1000-self.parameters._ref_Z2]
        else:
            r = [r[0]/1000, r[1]/1000, r[2]/1000]
        return r

    def run_stop_issued(self):
        self.parameters.scan_number = self.parameters.scan_number + 1
        self.ui.le_scannumber.setText(str(int(self.parameters.scan_number)))
        self.parameters.writeini()        

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

        # p0 = self.ui.findChild(QLineEdit, "ed_%i"%n).text()
        # if len(p0)==0:
        #     p0 = self.ui.findChild(QLabel, "lb_%i"%n).text()
        #     self.ui.findChild(QLineEdit, "ed_%i"%n).setText(p0)
        # p0 = float(p0)
        # st = float(self.ui.findChild(QLineEdit, "ed_lup_%i_L"%n).text())
        # fe = float(self.ui.findChild(QLineEdit, "ed_lup_%i_R"%n).text())
        # expt = float(self.ui.findChild(QLineEdit, "ed_lup_%i_t"%n).text())
        # step = float(self.ui.findChild(QLineEdit, "ed_lup_%i_N"%n).text())

        st = self.stepscan_st + self.stepscan_p0
        fe = self.stepscan_fe + self.stepscan_p0
        expt = self.stepscan_expt
        step = self.stepscan_step
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

        # start scan..
        self.pts.mv(axis, st)
        pos = np.arange(st, fe+step/2, step)
        if len(pos)==1:
            pos = np.array([st, fe])
        self.ui.progressBar.setValue(0)

        # prepare to collect Detector images
        # set the delay generator
        if expt != dg645_12ID._exposuretime:
            try:
                dg645_12ID.set_pilatus_fly(expt)
            except:
                print("EEEEE")

        if self.isStruckCountNeeded:
            struck.mcs_counter_init()

        if len(self.detector)>0:
            for det in self.detector:
                if det is not None:
                    print(f"Exposure time set to %0.3f seconds for {det._prefix}."% expt)
                    try:
                        det.fly_ready(expt, len(pos))
    #                            print("det is ready.")
                    except TimeoutError:
                        msg = f"Detector, {det._prefix}, hasnt started yet. Fly scan own start."
                        print(msg)
                        self.ui.statusbar.showMessage(msg)
                        #showerror("Detector timeout.")
                        return

        self.plotlabels = []
        
        ## make a plot if needed.
#        print(pos)
        for i, value in enumerate(pos):
            if self.isStopScanIssued:
                break
            self.pts.mv(axis, value)
            #print(value)
            if len(self.detector)>0:
                struck.arm_mcs_counter()
                struck.mcs_counter_waitstarted()
                dg645_12ID.trigger()
                while struck.strk.scaler.CNT:
                    time.sleep(0.01)
            if self.isStruckCountNeeded:
                if len(self.detector)==0:
                    struck.mcs_counter_count(expt)
                    while struck.strk.scaler.CNT:
                        time.sleep(0.01)
                cnts = struck.read_scaler_all()
                self.rpos.append([cnts[2], cnts[3], cnts[4]])
                # data = [value, cnts[2],cnts[3],cnts[4]]
                # self.log_data(data)
            else:
                r = self.get_qds_pos()
                self.rpos.append([r[0], r[1], r[2]])
                # data = [value, [r[0], r[1], r[2]]]
                # self.log_data(data)
            #pos = self.get_motorpos(self.signalmotor)
            self.mpos.append(value)
            self.ui.progressBar.setValue(int((i+1)/len(pos)*100))

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
        # n = phimotor+1
        # p0 = self.ui.findChild(QLineEdit, "ed_%i"%n).text()
        # if len(p0)==0:
        #     p0 = self.ui.findChild(QLabel, "lb_%i"%n).text()
        #     self.ui.findChild(QLineEdit, "ed_%i"%n).setText(p0)
        # p0 = float(p0)
        # st = float(self.ui.findChild(QLineEdit, "ed_lup_%i_L"%n).text())+p0
        # fe = float(self.ui.findChild(QLineEdit, "ed_lup_%i_R"%n).text())+p0
        # #tm = float(self.ui.findChild(QLineEdit, "ed_lup_%i_t"%n).text())
        # step = float(self.ui.findChild(QLineEdit, "ed_lup_%i_N"%n).text())
        
        st = self.fly3d_st + self.fly3d_p0
        fe = self.fly3d_fe + self.fly3d_p0
        step = self.fly3d_step

        if st>fe:
            step = -1*abs(step)
        if st<fe:
            step = abs(step)
        # revsere scan disabled: always scan from start to final regardless of the initial position.
        # if self.ui.cb_reversescandir.isChecked():
        #     if abs(st-pos)>abs(fe-pos):
        #         t = fe
        #         fe = st
        #         st = t 
        #         step = -step
        self.pts.mv(axis, st)
        pos = np.arange(st, fe+step/2, step)

        if len(scanname):
            scanname=axis
        else:
#            print(scanname, axis)
            scanname=f"{scanname}{axis}"        
        for i, value in enumerate(pos):
            if self.isStopScanIssued:
                break
            
            # loging phi angle information
            print(f"phi position : {value}")
            scaninfo = []
            scaninfo.append('#I phi = ')
            scaninfo.append(value)
            self.write_scaninfo_to_logfile(scaninfo) 

            self.pts.mv(axis, value)
            # fly here
            scan="%s%0.3d"%(scanname, i)
            self.fly2d0(xmotor=xmotor, ymotor=ymotor, scanname=scan)
#            r = self.get_qds_pos()
#            self.rpos3.append([r[0], r[1], r[2]])
#            self.mpos3.append(value)        

    def fly2d0(self, xmotor = 0, ymotor=1, scanname = ""):
        self.update_scanname()

        # xmotor is for flying
        # ymotor is for stepping
        axis = self.motornames[ymotor]
        print(f'{axis=}, which is {ymotor=}') #JD
        self.signalmotor2 = axis
        self.signalmotorunit2 = self.motorunits[ymotor]
#        self.rpos2 = []
#        self.mpos2 = []
        pos = self.pts.get_pos(axis)
        self.isfly2 = False

    # Just in case when the user update edit box (during 3d scan) 
    # Will need to update the positions.
        n = ymotor+1
        p0 = self.ui.findChild(QLineEdit, "ed_%i"%n).text()
#        if len(p0)==0:
#            p0 = self.ui.findChild(QLabel, "lb_%i"%n).text()
#            self.ui.findChild(QLineEdit, "ed_%i"%n).setText(p0)
        p0 = float(p0)
        st = float(self.ui.findChild(QLineEdit, "ed_lup_%i_L"%n).text())
        fe = float(self.ui.findChild(QLineEdit, "ed_lup_%i_R"%n).text())
        self.fly2d_p0 = p0
        self.fly2d_st = st
        self.fly2d_fe = fe

        n = xmotor+1
        p0 = self.ui.findChild(QLineEdit, "ed_%i"%n).text()
#        if len(p0)==0:
#            p0 = self.ui.findChild(QLabel, "lb_%i"%n).text()
#            self.ui.findChild(QLineEdit, "ed_%i"%n).setText(p0)
        p0 = float(p0)
        st = float(self.ui.findChild(QLineEdit, "ed_lup_%i_L"%n).text())
        fe = float(self.ui.findChild(QLineEdit, "ed_lup_%i_R"%n).text())
        self.fly1d_p0 = p0
        self.fly1d_st = st
        self.fly1d_fe = fe

        # get relative scan range and convert it to absolute...
        st = self.fly2d_st + self.fly2d_p0
        fe = self.fly2d_fe + self.fly2d_p0
        step = self.fly2d_step

        maxexposuretime = float(self.ui.findChild(QLineEdit, "ed_lup_%i_t"%(xmotor+1)).text())
        #epics.caput('12idc:scaler1.TP', maxexposuretime+5)

        if st>fe:
            step = -1*abs(step)
        if st<fe:
            step = abs(step)
        # Reverse disabled... Scan will be done from start to end regardless of the initial position.
        # if self.ui.cb_reversescandir.isChecked():
        #     if abs(st-pos)>abs(fe-pos):
        #         t = fe
        #         fe = st
        #         st = t 
        #         step = -step
        self.pts.mv(axis, st)
        pos = np.arange(st, fe+step/2, step)
#        print(pos)
        if len(scanname):
            scanname=axis
        else:
            scanname=f"{scanname}{axis}"
        
        for i, value in enumerate(pos):
            if self.isStopScanIssued:
                break
            for det in self.detector: #JD
                if det is not None:  #JD
                    det.filePut('FileNumber', 1)  #JD
                    det.FileNumber = 1
            # try:
            # except:
            #     print("error epics")
            print()
            print(f"Y position : %0.3f" % value)
            scaninfo = []
            scaninfo.append('#I Y = ')
            scaninfo.append(value)
            self.write_scaninfo_to_logfile(scaninfo) 

            t0 = time.time()
            self.pts.mv(axis, value)
            ismoving = True
            while ismoving:
                ismoving = self.pts.ismoving(axis)
            print("All motors are ready for fly scan.")
            # fly here
            self.fly0(xmotor)
#            print("CCCC")
            self.flydone(return_motor=False)
            # try:
            #epics.caput('12idc:scaler1.CNT', 0)
            # except:
            #     print("epics 2 error")
            t1 = time.time()
            while (time.time()-t1 < self.parameters._waittime_between_scans):
                time.sleep(0.01)
            timeelapsed = time.time()-t0
            print(f"Remaining time for the current 2D scan is {np.round(timeelapsed*(len(pos)-i-1),2)}s\n")
#            self.ui.progressBar.setValue(int((i+1)/len(pos)*100))
            #await save_softglue(self.pts.hexapod.pulse_number, self.softglue_channels,
            #                    self.parameters.working_folder, filename)
            #while self.isfly:
            #    time.sleep(0.02)
#            filename = "%s%0.3d"%(scanname, i)
#            self.save_qds(filename=filename)
        self.run_stop_issued()

    def fly0(self, motornumber):
        axis = self.motornames[motornumber]
        self.signalmotor = axis
        self.signalmotorunit = self.motorunits[motornumber]
        #self.rpos = []
        #self.mpos = []
        self.plotlabels = []
        if self.ui.actionckTime_reset_before_scan.isChecked():
            s12softglue.ckTime_reset()
        if self.ui.actionMemory_clear_before_scan.isChecked():
            try:
                s12softglue.memory_clear()
            except TimeoutError:
                print("softglue memory_clear timeout")

        print("")
        isTestRun = self.ui.actionTestFly.isChecked()
        if isTestRun:
            print("**** Test Run:")
        self.isfly = True

        # disable fit menu
        self.ui.actionFit_QDS_phi.setEnabled(False)

        if not self.ui.cb_keepprevscan.isChecked():
#            print("Clear plot")
            self.clearplot()
        
        # logging datatype
        scaninfo = []
        scaninfo.append('#H')
        if self.isStruckCountNeeded:
            scaninfo.append(self.motornames[motornumber])
            scaninfo.append(struck.strk.scaler.NM2)
            scaninfo.append(struck.strk.scaler.NM3)
            scaninfo.append(struck.strk.scaler.NM4)
        else:
            scaninfo.append(self.motornames[motornumber])
            scaninfo.append('QDS1')
            scaninfo.append('QDS2')
            scaninfo.append('QDS3')
        self.write_scaninfo_to_logfile(scaninfo)  

        st = self.fly1d_st + self.fly1d_p0
        fe = self.fly1d_fe + self.fly1d_p0
        step = self.fly1d_step
        tm = self.fly1d_tm

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
#                print("Will set the traj up")
                self.pts.hexapod.set_traj(axis, tm, fe-st, st, direction, abs(step), 50)
                #expt = np.around(self.pts.hexapod.scantime/self.pts.hexapod.pulse_number*0.75, 3)
                period = self.pts.hexapod.scantime/self.pts.hexapod.pulse_number
                #expt = period-self.det_readout_time  JD
                expt = period*self.parameters._ratio_exp_period # JMM, *0.2 previously for JD. -0.02 previously for BL
                if period-expt < DETECTOR_READOUTTIME:
                    raise RuntimeError("expouretime is too short to readout DET images.")

                if expt <= 0:
                    print(f"Note that after subtracting the detector readout time {self.det_readout_time} s, the exposure time becomes equal or less than 0.")
                    print("******* Cannot run.")
                    return
                if abs(step) <= 0.033:
                    print(f"Note that Max speed of Pilatus2M is 30Hz.")
                    print("******* Cannot run.")
                    return
                # set the delay generator
                if expt != dg645_12ID._exposuretime:
                    try:
#                        print(f"Acutal exposure time: {expt}s.")
                        dg645_12ID.set_pilatus_fly(expt)
                    except:
                        print("EEEEE")
                print(f"Actual exposure time set to %0.3f seconds."% expt)
                movestep = abs(fe-st)/self.pts.hexapod.pulse_number*1000*self.parameters._ratio_exp_period
                print("During the exposure, the motor moves %0.3f um." % movestep)
                N_counts = s12softglue.number_acquisition(expt, self.pts.hexapod.pulse_number)
                self.parameters.countsperexposure = np.round(N_counts/self.pts.hexapod.pulse_number)
                print(f"Total {self.parameters.countsperexposure} encoder positions will be collected per a shot.")
                if N_counts>100000:
                    print(f"******** CAUTION: Number of softglue counts: {N_counts} is larger than 100E3. Slow down the clock speed.")

                if isTestRun:
                    return
                
                self.pts.hexapod.goto_start_pos(axis)

                for det in self.detector:
                    if det is not None:
                        try:
                            det.fly_ready(expt, self.pts.hexapod.pulse_number, period=period, 
                                          isTest = isTestRun, capture=self.use_hdf_plugin)
#                            print("det is ready.")
                        except TimeoutError:
                            msg = f"Detector, {det._prefix}, hasnt started yet. Fly scan will not start."
                            print(msg)
                            self.ui.statusbar.showMessage(msg)
                            #showerror("Detector timeout.")
                            return
#                print("Ready for traj")
                pos = self.pts.get_pos(axis)
                print(f"pos is {pos} before traj run start.")
                if not isTestRun:
                    if self.isStruckCountNeeded:
                        struck.mcs_init()
                        struck.mcs_ready(self.pts.hexapod.pulse_number, tm+10)
                        print(self.pts.hexapod.pulse_number, " MCS Ncouts updated.")
                        struck.arm_mcs()
                    else:
                        epics.caput('12idc:scaler1.CNT', 1)
                    istraj_running = False
                    timeout = 5
                    i = 0
#                    print("Hexapod is at the initial position.")
                    while not istraj_running:
                        try:
                            self.pts.hexapod.run_traj(axis)
                        except:
                            pass
                        time.sleep(0.5)
                        pos_tmp = self.pts.get_pos(axis)
                        if pos_tmp != pos:
                            istraj_running = True
                        #istraj_running = self.is_traj_running()
                        i = i+1
                        if i>timeout:
                            print("traj scan command is resent for 5 times to the hexapod without success.")
                            break
                print("Run_traj is sent command in rungui.")
                isattarget = False
                while not isattarget:
                    try:
                        isattarget = self.pts.hexapod.isattarget(axis)
                    except:
                        isattarget = False
                    #self.updatepos()
#                    print("Waiting to be done...")
                    time.sleep(0.5)
                if self.isStruckCountNeeded:
                    struck.strk.stop()
                else:
                    epics.caput('12idc:scaler1.CNT', 0)
                pos = self.pts.get_pos(axis)
                print(f"pos is {pos} after the traj run done.")

            if (self.hexapod_flymode==HEXAPOD_FLYMODE_STANDARD):
#            if (self.hexapod_flymode==HEXAPOD_FLYMODE_STANDARD) or (axis != "X"):
#                print(" Running the fly scan without controller")
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

    def is_traj_running(self):
        if s12softglue.get_eventN() == 0:
            return False
        else:
            return True
        
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
        p0 = self.ui.findChild(QLineEdit, "ed_%i"%n).text()
        if len(p0)==0:
            p0 = self.ui.findChild(QLabel, "lb_%i"%n).text()
            self.ui.findChild(QLineEdit, "ed_%i"%n).setText(p0)
        p0 = float(p0)
        st = float(self.ui.findChild(QLineEdit, "ed_lup_%i_L"%n).text())
        fe = float(self.ui.findChild(QLineEdit, "ed_lup_%i_R"%n).text())
        tm = float(self.ui.findChild(QLineEdit, "ed_lup_%i_t"%n).text())
        st = st + p0
        fe = fe + p0
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
            filename = os.path.join(self.parameters.working_folder, filename)
        else:
            self.parameters.working_folder = d
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
        if self.isStruckCountNeeded:
            pass
        else:
            # data unit and data
            if self.parameters._qds_unit == QDS_UNIT_MM:
                self.rpos = self.rpos/1E3
            if self.parameters._qds_unit == QDS_UNIT_UM:
                pass
            if self.parameters._qds_unit == QDS_UNIT_NM:
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

    def save_nparray(self, filename, mpos, rpos, col, option="w"):
        with open(filename, option) as f:
            for i, m in enumerate(mpos):
                strv = ""
                for cind in col:
                    strv = "%s    %0.5e"%(strv, rpos[cind][i])
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

    def fly_result(self):
        #if len(filename)==0:
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
            filename = os.path.join(self.parameters.working_folder, filename)
        else:
            self.parameters.working_folder = d
        # rea
        data = self.pts.hexapod.get_records()
        #print("Done.. Preparing to plot.")
        if isinstance(data, type({})):
            l_data = [data]
        else:
            l_data = data
        # try:
        #     qds_data = s12softglue.get_pos_array()
        # except:
        #     showerror("PanDA is needed.")
        #     return
        # qds_data = qds_data/1000
        #print(self.pts.hexapod.wave_start, " wave_start position")
        #qds_data = qds_data[-1]-qds_data-self.pts.hexapod.wave_start*1000
        try:
            axis = self.signalmotor
        except:
            axis = 'X'
        for data in l_data:
            #ndata = data[axis][0].size
            #x = range(0, ndata)
            if len(filename)>0:
                print(f"Target, Encoder, and Pulse positions for axis {axis} are saved in {filename}.")
                target = data[axis][0]*1000
                encoded = data[axis][1]*1000
                ind = np.zeros(target.shape, int)
                ind[self.pts.hexapod.pulse_positions_index] = 1
                #target = target[self.pts.hexapod.pulse_positions_index]
                #encoded = encoded[self.pts.hexapod.pulse_positions_index]
                try:
                    dt2 = np.column_stack((target, encoded, ind))
#                    dt2 = np.column_stack((target, encoded, qds_data))
                    np.savetxt(filename, dt2, fmt="%1.8e %1.8e %i")
#                    np.savetxt(filename, dt2, fmt="%1.8e %1.8e %1.8e")
                except:
                    print("Error in fly_result.")
                    #print(target.shape, " encoded data")
                    #print(encoded.shape, " encoded data")
                    #print(qds_data.shape, " qds data")
                print("Done...")
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
        #if self.isfly:
        #    return
        try:
            r = self.get_qds_pos()
        except:
            print("QDS does not work.")
            return
#        print(r)
        self.ui.lcd_X.display("%0.3f" % (r[0]))     
        self.ui.lcd_Z.display("%0.3f" % (r[1]))
        self.ui.lcd_Z_2.display("%0.3f" % (r[2]))
        #self.rpos = []
        #self.mpos = []
        if self.isscan:
            self.updatepos()
            if self.isfly:
                self.rpos.append([r[0], r[1], r[2]])
                #self.mpos.append(self.pts.get_pos(self.signalmotor))
                self.mpos.append(self.get_motorpos(self.signalmotor))
            self.plot()
        else:
            self.updatepos()

    def reset_qdsX(self):
        r = self.get_qds_pos(False)
        self.parameters._ref_X = r[0]
        self.parameters.writeini()
        #self.parameters._ref_X = self.ui.lcd_X.value()  

    def reset_qdsZ(self):
        r = self.get_qds_pos(False)
        self.parameters._ref_Z = r[1]
        self.parameters.writeini()
#        self.parameters._ref_Z = self.ui.lcd_Z.value()

    def reset_qdsZ2(self):
        r = self.get_qds_pos(False)
        self.parameters._ref_Z2 = r[2]
        self.parameters.writeini()
#        self.parameters._ref_Z = self.ui.lcd_Z.value()

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
#        return
        if self.isStruckCountNeeded:
            if self.isfly:
                #print("this is fly in plot")
                #pos = np.asarray(self.mpos)
                r = struck.read_mcs([0, 1, 2])
                pos = np.arange(len(r[0]))
                r = np.stack(r).T
                r = np.asarray(r)
                xl = "N"
            else:
                #print("this is scan in plot")
                r = np.asarray(self.rpos)
                pos = np.asarray(self.mpos)
        else:
            r = np.asarray(self.rpos)
            pos = np.asarray(self.mpos)
        try:
            xl = f"{self.signalmotor} ({self.signalmotorunit})"
        except:
            xl = ""

        if not hasattr(self, 'plotlabels'):
            self.plotlabels = ['']
            self.plotlabels.append('')
            self.plotlabels.append('')
        try:
            self.ax.clear()
            self.ax2.clear()
            self.ax3.clear()
            if r.ndim == 1:
                self.ax.plot(pos, r, 'r')
            else:
                self.ax.plot(pos, r[:,0], 'r')
            self.ax.set_xlabel(xl)
            # if struck is selected, it will plot struck
            
            if len(self.plotlabels) == 0:
                if self.isStruckCountNeeded:
                    yl = struck.strk.scaler.NM2
                    yl2 = struck.strk.scaler.NM3
                    yl3 = struck.strk.scaler.NM4
                else:
                    yl = 'X position (um)'
                    yl2 = 'Z position (um)'
                    yl3 = 'Z position (um)'                
                self.plotlabels = [yl, yl2, yl3]
            self.ax.set_ylabel(self.plotlabels[0])
#            print("dimension of r = ", r.ndim)
            if r.ndim == 2:
                self.ax2.plot(pos, r[:,1], 'b')
                self.ax2.set_xlabel(xl)
                self.ax3.plot(pos, r[:,2], 'k')
                self.ax3.set_xlabel(xl)
                self.ax2.set_ylabel(self.plotlabels[1])
                self.ax3.set_ylabel(self.plotlabels[2])
        except Exception as e:
            print(e)
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
                n = motornumber+1
                self.ui.findChild(QLineEdit, "ed_%i"%n).setText(pos)
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
            self.parameters.working_folder = folder
            self.update_workingfolder(self.parameters.working_folder)
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
