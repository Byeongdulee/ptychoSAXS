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
#from tools import detectors
SCAN_NUMBER_IOC = epics.PV("12idc:data:fileIndex")

from PyQt5 import uic, QtCore, QtGui
from PyQt5.QtWidgets import QMainWindow, QApplication, QPushButton, QFileDialog, QWidget, QFormLayout
from PyQt5.QtWidgets import QLabel, QLineEdit, QMessageBox, QInputDialog, QDialog, QDialogButtonBox, QMenu
from PyQt5.QtCore import QTimer, QObject, pyqtSlot, pyqtSignal, QRunnable, QThreadPool, Qt, QPoint
import pathlib

import time
#import QThread

sys.path.append('..')
sys.path.append('../ptychosaxs')

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

import pathlib
import numpy as np
#sys.path.append('../..')
#sys.path.append('../tools')

from tools.scptransfer import scp
from tools.softglue import sgz_pty, SOFTGLUE_Setup_Error
s12softglue = sgz_pty()

#Delay generator
import tools.dg645 as dg645
from tools.dg645 import DG645_Error

try:
    dg645_12ID = dg645.dg645_12ID.open_from_uri(dg645.ADDRESS_12IDC)
except:
    print("failed to connect DG645. Will not be able to collect detector images")

# struck
from tools.struck import struck
from tools.shutter import shutter

# detectors
from tools.detectors import pilatus, dante, SGstream, XSP, DET_MIN_READOUT_Error, DET_OVER_READOUT_SPEED_Error
import re
import analysis.planeeqn as eqn
import py12inifunc

from typing import List

from threading import Lock

import requests
status_url = "https://12ide.xray.aps.anl.gov/PVapp/ptycho_status"

HEXAPOD_FLYMODE_WAVELET = 0
HEXAPOD_FLYMODE_STANDARD = 1
FRACTION_EXPOSURE_PERIOD = 0.2
DETECTOR_READOUTTIME = 0.02
DETECTOR_NOT_STARTED_ERROR = -1
QDS_UNIT_NM = 0
QDS_UNIT_UM = 1
QDS_UNIT_MM = 2
QDS_UNIT_DEFAULT = QDS_UNIT_UM  # default QDS output is um
DEFAULTS = {'xmotor':0, 'ymotor':2, 'phimotor':6}  #vertical stage is Z in the scan_gui, change 'ymotor' from 1 to 2, JD
inifilename = "pty-co-saxs.ini"
STRUCK_CHANNELS = [2,3,4,5]

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
    statusmessage = pyqtSignal(str)

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


# # Step 1: Create a move class
# class runstruck(QRunnable):

#     def __init__(self, pulseN, tm):
#         super(mover, self).__init__()
#         self.pulseN = pulseN
#         self.tm = tm
#         self.signal = workerSignals()
    
#     @pyqtSlot()
#     def run(self):
#         struck.mcs_init()
#         struck.mcs_ready(self.pulseN, self.tm)
#         struck.arm_mcs()
#         self.signal.finished.emit(True)

class ptyco_main_control(QMainWindow):
#    resized = QtCore.pyqtSignal()

    def __init__(self):
        super(ptyco_main_control, self).__init__()
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose)
        guiName = "ptycoSAXS.ui"
        self.pts = pts
        print("Connecting to PTS...")
        if not self.pts.hexapod.is_servo_on('X'):
            print("Hexapod servo is off. Trying to turn it on...")
            self.handle_hexapod_error()
            print("Hexapod servo is now on.")
        #self.beamstatus = beamstatus()
        self.ui = uic.loadUi(guiName)
        self.messages = {}
        self.messages["recent error message"] = ''
        self.isOK2run = True
        self.is_softglue_savingdone = True
        self.monitor_beamline_status = True
        # list all possible motors
        # this should came from the pts.
        motornames = ['X', 'Y', 'Z', 'U', 'V', 'W', 'phi']
        motorunits = ['mm', 'mm', 'mm', 'deg', 'deg', 'deg', 'deg']
        self.hexapod_flymode = HEXAPOD_FLYMODE_WAVELET
#        self.hexapod_flymode = HEXAPOD_FLYMODE_STANDARD

        self.is_selfsaved = False
        self.is_ptychomode = True
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
            self.parameters.scan_name = ""
            self.parameters._ratio_exp_period = FRACTION_EXPOSURE_PERIOD
            self.parameters._fly_idletime = 0.033
            self.parameters.scan_time = -1
            self.parameters._pulses_per_step = 1
            self.parameters.saxsmode = 1  # 0 for ptychography, 1 for SAXS
            self.parameters.base_linux_datafolder = "/net/s12data/export/12id-c/"
        
        self.isscan = False
        self.isfly = False
        self.hdf_plugin_savemode = 2

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
        xm=DEFAULTS['xmotor']  #JD
        ym=DEFAULTS['ymotor']  #JD

        phim = self.motornames.index('phi')
        # update GUI
        for i, name in enumerate(self.motornames):
            n = i+1
            self.ui.findChild(QLabel, "lb%i"%n).setText(name)
                    # Enable a custom context menu on the label from Designer
            if n>6:
                widget_label_pos = self.ui.findChild(QLabel, "lb_%i"%n)
                widget_label_pos.setContextMenuPolicy(Qt.CustomContextMenu)
                widget_label_pos.customContextMenuRequested.connect(lambda pos, w=n: self._on_motor_context_menu(w, pos))

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
        
        self.read_motor_scan_range()

        self.ui.actionSet_Log_Filename.triggered.connect(self.set_logfilename)
        self.ui.actionRun.triggered.connect(self.timescan)
        self.ui.actionStop.triggered.connect(self.timescanstop)
        self.ui.actionClear.triggered.connect(self.clearplot)
        self.ui.actionEnable_fly_with_controller.setCheckable(True)
        self.ui.actionEnable_fly_with_controller.setChecked(True)
        self.ui.actionEnable_fly_with_controller.triggered.connect(self.select_flymode) # hexapod flyscan type.
        self.ui.actionRecord_traj_during_scan.triggered.connect(self.select_hexrecord) # hexapod record during scan.
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
        self.is_hexrecord_required = False
        self.shutter_close_after_scan = False
        self.ui.actionflyX_and_stepY.triggered.connect(lambda: self.fly2d(xm, ym))
        self.ui.actionsnake.triggered.connect(lambda: self.fly2d(xm, ym, snake=True))
        self.ui.actionstepscan.triggered.connect(lambda : self.stepscan2d(xm, ym))
        self.ui.actionnormal_2D.triggered.connect(lambda: self.fly3d(xm, ym, phim))
        self.ui.actionsnake_2D.triggered.connect(lambda: self.fly3d(xm, ym, phim, snake=True))
        self.ui.actionstep_2D.triggered.connect(lambda: self.stepscan3d(xm, ym, phim))
        self.ui.actionSelect_time_intervals.triggered.connect(self.select_timeintervals)
        self.ui.actionTrigout.triggered.connect(lambda: self.set_softglue_in(1))
        self.ui.actionDetout.triggered.connect(lambda: self.set_softglue_in(2))
        self.ui.actionevery_10_millie_seconds.triggered.connect(lambda: self.set_softglue_in(3))
        self.ui.actionPrint_flyscan_settings.triggered.connect(lambda: self.print_fly_settings(0))
        self.ui.actionSAXS.triggered.connect(lambda: self.select_detectors(1))
        self.ui.actionWAXS.triggered.connect(lambda: self.select_detectors(2))
        self.ui.actionStruck.triggered.connect(lambda: self.select_detectors(3))
        self.ui.actionSG.triggered.connect(lambda: self.select_detectors(4))
        self.ui.actionDante.triggered.connect(lambda: self.select_detectors(5))
        self.ui.actionXSP3.triggered.connect(lambda: self.select_detectors(6))
        self.ui.actionReset_to_Fly_mode.triggered.connect(self.reset_det_flymode)
        self.ui.actionChannels_to_record.triggered.connect(self.choose_softglue_channels)
        self.ui.actionSave_current_results.triggered.connect(self.save_softglue)
        self.pts.signals.AxisPosSignal.connect(self.update_motorpos)
        self.pts.signals.AxisNameSignal.connect(self.update_motorname)
        self.ui.actionTestFly.triggered.connect(self.scantest)
        self.ui.ed_workingfolder.setText(self.parameters.working_folder)
        self.ui.ed_workingfolder.returnPressed.connect(self.update_workingfolder)
        self.ui.ed_scanname.returnPressed.connect(lambda: self.update_scanname(True))
        self.ui.actionSet_waittime_between_scans.triggered.connect(self.set_waittime_between_scans)
        self.ui.actionMonitor_Beamline_Status.triggered.connect(self.set_monitor_beamline_status)
        self.ui.actionShutter_Close_Afterscan.triggered.connect(self.set_shutter_close_after_scan)
        self.ui.actionUse_hdf_plugin.triggered.connect(self.set_hdf_plugin_use)
        self.ui.actionPtychography_mode.triggered.connect(self.select_detector_mode)
        self.ui.actionCapture_multi_frames.triggered.connect(self.select_hdf_multiframecapture)
        self.ui.actionSet_basepaths.triggered.connect(self.set_basepaths)
        self.ui.actionPut_DET_alignmode.triggered.connect(self.set_det_alignmode)
        self.ui.actionSet_shot_number_per_a_step.triggered.connect(self.set_shotnumber_per_step)
        self.parameters.scan_number +=1
        #self.ui.le_scannumber.setText(str(int(self.parameters.scan_number)+1))
        self.update_scannumber()
        #self.ui.actionRatio_of_exptime_period_for_Flyscan.triggered.connect(self.set_exp_period_ratio)
        self.ui.actionRatio_of_exptime_period_for_Flyscan.triggered.connect(self.set_fly_idletime)

        if os.name != 'nt':
            self.ui.menuQDS.setDisabled(True)
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

        # Defaults
        self.isStruckCountNeeded = False
        self.set_hdf_plugin_use(True)

        # set default softglue collection freq. 10 micro seconds.
        if s12softglue.isConnected:
            s12softglue.set_count_freq(100)
        else:
            print("Softglue does not work.")

        self.rpos = []
        self.mpos = []
        ## shutter control
        #self.shutter_status = epics.PV('PA:12ID:STA_A_BEAMREADY_PL.VAL', callback=self.checkshutter)
        #self.shutter = epics.PV('12ida2:rShtrA:Open')
        self.shutter = shutter()

        # figure to plot
        # a figure instance to plot on
        self.figure = plt.figure()

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
        self.det_readout_time = DETECTOR_READOUTTIME # detector minimum readout time.
        self.detector = [None]*5
        self.detector_mode = ['', '', '', '', 'XRF']
        self.hdf_plugin_name = ['','','','', '']

        self.ui.ed_scanname.setText(self.parameters.scan_name)

        if os.name == 'nt':
            self.timer = QTimer()
            self.timer.timeout.connect(self.update_qds)
            self.timer.start(100)        

        if os.name == 'nt':
            self.timer_update = QTimer()
            self.timer_update.timeout.connect(self.update_status)
            self.timer_update.start(10_000)        
        self.ui.show()

    # FOR adding context menu on the motor label.
    def _on_motor_context_menu(self, n, pos: QPoint):
        #print(f"Context menu on: {n}")
        menu = QMenu(self.ui)
        set_zero_action = menu.addAction("Set to 0")
        set_zero_action.triggered.connect(lambda chosen, wn=n: self._on_set_to_zero(chosen, wn))

        # Map the position from the label to global screen coordinates

        global_pos = self.ui.findChild(QLabel, "lb_%i"%n).mapToGlobal(pos)
        menu.exec_(global_pos)

    def _on_set_to_zero(self, checked=False,n=0):
        self.pts.set_pos(self.motornames[n-1], 0)
        #print(f"onset zero is called for {self.motornames[n-1]}.")
        # Optionally update the label text:
        # self.ui.label_1.setText("0")

    def handle_hexapod_error(self):
        self.pts.hexapod.handle_error()

    def write_motor_scan_range(self):
        numbers = np.random.rand(len(self.motornames), 6)
        for i, name in enumerate(self.motornames):
            n = i + 1
            line_edit_suffixes = ["pos", "tweak", "L", "R", "N", "t"]
            arr = []
            
            for suffix in line_edit_suffixes:
                if len(suffix) == 1:
                    line_edit_name = f"ed_lup_{n}_{suffix}" 
                else:
                    if suffix == "tweak":
                        line_edit_name = f"ed_{n}_{suffix}"
                    if suffix == "pos":
                        line_edit_name = f"ed_{n}"
                try:
                    value = float(self.ui.findChild(QLineEdit, line_edit_name).text())
                except ValueError:  # More specific exception for parsing errors
                    value = -999999
                arr.append(value)
            
            numbers[i] = arr

            # Save the array to a file
        np.save('_numbers.npy', numbers)

    def read_motor_scan_range(self):
        # Load the array from the file
        numbers = np.load('_numbers.npy')

        for i, name in enumerate(self.motornames):
            n = i + 1
            if numbers.shape[1]==5:
                line_edit_suffixes = ["tweak", "L", "R", "N", "t"]
            if numbers.shape[1]==6:
                line_edit_suffixes = ["pos", "tweak", "L", "R", "N", "t"]
            try:
                for j, suffix in enumerate(line_edit_suffixes):
                    value = '' if numbers[i, j] == -999999 else str(numbers[i, j])
                    if len(suffix) == 1:
                        line_edit_name = f"ed_lup_{n}_{suffix}"
                    else :
                        if suffix == "tweak":
                            line_edit_name = f"ed_{n}_{suffix}"
                        if suffix == "pos":
                            line_edit_name = f"ed_{n}"
                            if len(value)>0:
                                value = "%0.6f"%float(value)
                    self.ui.findChild(QLineEdit, line_edit_name).setText(value)
            except:
                pass
            
    def set_hdf_plugin_use(self, value=None):
        if value is None:
            value = self.ui.actionUse_hdf_plugin.isChecked()
        if value:
            self.ui.actionUse_hdf_plugin.setChecked(True)
            self.ui.actionCapture_multi_frames.setEnabled(True)
            self.use_hdf_plugin = True
        else:
            self.ui.actionUse_hdf_plugin.setChecked(False)
            self.ui.actionCapture_multi_frames.setChecked(False)
            self.ui.actionCapture_multi_frames.setEnabled(False)
            self.use_hdf_plugin = False

    def select_detector_mode(self, value=None):
        if value is None:
            value = self.ui.actionPtychography_mode.isChecked()
        if value:
            self.ui.actionPtychography_mode.setChecked(True)
            self.is_ptychomode = True
            # if both detectors are chosen..
            if self.ui.actionSAXS.isChecked() and self.ui.actionWAXS.isChecked():
                # ask which one is for ptychography
                detectors = ["SAXS", "WAXS"]
                selected, ok = QInputDialog.getItem(
                    self, "Select Detector for Ptychography",
                    "Which detector will be used for ptychography measurement?",
                    detectors, 0, False
                )
                if ok:
                    if selected == "SAXS":
                        self.detector_mode[0] = 'ptycho'
                        self.detector_mode[1] = 'scattering'
                    else:
                        self.detector_mode[1] = 'ptycho'
                        self.detector_mode[0] = 'scattering'                
            else:
                if self.ui.actionSAXS.isChecked():
                    self.detector_mode[0] = 'ptycho'
                if self.ui.actionWAXS.isChecked():
                    self.detector_mode[1] = 'ptycho'
                
        else:
            self.ui.actionPtychography_mode.setChecked(False)
            self.is_ptychomode = False
            if self.ui.actionSAXS.isChecked():
                self.detector_mode[0] = 'scattering'
            if self.ui.actionWAXS.isChecked():
                self.detector_mode[1] = 'scattering'            

    def select_hdf_multiframecapture(self, value=None):
        if value is None:
            value = self.ui.actionCapture_multi_frames.isChecked()
        if value:
            self.ui.actionCapture_multi_frames.setChecked(True)
            if self.ui.actionSG.isChecked():
                self.hdf_plugin_savemode = 2
            else:    
                self.hdf_plugin_savemode = 1

        else:
            self.ui.actionCapture_multi_frames.setChecked(False)
            self.hdf_plugin_savemode = 0
