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
from ptychosaxs import pts

from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.backends.backend_qt5agg import NavigationToolbar2QT as NavigationToolbar
import matplotlib.pyplot as plt
import numpy as np

# Step 1: Create a worker class
class Worker(QRunnable):
    finished = pyqtSignal()
    progress = pyqtSignal(int)
    def __init__(self, fn, *args, **kwargs):
        super(Worker, self).__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
    
    @pyqtSlot()
    def run(self):
        """Long-running task."""
        self.fn(*self.args, **self.kwargs)

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

        self.ui.pb_SAXSscan_1.setEnabled(False)
        self.ui.pb_SAXSscan_2.setEnabled(False)
        self.ui.pb_SAXSscan_3.setEnabled(False)
        self.ui.pb_SAXSscan_4.setEnabled(False)
        self.ui.pb_SAXSscan_5.setEnabled(False)
        self.ui.pb_SAXSscan_6.setEnabled(False)
        self.ui.pb_SAXSscan_7.setEnabled(True)

        self.ui.pb_lup_1.clicked.connect(lambda: self.fly(0, 0))
        self.ui.pb_lup_2.clicked.connect(lambda: self.fly(1, 0))
        self.ui.pb_lup_3.clicked.connect(lambda: self.fly(2, 0))
        self.ui.pb_lup_4.clicked.connect(lambda: self.fly(3, 0))
        self.ui.pb_lup_5.clicked.connect(lambda: self.fly(4, 0))
        self.ui.pb_lup_6.clicked.connect(lambda: self.fly(5, 0))
        self.ui.pb_lup_7.clicked.connect(lambda: self.fly(6, 0))
        self.ui.pb_SAXSscan_7.clicked.connect(lambda: self.fly(6, 1))
        self.ui.actionRun.triggered.connect(self.timescan)
        self.ui.actionStop.triggered.connect(self.timescanstop)
        self.ui.actionClear.triggered.connect(self.clearplot)
        self.ui.actionSet_default_speed.triggered.connect(self.setphivel_default)
        self.ui.actionSave.triggered.connect(self.save_qds)
        self.pts.signals.AxisPosSignal.connect(self.update_motorpos)
        self.pts.signals.AxisNameSignal.connect(self.update_motorname)

        self.rpos = []
        self.mpos = []
        self.threadpool = QThreadPool()
        # qds
        self.ref_X = 0
        self.ref_Z = 0
        self.isscan = False
        self.isfly = False
        self.ui.pb_resetx.clicked.connect(self.reset_qdsX)
        self.ui.pb_resetz.clicked.connect(self.reset_qdsZ)

        self.ui.pb_recordx1.clicked.connect(lambda: self.record_qdsX(1))
        self.ui.pb_recordx2.clicked.connect(lambda: self.record_qdsX(2))
        self.ui.pb_recordx3.clicked.connect(lambda: self.record_qdsX(3))

        self.ui.pb_recordz1.clicked.connect(lambda: self.record_qdsZ(1))
        self.ui.pb_recordz2.clicked.connect(lambda: self.record_qdsZ(2))
        self.ui.pb_recordz3.clicked.connect(lambda: self.record_qdsZ(3))
        
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
        self.pts.vel = 36
        self.pts.acc = self.pts.vel*10

    def fly(self, motornumber, type):
        w = Worker(self.fly0, motornumber, type)
        self.threadpool.start(w)
    
    def timescanstop(self):
        self.isscan = False

    def timescan(self):
        self.t0 = time.time()
        self.signalmotor = "Time"
        self.signalmotorunit = "s"

        self.mpos = []
        self.rpos = []

        self.isscan = True
        w = Worker(self.timescan0)
        self.threadpool.start(w)

    def timescan0(self):
        while self.isscan:
            r = self.get_qds_pos()
            self.rpos.append([r[0], r[1], r[2]])
            t = time.time()-self.t0
            self.mpos.append(t)
            time.sleep(0.1)

    def get_qds_pos(self):
        r, a = self.pts.qds.get_position()
        r = r[0]
        r = [r[0]/1000-self.ref_X, r[1]/1000-self.ref_Z, r[2]]
        return r
    
    def fly0(self, motornumber, type):
        axis = self.motornames[motornumber]
        self.signalmotor = axis
        self.signalmotorunit = self.motorunits[motornumber]
        self.rpos = []
        self.mpos = []
        self.isscan = True
        
        if type == 0:
            self.isfly = False
            if motornumber ==6:
                st = float(self.ui.ed_lup_7_L.text())
                fe = float(self.ui.ed_lup_7_R.text())
                tm = float(self.ui.ed_lup_7_t.text())
                step = float(self.ui.ed_lup_7_N.text())
            
            self.pts.mv(axis, st)
            pos = st
            while (abs(pos - fe)/(fe-st)*100 > 0.1):
                self.pts.mv(axis, pos+step)
                r = self.get_qds_pos()
                self.rpos.append([r[0], r[1], r[2]])
                pos = self.get_motorpos(self.signalmotor)
                self.mpos.append(pos)

        if type == 1:
            self.isfly = True
            if motornumber ==6:
                st = float(self.ui.ed_lup_7_L.text())
                fe = float(self.ui.ed_lup_7_R.text())
                tm = float(self.ui.ed_lup_7_t.text())
                self.pts.vel = 36
                self.pts.acc = self.pts.vel*10
                print(f"Speed of phi is set to {self.pts.vel}.")
                self.pts.mv('phi', st)
                self.pts.vel = abs(fe-st)/tm
                self.pts.acc = self.pts.vel*10
                print(f"Speed of phi is set to {self.pts.vel}.")
                self.pts.mv('phi', fe)
        self.isscan = False

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
        self.pts.savedata(filename, self.mpos, self.rpos, col=[0,1])

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
        self.pts.mv(axis,val)
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
        self.pts.mvr(axis, sign*val)
        self.updatepos()
    
    def update_qds(self):
        r = self.get_qds_pos()
