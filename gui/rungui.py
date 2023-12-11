# -*- coding: utf-8 -*-
"""
Created on Thu Oct 27 16:42:18 2016

@author: Byeongdu Lee
@Date: Nov. 1. 2016
"""

import sys 
import os

from PyQt5 import uic, QtCore
from PyQt5.QtWidgets import QMainWindow, QApplication, QPushButton
from PyQt5.QtCore import QTimer
import time
from ptychosaxs import pts

from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.backends.backend_qt5agg import NavigationToolbar2QT as NavigationToolbar
import matplotlib.pyplot as plt
import numpy as np

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
        self.ui.actionClear.triggered.connect(self.clearplot)
        self.ui.actionSet_default_speed.triggered.connect(self.setphivel_default)
        self.pts.signals.AxisPosSignal.connect(self.update_motorpos)
        self.pts.signals.AxisNameSignal.connect(self.update_motorname)

        # qds
        self.ref_X = 0
        self.ref_Z = 0
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
        self.button = QPushButton('Plot')
        self.button.clicked.connect(self.plot)

        # set the layout
        
        self.ui.vlayout_plot.addWidget(self.toolbar)
        self.ui.vlayout_plot.addWidget(self.canvas)
        self.ui.vlayout_plot.addWidget(self.button)
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
        axis = self.motornames[motornumber]
        self.signalmotor = axis
        self.signalmotorunit = self.motorunits[motornumber]
        self.rpos = []
        self.mpos = []
        self.isscan = True
        if type == 0:
            if motornumber ==6:
                st = float(self.ui.ed_lup_7_L.text())
                fe = float(self.ui.ed_lup_7_R.text())
                tm = float(self.ui.ed_lup_7_t.text())
                step = float(self.ui.ed_lup_7_N.text())
            
            self.pts.mv(axis, st)
            pos = st
            while (abs(pos - fe)/(fe-st)*100 > 0.1):
                time.sleep(0.1)
                self.pts.mv(axis, pos+step)

        if type == 1:
            if motornumber ==6:
                st = float(self.ui.ed_lup_7_L.text())
                fe = float(self.ui.ed_lup_7_R.text())
                tm = float(self.ui.ed_lup_7_t.text())
                self.pts.mv('phi', st)
                self.pts.vel = (fe-st)/tm
                self.pts.acc = self.pts.vel*10
                self.pts.mv('phi', fe)

    def clearplot(self):
        self.isscan = False

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
        r, a = self.pts.qds.get_position()
        r = r[0]
#        print(r)
        self.ui.lcd_X.display("%0.3f" % (r[0]/1000-self.ref_X))     
        self.ui.lcd_Z.display("%0.3f" % (r[1]/1000-self.ref_Z))
        
        if self.isscan:
            self.signalmotor
            self.rpos.append([r[0], r[1], r[2]])
            self.mpos.append(self.get_motorpos(self.signalmotor))

            self.plot()



    def reset_qdsX(self):
        self.ref_X = self.ui.lcd_X.value()  

    def reset_qdsZ(self):
        self.ref_Z = self.ui.lcd_Z.value()

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
        ax = self.figure.add_subplot(211)
        ax2 = self.figure.add_subplot(212)

        # discards the old graph
        # ax.hold(False) # deprecated, see above


        r = np.asarray(self.rpos)
        pos = np.asarray(self.mpos)
        ax.plot(pos, r[:,0]/1000, 'r')
        ax2.plot(pos, r[:,1]/1000, 'b')
        ax.ylabel('Positions (um)')
        ax.xlabel(f"{self.signalmotor} ({self.signalmotorunit})")
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