#        print(self.hdf_plugin_savemode, " This is hdf plugin save mode...")

    def set_monitor_beamline_status(self, value=None):
        if value is None:
            value = self.ui.actionMonitor_Beamline_Status.isChecked()
        if value:
            self.ui.actionMonitor_Beamline_Status.setChecked(True)
            self.monitor_beamline_status = True
        else:
            self.ui.actionMonitor_Beamline_Status.setChecked(False)
            self.monitor_beamline_status = False

    def set_shutter_close_after_scan(self, value=None):
        if value is None:
            value = self.ui.actionShutter_Close_Afterscan.isChecked()
        if value:
            self.ui.actionShutter_Close_Afterscan.setChecked(True)
            self.shutter_close_after_scan = True
        else:
            self.ui.actionShutter_Close_Afterscan.setChecked(False)
            self.shutter_close_after_scan = False


    def checkshutter(self, value, **kws):
        if not self.ui.actionMonitor_Beamline_Status.isChecked():
            self.isOK2run = True
            return
        #shutter_events = {"time":time.time(), "state": value}
        #print(f"Value of the shutter is {value}")
        if value==0:
            self.isOK2run = False
        else:
            self.isOK2run = True

    # def run_hold(self, sevnt):
    #     print("run hold executed. This will hold the scan.")
    #     self.shutter_events = sevnt
    #     while self.isOK2run==False:
    #         time.sleep(10)
        
    # def run_resume(self):
    #     self.isOK2run = True

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
    def set_shotnumber_per_step(self):
        if hasattr(self.parameters, '_pulses_per_step'):
            wtime = self.parameters._pulses_per_step
        else:
            wtime = 1.0
        value, okPressed = QInputDialog.getDouble(self, "How many shots per step?","Number of shots:", wtime)
        if okPressed:
            self.parameters._pulses_per_step = value
            self.parameters.writeini()

    def update_workingfolder(self, folder=""):
        if len(folder) == 0:
            self.parameters.working_folder = self.ui.ed_workingfolder.text()
            self.parameters.writeini()
        else:
            self.ui.ed_workingfolder.setText(self.parameters.working_folder)
        self.update_scanname(update_detector=True)

    def get_detectors_ready(self):
        for i, det in enumerate(self.detector):
            #print(det, " Checking detector ", i)
            if det is not None:
                try:
                    det.filePut('FileNumber',    1)
                except:
                    continue
                det.ArrayCounter = 0
                det.set_fly_configuration()
                #if i<2:
                #    det.FileNumber = 1                

    def update_scanname(self, update_detector = True):
        self.parameters.scan_name = self.ui.ed_scanname.text()
        self.parameters.scan_number = int(self.ui.le_scannumber.text())
        self.scannumberstring = '%0.3i'%self.parameters.scan_number
        txt = "%s%0.3i"%(self.parameters.scan_name,self.parameters.scan_number)
        self.ui.lb_scanname.setText(txt)
        #wf_temp = self.ui.ed_workingfolder.text().split(':')
        p = pathlib.Path(self.ui.ed_workingfolder.text())
        wf_temp = p.parts
        #wf_temp = tmp[1]
        #workingfolder = ""
        for i in range(1, len(wf_temp)):
            if i==1:
                workingfolder = wf_temp[i]
            else:
                workingfolder = "%s/%s" %(workingfolder, wf_temp[i])
        #print(workingfolder, " update_scanname is called. with update_detector ", update_detector)
        hdfname = ""
        #print(self.detector)
        if update_detector:
            for i, det in enumerate(self.detector):
                if i==0:
                    #tp = 'S'
                    tp = 'S'
                if i==1:
                    #tp = 'W'
                    tp = "W"
                if i>1:
                    tp = ""

                Windows_workingfolder = self.ui.ed_workingfolder.text()
                
                if det is not None:
                    hdf_path = ""
                    filename = ""
                    if i<2:
                        if self.is_ptychomode:
                            folder_type = 'ptycho'
                            if self.detector_mode[i] == "":
                                self.detector_mode[i] = "ptycho"
                            if self.detector_mode[i] == 'ptycho':
                                tp = ""
                            
                            basepath = det.basepath
                            tif_path = os.path.join("/ramdisk").replace('\\', '/')

                        else: # scattering mode
                            if len(tp)==0:
                                continue
                            basepath = self.parameters.base_linux_datafolder
                            folder_type = tp+"AXS"
                            tif_path = ""
                    if "3820" in det._prefix:
                        continue
                    if "SG" in det._prefix:
                        folder_type = 'positions'
                        if self.is_ptychomode:
                            basepath = det.basepath
                        else:
                            basepath = self.parameters.base_linux_datafolder
                        tif_path = ""
                    if ("dante" in det._prefix) or ("XSP" in det._prefix):
                        folder_type = 'DANTE'
                        if self.is_ptychomode:
                            basepath = det.basepath
                        else:
                            basepath = self.parameters.base_linux_datafolder
                        
                    hdfname = tp+txt
                    if i<2:
                        filename = hdfname
                        if not self.use_hdf_plugin:
                            tif_path = hdf_path
                            filename = hdfname
                        if len(tif_path) ==0:
                            tif_path = '/ramdisk'
                        det.FilePath = tif_path
                        det.FileName = filename
                    Windows_hdf_path = os.path.join(Windows_workingfolder, folder_type, self.scannumberstring).replace('\\', '/')
                    #print(Windows_hdf_path, " This is Windows hdf path")
                    self.make_positions_folder(Windows_hdf_path)
                    hdf_path = os.path.join(basepath, workingfolder, folder_type, self.scannumberstring).replace('\\', '/')
                    #print(hdf_path, " This is ptycho path")
                    det.filePut('FilePath', hdf_path)
#                    print(f"txt is {txt}")
                    #print(f"Setting detector {i} path to {hdf_path}, filename to {hdfname}")
                    det.filePut('FileName', hdfname)
                    #print(f"Detector {i} path set to {hdf_path}, filename set to {hdfname}")
                    self.hdf_plugin_name[i] = hdfname

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
            if s12softglue.isConnected:
                s12softglue.set_count_freq(10)
        if val==2:
            self.ui.actionevery_10_millie_seconds.setChecked(False)
            self.ui.actionDetout.setChecked(True)
            self.ui.actionTrigout.setChecked(False)
            if s12softglue.isConnected:
                s12softglue.set_count_freq(100)
        if val==3:
            self.ui.actionevery_10_millie_seconds.setChecked(True)
            self.ui.actionDetout.setChecked(False)
            self.ui.actionTrigout.setChecked(False)
            if s12softglue.isConnected:
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
        val, ok = QInputDialog().getDouble(self, "Exposuretime/Period for Flyscan", "Fraction", self.parameters._ratio_exp_period, decimals=2)
        self.parameters._ratio_exp_period = val
        self.parameters.writeini()

    def set_fly_idletime(self):
        val, ok = QInputDialog().getDouble(self, "Flyscan step time-exptime", "Time (s)", self.parameters._fly_idletime, decimals=3)
        self.parameters._fly_idletime = val
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
            #self.detector[1] = None

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
    
    def get_pos_all(self):
        motors = {}
        for name in self.motornames:
            motors[name] = self.pts.get_pos(name)
        return motors
    
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
        #self.pts.phi.vel = 36
        #time.sleep(0.1)
        #self.pts.phi.acc = self.pts.phi.vel*10
        self.pts.set_speed('phi', 36, 360)

    def sethexapodvel_default(self):
#        print(self.pts.phi.vel, " This was vel value")
        self.pts.set_speed(self.pts.hexapod.axes[0], 5, None)

    def scandone(self, update_scannumber=True, donedone = True):
        # return to the initial positions
        for i, key in enumerate(self.motor_p0):
            # put only x motors and ymotors back to initial positions
            if i<2:
                self.mv(key, self.motor_p0[key])
        if donedone:
            if self.shutter_close_after_scan:
                self.shutter.close()        

        self.messages["current status"] = f"stepscan done. {time.ctime()}"
        print(self.messages["current status"])
        self.isscan = False
        self.updatepos()
        #self.plot()

        fn = ""
        for i, det in enumerate(self.detector):
            #print(det, " this is in scandone for detector ", i)
            if det is not None:
                if 'SG' in det._prefix:
                    s12softglue.flush()
                    #time.sleep(5)
                    det.ForceStop()
                    success = True
                if '3820' in det._prefix:
                    det.stop()
                    self.rpos = det.read_mcs(STRUCK_CHANNELS)
                    continue
                if 'XSP3' in det._prefix:
                    det.Acquire = 0
                    print(f"Detector {i} is still armed. Disarming it now.")
                if 'cam' in det._prefix:
                    if det.Armed == 1:
                        det.Acquire = 0
                        print(f"Detector {i} is still armed. Disarming it now.")
                if self.use_hdf_plugin:
                    while det.fileGet('WriteFile_RBV'):
                        time.sleep(0.01)
                    if len(fn)==0:
                        fnum = det.fileGet('FileNumber_RBV')
                        fn = det.fileGet('FullFileName_RBV', as_string=True)
                        if str(fnum-1) not in fn:
                            fn = det.fileGet('FullFileName_RBV', as_string=True)
                    
                    # when the measurement is all done, reset the file number to 0.
                    if update_scannumber:
                        det.filePut('FileNumber', 1)
                        #print(f"Resetting file number of detector {i} to 0.")
                        if i<2: # tiff file number 0
                            det.FileNumber = 1
                else:
                    if len(fn)==0:
                        fnum = det.FileNumber_RBV
                        fn = bytes(det.FullFileName_RBV).decode().strip('\x00')

        # save Struck as a separate txt file.
        if self.isStruckCountNeeded:
            #data = self.detector[2].read_mcs(STRUCK_CHANNELS)
            foldername, filename = self.get_softglue_filename()
            if len(foldername) == 0:
                return
            foldername = os.path.join(foldername, 'Struck', self.scannumberstring)
            os.makedirs(foldername, exist_ok=True)
            np.savetxt(os.path.join(foldername, filename + '.txt'), self.rpos)
        
        # update logfile if logfilename is set.
        if len(self.parameters.logfilename)>0:
            #pos = np.asarray(self.mpos)
            #r = np.asarray(self.rpos)
            #if len(r) > 0:
            #    self.save_list(self.parameters.logfilename, pos,r,[0,1,2],"a")
            self.mpos = []
            self.rpos = []
            scaninfo = []
            scaninfo.append('#I detector_filename')
            if len(fn)>0:
                filename = os.path.basename(fn)
                scaninfo.append(filename)
            if len(scaninfo)>1:
                self.write_scaninfo_to_logfile(scaninfo)
            scaninfo = []
            scaninfo.append('#D')
            scaninfo.append(time.ctime())

        # when the measurement is all done, update the scan number.
        if update_scannumber:
            self.run_stop_issued()
        if donedone:
            self.update_status_scan_time()

    def update_status(self):
        parameters = {}
        parameters["scan number"] = self.parameters.scan_number
        parameters["scan name"] = self.parameters.scan_name
        parameters["scan scan elapsed time"] = self.parameters.scan_time
        self.messages["parameters"] = parameters
        msg= json.dumps(self.messages)
        status = {'status': msg}
        res = requests.post(status_url, json=status)

    def set_det_alignmode(self, value=None):
        if value is None:
            value = self.ui.actionPut_DET_alignmode.isChecked()
        print("Setting detector align mode to ", value)
        if value:
            self.ui.actionPut_DET_alignmode.setChecked(True)
            for i, det in enumerate(self.detector):
                if i>1:
                    continue
                if det is not None:
                    det.filePut('AutoSave', 0)
                    det.TriggerMode = 4
                    det.Acquire = 1
        else:
            self.ui.actionPut_DET_alignmode.setChecked(False)
            for i, det in enumerate(self.detector):
                if i>1:
                    continue
                if det is not None:
                    det.filePut('AutoSave', 1)
                    det.TriggerMode = 3
                    det.Acquire = 0

    def set_basepaths(self, text=""):
        if type(text) == bool:
            text = ""
        # Prompt user for base path for detectors
#        default_basepath = '/net/micdata/data2'
        default_basepath = self.parameters.base_linux_datafolder
        if len(text)==0:
            if len(self.parameters.base_linux_datafolder)>0:
                default_basepath = self.parameters.base_linux_datafolder
            text, ok = QInputDialog.getText(self, "Set Detector Base Path", "Basepath in linux for detectors:", QLineEdit.Normal, default_basepath)
        else:
            ok = True
        if ok and text:
            self.parameters.base_linux_datafolder = text
        else:
            self.parameters.base_linux_datafolder = default_basepath

    def select_detectors(self, N, value=None):
        if N==1:
            basename = 'S12-PILATUS1:'
            if value is None:
                value = self.ui.actionSAXS.isChecked()
            if value:
                self.ui.actionSAXS.setChecked(True)
                self.detector[0] = pilatus(basename)
            else:
                self.ui.actionSAXS.setChecked(False)
                self.detector[0] = None
        if N==2:
            basename = '12idcPIL:'
            if value is None:
                value = self.ui.actionWAXS.isChecked()
            if value:
                self.ui.actionWAXS.setChecked(True)
                self.detector[1] = pilatus(basename)
            else:
                self.ui.actionWAXS.setChecked(False)
                self.detector[1] = None
        if N==3:
            if value is None:
                value = self.ui.actionStruck.isChecked()
            if value:
                self.switch_MCS(True)
                self.detector[2] = struck('12idc:')
            else:
                self.switch_MCS(False)
        if N==4:
            if value is None:
                value = self.ui.actionSG.isChecked()
            if value:
                self.switch_SGstream(True)
            else:
                self.switch_SGstream(False)
        if N==5:
            basename = '12idcDAN:'
            if value is None:
                value = self.ui.actionDante.isChecked()
            if value:
                self.ui.actionDante.setChecked(True)
                self.ui.actionXSP3.setChecked(False)
                self.detector[4] = dante(basename)
            else:
                self.ui.actionDante.setChecked(False)
                self.detector[4] = None
        if N==6:
            basename = 'XSP3_4Chan:'
            if value is None:
                value = self.ui.actionXSP3.isChecked()
            if value:
                self.ui.actionXSP3.setChecked(True)
                self.ui.actionDante.setChecked(False)
                self.detector[4] = XSP(basename)
            else:
                self.ui.actionXSP3.setChecked(False)
                self.detector[4] = None
        self.update_scanname()

    def switch_SGstream(self, status=True):
        basename = '12idSGSocket:'
        if status:
            self.ui.actionSG.setChecked(True)
            self.detector[3] = SGstream(basename, s12softglue)
            if self.ui.actionCapture_multi_frames.isChecked():
                self.hdf_plugin_savemode = 2
        else:
            self.ui.actionSG.setChecked(False)
            self.detector[3] = None
            if self.ui.actionCapture_multi_frames.isChecked():
                self.hdf_plugin_savemode = 1
            else:
                self.hdf_plugin_savemode = 0        

    def switch_MCS(self, status=True):
        if status:
            self.ui.actionStruck.setChecked(True)
            self.isStruckCountNeeded = True
            print("Struct in on")
        else:
            self.ui.actionStruck.setChecked(False)
            self.isStruckCountNeeded = False
            print("Struck is off")      

    def select_flymode(self):
        if self.ui.actionEnable_fly_with_controller.isChecked():  # when checked, this value is False
            self.ui.actionEnable_fly_with_controller.setChecked(True)
            self.hexapod_flymode = HEXAPOD_FLYMODE_WAVELET
        else:
            self.hexapod_flymode = HEXAPOD_FLYMODE_STANDARD
            self.ui.actionEnable_fly_with_controller.setChecked(False)
       
    def select_hexrecord(self):
        if self.ui.actionRecord_traj_during_scan.isChecked():  # when checked, this value is False
            self.ui.actionRecord_traj_during_scan.setChecked(True)
            self.is_hexrecord_required = True
        else:
            self.is_hexrecord_required = False
            self.ui.actionRecord_traj_during_scan.setChecked(False)

    def get_softglue_filename(self):
        foldername = self.ui.ed_workingfolder.text()
        filename = self.ui.lb_scanname.text()
        #return (foldername, filename)

        filename = ""
        for det in self.detector:
            if det is not None:
                if self.use_hdf_plugin and self.hdf_plugin_savemode>0:# capture mode
                    while det.fileGet('WriteFile_RBV'):
                        time.sleep(0.01)
                    fnum = det.fileGet('FileNumber_RBV')
                    fn = det.fileGet('FullFileName_RBV', as_string=True)
                    if str(fnum-1) not in fn:
                        fn = det.fileGet('FullFileName_RBV', as_string=True)
                    filename = os.path.basename(fn)
                    filename = "%s_%0.5i" % (rstrip_from_char(filename, "_"), fnum-1)      
                else:
                    fnum = det.FileNumber_RBV
                    fn = bytes(det.FullFileName_RBV).decode().strip('\x00')
                    filename = os.path.basename(fn)
                    filename = "%s" % rstrip_from_char(filename, "_")
            if len(filename)>0:
                break
                
        if len(filename) ==0:
            self.messages["recent error message"] = "****** Detector ioc is not available."
            print(self.messages["recent error message"])
            filename = "temp%i"%int(time.time())
        return (foldername, filename)
    
    def softglue_savingdone(self):
        self.is_softglue_savingdone = True

    def save_softglue(self):
        # read softglue data
            #foldername = os.getcwd()
        if not s12softglue.isConnected:
            print("Cannot save_softglue because softglue is not connected.")
            return
        
        N_cnt = 0
        if hasattr(self.pts.hexapod, "pulse_number"):
            N_cnt = self.pts.hexapod.pulse_number
        t = []
        ct0 = time.time()
        count = 0
        self.softglue_data = []
        #s12softglue.PROC=1
        t0 = time.time()
        t, timearray = s12softglue.get_latest_scantime()
        timeout = 10
        while (t<self.fly1d_tm):
            if time.time()-t0>timeout:
                break
            s12softglue.flush()
            time.sleep(0.25)
            t, timearray = s12softglue.get_latest_scantime()
            print(f'Flushed and {t=}')
        print(f"Time required to have softglue reading ready is {time.time()-t0}")
        arrs = s12softglue.get_arrays(self.parameters.softglue_channels)
        print(f"Time required to read softglue is {time.time()-t0}")

        self.softglue_data = (timearray, arrs)
        self.softglue_N_cnt = N_cnt
        foldername, filename = self.get_softglue_filename()
        if len(foldername) == 0:
            return
        foldername = os.path.join(foldername, 'positions', self.scannumberstring)
        self.softglue_folder = foldername
        self.softglue_filename =filename

        while self.is_softglue_savingdone is False:
            print("Previous soft glue has not been done. Waiting for done.")
            time.sleep(0.025)
        self.is_softglue_savingdone = False
        w = Worker(self.save2disk_softglue)
        w.signal.finished.connect(self.softglue_savingdone)
        self.threadpool.start(w)

    def make_positions_folder(self, foldername):
        p = pathlib.Path(foldername)
        if p.exists():
            return
        try:
            p.mkdir(parents=True, exist_ok=True)
        except:
            print("Error of creating a folder: %s. ************************"%foldername)

    def save2disk_softglue(self):
        if not s12softglue.isConnected:
            print("Cannot save2disk_softglue since softglue is not connected.")
            return
        t, indices = s12softglue.slice_timearray(self.softglue_data[0])
        dt = s12softglue.slice_arrays(indices, self.softglue_data[1]) # Skip the first array (timearray)
        N_cnt = self.softglue_N_cnt
        #t, dt = self.softglue_data
        foldername = self.softglue_folder
        filename = self.softglue_filename
        self.make_positions_folder(foldername)
        if len(t)<N_cnt:
            print("*********************************")
            print(f"Only {len(t)}, less than the ideal {N_cnt} data will be saved in {foldername}/{filename}.")
            print("*********************************")
        try:
            for i, td in enumerate(t):
                if i>=N_cnt:
                    continue
                scanname = '%s_%i.dat' % (filename, i)
                dt2 = np.column_stack((td, dt[0][i], dt[1][i], dt[2][i]))
                np.savetxt(os.path.join(foldername, scanname), dt2, fmt="%1.8e %1.8e %1.8e %1.8e")
        except:
            print("error in save2disk_softglue")

    def save2disk_softglue_original(self):
        N_cnt = self.softglue_N_cnt
        t, dt = self.softglue_data
        foldername = self.softglue_folder
        filename = self.softglue_filename

        p = pathlib.Path(foldername)
        p.mkdir(parents=True, exist_ok=True)
        print(f"Total {len(t)} data will be saved as {foldername}/{filename}.")

        try:
            for i, td in enumerate(t):
                if i>=N_cnt:
                    continue
                scanname = '%s_%i.dat' % (filename, i)
                dt2 = np.column_stack((td, dt[0][i], dt[1][i], dt[2][i]))
                np.savetxt(os.path.join(foldername, scanname), dt2, fmt="%1.8e %1.8e %1.8e %1.8e")
        except:
            print("error in save2disk_softglue")

    def save_hexapod_record(self, filename, option="a"):
        timeout = 5
        cnt = 0
        hpos = []
