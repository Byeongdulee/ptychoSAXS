# -*- coding: utf-8 -*-
"""
Created on Thu Oct 27 16:42:18 2016

@author: Byeongdu Lee
@Date: Nov. 1. 2016
"""

import sys 
import os

from PyQt5 import uic, QtCore
from PyQt5.QtWidgets import QMainWindow, QApplication, QPushButton, QFileDialog, QWidget
from PyQt5.QtCore import QTimer, QObject, QThread, pyqtSlot, pyqtSignal, QRunnable, QThreadPool
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
    def __init__(self):
        super(tweakmotors, self).__init__()
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose)
        guiName = "motorGUI.ui"
        self.pts = pts
        self.ui = uic.loadUi(guiName)
        self.motornames = ['X', 'Y', 'Z', 'U', 'V', 'W', 'phi']
        self.motorunits = ['mm', 'mm', 'mm', 'deg', 'deg', 'deg', 'deg']
        self.ui.pb_tweak1L.clicked.connect(lambda: self.mvr(0, -1))
        self.ui.pb_tweak1R.clicked.connect(lambda: self.mvr(0, 1))
        self.ui.pb_tweak2L.clicked.connect(lambda: self.mvr(1, -1))
        self.ui.pb_tweak2R.clicked.connect(lambda: self.mvr(1, 1))
        self.ui.pb_tweak3L.clicked.connect(lambda: self.mvr(2, -1))
        self.ui.pb_tweak3R.clicked.connect(lambda: self.mvr(2, 1))
        self.ui.pb_tweak4L.clicked.connect(lambda: self.mvr(3, -1))
        self.ui.pb_tweak4R.clicked.connect(lambda: self.mvr(3, 1))
        self.ui.pb_tweak5L.clicked.connect(lambda: self.mvr(4, -1))
        self.ui.pb_tweak5R.clicked.connect(lambda: self.mvr(4, 1))
        self.ui.pb_tweak6L.clicked.connect(lambda: self.mvr(5, -1))
        self.ui.pb_tweak6R.clicked.connect(lambda: self.mvr(5, 1))
        self.ui.pb_tweak7L.clicked.connect(lambda: self.mvr(6, -1))
        self.ui.pb_tweak7R.clicked.connect(lambda: self.mvr(6, 1))
        self.ui.ed_1.returnPressed.connect(lambda: self.mv(0))
        self.ui.ed_2.returnPressed.connect(lambda: self.mv(1))
        self.ui.ed_3.returnPressed.connect(lambda: self.mv(2))
        self.ui.ed_4.returnPressed.connect(lambda: self.mv(3))
        self.ui.ed_5.returnPressed.connect(lambda: self.mv(4))
        self.ui.ed_6.returnPressed.connect(lambda: self.mv(5))
        self.ui.ed_7.returnPressed.connect(lambda: self.mv(6))

        self.ui.pb_SAXSscan_1.setEnabled(True)
        self.ui.pb_SAXSscan_2.setEnabled(False)
        self.ui.pb_SAXSscan_3.setEnabled(False)
        self.ui.pb_SAXSscan_4.setEnabled(False)
        self.ui.pb_SAXSscan_5.setEnabled(False)
        self.ui.pb_SAXSscan_6.setEnabled(False)
        self.ui.pb_SAXSscan_7.setEnabled(True)

        self.ui.pb_lup_1.clicked.connect(lambda: self.stepscan(0))
        self.ui.pb_lup_2.clicked.connect(lambda: self.stepscan(1))
        self.ui.pb_lup_3.clicked.connect(lambda: self.stepscan(2))
        self.ui.pb_lup_4.clicked.connect(lambda: self.stepscan(3))
        self.ui.pb_lup_5.clicked.connect(lambda: self.stepscan(4))
        self.ui.pb_lup_6.clicked.connect(lambda: self.stepscan(5))
        self.ui.pb_lup_7.clicked.connect(lambda: self.stepscan(6))
        self.ui.pb_SAXSscan_1.clicked.connect(lambda: self.fly(0))
        self.ui.pb_SAXSscan_7.clicked.connect(lambda: self.fly(6))
        self.ui.actionRun.triggered.connect(self.timescan)
        self.ui.actionStop.triggered.connect(self.timescanstop)
        self.ui.actionClear.triggered.connect(self.clearplot)
        self.ui.actionSet_default_speed.triggered.connect(self.setphivel_default)
        self.ui.actionSave.triggered.connect(self.save_qds)
        self.ui.actionSave_flyscan_result.triggered.connect(self.fly_result)
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

        # this is the Canvas Widget that displays the `figure`
        # it takes the `figure` instance as a parameter to __init__
        self.canvas = FigureCanvas(self.figure)

        # this is the Navigation widget
        # it takes the Canvas widget and a parent
        self.toolbar = NavigationToolbar(self.canvas, self)

        # Just some button connected to `plot` method
        #self.button = QPushButton('Plot')
        #self.button.clicked.connect(self.plot)

        # set the layout
        
        self.ui.vlayout_plot.addWidget(self.toolbar)
        self.ui.vlayout_plot.addWidget(self.canvas)
        #self.ui.vlayout_plot.addWidget(self.button)
        #self.setLayout(layout)
        # instead of ax.hold(False)
        self.figure.clear()

        # create an axis
        self.ax = self.figure.add_subplot(131)
        self.ax2 = self.figure.add_subplot(132)
        self.ax3 = self.figure.add_subplot(133)

        self.updatepos()

        self.timer = QTimer()
        self.timer.timeout.connect(self.update_qds)
        self.timer.start(100)        
        self.ui.show()

    def get_motorpos(self, axis):
        if axis == 'X':
            return self.pts.posx
        if axis == 'Y':
            return self.pts.posy
        if axis == 'Z':
            return self.pts.posz
        if axis == 'U':
            return self.pts.posu
        if axis == 'V':
            return self.pts.posv
        if axis == 'W':
            return self.pts.posv
        if axis == 'phi':
            return self.pts.posphi
        
    def updatepos(self):
        
        self.ui.lb_1.setText("%0.4f"%self.pts.posx)
        self.ui.lb_2.setText("%0.4f"%self.pts.posy)
        self.ui.lb_3.setText("%0.4f"%self.pts.posz)
        self.ui.lb_4.setText("%0.4f"%self.pts.posu)
        self.ui.lb_5.setText("%0.4f"%self.pts.posv)
        self.ui.lb_6.setText("%0.4f"%self.pts.posw)
        self.ui.lb_7.setText("%0.4f"%self.pts.posphi)

    def update_motorpos(self, value):
        #print(value, " this in rungui.py")
        if self.signalmotor == 'X':
            self.ui.lb_1.setText("%0.4f"%value)
        if self.signalmotor == 'Y':
            self.ui.lb_2.setText("%0.4f"%value)
        if self.signalmotor == 'Z':
            self.ui.lb_3.setText("%0.4f"%value)
        if self.signalmotor == 'U':
            self.ui.lb_4.setText("%0.4f"%value)
        if self.signalmotor == 'V':
            self.ui.lb_5.setText("%0.4f"%value)
        if self.signalmotor == 'W':
            self.ui.lb_6.setText("%0.4f"%value)
        if self.signalmotor == 'phi':
            self.ui.lb_7.setText("%0.4f"%value)

    def update_motorname(self, axis):
        self.signalmotor = axis
    
    def setphivel_default(self):
        print(self.pts.phi.vel, " This was vel value")
        self.pts.phi.vel = 36
        time.sleep(0.1)
        self.pts.phi.acc = self.pts.phi.vel*10

    def scandone(self, value):
        self.isscan = False
        self.updatepos()
        #disarm_panda()
        print("scan done.......")
    
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
    
    def fly(self, motornumber):
        if not self.ui.cb_keepprevscan.isChecked():
            self.clearplot()
        self.isscan = True
        #self.thread = self.createflyscanthread(motornumber, type)
        #self.thread.start()
        #self.timer.set_interval(100)
        w = Worker(self.fly0, motornumber)
        w.signal.finished.connect(self.scandone)
        self.threadpool.start(w)

    def stepscan(self, motornumber):
        if not self.ui.cb_keepprevscan.isChecked():
            self.clearplot()
        self.isscan = True
        #self.thread = self.createflyscanthread(motornumber, type)
        #self.thread.start()
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
        
        self.isfly = False
        if motornumber ==0:
            st = float(self.ui.ed_lup_1_L.text())
            fe = float(self.ui.ed_lup_1_R.text())
            step = float(self.ui.ed_lup_1_N.text())
        
        if motornumber ==1:
            st = float(self.ui.ed_lup_2_L.text())
            fe = float(self.ui.ed_lup_2_R.text())
            step = float(self.ui.ed_lup_2_N.text())
        
        if motornumber ==2:
            st = float(self.ui.ed_lup_3_L.text())
            fe = float(self.ui.ed_lup_3_R.text())
            step = float(self.ui.ed_lup_3_N.text())
        
        if motornumber ==3:
            st = float(self.ui.ed_lup_4_L.text())
            fe = float(self.ui.ed_lup_4_R.text())
            step = float(self.ui.ed_lup_4_N.text())
        
        if motornumber ==4:
            st = float(self.ui.ed_lup_5_L.text())
            fe = float(self.ui.ed_lup_5_R.text())
            step = float(self.ui.ed_lup_5_N.text())
        
        if motornumber ==5:
            st = float(self.ui.ed_lup_6_L.text())
            fe = float(self.ui.ed_lup_6_R.text())
            step = float(self.ui.ed_lup_6_N.text())
        
        if motornumber ==6:
            st = float(self.ui.ed_lup_7_L.text())
            fe = float(self.ui.ed_lup_7_R.text())
            step = float(self.ui.ed_lup_7_N.text())
        if self.ui.cb_reversescandir.isChecked():
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

    def fly0(self, motornumber):
        axis = self.motornames[motornumber]
        self.signalmotor = axis
        self.signalmotorunit = self.motorunits[motornumber]
        self.rpos = []
        self.mpos = []
        
        self.isfly = True
        if motornumber ==0:
            st = float(self.ui.ed_lup_1_L.text())
            fe = float(self.ui.ed_lup_1_R.text())
            tm = float(self.ui.ed_lup_1_t.text())
            step = float(self.ui.ed_lup_1_N.text())
            if self.ui.cb_reversescandir.isChecked():
                t = fe
                fe = st
                st = t 
                step = -step

            self.pts.hexapod.set_traj(tm, fe-st, st, 50, step)
            #self.pts, axis, self.pts.hexapod.wave_start)

            #t0 = time.time()
            #t = 0
            #k = 0
            self.pts.hexapod.run_traj()
            while self.pts.ismoving(axis):
                time.sleep(0.01)
            # sec = self.pts.hexapod.scantime + 1
            # while (t - t0) < sec:
            #     r = self.get_qds_pos()
            #     self.rpos.append([r[0], r[1], r[2]])
            #     time.sleep(0.0001)
            #     t = time.time()
            #     self.mpos.append(t)

        if motornumber ==6:
            st = float(self.ui.ed_lup_7_L.text())
            fe = float(self.ui.ed_lup_7_R.text())
            tm = float(self.ui.ed_lup_7_t.text())
            if self.ui.cb_reversescandir.isChecked():
                t = fe
                fe = st
                st = t 
            self.pts.phi.vel = 36/2
            #time.sleep(0.1)
            self.pts.phi.acc = self.pts.phi.vel*10
            #time.sleep(0.1)
            print(f"Speed of phi is set to {self.pts.phi.vel}.")
            self.pts.mv('phi', st, wait=True)
            time.sleep(0.5)
            self.pts.phi.vel = abs(fe-st)/tm
            #time.sleep(0.1)
            self.pts.phi.acc = self.pts.phi.vel*10
            #time.sleep(0.1)
            print(f"Speed of phi is set to {self.pts.phi.vel}.")
            self.pts.mv('phi', fe, wait=True)
            print("Should be in run.")
        #self.isscan = False

    def save_qds(self):
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
        self.pts.savedata(filename, self.mpos, self.rpos, col=[0,1,2])

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
        qds_data = get_pandadata()
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
        self.canvas.draw()

    def mv(self, motornumber):
        axis = self.motornames[motornumber]
        self.signalmotor = axis
        self.signalmotorunit = self.motorunits[motornumber]
        if motornumber ==0:
            val = float(self.ui.ed_1.text())
        if motornumber ==1:
            val = float(self.ui.ed_2.text())
        if motornumber ==2:
            val = float(self.ui.ed_3.text())
        if motornumber ==3:
            val = float(self.ui.ed_4.text())
        if motornumber ==4:
            val = float(self.ui.ed_5.text())
        if motornumber ==5:
            val = float(self.ui.ed_6.text())
        if motornumber ==6:
            val = float(self.ui.ed_7.text())
        
        w = move(self.pts, axis, val)
        #w.signal.finished.connect(self.scandone)
        self.threadpool.start(w)
        self.updatepos()

    def mvr(self, motornumber, sign):
        axis = self.motornames[motornumber]
        self.signalmotor = axis
        self.signalmotorunit = self.motorunits[motornumber]
        if motornumber ==0:
            val = float(self.ui.ed_1_tweak.text())
        if motornumber ==1:
            val = float(self.ui.ed_2_tweak.text())
        if motornumber ==2:
            val = float(self.ui.ed_3_tweak.text())
        if motornumber ==3:
            val = float(self.ui.ed_4_tweak.text())
        if motornumber ==4:
            val = float(self.ui.ed_5_tweak.text())
        if motornumber ==5:
            val = float(self.ui.ed_6_tweak.text())
        if motornumber ==6:
            val = float(self.ui.ed_7_tweak.text())
        #print(sign*val)
        #self.pts.mvr(axis, sign*val)
        w = mover(self.pts, axis, sign*val)
        self.threadpool.start(w)
        self.updatepos()
    
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
        self.canvas.draw()



if __name__ == "__main__":
#    import logging
#    logging.basicConfig(level=logging.INFO)
    from PyQt5.QtWidgets import QMainWindow
    app = QApplication(sys.argv)
    a = tweakmotors()
    sys.exit(app.exec_())