#        print(r)
        self.ui.lcd_X.display("%0.3f" % (r[0]))     
        self.ui.lcd_Z.display("%0.3f" % (r[1]))
        #self.rpos = []
        #self.mpos = []

        if self.isscan:
            if self.isfly:
                self.rpos.append([r[0], r[1], r[2]])
                self.mpos.append(self.get_motorpos(self.signalmotor))
            self.plot()

    def reset_qdsX(self):
        r = self.get_qds_pos()
        self.ref_X = r[0]
        #self.ref_X = self.ui.lcd_X.value()  

    def reset_qdsZ(self):
        r = self.get_qds_pos()
        self.ref_Z = r[1]
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

    def plot(self):
        
        #import random
        ''' plot some random stuff '''
        # random data
        #data = [random.random() for i in range(10)]

        # instead of ax.hold(False)
        self.figure.clear()

        # create an axis
        ax = self.figure.add_subplot(121)
        ax2 = self.figure.add_subplot(122)

        # discards the old graph
        # ax.hold(False) # deprecated, see above


        r = np.asarray(self.rpos)
        pos = np.asarray(self.mpos)
        xl = f"{self.signalmotor} ({self.signalmotorunit})"
        

        try:
            #ax.plot(xl, yl, xdata=pos, ydata=r[:,0]/1000, color='r')
            #ax2.plot(xl, yl, xdata=pos, ydata=r[:,1]/1000, color='b')
            ax.plot(pos, r[:,0], 'r')
            ax.set_xlabel(xl)
            yl = 'X position (um)'
            ax.set_ylabel(yl)
            ax2.plot(pos, r[:,1], 'b')
            ax2.set_xlabel(xl)
            yl = 'Z position (um)'
            ax2.set_ylabel(yl)
        except:
            pass
        #ax.ylabel()
        #ax.xlabel()
        #plt.draw()
        #plt.pause(0.1)




        # plot data
        #ax.plot(data, '*-')

        # refresh canvas
        self.canvas.draw()



if __name__ == "__main__":
#    import logging
#    logging.basicConfig(level=logging.INFO)
    from PyQt5.QtWidgets import QMainWindow
    app = QApplication(sys.argv)
    a = tweakmotors()
    sys.exit(app.exec_())