#        self.timer.stop()
#        time.sleep(0.5)
        wave = self.pts.hexapod.get_wavelen()
        Ndata = wave[1][1] # read the wavelet 1.
        print(f"Number of Ndata : {Ndata}")
        while True:
            try:
                hpos = self.pts.hexapod.get_records(Ndata-100)
                break
            except:
                pass
            time.sleep(0.5)
            cnt = cnt + 1
            if cnt>timeout:
                break
        if len(hpos)==0:
            print("Hexapod record saving failed.")
            return
#        self.timer.start(100)

        with open(filename, option) as f:
            strv = "N   X0   X1   Y0    Y1"
            f.write("%s\n"%strv)
            for i, m in enumerate(hpos["X"][0]):
                strv = "%0.5e   %0.5e   %0.5e   %0.5e"%(hpos["X"][0][i],hpos["X"][1][i],hpos["Z"][0][i],hpos["Z"][1][i])
                f.write("%i    %s\n"%(i, strv))

    def flydone(self, return_motor=True, reset_scannumber=True, donedone=True):
        if return_motor:
            # when 1D scan is done.
            #if self.shutter_close_after_scan:
            #    self.shutter.close()
            for i, key in enumerate(self.motor_p0):
                if self.motornames[key] == 'phi':
                    self.setphivel_default()
                if i==0:
                    if hasattr(self, '_prev_vel'):
                        self.pts.set_speed(self.motornames[key], self._prev_vel,self._prev_acc)
                self.mv(key, self.motor_p0[key])

        self.messages["current status"] = f"fly done. {time.ctime()}"
        print(self.messages["current status"])
        ct0 = time.time()

        isTestRun = self.ui.actionTestFly.isChecked()
        if isTestRun:
            return

        self.isscan = False
        self.isfly = False
        s12softglue.flush()
        print(f"softglue flushed at {time.ctime()}")
        # if len(self.parameters.logfilename)>0:
        #     if self.detector[2] is not None:
        #         # save struck data.
        #         r = self.detector[2].read_mcs(STRUCK_CHANNELS)
        #         pos = np.arange(len(r[0]))
        #         self.mpos = pos
        #         print("Number of MCS channels : ", len(r))
        #     else:
        #         # save qds data.
        #         pos = np.asarray(self.mpos)
        #         r = np.asarray(self.rpos)
        #     try:
        #         self.save_nparray(self.parameters.logfilename, pos,r,[0,1,2],"a")
        #     except:
        #         self.save_list(self.parameters.logfilename, pos,r,[0,1,2],"a")
        #     # hexapod read
        #     if self.is_hexrecord_required:
        #         self.save_hexapod_record(self.parameters.logfilename)

        #     scaninfo = []
        #     scaninfo.append('#D')
        #     scaninfo.append(time.ctime())
        #     self.write_scaninfo_to_logfile(scaninfo)
        # success=False

# #        if self.is_ptychomode:
#         try:
#             s12softglue.flush()
#             #time.sleep(0.1)
#         except:
#             self.messages["recent error message"] = "The softglue flush failed, it will be flushed again....."
#             print(self.messages["recent error message"]) 
#         if not self.ui.actionSG.isChecked(): # SG streammode is not on.
#             try:
#                 self.save_softglue()
#                 success = True
#             except:
#                 pass
#         print(f"Elapsed time to save softglue data since flydone = {time.time()-ct0}")

        # if read softglue failed...
        fn = ""
        for i, det in enumerate(self.detector):
            if det is not None:
                if 'SG' in det._prefix:
                    #time.sleep(5)
                    det.ForceStop()
                    success = True
                if '3820' in det._prefix:
                    det.stop()
                    self.rpos = det.read_mcs(STRUCK_CHANNELS)
                    continue
                if 'XSP3' in det._prefix:
                    det.Acquire = 0
                if det.Armed == 1:
                    print(f"Detector {i} is still armed. Disarming it now.")
                    fn = ""
                if self.use_hdf_plugin and (self.hdf_plugin_savemode>0):# capture mode
                    while det.fileGet('WriteFile_RBV'): # still saving?
                        time.sleep(0.01)
                    fnum = det.fileGet('FileNumber_RBV')
                    if str(fnum-1) not in fn:
                        fn = det.fileGet('FullFileName_RBV', as_string=True)
                    if reset_scannumber:
                        det.filePut('FileNumber', 1)
                else:
                    if 'cam' in det._prefix:
                        fnum = det.FileNumber_RBV
                        fn = bytes(det.FullFileName_RBV).decode().strip('\x00')
                        if reset_scannumber:
                            det.FileNumber = 1
                # if len(fn)>0:
                #     print("===============================")
                #     print(f"saved filename: {fn}")
                #     print("===============================")
                #     filename = os.path.basename(fn)
                # else:
                #     filename = ""
        # save Struck as a separate txt file.
        if self.isStruckCountNeeded:
            #data = self.detector[2].read_mcs(STRUCK_CHANNELS)
            foldername, filename = self.get_softglue_filename()
            if len(foldername) == 0:
                return
            foldername = os.path.join(foldername, 'Struck', self.scannumberstring)
            os.makedirs(foldername, exist_ok=True)
            np.savetxt(os.path.join(foldername, filename + '.txt'), self.rpos)
        
        if len(self.parameters.logfilename)>0:
            # pos = np.asarray(self.mpos)
            # r = np.asarray(self.rpos)
            # if len(r) > 0:
            #     self.save_list(self.parameters.logfilename, pos,r,[0,1,2],"a")
            self.mpos = []
            self.rpos = []
            scaninfo = []
            scaninfo.append('#I detector_filename')
            if len(fn)>0:
                    filename = os.path.basename(fn)
                    scaninfo.append(filename)
            if len(scaninfo)>1:
                self.write_scaninfo_to_logfile(scaninfo)
            scaninfo = []
            scaninfo.append('#D')
            scaninfo.append(time.ctime())
        if len(self.motor_p0.keys()) ==1: # 1d fly
            self.updateprogressbar(100)
        print(f"Elapsed time to finish flydone = {time.time()-ct0}")
        if donedone:
            self.update_status_scan_time()
            if self.shutter_close_after_scan:
                self.shutter.close()        


    def flydone2d(self, value=0):
        for key in self.motor_p0:
            self.mv(key, self.motor_p0[key])
#            self.ui.findChild(QLineEdit, "ed_%i"%n).setText("%0.4f"%self.motor_p0[m])
        self.messages["current status"] = f"2D fly done. {time.ctime()}"
        print(self.messages["current status"])
        isTestRun = self.ui.actionTestFly.isChecked()
        if isTestRun:
            return
        self.isscan = False
        self.updatepos()
#        try:
#            self.save_scaninfo()
#        except:
#            print("save_scaninfo is empty yet. This will save phi angles......")
        self.isfly = False
        self.updateprogressbar(100)
        if self.shutter_close_after_scan:
            self.shutter.close()
        self.update_status_scan_time()

    def flydone3d(self, value=0):
        for key in self.motor_p0:
            self.mv(key, self.motor_p0[key])        
        print("")
        self.messages["current status"] = f"3D fly done. {time.ctime()}"
        print(self.messages["current status"])
        isTestRun = self.ui.actionTestFly.isChecked()
        if isTestRun:
            return
        self.isscan = False
        self.updatepos()
        self.isfly = False
        self.updateprogressbar(100)
        if self.shutter_close_after_scan:
            self.shutter.close()
        self.update_status_scan_time()

    def update_status_scan_time(self, time=-1): 
        self.parameters.scan_time = time
        self.parameters.writeini()

    def timescanstop(self):
        self.isscan = False

    def timescan(self):
        if self.ui.actionckTime_reset_before_scan.isChecked():
            if s12softglue.isConnected:
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

    def check_start_position(self, n):
                        # Compare p0 and p0_original at 4 digits
        p0_original = self.ui.findChild(QLineEdit, "ed_%i"%n).text()
        p0 = self.ui.findChild(QLabel, "lb_%i"%n).text()
        if len(p0_original)>0:
            try:
                p0_float = float(p0)
                p0_original_float = float(p0_original)
            except Exception:
                p0_float = p0
                p0_original_float = p0_original
            if round(p0_float, 4) != round(p0_original_float, 4):
                msg = (
                    f"Original position ({p0_original_float:.4f}) and new position ({p0_float:.4f}) differ.\n"
                    "Do you want to move to the original position or update the original position with the current?"
                )
                dlg = QMessageBox(self)
                dlg.setWindowTitle("Position Mismatch")
                dlg.setText(msg)
                move_btn = dlg.addButton("Move to original position", QMessageBox.AcceptRole)
                update_btn = dlg.addButton("Update the original position", QMessageBox.DestructiveRole)
                cancel_btn = dlg.addButton(QMessageBox.Cancel)
                dlg.setIcon(QMessageBox.Question)
                dlg.exec_()
                clicked = dlg.clickedButton()
                if clicked == move_btn:
                    # Move to p0_original
                    p0 = p0_original
                    self.ui.findChild(QLineEdit, "ed_%i"%n).setText("%0.6f"%float(p0))
                elif clicked == update_btn:
                    # Update the roginal position to current (new)
                    p0_original = p0
                    self.ui.findChild(QLineEdit, "ed_%i"%n).setText("%0.6f"%float(p0_original))
                elif clicked == cancel_btn:
                    return None
        return p0

    def detectortime_error_question(self, expt, period):
        msg = (
            f"Exposure time {expt:.4f} and period {period:.4f} requires the readout time {period-expt},\n"
            "which is too short."
        )
        dlg = QMessageBox(self)
        dlg.setWindowTitle("Scanparameter Error")
        dlg.setText(msg)
        #move_btn = dlg.addButton("Move to original position", QMessageBox.AcceptRole)
        #update_btn = dlg.addButton("Update the original position", QMessageBox.DestructiveRole)
        cancel_btn = dlg.addButton(QMessageBox.Cancel)
        dlg.setIcon(QMessageBox.Question)
        dlg.exec_()
        clicked = dlg.clickedButton()
        return None
        #if clicked == move_btn:
        #    # Move to p0_original
        #    p0 = p0_original
        #    self.ui.findChild(QLineEdit, "ed_%i"%n).setText("%0.6f"%float(p0))
        #elif clicked == update_btn:
        #    # Update the roginal position to current (new)
        #    p0_original = p0
        #    self.ui.findChild(QLineEdit, "ed_%i"%n).setText("%0.6f"%float(p0_original))
        #elif clicked == cancel_btn:
        #    return None
    def fly2d(self, xmotor=0, ymotor=1, scanname = "", snake=False):
        self.get_detectors_ready()
        if snake:
            # if snake scan chosen, softglue socket stream and MCS will be on automatically.            
            self.switch_SGstream(True)
        else:
            self.switch_SGstream(False)

        self.isMCS_ready = False
        if self.detector[2] is not None:
            self.detector[2].mcs_init()

        self.write_motor_scan_range()
        self.isStopScanIssued = False

        if self.ui.actionckTime_reset_before_scan.isChecked():
            if s12softglue.isConnected:
                s12softglue.ckTime_reset()

        # reset the progress bar
        self.ui.progressBar.setValue(0)
        if snake:
            scan_name = 'fly2d_SNAKE'
        else:
            scan_name = 'fly2d'
        motor = [xmotor, ymotor]
        print(f'\n\n{scan_name}:{xmotor=}; {ymotor=}') #JD
                # logging
        scaninfo = []
        scaninfo.append('\n#S')
        scaninfo.append(self.parameters.scan_number)
        scaninfo.append(scan_name)
        initial_motorpos = {}
        for i, m in enumerate(motor):
            n = m+1
            try:
                scaninfo.append(n)
                #p0 = self.ui.findChild(QLineEdit, "ed_%i"%n).text()
                #if len(p0)==0:
                p0 = self.check_start_position(n)
                if type(p0) == type(None):
                    print("Canceled.")
                    break
                if type(p0) == type("a"):
                    p0 = float(p0)
                self.ui.findChild(QLineEdit, "ed_%i"%n).setText("%0.6f"%p0)

                initial_motorpos[m] = p0
                st = float(self.ui.findChild(QLineEdit, "ed_lup_%i_L"%n).text())
                fe = float(self.ui.findChild(QLineEdit, "ed_lup_%i_R"%n).text())
                tm = float(self.ui.findChild(QLineEdit, "ed_lup_%i_t"%n).text())
                step = float(self.ui.findChild(QLineEdit, "ed_lup_%i_N"%n).text())
                scaninfo.append(p0)
                scaninfo.append(st)
                scaninfo.append(fe)
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
        
        # signal for qds
        axis = self.motornames[xmotor]
        self.signalmotor = axis
        self.signalmotorunit = self.motorunits[xmotor]
        print("fly2d is called............................")

        self.time_scanstart = time.time()
        dg645_12ID.set_pilatus_fly(0.001)
        self.fly3d_p0 = None
        self.fly3d_st = None
        self.fly3d_fe = None
        self.fly3d_tm = None
        self.fly3d_step = None
        self.progress_3d = None
        self.motor_p0 = initial_motorpos

        scaninfo.append('\n#Motor Information\n')
        m = self.get_pos_all()
        for name in self.motornames:
            scaninfo.append(name)
        scaninfo.append('\n')
        for key in m:
            scaninfo.append(m[key])

        self.write_scaninfo_to_logfile(scaninfo)
        scaninfo = []
        scaninfo.append('#D')
        scaninfo.append(time.ctime())
        if snake:
            self.fly_traj(xmotor, ymotor)
            w = Worker(self.fly2d0_SNAKE, xmotor, ymotor, scanname=scanname, 
                    update_progress=None, update_status=None)
            w.signal.finished.connect(self.flydone)
            #w.signal.finished.connect(self.flydone2d)
        else:
            self.fly_traj(xmotor)
            w = Worker(self.fly2d0, xmotor, ymotor, scanname=scanname, 
                    update_progress=None, update_status=None)
            w.signal.finished.connect(self.flydone2d)
        w.signal.progress.connect(self.updateprogressbar)
        w.signal.statusmessage.connect(self.update_status_bar)
        w.kwargs['update_progress'] = w.signal.progress.emit
        w.kwargs['update_status'] = w.signal.statusmessage.emit

        self.isscan = True
        if self.monitor_beamline_status:
            self.shutter.open()        
        self.threadpool.start(w)

    # def fly2d_SNAKE(self, xmotor=0, ymotor=1, scanname = "", snake=False):
    #     if self.isStruckCountNeeded:
    #         struck.mcs_init()
    #         self.isMCS_ready = False

    #     self.write_motor_scan_range()
    #     self.isStopScanIssued = False

    #     if self.ui.actionckTime_reset_before_scan.isChecked():
    #         if s12softglue.isConnected:
    #             s12softglue.ckTime_reset()

    #     # reset the progress bar
    #     #self.ui.progressBar.setValue(0)
    #     motor = [xmotor, ymotor]
    #     print(f'\n\nfly2d_SNAKE:{xmotor=}; {ymotor=}') #JD
    #             # logging
    #     scaninfo = []
    #     scaninfo.append('\n#S')
    #     scaninfo.append(self.parameters.scan_number)
    #     scaninfo.append('fly2d_SNAKE')
    #     initial_motorpos = {}
    #     for i, m in enumerate(motor):
    #         n = m+1
    #         try:
    #             scaninfo.append(n)
    #             #p0 = self.ui.findChild(QLineEdit, "ed_%i"%n).text()
    #             #if len(p0)==0:
    #             p0 = self.check_start_position(n)
    #             p0 = float(p0)
    #             self.ui.findChild(QLineEdit, "ed_%i"%n).setText("%0.6f"%p0)
    #             initial_motorpos[m] = p0
    #             st = float(self.ui.findChild(QLineEdit, "ed_lup_%i_L"%n).text())
    #             fe = float(self.ui.findChild(QLineEdit, "ed_lup_%i_R"%n).text())
    #             tm = float(self.ui.findChild(QLineEdit, "ed_lup_%i_t"%n).text())
    #             step = float(self.ui.findChild(QLineEdit, "ed_lup_%i_N"%n).text())
    #             scaninfo.append(p0)
    #             scaninfo.append(st)
    #             scaninfo.append(fe)
    #             scaninfo.append(tm)      
    #             scaninfo.append(step)      
    #         except:
    #             showerror("Check scan paramters.")
    #             return 0
    #         if i == 0:
    #             self.fly1d_p0 = p0
    #             self.fly1d_st = st
    #             self.fly1d_fe = fe
    #             self.fly1d_tm = tm
    #             self.fly1d_step = step
    #         if i == 1:
    #             self.fly2d_p0 = p0
    #             self.fly2d_st = st
    #             self.fly2d_fe = fe
    #             self.fly2d_tm = tm
    #             self.fly2d_step = step
    #     # signal for qds
    #     axis = self.motornames[xmotor]
    #     self.signalmotor = axis
    #     self.signalmotorunit = self.motorunits[xmotor]
    #     print("fly2d_snake is called............................")
    #     self.time_scanstart = time.time()
    #     dg645_12ID.set_pilatus_fly(0.001)
    #     self.fly3d_p0 = None
    #     self.fly3d_st = None
    #     self.fly3d_fe = None
    #     self.fly3d_tm = None
    #     self.fly3d_step = None
    #     self.progress_3d = None
    #     self.motor_p0 = initial_motorpos

    #     self.fly_traj(xmotor, ymotor)

    #     scaninfo.append('\n#Motor Information\n')
    #     m = self.get_pos_all()
    #     for name in self.motornames:
    #         scaninfo.append(name)
    #     scaninfo.append('\n')
    #     for key in m:
    #         scaninfo.append(m[key])

    #     self.write_scaninfo_to_logfile(scaninfo)
    #     scaninfo = []
    #     scaninfo.append('#D')
    #     scaninfo.append(time.ctime())
    #     self.isscan = True
    #     if self.monitor_beamline_status:
    #         self.shutter.open()
    #     w = Worker(self.fly2d0_SNAKE, xmotor, ymotor, scanname=scanname, 
    #                update_progress=None, update_status=None)
    #     w.signal.finished.connect(lambda: self.flydone(False))
    #     w.signal.progress.connect(self.updateprogressbar)
    #     w.signal.statusmessage.connect(self.update_status_bar)
    #     w.kwargs['update_progress'] = w.signal.progress.emit
    #     w.kwargs['update_status'] = w.signal.statusmessage.emit
    #     self.threadpool.start(w)

    def updateprogressbar(self, value):
        self.ui.progressBar.setValue(value)
        self.update_status_scan_time(value)
        
    def update_status_bar(self, message):
        self.ui.statusbar.showMessage(message)

    def fly3d(self, xmotor=0, ymotor=1, phimotor=6, scanname="", snake=False):
        self.get_detectors_ready()
        if snake:
            # if snake scan chosen, softglue socket stream and MCS will be on automatically.            
            self.switch_SGstream(True)
        else:
            self.switch_SGstream(False)
        self.isMCS_ready = False

        if self.detector[2] is not None:
            self.detector[2].mcs_init()

        self.write_motor_scan_range()
        self.isStopScanIssued = False
        if self.ui.actionckTime_reset_before_scan.isChecked():
            if s12softglue.isConnected:
                s12softglue.ckTime_reset()
        if snake:
            scan_name = 'fly3d_SNAKE'
        else:
            scan_name = 'fly3d'

        motor = [xmotor, ymotor, phimotor]
        axis = self.motornames[xmotor]
#        print(motor)
        # logging
        scaninfo = []
        scaninfo.append('\n#S')
        scaninfo.append(self.parameters.scan_number)
        scaninfo.append(scan_name)
        initial_motorpos = {}
        for i, m in enumerate(motor):
            n = m+1
            try:
                scaninfo.append(n)
                #p0 = self.ui.findChild(QLineEdit, "ed_%i"%n).text()
                
                p0 = self.check_start_position(n)
                p0 = float(p0)
                self.ui.findChild(QLineEdit, "ed_%i"%n).setText("%0.6f"%p0)
                initial_motorpos[m] = p0
                st = float(self.ui.findChild(QLineEdit, "ed_lup_%i_L"%n).text())
                fe = float(self.ui.findChild(QLineEdit, "ed_lup_%i_R"%n).text())
                tm = float(self.ui.findChild(QLineEdit, "ed_lup_%i_t"%n).text())
                step = float(self.ui.findChild(QLineEdit, "ed_lup_%i_N"%n).text())
                scaninfo.append(p0)
                scaninfo.append(st)
                scaninfo.append(fe)
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

        # signal for qds
        axis = self.motornames[xmotor]
        self.signalmotor = axis
        self.signalmotorunit = self.motorunits[xmotor]
        print("fly2d is called............................")

        dg645_12ID.set_pilatus_fly(0.001)
        self.motor_p0 = initial_motorpos

        scaninfo.append('\n# Motor Information\n')
        m = self.get_pos_all()
        for name in self.motornames:
            scaninfo.append(name)
        scaninfo.append('\n')
        for key in m:
            scaninfo.append(m[key])

        self.write_scaninfo_to_logfile(scaninfo)
        scaninfo = []
        scaninfo.append('#D')
        scaninfo.append(time.ctime())
        self.time_scanstart = time.time()

        if snake:
            self.fly_traj(xmotor, ymotor)
        else:
            self.fly_traj(xmotor)

        self.isscan = True
        if self.monitor_beamline_status:
            self.shutter.open()
        w = Worker(self.fly3d0, xmotor, ymotor, phimotor, scanname=scanname, snake=snake,
            update_progress=None, update_status=None)
        w.signal.finished.connect(self.flydone3d)
        w.signal.progress.connect(self.updateprogressbar)
        w.signal.statusmessage.connect(self.update_status_bar)
        w.kwargs['update_progress'] = w.signal.progress.emit
        w.kwargs['update_status'] = w.signal.statusmessage.emit
        self.threadpool.start(w)

    def fly(self, motornumber=-1):
        self.get_detectors_ready()
        self.update_scanname()
        self.isMCS_ready = False
        if self.detector[2] is not None:
            self.detector[2].mcs_init()

        self.write_motor_scan_range()
        self.isStopScanIssued = False
        if motornumber<0:
            pb = self.sender()
            objname = pb.objectName()
            n = int(re.findall(r'\d+', objname)[0])
            motornumber = n-1
        # else:
        #     axis = self.motornames[motornumber]
        #     n = motornumber + 1
        axis = self.motornames[motornumber]
        n = motornumber + 1

        # logging
        scaninfo = []
        scaninfo.append('\n#S')
        scaninfo.append(self.parameters.scan_number)
        scaninfo.append('fly')
        scaninfo.append(n)    
        initial_motorpos = {}    

        try:
            p0 = self.check_start_position(n)
            p0 = float(p0)
            self.ui.findChild(QLineEdit, "ed_%i"%n).setText("%0.6f"%p0)
            initial_motorpos[motornumber] = p0
            st = float(self.ui.findChild(QLineEdit, "ed_lup_%i_L"%n).text())
            fe = float(self.ui.findChild(QLineEdit, "ed_lup_%i_R"%n).text())
            tm = float(self.ui.findChild(QLineEdit, "ed_lup_%i_t"%n).text())
            step = float(self.ui.findChild(QLineEdit, "ed_lup_%i_N"%n).text())
        except:
            showerror("Check scan paramters.")
            return 0
        dg645_12ID.set_pilatus_fly(0.001)
        self.motor_p0 = initial_motorpos
        self.fly1d_p0 = p0
        self.fly1d_st = st
        self.fly1d_fe = fe
        self.fly1d_tm = tm
        self.fly1d_step = step
        scaninfo.append(p0)
        scaninfo.append(st)
        scaninfo.append(fe)
        scaninfo.append(tm)
        scaninfo.append(step)
        scaninfo.append('\n#Motor Information\n')
        m = self.get_pos_all()
        for name in self.motornames:
            scaninfo.append(name)
        scaninfo.append('\n')
        for key in m:
            scaninfo.append(m[key])
        self.write_scaninfo_to_logfile(scaninfo)
        print(axis, " this is axis name")
        if axis in self.pts.hexapod.axes:
            self.fly_traj(motornumber)
        self.isscan = True
        if self.monitor_beamline_status:
            self.shutter.open()
        w = Worker(self.fly0, motornumber, update_progress=None, update_status=None)
        w.signal.finished.connect(self.flydone)
        w.signal.progress.connect(self.updateprogressbar)
        w.kwargs['update_progress'] = w.signal.progress.emit
        self.threadpool.start(w)
        self.run_stop_issued()
        self.update_status_scan_time()

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
        if self.parameters._pulses_per_step>1 and not self.use_hdf_plugin:
            dlg = QMessageBox(self)
            dlg.setWindowTitle("Check HDF Plugin")
            msg = (
                f"Pulses per step is set to {self.parameters._pulses_per_step}.\n"
                "HDF5 plugin must be used for multi-pulse per step scans.\n")
            dlg.setText(msg)
            cancel_btn = dlg.addButton(QMessageBox.Cancel)
            dlg.setIcon(QMessageBox.Question)
            dlg.exec_()
            clicked = dlg.clickedButton()
            return
        
        self.get_detectors_ready()
        self.update_scanname()
        self.write_motor_scan_range()
        self.isStopScanIssued = False
        motor = motornumber

        scan_name = "stepscan"
        print(f'\n\n{scan_name}:{motor=}')


        # logging
        scaninfo = []
        scaninfo.append('\n#S')
        scaninfo.append(self.parameters.scan_number)
        scaninfo.append(scan_name)
        initial_motorpos = {}
        if motornumber<0:
            pb = self.sender()
            objname = pb.objectName()
            n = int(re.findall(r'\d+', objname)[0])
            motornumber = n-1
        else:
            n = motornumber + 1
        
        try:
            p0 = self.check_start_position(n)
            p0 = float(p0)
            self.ui.findChild(QLineEdit, "ed_%i"%n).setText("%0.6f"%p0)
            st = float(self.ui.findChild(QLineEdit, "ed_lup_%i_L"%n).text())
            fe = float(self.ui.findChild(QLineEdit, "ed_lup_%i_R"%n).text())
            tm = float(self.ui.findChild(QLineEdit, "ed_lup_%i_t"%n).text())
            step = float(self.ui.findChild(QLineEdit, "ed_lup_%i_N"%n).text())
        except:
            showerror("Check scan parameters.")
            return 0

        # signal for qds
        axis = self.motornames[motornumber]
        self.signalmotor = axis
        self.signalmotorunit = self.motorunits[motornumber]

        self.time_scanstart = time.time()
        self.stepscan_p0 = p0
        self.stepscan_st = st
        self.stepscan_fe = fe
        self.stepscan_expt = tm
        self.stepscan_step = step
        self.motor_p0 = initial_motorpos

        scaninfo.append('\n#Motor Information\n')
        m = self.get_pos_all()
        for name in self.motornames:
            scaninfo.append(name)
        scaninfo.append('\n')
        for key in m:
            scaninfo.append(m[key])

        self.write_scaninfo_to_logfile(scaninfo)
        scaninfo = []
        scaninfo.append('#D')
        scaninfo.append(time.ctime())

        w = Worker(self.stepscan0, motornumber,update_progress=None, update_status=None)
        w.signal.finished.connect(self.scandone)
        w.signal.progress.connect(self.updateprogressbar)
        w.signal.statusmessage.connect(self.update_status_bar)
        w.kwargs['update_progress'] = w.signal.progress.emit
        w.kwargs['update_status'] = w.signal.statusmessage.emit

        self.isscan = True
        if self.monitor_beamline_status:
            self.shutter.open()        
        self.threadpool.start(w)


    def stepscan2d(self, xmotor=0, ymotor=1):
        self.get_detectors_ready()
        self.update_scanname()
        self.write_motor_scan_range()
        self.isStopScanIssued = False
        motor = [xmotor, ymotor]
        scan_name = "stepscan2d"
        print(f'\n\n{scan_name}:{xmotor=}; {ymotor=}')

        if self.parameters._pulses_per_step>1 and not self.use_hdf_plugin:
            dlg = QMessageBox(self)
            dlg.setWindowTitle("Check HDF Plugin")
            msg = (
                f"Pulses per step is set to {self.parameters._pulses_per_step}.\n"
                "HDF5 plugin must be used for multi-pulse per step scans.\n")
            dlg.setText(msg)
            cancel_btn = dlg.addButton(QMessageBox.Cancel)
            dlg.setIcon(QMessageBox.Question)
            dlg.exec_()
            clicked = dlg.clickedButton()
            return
        # logging
        scaninfo = []
        scaninfo.append('\n#S')
        scaninfo.append(self.parameters.scan_number)
        scaninfo.append(scan_name)
        initial_motorpos = {}
        for i, m in enumerate(motor):
            n = m+1
            try:
                scaninfo.append(n)
                p0 = self.check_start_position(n)
                if type(p0) == type(None):
                    print("Canceled.")
                    break
                if type(p0) == type("a"):
                    p0 = float(p0)
                self.ui.findChild(QLineEdit, "ed_%i"%n).setText("%0.6f"%p0)

                initial_motorpos[m] = p0
                st = float(self.ui.findChild(QLineEdit, "ed_lup_%i_L"%n).text())
                fe = float(self.ui.findChild(QLineEdit, "ed_lup_%i_R"%n).text())
                tm = float(self.ui.findChild(QLineEdit, "ed_lup_%i_t"%n).text())
                step = float(self.ui.findChild(QLineEdit, "ed_lup_%i_N"%n).text())
                scaninfo.append(p0)
                scaninfo.append(st)
                scaninfo.append(fe)
                scaninfo.append(tm)      
                scaninfo.append(step)      
            except:
                showerror("Check scan paramters.")
                return 0
            if i == 0:
                self.stepscan1d_p0 = p0
                self.stepscan1d_st = st
                self.stepscan1d_fe = fe
                self.stepscan1d_tm = tm
                self.stepscan1d_step = step
            if i == 1:
                self.stepscan2d_p0 = p0
                self.stepscan2d_st = st
                self.stepscan2d_fe = fe
                self.stepscan2d_tm = tm
                self.stepscan2d_step = step

        # signal for qds
        axis = self.motornames[xmotor]
        self.signalmotor = axis
        self.signalmotorunit = self.motorunits[xmotor]

        self.time_scanstart = time.time()
        self.stepscan3d_p0 = None
        self.stepscan3d_st = None
        self.stepscan3d_fe = None
        self.stepscan3d_tm = None
        self.stepscan3d_step = None
        self.progress_3d = None
        self.motor_p0 = initial_motorpos

        scaninfo.append('\n#Motor Information\n')
        m = self.get_pos_all()
        for name in self.motornames:
            scaninfo.append(name)
        scaninfo.append('\n')
        for key in m:
            scaninfo.append(m[key])

        self.write_scaninfo_to_logfile(scaninfo)
        scaninfo = []
        scaninfo.append('#D')
        scaninfo.append(time.ctime())

        w = Worker(self.stepscan2d0, xmotor, ymotor, update_progress=None, update_status=None)
        w.signal.finished.connect(self.scandone)
        w.signal.progress.connect(self.updateprogressbar)
        w.signal.statusmessage.connect(self.update_status_bar)
        w.kwargs['update_progress'] = w.signal.progress.emit
        w.kwargs['update_status'] = w.signal.statusmessage.emit

        self.isscan = True
        if self.monitor_beamline_status:
            self.shutter.open()        
        self.threadpool.start(w)


    def stepscan3d(self, xmotor=0, ymotor=1, phimotor=6):
        if self.parameters._pulses_per_step>1 and not self.use_hdf_plugin:
            dlg = QMessageBox(self)
            dlg.setWindowTitle("Check HDF Plugin")
            msg = (
                f"Pulses per step is set to {self.parameters._pulses_per_step}.\n"
                "HDF5 plugin must be used for multi-pulse per step scans.\n")
            dlg.setText(msg)
            cancel_btn = dlg.addButton(QMessageBox.Cancel)
            dlg.setIcon(QMessageBox.Question)
            dlg.exec_()
            clicked = dlg.clickedButton()
            # if clicked == move_btn:
            #     pass
            return
                
        self.get_detectors_ready()
        self.isMCS_ready = False
        if self.detector[2] is not None:
            self.detector[2].mcs_init()

        self.write_motor_scan_range()
        self.isStopScanIssued = False
        if self.ui.actionckTime_reset_before_scan.isChecked():
            if s12softglue.isConnected:
                s12softglue.ckTime_reset()
        scan_name = 'stepscan3d'

        motor = [xmotor, ymotor, phimotor]
        axis = self.motornames[xmotor]
        # logging
        scaninfo = []
        scaninfo.append('\n#S')
        scaninfo.append(self.parameters.scan_number)
        scaninfo.append(scan_name)
        initial_motorpos = {}
        for i, m in enumerate(motor):
            n = m+1
            try:
                scaninfo.append(n)
                
                p0 = self.check_start_position(n)
                p0 = float(p0)
                self.ui.findChild(QLineEdit, "ed_%i"%n).setText("%0.6f"%p0)
                initial_motorpos[m] = p0
                st = float(self.ui.findChild(QLineEdit, "ed_lup_%i_L"%n).text())
                fe = float(self.ui.findChild(QLineEdit, "ed_lup_%i_R"%n).text())
                tm = float(self.ui.findChild(QLineEdit, "ed_lup_%i_t"%n).text())
                step = float(self.ui.findChild(QLineEdit, "ed_lup_%i_N"%n).text())
                scaninfo.append(p0)
                scaninfo.append(st)
                scaninfo.append(fe)
                scaninfo.append(tm)
                scaninfo.append(step)
            except:
                showerror("Check scan paramters.")
                return 0
            if i == 0:
                self.stepscan1d_p0 = p0
                self.stepscan1d_st = st
                self.stepscan1d_fe = fe
                self.stepscan1d_tm = tm
                self.stepscan1d_step = step
            if i==1:
                self.stepscan2d_p0 = p0
                self.stepscan2d_st = st
                self.stepscan2d_fe = fe
                self.stepscan2d_tm = tm
                self.stepscan2d_step = step
            if i==2:
                self.stepscan3d_p0 = p0
                self.stepscan3d_st = st
                self.stepscan3d_fe = fe
                self.stepscan3d_tm = tm
                self.stepscan3d_step = step

        # signal for qds
        axis = self.motornames[xmotor]
        self.signalmotor = axis
        self.signalmotorunit = self.motorunits[xmotor]
        print("stepscan2d is called............................")

        dg645_12ID.set_pilatus_fly(0.001)
        self.motor_p0 = initial_motorpos

        scaninfo.append('\n# Motor Information\n')
        m = self.get_pos_all()
        for name in self.motornames:
            scaninfo.append(name)
        scaninfo.append('\n')
        for key in m:
            scaninfo.append(m[key])

        self.write_scaninfo_to_logfile(scaninfo)
        scaninfo = []
        scaninfo.append('#D')
        scaninfo.append(time.ctime())
        self.time_scanstart = time.time()

        self.isscan = True
        if self.monitor_beamline_status:
            self.shutter.open()
        w = Worker(self.stepscan3d0, xmotor, ymotor, phimotor, update_progress=None, update_status=None)
        #w.signal.finished.connect(self.scandone)
        w.signal.progress.connect(self.updateprogressbar)
        w.signal.statusmessage.connect(self.update_status_bar)
        w.kwargs['update_progress'] = w.signal.progress.emit
        w.kwargs['update_status'] = w.signal.statusmessage.emit
        self.threadpool.start(w)


    def update_graph(self):
        print("update_graph called")
        pass

    def timescan0(self):
        self.tempfilename = "C:\\TEMP\\_qds_temporary.txt"
        N_selfsave_points = 1024
        k = 0
        while self.isscan:
            r = self.get_qds_pos()
            self.rpos.append(r)
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
            pos = []
            for p in r:
                for p2 in p:
                    pos.append(p2)
        else:
            pos = self.pts.qds.get_position(self.parameters.softglue_channels)
        pos = np.array(pos)
        r = pos/1000
        r = np.append(r, epics.caget('usxLAX:12IDE_temperature'))
        if isrefavailable:
            ref = [self.parameters._ref_X, self.parameters._ref_Z, self.parameters._ref_Z2]
            if len(ref)<len(r):
                ref = ref + [0]*(len(r)-len(ref))
            ref = np.array(ref) 
            r = r-ref
        return r

    def run_stop_issued(self):
        self.parameters.scan_number = self.parameters.scan_number + 1
        self.update_scannumber()
        self.parameters.writeini()

    def update_scannumber(self):
        SCAN_NUMBER_IOC.put(int(self.parameters.scan_number))
        self.ui.le_scannumber.setText(str(int(self.parameters.scan_number)))

    def stepscan0(self, motornumber=-1, update_progress=None, update_status=None):
        axis = self.motornames[motornumber]
        self.signalmotor = axis
        self.signalmotorunit = self.motorunits[motornumber]
        self.rpos = []
        self.mpos = []
        pos = self.pts.get_pos(axis)
        pos0 = pos
        self.isfly = False
        n = motornumber+1

        if not self.ui.cb_keepprevscan.isChecked():
            self.clearplot()

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

        # scaninfo = []
        # scaninfo.append('#H')
        # if self.detector[2] is not None:
        #     scaninfo.append(axis)
        #     scaninfo.append(self.detector[2].scaler.NM2)
        #     scaninfo.append(self.detector[2].scaler.NM3)
        #     scaninfo.append(self.detector[2].scaler.NM4)
        # else:
        #     scaninfo.append(axis)
        #     scaninfo.append('QDS1')
        #     scaninfo.append('QDS2')
        #     scaninfo.append('QDS3')
        # self.write_scaninfo_to_logfile(scaninfo)  

        # prepare to collect Detector images
        isDET_selected = False
#        print(pos, " This is the phi angle to be measured...............")
        if len(self.detector)>0:
            for detN, det in enumerate(self.detector):
                if det is not None:
                    isDET_selected = True
                    print(detN, det)
                    print(det._prefix)
                    print(f"Exposure time set to %0.3f seconds for {det._prefix}."% expt)
                    try:
                        #det.fly_ready(expt, len(pos))
                        det.step_ready(expt, len(pos), pulsespershot = self.parameters._pulses_per_step, fn=self.hdf_plugin_name[detN])  # Arm detector for multiple data.
                        print("det is ready.")
                    except TimeoutError:
                        self.messages["recent error message"] = f"Detector, {det._prefix}, hasnt started yet. Fly scan own start."
                        print(self.messages["recent error message"])
                        self.ui.statusbar.showMessage(self.messages["recent error message"])
                        #showerror("Detector timeout.")
                        return
        # each time it will send a pulse
        if self.parameters._pulses_per_step==1:
            period = 0
        else:
            period = expt + 0.020  # in seconds
            if period<0.03:
                period = 0.03
        dg645_12ID.set_pilatus(expt, trigger_source=5, DGNimage = self.parameters._pulses_per_step, Cycperiod=period)
        
        if self.isStruckCountNeeded:
            self.detector[2].mcs_counter_init()
            #struck.mcs_counter_init()

        self.plotlabels = []
        
        ## make a plot if needed.
#        print(pos)
        N_imgcollected = 0
        t0 = time.time()
        for i, value in enumerate(pos):
            if self.isStopScanIssued:
                break
            self.pts.mv(axis, value)
            print("Motor moved...")
                        # if needed, wait between scans
            time.sleep(self.parameters._waittime_between_scans)

            # trigger the detector.
            if self.parameters._pulses_per_step>1:
                for detN, det in enumerate(self.detector):
                    if det is not None:
                        while det.Armed == 0 or det.getCapture() == 0:
                            det.StartCapture()
                            time.sleep(0.1)
                            print("Start capture ....")
            # make sure trigger done.                
            for ndet, det in enumerate(self.detector):
                if ndet>2: 
                    continue
                if det is not None:
                    if self.parameters._pulses_per_step>1:
                        while det.Armed == 0 or det.getCapture() == 0:
                            time.sleep(0.02)
                    else:
                        while det.Armed == 0:
                            time.sleep(0.02)
            if isDET_selected:
                dg645_12ID.trigger()
#                print("Trigger sent")
            # Waiting for data collection done.
            TIMEOUT = 10
            t_start = time.time()
            timeout_occurred = False
            for ndet, det in enumerate(self.detector):
                if ndet>1: 
                    continue
                if det is not None:
                    while det.ArrayCounter_RBV < self.parameters._pulses_per_step*(i+1):
                        time.sleep(0.02)
                        if (time.time() - t_start) > TIMEOUT:
                            timeout_occurred = True
                            print(f"Timeout occurred for detector {det._prefix} after {TIMEOUT} seconds.")
                            break
                    if timeout_occurred:  
                        print("Breaking out of detector loop due to timeout.") 
                        break
            if timeout_occurred:
                print(f"Timeout occurred after {TIMEOUT} seconds while waiting for detector to finish.")
                self.messages["recent error message"] = f"Timeout occurred after {TIMEOUT} seconds while waiting for detector to finish."
                return DETECTOR_NOT_STARTED_ERROR

            # Update progress bar and status message.
            timeelapsed = time.time()-t0
            prog = float(i+1)/float(len(pos))
            if update_progress:
                update_progress(int(prog*100))
            msg1 = f'Elapsed time = {int(timeelapsed)}s since the start.'
            if prog>0:
                remainingtime = timeelapsed/prog - timeelapsed
            else:
                remainingtime = 999
            msg2 = f"; Remaining time for the current 2D scan is {np.round(remainingtime,2)}s\n"
            msg = "%s%s"%(msg1, msg2)
            if update_status:
                update_status(msg)

            self.mpos.append(value)
        self.pts.mv(axis, pos0)


    def get_detectors_armed(self):
        TIMEOUT = 10               
        t_start = time.time()
        timeout_occurred = False
    
        for ndet, det in enumerate(self.detector):
            if ndet>2: 
                continue
            if det is not None:
                while det.Armed == 0:
                    det.Arm()
                    time.sleep(0.5)
                    print(f"Detector {ndet} is Armed again.................")
                    if (time.time() - t_start) > TIMEOUT:
                        timeout_occurred = True
                        print(f"Timeout occurred for detector {det._prefix} after {TIMEOUT} seconds.")
                        break
        return timeout_occurred, TIMEOUT
    
    def is_arming_detecotors_timedout(self):
        TIMEOUT = 10               
        t_start = time.time()
        timeout_occurred = False
        for detN, det in enumerate(self.detector):
            if det is not None:
                if self.parameters._pulses_per_step>1:
                    while det.Armed == 0 or det.getCapture() == 0:
                        det.StartCapture()
                        time.sleep(0.1)
                        if (time.time() - t_start) > TIMEOUT:
                            timeout_occurred = True
                            print(f"Timeout occurred for detector {det._prefix} after {TIMEOUT} seconds.")
                            break
                else:
                    while det.Armed == 0:
                        det.Arm()
                        time.sleep(0.1)
                        if (time.time() - t_start) > TIMEOUT:
                            timeout_occurred = True
                            print(f"Timeout occurred for detector {det._prefix} after {TIMEOUT} seconds.")
                            break
                    if timeout_occurred:  
                        print("Breaking out of detector loop due to timeout.") 
                        break
        return timeout_occurred, TIMEOUT

    def is_waiting_detectors_timedout(self, expt, i):
        if self.parameters._pulses_per_step > 1.5:
            TIMEOUT = (expt+0.03) * self.parameters._pulses_per_step + 10
        else:
            TIMEOUT = expt + 3
        t_start = time.time()
        timeout_occurred = False
        for ndet, det in enumerate(self.detector):
            if ndet>1: 
                continue
            if det is not None:
                while det.ArrayCounter_RBV < self.parameters._pulses_per_step*(i+1):
                    time.sleep(0.02)
                    if (time.time() - t_start) > TIMEOUT:
                        timeout_occurred = True
                        print(f"Timeout occurred for detector {det._prefix} after {TIMEOUT} seconds.")
                        break
                if timeout_occurred:  
                    print("Breaking out of detector loop due to timeout.") 
                    break        
        return timeout_occurred, TIMEOUT
    
    def stepscan2d0(self, xmotor=0, ymotor=1, update_progress=None, update_status=None):
        self.update_scanname()
        #print(ymotor, " this is ymortor")
        yaxis = self.motornames[ymotor]
        xaxis = self.motornames[xmotor]
        self.signalmotor2 = yaxis
        self.signalmotorunit2 = self.motorunits[ymotor]
        #pos = self.pts.get_pos(yaxis)
        self.isfly2 = False

        # Just in case when the user update edit box (during 3d scan) 
        # Will need to update the positions.
        n = ymotor+1
        p0 = self.ui.findChild(QLineEdit, "ed_%i"%n).text()

        p0 = float(p0)
        st = float(self.ui.findChild(QLineEdit, "ed_lup_%i_L"%n).text())
        fe = float(self.ui.findChild(QLineEdit, "ed_lup_%i_R"%n).text())
        step = float(self.ui.findChild(QLineEdit, "ed_lup_%i_N"%n).text())
        self.stepscan2d_p0 = p0
        self.stepscan2d_st = st
        self.stepscan2d_fe = fe
        self.stepscan2d_step = step

        n = xmotor+1
        p0 = self.ui.findChild(QLineEdit, "ed_%i"%n).text()

        p0 = float(p0)
        st = float(self.ui.findChild(QLineEdit, "ed_lup_%i_L"%n).text())
        fe = float(self.ui.findChild(QLineEdit, "ed_lup_%i_R"%n).text())
        expt = float(self.ui.findChild(QLineEdit, "ed_lup_%i_t"%n).text())
        step = float(self.ui.findChild(QLineEdit, "ed_lup_%i_N"%n).text())
        self.stepscan1d_p0 = p0
        self.stepscan1d_st = st
        self.stepscan1d_fe = fe
        self.stepscan1d_step = step

        ## prepare zig-zag positions ................
        # build Y range (absolute)
        yst = self.stepscan2d_st + self.stepscan2d_p0
        yfe = self.stepscan2d_fe + self.stepscan2d_p0
        ystep = self.stepscan2d_step

        # build X range (absolute)
        xst = self.stepscan1d_st + self.stepscan1d_p0
        xfe = self.stepscan1d_fe + self.stepscan1d_p0
        xstep = self.stepscan1d_step
        
        if xstep == 0:
            xstep = (xfe - xst) if (xfe != xst) else 1.0
        xstep = -abs(xstep) if xst > xfe else abs(xstep)
        x_coords = np.arange(xst, xfe + 0.5 * xstep, xstep)

        # build Y range (absolute) from st, fe, step already computed above
        ystep = ystep if ystep != 0 else ((yfe - yst) if (yfe != yst) else 1.0)
        ystep = -abs(ystep) if yst > yfe else abs(ystep)
        y_coords = np.arange(yst, yfe + 0.5 * ystep, ystep)

        # create zig-zag list of (x,y) pairs: left-to-right on first row, right-to-left on next, etc.
        coords = []
        for j, y in enumerate(y_coords):
            xs = x_coords if (j % 2 == 0) else x_coords[::-1]
            coords.extend([(x, y) for x in xs])

        # Nx2 numpy array of (x, y)
        pos = np.asarray(coords)
        Nline = len(pos)
        # keep for later use if needed
        self.stepscan2d_positions = pos
        #dg645_12ID.set_pilatus(expt, trigger_source=5, DGNimage=1)
        # each time it will send a pulse

        # scaninfo = []
        # scaninfo.append('#H')
        # if self.detector[2] is not None:
        #     scaninfo.append(xaxis)
        #     scaninfo.append(yaxis)
        #     scaninfo.append(self.detector[2].scaler.NM2)
        #     scaninfo.append(self.detector[2].scaler.NM3)
        #     scaninfo.append(self.detector[2].scaler.NM4)
        # else:
        #     scaninfo.append(xaxis)
        #     scaninfo.append(yaxis)
        #     scaninfo.append('QDS1')
        #     scaninfo.append('QDS2')
        #     scaninfo.append('QDS3')
        # self.write_scaninfo_to_logfile(scaninfo)  

        if self.parameters._pulses_per_step==1:
            period = 0
        else:
            period = expt + 0.020  # in seconds
            if period<0.03:
                period = 0.03
        dg645_12ID.set_pilatus(expt, trigger_source=5, DGNimage = self.parameters._pulses_per_step, Cycperiod=period)


        ## prepre detectors ............
        for detN, det in enumerate(self.detector): #JD
            if det is not None:  #JD
                #det.step_ready(expt, Nline)
                det.step_ready(expt, Nline, pulsespershot = self.parameters._pulses_per_step, fn=self.hdf_plugin_name[detN])  # Arm detector for multiple data.
                print(f"step _ready, detector {detN}'s status: {det.Armed}")  #JD

        t0 = time.time()
        self.isStopScanIssued = False

        # make sure detectors get armed.                
        self.get_detectors_armed()

        self.messages["recent error message"] = ""
        self.messages["current status"] = ""
        self.messages["progress"] = ""

        print("Starting 2D step scan now...........................")
        for i, value in enumerate(pos):
            # prepare for status update

            if self.isStopScanIssued:
                break
#            print(pos[i,0], pos[i,1], " Moving to this position...............")

            pos_status = False
            while not pos_status:
                pos_status = self.pts.hexapod.mv(xaxis, pos[i,0], yaxis, pos[i,1], wait=True)
                #print(pos_status, " Hexapod move status")
                if not pos_status:
                    self.messages["recent error message"] = f"Hexapod move failed, trying to handle the error... {time.ctime()}"
                    print(self.messages["recent error message"])
                    pos_status = self.pts.hexapod.handle_error()
                    self.messages["current status"] = f"Hexapod error is fixed.... {time.ctime()}"
                    #update_status(self.currnt_status_msg)
                    print(self.messages["current status"])

            time.sleep(self.parameters._waittime_between_scans)

            timeout_occurred, TIMEOUT = self.is_arming_detecotors_timedout()
            if timeout_occurred:
                self.messages["recent error message"] = f"Timeout occurred after {TIMEOUT} seconds while waiting for detector to be Armed. {time.ctime()}"
                print(self.messages["recent error message"])
                return DETECTOR_NOT_STARTED_ERROR
            
            # trigger the detector.
            dg645_12ID.trigger()
            self.messages["current status"] = f"Trigger sent out for {i}th point.......................... {time.ctime()}\r"
            print(self.messages["current status"])

            # waiting for data collection done.
            timeout_occurred, TIMEOUT = self.is_waiting_detectors_timedout(expt, i)

            if timeout_occurred:
                self.messages["recent error message"] = f"Timeout occurred after {TIMEOUT} seconds while waiting for detector to collect data. {time.ctime()}"
                print(self.messages["recent error message"])
                return DETECTOR_NOT_STARTED_ERROR

            timeelapsed = time.time()-t0
            self.mpos.append(value)
            #self.mpos.append(timeelapsed)
            msg = ""
            if update_progress:
                #print("Updating progress bar in 2d step scan")
                # if this is a part of 3d scan
                if self.stepscan3d_p0 is not None: # 3d scan
                    #print("3d scan progress update")
                    c3d, all3d = self.progress_3d
                    progress_fraction = (Nline * c3d + (i + 1)) / (Nline * all3d)
                    timeelapsed = time.time()-self.time_scanstart
                    update_progress(int(progress_fraction*100))
                    remtime= np.round(timeelapsed*(1/progress_fraction-1),2)
                    msg1 = f'Updated at {time.ctime()} : {int(timeelapsed)}s since the start.'
                    msg2 = f"; Remaining time for the current 3D scan is {remtime}s, or {time.ctime(time.time()+remtime)}\n"
                else:
                    #print("2d scan progress update")
                    progress_fraction = (i+1)/Nline
                    update_progress(int(progress_fraction*100))
                    remtime = np.round(timeelapsed*(1/progress_fraction-1),2)
                    msg1 = f'Updated at {time.ctime()} : {int(timeelapsed)}s since the start.'
                    msg2 = f"; Remaining time for the current 2D scan is {remtime}s, or {time.ctime(time.time()+remtime)}\n"
                self.messages["progress"] = "%s%s"%(msg1, msg2)
            if update_status:
                update_status(self.messages["progress"])

        #self.run_stop_issued()
        return 1


    def stepscan3d0(self, xmotor=0, ymotor=-1, phimotor=-1, update_progress=None, update_status=None):
        axis = self.motornames[phimotor]
        self.signalmotor3 = axis
        self.signalmotorunit3 = self.motorunits[phimotor]
        self.isfly3 = False
    
        st = self.stepscan3d_st + self.stepscan3d_p0
        fe = self.stepscan3d_fe + self.stepscan3d_p0
        step = self.stepscan3d_step

        if st>fe:
            step = -1*abs(step)
        if st<fe:
            step = abs(step)

        # revsere scan disabled: always scan from start to final regardless of the initial position.
        self.pts.mv(axis, st)
        pos = np.arange(st, fe+step/2, step)
        
        i=0
        Npos = len(pos)
        retried_dueto_timeout = 0

        while i<Npos:
            wait_long = False
            value = pos[i]
            if self.isStopScanIssued:
                break
            
            # loging phi angle information
            print("")
            print("*****")
            print(f"phi position : {value:.3e}")
            scaninfo = []
            scaninfo.append('#I phi = ')
            scaninfo.append(value)
            self.write_scaninfo_to_logfile(scaninfo) 

            self.pts.mv(axis, value)
            # fly here
            #scan="%s%0.3d"%(scanname, i)
            self.progress_3d = (i, Npos)
            retval = self.stepscan2d0(xmotor=xmotor, ymotor=ymotor, update_progress=update_progress, update_status=update_status)
            if retval == DETECTOR_NOT_STARTED_ERROR:
                msg = f'Detector refresh failed .'
                update_status(msg)
                retried_dueto_timeout = retried_dueto_timeout + 1
                wait_long = True
                i = i - 1  # retry the same angle
                if retried_dueto_timeout > 2:
                    msg = f'Detector refresh failed 3 times. Aborting 3D scan.'
                    update_status(msg)
                    break
            if update_status:
                msg = f'Elapsed time = {time.time()-self.time_scanstart}s to finish {(i+1)/len(pos)*100}%.'
                update_status(msg)
            
            self.scandone(True, False)
            if wait_long:
                time.sleep(60)

            # monitoring the station ready
            if self.monitor_beamline_status:
                # if beam is down, wait here
                if self.isOK2run is not True:
                    self.wait_for_beam(update_status, value)
                    # retry the same angle
                    i -= 1
            i=i+1



    def fly3d0(self, xmotor=0, ymotor=1, phimotor=6, scanname = "", snake=False, update_progress=None, update_status=None):
        # xmotor is for flying
        # ymotor is for stepping
        # phimotor is for rotation
        axis = self.motornames[phimotor]
        self.signalmotor3 = axis
        self.signalmotorunit3 = self.motorunits[phimotor]
        pos = self.pts.get_pos(axis)
        self.isfly3 = False
        
        st = self.fly3d_st + self.fly3d_p0
        fe = self.fly3d_fe + self.fly3d_p0
        step = self.fly3d_step

        if st>fe:
            step = -1*abs(step)
        if st<fe:
            step = abs(step)

        # revsere scan disabled: always scan from start to final regardless of the initial position.
        self.pts.mv(axis, st)
        pos = np.arange(st, fe+step/2, step)
        retried_dueto_timeout = 0

        if len(scanname):
            scanname=axis
        else:
#            print(scanname, axis)
            scanname=f"{scanname}{axis}"
        i=0
        while i<len(pos):
            wait_long = False
            value = pos[i]
#        for i, value in enumerate(pos):
            if self.isStopScanIssued:
                break
            
            # loging phi angle information
            print("")
            print("*****")
            print(f"phi position : {value:.3e}")
            scaninfo = []
            scaninfo.append('#I phi = ')
            scaninfo.append(value)
            self.write_scaninfo_to_logfile(scaninfo) 

            self.pts.mv(axis, value)
            # fly here
            scan="%s%0.3d"%(scanname, i)
            self.progress_3d = (i, len(pos))
            if snake:
                retval = self.fly2d0_SNAKE(xmotor=xmotor, ymotor=ymotor, scanname=scan, 
                    update_progress=update_progress, update_status=update_status)
            else:
                retval = self.fly2d0(xmotor=xmotor, ymotor=ymotor, scanname=scan, 
                    update_progress=update_progress, update_status=update_status)
            
            if retval == DETECTOR_NOT_STARTED_ERROR:
                msg = f'Detector refresh failed .'
                update_status(msg)
                retried_dueto_timeout = retried_dueto_timeout + 1
                wait_long = True
                i = i - 1  # retry the same angle
                if retried_dueto_timeout > 2:
                    msg = f'Detector refresh failed 3 times. Aborting 3D scan.'
                    update_status(msg)
                    break
            if update_status:
                msg = f'Elapsed time = {time.time()-self.time_scanstart}s to finish {(i+1)/len(pos)*100}%.'
                update_status(msg)
            
            self.flydone(False, reset_scannumber=True, donedone=False)
            if wait_long:
                time.sleep(60)

            # monitoring the station ready
            if self.monitor_beamline_status:
                # if beam is down, wait here
                if self.isOK2run is not True:
                    self.wait_for_beam(update_status, value)
                    # retry the same angle
                    i -= 1
            i=i+1


    def wait_for_beam(self, update_status, value):
        ct0 = time.time()
        while self.isOK2run is not True:
            time.sleep(10)
            self.messages["current status"] = f'Beam has been down for {int((time.time()-ct0)/60)} minutes. {time.ctime()}'
            update_status(self.messages["current status"])
            if self.isStopScanIssued:
                break
        # Need some action after shutter back up
        self.shutter.open_A()
        self.messages["current status"] = f'Beam just came back. A-shutter open command was sent and run will resume in 10mins. {time.ctime()}'
        update_status(self.messages["current status"])
        time.sleep(60)
        self.shutter.open_A()
        time.sleep(60*9)
        scaninfo = []
        scaninfo.append('\n')
        scaninfo.append('#Note: Shutter has been closed for %i mins'%int((time.time()-ct0)/60))
        scaninfo.append('#Note: angle %0.3f will be re-run'%value)
        self.write_scaninfo_to_logfile(scaninfo) 

    def fly2d0(self, xmotor = 0, ymotor=1, scanname = "", update_progress=None, update_status=None):
        self.update_scanname()

        axis = self.motornames[ymotor]
        self.signalmotor2 = axis
        self.signalmotorunit2 = self.motorunits[ymotor]
        pos = self.pts.get_pos(axis)
        self.isfly2 = False

    # Just in case when the user update edit box (during 3d scan) 
    # Will need to update the positions.
        n = ymotor+1
        p0 = self.ui.findChild(QLineEdit, "ed_%i"%n).text()

        p0 = float(p0)
        st = float(self.ui.findChild(QLineEdit, "ed_lup_%i_L"%n).text())
        fe = float(self.ui.findChild(QLineEdit, "ed_lup_%i_R"%n).text())
        self.fly2d_p0 = p0
        self.fly2d_st = st
        self.fly2d_fe = fe

        n = xmotor+1
        p0 = self.ui.findChild(QLineEdit, "ed_%i"%n).text()

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

        if st>fe:
            step = -1*abs(step)
        if st<fe:
            step = abs(step)

        self.pts.mv(axis, st)
        pos = np.arange(st, fe+step/2, step)
        if len(scanname):
            scanname=axis
        else:
            scanname=f"{scanname}{axis}"
        Nline = len(pos)
        isreshreshed = 1
        for i, value in enumerate(pos):
            if self.isStopScanIssued:
                break
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

            # fly here

            status = 0
            while status < 1:
                if self.use_hdf_plugin and (self.hdf_plugin_savemode>0):
                    for det in self.detector: #JD
                        if det is not None:  #JD
                            if not hasattr(det, 'filePut'):
                                continue
                            if "cam" or "SG" or "dante" or "xsp3" in det._prefix.lower():
                                det.filePut('FileNumber', i+1) 

                status = self.fly0(xmotor)
                if status is DETECTOR_NOT_STARTED_ERROR:
                    isreshreshed = self.refresh_detectors()
                if isreshreshed == 0:
                    print("Detector refresh failed. Stopping scan.")
                    self.messages["current status"] = f'Detector refresh failed. Stopping scan. {time.ctime()}'
                    update_status(self.messages["current status"])
                    break
            if isreshreshed == 0:
                return DETECTOR_NOT_STARTED_ERROR

            self.flydone(return_motor=False, reset_scannumber=False)
 
            t1 = time.time()
            while (time.time()-t1 < self.parameters._waittime_between_scans):
                time.sleep(0.01)
            msg = ""
            timeelapsed = time.time()-t0
            if update_progress:
                if self.fly3d_p0: # 3d scan
                    c3d, all3d = self.progress_3d
                    progress_fraction = (Nline * c3d + (i + 1)) / (Nline * all3d)
                    timeelapsed = time.time()-self.time_scanstart
                    update_progress(int(progress_fraction*100))
                    remtime = np.round(timeelapsed*(1/progress_fraction-1),2)
                    msg1 = f'Elapsed time = {int(timeelapsed)}s since the start.'
                    msg2 = f"; Remaining time for the current 3D scan is {remtime}s or {time.ctime(time.time()+remtime)}\n"
                else:
                    #print("2d scan progress update")
                    progress_fraction = (i+1)/Nline
                    update_progress(int(progress_fraction*100))
                    remtime = np.round(timeelapsed*(1/progress_fraction-1),2)
                    msg1 = f'Elapsed time = {int(timeelapsed)}s since the start.'
                    msg2 = f"; Remaining time for the current 2D scan is {remtime}s or {time.ctime(time.time()+remtime)}\n"
                self.messages["current status"] = "%s%s"%(msg1, msg2)
            if update_status:
                update_status(self.messages["current status"])

        self.run_stop_issued()
        return 1

    def refresh_detectors(self):
        """Refresh the detectors to ensure they are ready for the next scan."""
        stata = 1
        for detN, det in enumerate(self.detector):
            if detN > 1:
                continue
            if det is not None:
                scaninfo = []
                scaninfo.append("\n")
                scaninfo.append(f'#I {det._prefix} IOC error at %{time.ctime()}.\n')
                m1, m2, m3 = det.getMessages()
                scaninfo.append(f"{m1}\n{m2}\n{m3}")
                scaninfo.append("\n")
                self.write_scaninfo_to_logfile(scaninfo)
                try:
                    status = det.refresh() # if failed, it will return 0. ohterwise it will return 1.
                    stata = stata*status
                except Exception as e:
                    print(f"Error refreshing detector {det._prefix}: {e}")
                    self.ui.statusbar.showMessage(f"Error refreshing detector {det._prefix}: {e}")
        return stata
    
    def fly_traj(self, xmotor=0, ymotor=-1):
    # Just in case when the user update edit box (during 3d scan) 
    # Will need to update the positions.
        isSNAKE = False
        if ymotor>-1: # 2D SNAKE scan
            n = ymotor+1
            Yaxis = self.motornames[ymotor]
            isSNAKE = True

        n = xmotor+1
        Xaxis = self.motornames[xmotor]

        # get relative scan range and convert it to absolute...
        Xst = self.fly1d_st + self.fly1d_p0
        Xfe = self.fly1d_fe + self.fly1d_p0
        # This was step time before, but now we will use it as distance step and compute step time based on the speed of hexapod and the distance step.
        Xstep = self.fly1d_step  # step distance (this was step time before)
        # This was the total time before, but now we will use it as the exposure time
        # a time for each step will be calculated.
        Xtm = self.fly1d_tm

        # step time calculation
        flyidletime= self.parameters._fly_idletime
        if flyidletime < self.det_readout_time:
            print("Note that the fly idle time per step %.3f s is too short to readout the detector images. It will be automatically set to %.3f s, which is the detector readout time."%(self.parameters._fly_idletime, self.det_readout_time))
            flyidletime = self.det_readout_time
        if Xtm + flyidletime < 0.033:
            print(f"Note that the step time is too short for the detector collection frequency. It will be automatically set to 0.033s.")
            flyidletime = 0.033 - Xtm
        step_time  = Xtm + flyidletime

        #if step_time < 0.033:
        #    step_time = 0.033
        self.parameters._ratio_exp_period = Xtm / step_time
        # total time calculation
        Nsteps = int((Xfe-Xst)/Xstep)+1
        total_time = Nsteps * step_time
        #Xtm = total_time
        #Xstep = step_time
        
        self.Xi = Xst
        self.Xf = Xfe
        self.Xaxis = Xaxis
        
        if isSNAKE:
            # get relative scan range and convert it to absolute...
            Yst = self.fly2d_st + self.fly2d_p0
            Yfe = self.fly2d_fe + self.fly2d_p0
            Ystep = self.fly2d_step

            self.Yi = Yst
            self.Yf = Yfe
            self.Yaxis = Yaxis

            #self.pts.hexapod.set_traj_SNAKE(total_time, Xst, Xfe-Xst, Yst, Yfe, Ystep, step_time)
            self.pts.hexapod.set_traj_SNAKE2(step_time, Xst, Xfe-Xst, Xstep,Yst, Yfe, Ystep)
            minstep, commonstep = self.pts.hexapod.analyze_pulse_steps()
            if minstep != commonstep:
                binsize = 0.001 # convert index to second
                print(f"Warning: The pulse steps is mostly {commonstep*binsize*1000:.1f} ms")
                print(f"but there are some smaller steps of {minstep*binsize*1000:.3f} ms.")
                print(f"This is likely due to the way the hexapod handles the trajectory.") 
                print(f"You may want to adjust the fly step size to be a multiple of {commonstep*binsize*1000:.3f} ms to achieve more consistent step sizes.")
        else: # regular scan
            self.pts.hexapod.set_traj(Xaxis, total_time, Xfe-Xst, Xst, 1, abs(step_time), 50)

    def fly2d0_SNAKE(self, xmotor = 0, ymotor=1, scanname = "", update_progress=None, update_status=None):
        self.update_scanname()

        self.isfly2 = False
        ##### ############## need to work from this........

        print()
        # scaninfo = []
        # scaninfo.append('#I Y = ')
        # scaninfo.append(value)
        # self.write_scaninfo_to_logfile(scaninfo) 
        #print("In fly2d0")
        t0 = time.time()

        self.plotlabels = []
        if self.ui.actionckTime_reset_before_scan.isChecked():
            if s12softglue.isConnected:
                s12softglue.ckTime_reset()
        if self.ui.actionMemory_clear_before_scan.isChecked():
            try:
                if s12softglue.isConnected:
                    s12softglue.memory_clear()
            except TimeoutError:
                self.messages["recent error message"] = "softglue memory_clear timeout"
                print(self.messages["recent error message"])


        isTestRun = self.ui.actionTestFly.isChecked()
        if isTestRun:
            print("**** Test Run:")
        self.isfly = True
        self.isscan = True

        # disable fit menu
        self.ui.actionFit_QDS_phi.setEnabled(False)

        if not self.ui.cb_keepprevscan.isChecked():
            self.clearplot()

        #expt = np.around(self.pts.hexapod.scantime/self.pts.hexapod.pulse_number*0.75, 3)
        period = self.pts.hexapod.pulse_step
        print(self.pts.hexapod.pulse_number, "This is the number of pulses......")
        #expt = period-self.det_readout_time  JD
        expt = period*self.parameters._ratio_exp_period # JMM, *0.2 previously for JD. -0.02 previously for BL
        #if period-expt < DETECTOR_READOUTTIME:
        #    raise RuntimeError("expouretime is too short to readout DET images.")

        if expt <= 0:
            self.messages["recent error message"] = f"Note that after subtracting the detector readout time {self.det_readout_time:.3e} s, the exposure time becomes equal or less than 0."
            print(self.messages["recent error message"])
#                    print("******* Cannot run.")
            raise DET_MIN_READOUT_Error(self.messages["recent error message"])
        if abs(period) < 0.033:
            self.messages["recent error message"] = f"Note that Max speed of Pilatus2M is 30Hz."
            print(self.messages["recent error message"])
            raise DET_OVER_READOUT_SPEED_Error(self.messages["recent error message"])

        # set the delay generator
        if expt != dg645_12ID._exposuretime:
            try:
                dg645_12ID.set_pilatus_fly(expt)
            except:
                raise DG645_Error
            

        #SoftGlue ready for recording interferometer values
        movestep = self.fly1d_step*1000*self.parameters._ratio_exp_period
        print(f"Actual exposure time: {expt:0.3e} s. In distance: {movestep:.3e} um.")

        if isTestRun:
            return
        
        # Scan start ............................
#        print("Time to finish line 2182: %0.3f" % (time.time()-t0))
        axes = [self.Xaxis, self.Yaxis]
        #axes = ["SNAKE_X", "SNAKE_Y"]
#        print(axes)
        self.pts.hexapod.goto_start_pos(axes) # took 0.4 second
#        print("Time to finish line 2184: %0.3f" % (time.time()-t0))
#        print(self.pts.hexapod.pulse_number_per_line, " pulses per line")
#        print(self.pts.hexapod.number_of_lines, " number of lines")
        for det in self.detector:
            if det is not None:
                try:
                    # det.fly_ready(expt, self.pts.hexapod.pulse_number_per_line, self.pts.hexapod.number_of_lines, period=period, 
                    #                 isTest = isTestRun, capture=(self.use_hdf_plugin, self.hdf_plugin_savemode))
                    det.fly_ready(expt, self.pts.hexapod.pulse_number, 1, period=period, 
                                    isTest = isTestRun, capture=(self.use_hdf_plugin, self.hdf_plugin_savemode))
        #            print("Time to finish line 2190: %0.3f" % (time.time()-t0)) # take 0.3 second
                except TimeoutError:
                    self.messages["recent error message"] = f"Detector, {det._prefix}, hasnt started yet. Fly scan will not start."
                    print(self.messages["recent error message"])
                    self.ui.statusbar.showMessage(self.messages["recent error message"])
                    #showerror("Detector timeout.")
                    return
        timeout_occurred, TIMEOUT = self.is_arming_detecotors_timedout()
        if timeout_occurred:
            self.messages["recent error message"] = f"Timeout occurred after {TIMEOUT} seconds while waiting for detector to be Armed. {time.ctime()}"
            print(self.messages["recent error message"])
            return DETECTOR_NOT_STARTED_ERROR
        
        print("Ready for traj")

        if not isTestRun:
            self.pts.hexapod.run_traj(axes)

        # Update progress bar and status message.
        N_imgcollected = 0
        timeelapsed = time.time()-t0
        #TIMEOUT = period*2+1
        timestart = time.time()
        #Nstep = self.pts.hexapod.pulse_number_per_line*self.pts.hexapod.number_of_lines
        Nstep = self.pts.hexapod.pulse_number
        TIMEOUT = period*Nstep + 2
        while N_imgcollected<Nstep:
            for ndet, det in enumerate(self.detector):
                if ndet>1: 
                    continue
                if det is not None:
                    val = det.ArrayCounter_RBV
                    continue
            timeelapsed = time.time()-t0
            progress_fraction = float(val)/float(Nstep)
            if progress_fraction==0:
                progress_fraction=0.0001
            if update_progress:
                if self.fly3d_p0: # 3d scan
                    c3d, all3d = self.progress_3d
                    #update_progress(int(prog*c3d/all3d*100))
                    progress_fraction = progress_fraction*c3d/all3d
                    timeelapsed = time.time()-self.time_scanstart
                    #time_per_pos = timeelapsed / (i + 1)
                    update_progress(int(progress_fraction*100))
                    remtime = np.round(timeelapsed*(1/progress_fraction-1),2)
                    msg1 = f'Updated at {time.ctime()} : {int(timeelapsed)}s since the start.'
                    msg2 = f"; Remaining time for the current 3D scan is {remtime}s or {time.ctime(time.time()+remtime)}\n"
                else:
                    #print("2d scan progress update")
                    #progress_fraction = (i+1)/Nline
                    update_progress(int(progress_fraction*100))
                    remtime = np.round(timeelapsed*(1/progress_fraction-1),2)
                    msg1 = f'Updated at {time.ctime()} : {int(timeelapsed)}s since the start.'
                    msg2 = f"; Remaining time for the current 2D scan is {remtime}s or {time.ctime(time.time()+remtime)}\n"

                self.messages["current status"] = "%s%s"%(msg1, msg2)
            if update_status:
                update_status(self.messages["current status"])

            time.sleep(0.1)
            if val>N_imgcollected:
                N_imgcollected = val
                timestart = time.time()

            updatetime = time.time()-timestart
            if updatetime>TIMEOUT:
                self.messages["recent error message"] = f"Data collection timeout after {TIMEOUT} seconds."
                print(self.messages["recent error message"])
                self.ui.statusbar.showMessage(self.messages["recent error message"])
                return DETECTOR_NOT_STARTED_ERROR
            timeelapsed = time.time()-t0
            if self.isStopScanIssued:
                break

        self.run_stop_issued()
        return 1
        #print("Time to finish fly0: %0.3f" % (time.time()-t0))

    def fly0(self, motornumber=-1, update_progress=None, update_status=None):
        t0 = time.time()
        axis = self.motornames[motornumber]
        self.signalmotor = axis
        self.signalmotorunit = self.motorunits[motornumber]
        self.plotlabels = []
        if self.ui.actionckTime_reset_before_scan.isChecked():
            if s12softglue.isConnected:
                s12softglue.ckTime_reset()
        if self.ui.actionMemory_clear_before_scan.isChecked():
            try:
                if s12softglue.isConnected:
                    s12softglue.memory_clear()
            except TimeoutError:
                self.messages["recent error message"] = "softglue memory_clear timeout"
                print(self.messages["recent error message"])

        print("")
        isTestRun = self.ui.actionTestFly.isChecked()
        if isTestRun:
            print("**** Test Run:")
        self.isfly = True
        self.isscan = True

        # disable fit menu
        self.ui.actionFit_QDS_phi.setEnabled(False)

        if not self.ui.cb_keepprevscan.isChecked():
            self.clearplot()
        
        st = self.fly1d_st + self.fly1d_p0
        fe = self.fly1d_fe + self.fly1d_p0
        step = self.fly1d_step
        tm = self.fly1d_tm

        pos = self.pts.get_pos(axis)
        #print("Time to finish line 2127: %0.3f" % (time.time()-t0)) very fast down to this far
        if (axis in self.pts.hexapod.axes) and (self.hexapod_flymode==HEXAPOD_FLYMODE_WAVELET):
            if self.ui.cb_reversescandir.isChecked():
                if abs(st-pos)>abs(fe-pos):
                    t = fe
                    fe = st
                    st = t 
                    step = -step
            direction = int(step)/abs(step)
            if direction==1:
                dirv = 0
            else:
                dirv = 6
            self.pts.hexapod.assign_axis2wavtable(axis, self.pts.hexapod.WaveGenID[axis]+dirv)

            period = self.pts.hexapod.pulse_step  # pulse step time.
            expt = period*self.parameters._ratio_exp_period # JMM, *0.2 previously for JD. -0.02 previously for BL
            if isTestRun:
                print(f"{self.pts.hexapod.pulse_number} images will be collected every {period}s with exposure time of {expt}s.")
            
            if period-expt < DETECTOR_READOUTTIME:
                self.messages["recent error message"] = (
                    f"Exposure time {expt:.4f} and period {period:.4f} requires the readout time {period-expt}, which is too short."
                )
                print(self.messages["recent error message"])
                self.ui.statusbar.showMessage(self.messages["recent error message"])
                return None

            if expt <= 0:
                self.messages["recent error message"] = f"Note that after subtracting the detector readout time {self.det_readout_time:.3e} s, the exposure time becomes equal or less than 0."
                print(self.messages["recent error message"])
                raise DET_MIN_READOUT_Error(self.messages["recent error message"])
            
            if abs(period) < 0.033:
                self.messages["recent error message"] = f"Note that Max speed of Pilatus2M is 30Hz."
                print(self.messages["recent error message"])
                raise DET_OVER_READOUT_SPEED_Error(self.messages["recent error message"])

            # set the delay generator
            if expt != dg645_12ID._exposuretime:
                try:
                    dg645_12ID.set_pilatus_fly(expt)
                except:
                    raise DG645_Error

            #SoftGlue ready for recording interferometer values
            movestep = abs(fe-st)/self.pts.hexapod.pulse_number*1000*self.parameters._ratio_exp_period
            print(f"Actual exposure time: {expt:0.3e} s, during which {axis} will move {movestep:.3e} um.")

            # If softglue SG is not selected, use prepare for the softglue.
            if self.detector[3] is None: 
                if s12softglue.isConnected:
                    N_counts = s12softglue.number_acquisition(expt, self.pts.hexapod.pulse_number)
                    self.parameters.countsperexposure = np.round(N_counts/self.pts.hexapod.pulse_number)
                    print(f"Total {self.parameters.countsperexposure} encoder positions will be collected per a DET image.")
                    if N_counts>100000:
                        self.messages["recent error message"] = f"******** CAUTION: Number of softglue counts: {N_counts} is larger than 100E3. Slow down the clock speed."
                        raise SOFTGLUE_Setup_Error(self.messages["recent error message"])

            if isTestRun:
                return
            
            # Scan start ............................
            self.pts.hexapod.goto_start_pos(axis) # took 0.4 second
            for detN, det in enumerate(self.detector):
                if det is not None:
                    try:
                        det.fly_ready(expt, self.pts.hexapod.pulse_number, period=period, 
                                        isTest = isTestRun, capture=(self.use_hdf_plugin, self.hdf_plugin_savemode), fn=self.hdf_plugin_name[detN])
                    except TimeoutError:
                        self.messages["recent error message"] = f"Detector, {det._prefix}, hasnt started yet. Fly scan will not start."
                        print(self.messages["recent error message"])
                        self.ui.statusbar.showMessage(self.messages["recent error message"])
                        return DETECTOR_NOT_STARTED_ERROR
            print("Ready for traj")
            pos = self.pts.get_pos(axis)
            print(f"pos is {pos} before traj run start.")

            timeout_occurred, TIMEOUT = self.is_arming_detecotors_timedout()
            if timeout_occurred:
                self.messages["recent error message"] = f"Timeout occurred after {TIMEOUT} seconds while waiting for detector to be Armed. {time.ctime()}"
                print(self.messages["recent error message"])
                return DETECTOR_NOT_STARTED_ERROR
        
            istraj_running = False
            timeout = 5
            i = 0
            print("Trajectory scan initiated..")
            while not istraj_running:
                try:
                    self.pts.hexapod.run_traj(axis)
                except:
                    pass
                time.sleep(0.05)
                pos_tmp = self.pts.get_pos(axis)
                if pos_tmp != pos:
                    istraj_running = True
                #istraj_running = self.is_traj_running()
                i = i+1
                if i>timeout:
                    self.messages["recent error message"] = "traj scan command is resent for 5 times to the hexapod without success."
                    print(self.messages["recent error message"])
                    break
            print("Run_traj is sent command in rungui.")
            isattarget = False
            timeelapsed = 0
            t0 = time.time()
            while not isattarget:
                try:
                    isattarget = self.pts.hexapod.isattarget(axis)
                except:
                    isattarget = False
                time.sleep(0.02)
                #pos_tmp = self.pts.get_pos(axis)
                timeelapsed = time.time()-t0
                prog = float(timeelapsed)/float(tm)
                if update_progress:
                    update_progress(int(prog*100))
                msg1 = f'Elapsed time = {int(timeelapsed)}s since the start.'
                if prog>0:
                    remainingtime = timeelapsed/prog - timeelapsed
                else:
                    remainingtime = 999
                msg2 = f"; Remaining time for the current 2D scan is {np.round(remainingtime,2)}s\n"
                self.messages["current status"] = "%s%s"%(msg1, msg2)
                if update_status:
                    update_status(self.messages["current status"])

                if self.isStopScanIssued:
                    break

            pos = self.pts.get_pos(axis)
            print(f"pos is {pos:.3e} after the traj run done.")
        # fly scan with a constant velocity of motions.
        else: 
            print("Fly scan with phi.")
            # fly for phi scan is unique.
            # tm is the total time for the fly scan, which is determined by the user input.
            # step is the angle step, which is determined by the user input.
            Xstep = self.fly1d_step  # step angle (this was step time before)
            # This was the total time before, but now we will use it as the exposure time
            # a time for each step will be calculated.
            Xtm = self.fly1d_tm

            # step time calculation
            step_time  = Xtm + self.det_readout_time
            if step_time < 0.033:
                step_time = 0.033
            #self.parameters._ratio_exp_period = Xtm / step_time
            # total time calculation
            Nsteps = int((fe-st)/Xstep)
            total_time = Nsteps * step_time
            #expt = step_time*self.parameters._ratio_exp_period # JMM, *0.2 previously for JD. -0.02 previously for BL
            expt = Xtm
            if step_time - expt < 0.015:
                raise DET_MIN_READOUT_Error(f"Period - Exposure Time,{step_time-expt}s, should be longer than 50 microseconds.")

            # set the delay generator
            try:
                dg645_12ID.set_pilatus2(expt, Nsteps, step_time)  # exposuretime, number of images, and time period for fly scan.
            except:
                raise DG645_Error
            print(f"Exposure time: {expt:0.3e} s, number of steps: {Nsteps}, Step time: {step_time:.3e} s, Total time for the scan: {total_time:.3f} s.")
            if self.ui.cb_reversescandir.isChecked():
                if abs(st-pos)>abs(fe-pos):
                    t = fe
                    fe = st
                    st = t 

            if motornumber ==6:
                # enable fit menu
                self.ui.actionFit_QDS_phi.setEnabled(True)

            self._prev_vel,self._prev_acc = self.pts.get_speed(axis)
            self.pts.mv(axis, st, wait=True)
            time.sleep(0.1)
            #print(f"Setting speed for fly scan. Total time: {abs(fe-st)/total_time:.3f} s, acceleration: {abs(fe-st)/total_time*10:.3f}.")
            self.pts.set_speed(axis, abs(fe-st)/total_time, abs(fe-st)/total_time*10)
            time.sleep(0.02)

            # Need to make detectors ready
            for detN, det in enumerate(self.detector):
                if det is not None:
                    try:
                        det.fly_ready(expt, Nsteps, period=step_time, 
                                        isTest = isTestRun, capture=(self.use_hdf_plugin, self.hdf_plugin_savemode),fn=self.hdf_plugin_name[detN])
            #            print("Time to finish line 2190: %0.3f" % (time.time()-t0)) # take 0.3 second
                    except TimeoutError:
                        self.messages["recent error message"] = f"Detector, {det._prefix}, hasnt started yet. Fly scan will not start."
                        print(self.messages["recent error message"])
                        self.ui.statusbar.showMessage(self.messages["recent error message"])
                        #showerror("Detector timeout.")
                        return            

            timeout_occurred, TIMEOUT = self.is_arming_detecotors_timedout()
            if timeout_occurred:
                self.messages["recent error message"] = f"Timeout occurred after {TIMEOUT} seconds while waiting for detector to be Armed. {time.ctime()}"
                print(self.messages["recent error message"])
                return DETECTOR_NOT_STARTED_ERROR

            scaninfo = []
            print("")
            print(f"{axis} scan started..")
            scaninfo.append(f"FileIndex, {axis},    time(s)")
            scaninfo.append(f'0,   {st},   {time.time()}')
            self.pts.mv(axis, fe, wait=False)
            
            print("about to send out trigger.")
            # Start collect data while an axis is moving.
            dg645_12ID.trigger()
            print("Delay generator is triggered to start the fly scan.")
            # Update progress bar and status message.
            N_imgcollected = 0
            timeelapsed = time.time()-t0
            TIMEOUT = total_time+5
            if TIMEOUT < 5:
                TIMEOUT = 5
            timestart = time.time()
            val = 0
            #print(N_imgcollected, Nsteps)
            while N_imgcollected<Nsteps:
                for ndet, det in enumerate(self.detector):
                    if ndet>1: 
                        continue
                    if det is not None:
                        val = det.ArrayCounter_RBV
                        break
                prog = float(val)/float(Nsteps)
                pos = self.pts.get_pos(axis)
                scaninfo.append(f'{val},    {pos},  {time.time()}')
                
                if update_progress:
                    update_progress(int(prog*100))
                msg1 = f'Elapsed time = {int(timeelapsed)}s since the start.'
                if prog>0:
                    remainingtime = timeelapsed/prog - timeelapsed
                else:
                    remainingtime = 999
                msg2 = f"; Remaining time for the current 2D scan is {np.round(remainingtime,2)}s\n"
                self.messages["current status"] = "%s%s"%(msg1, msg2)
                if update_status:
                    update_status(self.messages["current status"])

                time.sleep(0.1)
                if val>N_imgcollected:
                    N_imgcollected = val
                    timestart = time.time()

                updatetime = time.time()-timestart
                if updatetime>TIMEOUT:
                    self.messages["recent error message"] = f"Detector {det._prefix} data collection timeout after {TIMEOUT} seconds."
                    print(self.messages["recent error message"])
                    self.ui.statusbar.showMessage(self.messages["recent error message"])
                    return DETECTOR_NOT_STARTED_ERROR
                timeelapsed = time.time()-t0
                if self.isStopScanIssued:
                    break
            self.write_scaninfo_to_logfile(scaninfo)

        return 1

    def is_traj_running(self):
        ret = False
        if s12softglue.isConnected:
            if s12softglue.get_eventN() == 0:
                ret = False
            else:
                ret = True
        return ret
            
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
        p0 = self.ui.findChild(QLabel, "lb_%i"%n).text()
        p0 = float(p0)
        self.ui.findChild(QLineEdit, "ed_%i"%n).setText("%0.6f"%p0)
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
                if direction==1:
                    dirv = 0
                else:
                    dirv = 6
                self.pts.hexapod.assign_axis2wavtable(axis, self.pts.hexapod.WaveGenID[axis]+dirv)                
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

        self.rpos = np.asarray(self.rpos)
        self.mpos = np.asarray(self.mpos)
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
        #print(self.rpos.shape, " This is the shape of rpos")
        col = []
        for ind in range(self.rpos.shape[1]):
            col.append(ind)
        self.save_list(filename, self.mpos, self.rpos, col=col, option=saveoption)

    def save_list(self, filename, mpos, rpos, col, option="w"):
        mpos = np.asarray(mpos)
        rpos = np.asarray(rpos)

        if len(rpos) == 0:
            return
        if len(mpos) ==0:
            mpos = np.arange(rpos.shape[1])
        if mpos.ndim ==2:
            with open(filename, option) as f:
                for i, m in enumerate(mpos):
                    strv = ""
                    for data in m:
                        strv = "%s    %0.5e"%(strv, data)
                    for cind in range(len(col)):
                        strv = "%s    %0.5e"%(strv, rpos[cind][i])
                    f.write("%s\n"%(strv))
        else:    
            with open(filename, option) as f:
                for i, m in enumerate(mpos):
                    strv = ""
                    for cind in range(len(col)):
                        strv = "%s    %0.5e"%(strv, rpos[cind][i])
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
        data = self.pts.hexapod.get_records()
        if isinstance(data, type({})):
            l_data = [data]
        else:
            l_data = data

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
                try:
                    dt2 = np.column_stack((target, encoded, ind))
                    np.savetxt(filename, dt2, fmt="%1.8e %1.8e %i")
                except:
                    self.messages["recent error message"] = "Error in fly_result."
                    print(self.messages["recent error message"])

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
        self.signalmotorunit = self.motorunits[motornumber]
        if val==0:
            val = float(self.ui.findChild(QLineEdit, "ed_%i_tweak"%n).text())

        w = mover(self.pts, axis, sign*val)
        self.threadpool.start(w)
        self.updatepos(axis)
    
    def update_qds(self):
        if not hasattr(self, 'qds_array'):
            self.qds_array = []
        try:
            r = self.get_qds_pos()
        except:
            self.messages["recent error message"] = "QDS does not work."
            print(self.messages["recent error message"])
            return
        self.qds_array.append(r)
        self.ui.lcd_X.display("%0.3f" % (r[0]))     
        self.ui.lcd_Z.display("%0.3f" % (r[1]))
        self.ui.lcd_Z_2.display("%0.3f" % (r[2]))
        # Keep only the latest 500 points
        if len(self.qds_array) > 500:
            self.qds_array = self.qds_array[-500:]
        self.plot()
        self.updatepos()

    def reset_qdsX(self):
        r = self.get_qds_pos(False)
        print(r)
        self.parameters._ref_X = r[0]
        self.parameters.writeini()

    def reset_qdsZ(self):
        r = self.get_qds_pos(False)
        self.parameters._ref_Z = r[1]
        self.parameters.writeini()

    def reset_qdsZ2(self):
        r = self.get_qds_pos(False)
        self.parameters._ref_Z2 = r[2]
        self.parameters.writeini()

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
        pos = np.arange(len(self.qds_array))*0.1  # assuming 0.1s interval
        r = np.asarray(self.qds_array)
        xl = 'Time (s)'        

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
            
            if len(self.plotlabels) == 0:
                if self.isStruckCountNeeded:
                    yl = self.detector[2].scaler.NM2
                    yl2 = self.detector[2].scaler.NM3
                    yl3 = self.detector[2].scaler.NM4
                else:
                    yl = 'X position (um)'
                    yl2 = 'Z position (um)'
                    yl3 = 'Z position (um)'                
                self.plotlabels = [yl, yl2, yl3]
            self.ax.set_ylabel(self.plotlabels[0])
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
            detectors = data['detectors']
        except:
            detectors = ''
        try:
            ymotor = int(data['ymotor'])
        except:
            ymotor = DEFAULTS['ymotor']
        try:
            phimotor = int(data['phimotor'])
        except:
            phimotor = DEFAULTS['phimotor']
        try:
            scanname = data['scanname']
        except:
            scanname = ""
        try:
            folder = data['folder']
        except:
            folder = ""
        try:
            saxsmode = bool(int(data['saxsmode']))
        except:
            saxsmode = False
        try:
            testmode = bool(int(data['testmode']))
        except:
            testmode = False

        if cmd == 'set':

            if saxsmode:
                self.set_hdf_plugin_use(True)
                self.select_detector_mode(False)
                self.set_hdf_plugin_use(True)
                #self.set_basepaths('/net/s12data/export/12id-c/')
        
            if testmode:
                print("Testmode is on.")
                self.set_monitor_beamline_status(False)
                self.set_shutter_close_after_scan(False)
            else:
                print("Testmode is off.")
                self.set_monitor_beamline_status(True)
                self.set_shutter_close_after_scan(True)
            # if scanname is provided, set it.
            if len(scanname)>0:
                try:
                    print(f"Setting scanname to {scanname}")
                    self.ui.ed_scanname.setText(scanname)
                    self.update_scanname()
                except:
                    pass
            if len(detectors)>0:
                for N in range(1, 7):
                    if str(N) in detectors:
                        try:
                            self.select_detectors(N, value=True)
                        except:
                            pass

        elif cmd == 'setrange':
            motornumber = self.motornames.index(data['axis'])
            n = motornumber+1
            for key, val in data.items():
                if key=='axis':
                    pass
                else:
                    self.ui.findChild(QLineEdit, "ed_lup_%i_%s"%(n, key)).setText(val)
        
        elif cmd == 'mv':
            for axis, pos in data.items():
                motornumber = self.motornames.index(axis)
                n = motornumber+1
                self.ui.findChild(QLineEdit, "ed_%i"%n).setText("%0.6f"%float(pos))
                self.mv(motornumber=motornumber, val=float(pos))
        elif cmd == 'mvr':
            for axis, pos in data.items():
                motornumber = self.motornames.index(axis)
                self.mvr(motornumber=motornumber, val=float(pos))
        
        elif cmd == 'fly2d':
            self.fly2d(xmotor=xmotor,ymotor=ymotor,scanname=scanname)

        elif cmd == 'fly2d_snake':
            self.fly2d(xmotor=xmotor,ymotor=ymotor,snake=True, scanname=scanname)
            
        elif cmd == 'fly3d':
            self.fly3d(xmotor=xmotor,ymotor=ymotor,phimotor=phimotor,scanname=scanname)
            
        elif cmd == 'fly3d_snake':
            self.fly3d(xmotor=xmotor,ymotor=ymotor,phimotor=phimotor,snake=True,scanname=scanname)

        elif cmd == 'stepscan3d':
            self.stepscan3d(xmotor=xmotor,ymotor=ymotor,phimotor=phimotor)
        
        elif cmd == 'stepscan2d':
            self.stepscan2d(xmotor=xmotor,ymotor=ymotor)

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
        elif cmd == "shclose":
            self.shutter.close()
        elif cmd == "setfolder":
            self.parameters.working_folder = folder
            self.update_workingfolder(self.parameters.working_folder)
        elif cmd == 'get_error_message':
            return self.messages["recent error message"]
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

app = QApplication(sys.argv)
main_panel = ptyco_main_control()

#import pygetwindow as gw

# def capture_screenshot():
#     """Capture screenshot of main_panel every 10 seconds"""
#     screenshot = app.primaryScreen().grabWindow(0)
#     #timestamp = time.strftime("%Y%m%d_%H%M%S")
#     filename = f"pty-co-SAXS.png"
#     screenshot.save(filename)
#     scp(filename)
#     #pass
#     #print(f"Screenshot saved: {filename}")

def main():
    # Run gui with server option
    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)
    
    # # Create a timer for periodic screenshots
    # screenshot_timer = QTimer()
    # screenshot_timer.timeout.connect(capture_screenshot)
    # screenshot_timer.start(30000)  # 30 seconds in milliseconds
    
    with loop:
        _, protocol = loop.run_until_complete(create_server(loop))
        protocol.rangeChanged.connect(main_panel.set_data)
        protocol.runRequested.connect(main_panel.run_cmd)
        protocol.mvRequested.connect(main_panel.set_mv)
        protocol.jsonReceived.connect(main_panel.run_json)
        loop.run_forever()

def main_no_server():
    # Non-server option
    app = QApplication(sys.argv)
    a = ptyco_main_control()
    sys.exit(app.exec_())

if __name__ == "__main__":
    # Server option included
    main()
