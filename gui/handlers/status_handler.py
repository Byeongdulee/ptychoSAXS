"""
gui/handlers/status_handler.py
Beam status, interferometer display, QDS, timescan, and plot handling.
Extracted from ptyco_main_control in rungui.py.
"""

import time
import os
import numpy as np
import traceback
import pathlib
from PyQt5.QtWidgets import QLabel, QLineEdit, QFileDialog, QWidget, QInputDialog
from PyQt5.QtCore import Qt
import pyqtgraph as pg
import analysis.planeeqn as eqn

# QDS unit constants (mirrored from rungui.py — never import rungui directly
# since doing so re-executes the module and creates a second GUI instance)
QDS_UNIT_NM = 0
QDS_UNIT_UM = 1
QDS_UNIT_MM = 2


class StatusHandler:
    def __init__(self, window) -> None:
        self.w = window
        self.ui = window.ui
        self._connect_signals()

    def _connect_signals(self) -> None:
        pass

    # ------------------------------------------------------------------
    # Beam / shutter status
    # ------------------------------------------------------------------

    def set_monitor_beamline_status(self, value=None):
        if value is None:
            value = self.ui.actionMonitor_Beamline_Status.isChecked()
        if value:
            self.ui.actionMonitor_Beamline_Status.setChecked(True)
            self.w.monitor_beamline_status = True
        else:
            self.ui.actionMonitor_Beamline_Status.setChecked(False)
            self.w.monitor_beamline_status = False

    def set_shutter_close_after_scan(self, value=None):
        if value is None:
            value = self.ui.actionShutter_Close_Afterscan.isChecked()
        if value:
            self.ui.actionShutter_Close_Afterscan.setChecked(True)
            self.w.shutter_close_after_scan = True
        else:
            self.ui.actionShutter_Close_Afterscan.setChecked(False)
            self.w.shutter_close_after_scan = False

    def checkshutter(self, value, **kws):
        if not self.ui.actionMonitor_Beamline_Status.isChecked():
            self.w.isOK2run = True
            return
        # shutter_events = {"time":time.time(), "state": value}
        # print(f"Value of the shutter is {value}")
        if value == 0:
            self.w.isOK2run = False
        else:
            self.w.isOK2run = True

    # def run_hold(self, sevnt):
    #     print("run hold executed. This will hold the scan.")
    #     self.shutter_events = sevnt
    #     while self.isOK2run==False:
    #         time.sleep(10)

    # def run_resume(self):
    #     self.isOK2run = True

    # ------------------------------------------------------------------
    # Interferometer / QDS parameters
    # ------------------------------------------------------------------

    def set_interferometer_params(self):
        # dialog = InputDialog(labels=["R0 for the top sensor(mm)","th0 for the top sensor(mm)"])
        value, okPressed = QInputDialog.getDouble(
            self.w.ui,
            "The top sensor positions",
            "R0 (mm):",
            self.w.parameters._qds_R_vert,
        )
        if okPressed:
            self.w.parameters._qds_R_vert = value
        value, okPressed = QInputDialog.getDouble(
            self.w.ui,
            "The top sensor positions",
            "th (deg):",
            self.w.parameters._qds_th0_vert,
            -360.0,
            360.0,
            2,
        )
        if okPressed:
            self.w.parameters._qds_th0_vert = value
        value, okPressed = QInputDialog.getDouble(
            self.w.ui,
            "The horizontal sensor positions",
            "R (mm):",
            self.w.parameters._qds_R_cyl,
        )
        if okPressed:
            self.w.parameters._qds_R_cyl = value
        self.w.parameters.writeini()

    def set_logfilename(self):
        strv = self.w.parameters.logfilename
        if len(strv) > 0:
            strv = os.path.basename(strv)
        text, okPressed = QInputDialog.getText(
            self.w.ui, "Log file", "Filename:", QLineEdit.Normal, strv
        )
        if okPressed:
            foldername = self.ui.edit_workingfolder.text()
            self.w.parameters.logfilename = os.path.join(foldername, text)
            self.w.parameters.scan_number = 0
            scaninfo = []
            scaninfo.append("#I logging started on")
            scaninfo.append(time.ctime())
            self.w.write_scaninfo_to_logfile(scaninfo)
        self.w.parameters.writeini()

    # ------------------------------------------------------------------
    # Working folder
    # ------------------------------------------------------------------

    def update_workingfolder(self, folder=""):
        if len(folder) == 0:
            self.w.parameters.working_folder = self.ui.edit_workingfolder.text()
            self.w.parameters.writeini()
        else:
            self.ui.edit_workingfolder.setText(self.w.parameters.working_folder)
        self.w.update_scanname()
        self.w.push_filepath_to_detectors()

    # ------------------------------------------------------------------
    # Status posting
    # ------------------------------------------------------------------

    def update_status(self):
        import json

        try:
            import requests as _requests
        except ImportError:
            _requests = None
        status_url = self.w.status_url
        parameters = {}
        parameters["scan number"] = self.w.parameters.scan_number
        parameters["scan name"] = self.w.parameters.scan_name
        parameters["scan scan elapsed time"] = self.w.parameters.scan_time
        self.w.messages["parameters"] = parameters
        msg = json.dumps(self.w.messages)
        status = {"status": msg}
        if _requests is not None:
            res = _requests.post(status_url, json=status)

    # ------------------------------------------------------------------
    # QDS position and display
    # ------------------------------------------------------------------

    def get_qds_pos(self, isrefavailable=True):
        if self.w.DEBUG_DEVICES:
            # Read current motor positions from UI labels (lb_1 = X, lb_3 = Z)
            lb1 = self.ui.findChild(QLabel, "lb_1")
            lb3 = self.ui.findChild(QLabel, "lb_3")
            x_val = float(lb1.text()) if lb1 else 0.0
            z_val = float(lb3.text()) if lb3 else 0.0
            r = np.array([0.0, x_val, z_val])
        else:
            try:
                import epics as _epics  # type: ignore[import-untyped]
            except ImportError as exc:
                raise RuntimeError(
                    "epics is required for QDS in non-debug mode"
                ) from exc
            pos = self.w.pts.qds.get_position()
            pos = np.array(pos)
            r = pos / 1000
            temperature = _epics.caget("usxLAX:12IDE_temperature")
            r = np.append(r, temperature)
        if isrefavailable:
            ref = [
                self.w.parameters._ref_X,
                self.w.parameters._ref_Z,
                self.w.parameters._ref_Z2,
            ]
            if len(ref) < len(r):
                ref = ref + [0] * (len(r) - len(ref))
            r = r - np.array(ref)
        return r

    def update_qds(self):
        if not hasattr(self.w, "qds_array"):
            self.w.qds_array = []
        try:
            r = self.get_qds_pos()
        except:
            self.w.messages["recent error message"] = "QDS does not work."
            print(self.w.messages["recent error message"])
            return
        self.w.qds_array.append(r)
        self.ui.lcd_X.display("%0.3f" % (r[0]))
        self.ui.lcd_Z.display("%0.3f" % (r[1]))
        self.ui.lcd_Z_2.display("%0.3f" % (r[2]))
        # Keep only the latest 500 points
        if len(self.w.qds_array) > 500:
            self.w.qds_array = self.w.qds_array[-500:]
        self.plot()
        self.w.updatepos()

    def reset_qdsX(self):
        r = self.get_qds_pos(False)
        print(r)
        self.w.parameters._ref_X = r[0]
        self.w.parameters.writeini()

    def reset_qdsZ(self):
        r = self.get_qds_pos(False)
        self.w.parameters._ref_Z = r[1]
        self.w.parameters.writeini()

    def reset_qdsZ2(self):
        r = self.get_qds_pos(False)
        self.w.parameters._ref_Z2 = r[2]
        self.w.parameters.writeini()

    def record_qdsX(self, value):
        txt = str(self.ui.lcd_X.value())
        if value == 1:
            self.ui.lbl_qds_x1.setText(txt)
        if value == 2:
            self.ui.lbl_qds_x2.setText(txt)
        if value == 3:
            self.ui.lbl_qds_x3.setText(txt)

    def record_qdsZ(self, value):
        txt = str(self.ui.lcd_Z.value())
        if value == 1:
            self.ui.lbl_qds_z1.setText(txt)
        if value == 2:
            self.ui.lbl_qds_z2.setText(txt)
        if value == 3:
            self.ui.lbl_qds_z3.setText(txt)
        if value > 3:
            txt = str(self.ui.lcd_Z_2.value())
            if value == 1:
                self.ui.lbl_qds_z1_2.setText(txt)
            if value == 2:
                self.ui.lbl_qds_z2_2.setText(txt)
            if value == 3:
                self.ui.lbl_qds_z3_2.setText(txt)

    # ------------------------------------------------------------------
    # Time scan
    # ------------------------------------------------------------------

    def update_status_scan_time(self, time=-1):
        self.w.parameters.scan_time = time
        self.w.parameters.writeini()

    def timescanstop(self):
        self.w.isscan = False

    def timescan(self):
        s12softglue = self.w.s12softglue
        if self.ui.actionckTime_reset_before_scan.isChecked():
            if s12softglue.isConnected:
                s12softglue.ckTime_reset()
        if not self.ui.chk_keep_prev_scan.isChecked():
            self.clearplot()
        # if self.isscan:
        #    print("Stop the scan first.")
        #    return
        self.w.t0 = time.time()
        self.w.signalmotor = "Time"
        self.w.signalmotorunit = "s"

        self.w.mpos = []
        self.w.rpos = []

        self.w.isscan = True
        # self.thread = self.createtimescanthread()
        # self.thread.start()
        self.w.is_selfsaved = False
        w = self.w.Worker(self.timescan0)
        w.signal.finished.connect(self.w.scandone)
        self.w.threadpool.start(w)

    def timescan0(self):
        self.w.tempfilename = "C:\\TEMP\\_qds_temporary.txt"
        N_selfsave_points = 1024
        k = 0
        while self.w.isscan:
            r = self.get_qds_pos()
            self.w.rpos.append(r)
            t = time.time() - self.w.t0
            self.w.mpos.append(t)
            wait_for_qds_interval_s = (
                self.w.parameters._qds_time_interval
            )  # configured QDS acquisition time interval between samples
            time.sleep(wait_for_qds_interval_s)
            if len(self.w.mpos) > N_selfsave_points:
                self.w.save_qds(self.w.tempfilename, "a")
                k = k + 1
                self.w.mpos = []
                self.w.rpos = []
                print(
                    f"Previous {N_selfsave_points} data points are saved in {self.w.tempfilename}"
                )
                self.w.is_selfsaved = True

    # ------------------------------------------------------------------
    # Graph / plot
    # ------------------------------------------------------------------

    def update_graph(self):
        print("update_graph called")
        pass

    def clearplot(self):
        self.w.ax.clear()
        self.w.ax2.clear()
        self.w.ax3.clear()

    def plot(self):
        pos = np.arange(len(self.w.qds_array)) * 0.1  # assuming 0.1s interval
        r = np.asarray(self.w.qds_array)
        xl = "Time (s)"

        if not hasattr(self.w, "plotlabels"):
            self.w.plotlabels = ["", "", ""]
        try:
            self.w.ax.clear()
            self.w.ax2.clear()
            self.w.ax3.clear()
            if r.ndim == 1:
                self.w.ax.plot(pos, r, pen=pg.mkPen("r"))
            else:
                self.w.ax.plot(pos, r[:, 0], pen=pg.mkPen("r"))
            self.w.ax.setLabel("bottom", xl)

            if len(self.w.plotlabels) == 0:
                if self.w.isStruckCountNeeded:
                    yl = self.w.detector[2].scaler.NM2
                    yl2 = self.w.detector[2].scaler.NM3
                    yl3 = self.w.detector[2].scaler.NM4
                else:
                    yl = "X position (um)"
                    yl2 = "Z position (um)"
                    yl3 = "Z position (um)"
                self.w.plotlabels = [yl, yl2, yl3]
            self.w.ax.setLabel("left", self.w.plotlabels[0])
            if r.ndim == 2:
                self.w.ax2.plot(pos, r[:, 1], pen=pg.mkPen("r"))
                self.w.ax2.setLabel("bottom", xl)
                self.w.ax3.plot(pos, r[:, 2], pen=pg.mkPen("r"))
                self.w.ax3.setLabel("bottom", xl)
                self.w.ax2.setLabel("left", self.w.plotlabels[1])
                self.w.ax3.setLabel("left", self.w.plotlabels[2])
        except Exception as e:
            print(e)

    # ------------------------------------------------------------------
    # File dialog
    # ------------------------------------------------------------------

    def getfilename(self):
        w = QWidget()
        w.resize(320, 240)
        # Set window title
        w.setWindowTitle("Save QDS Data As")
        fn = QFileDialog.getSaveFileName(
            w,
            "Save File",
            "",
            "Text (*.txt *.dat)",
            None,
            QFileDialog.DontUseNativeDialog,
        )
        filename = fn[0]
        if filename == "":
            return 0
        if ".txt" not in filename:
            filename = filename + ".txt"
        d = os.path.dirname(filename)
        if len(d) == 0:
            filename = os.path.join(self.w.parameters.working_folder, filename)
        else:
            self.w.parameters.working_folder = d
        return filename

    # ------------------------------------------------------------------
    # QDS config selectors
    # ------------------------------------------------------------------

    def select_qds_units(self):
        text, ok = QInputDialog().getItem(
            self.w,
            "Select QDS units",
            "Units:",
            ("nm", "um", "mm"),
            current=1,
            editable=False,
        )
        if text == "nm":
            self.w.parameters._qds_unit = QDS_UNIT_NM
        if text == "um":
            self.w.parameters._qds_unit = QDS_UNIT_UM
        if text == "mm":
            self.w.parameters._qds_unit = QDS_UNIT_MM
        self.w.parameters.writeini()

    def select_timeintervals(self):
        val, ok = QInputDialog().getDouble(
            self.w,
            "QDS acqusition time intervals",
            "time intervals(s)",
            self.w.parameters._qds_time_interval,
        )
        self.w.parameters._qds_time_interval = val
        self.w.parameters.writeini()

    def select_qds_x(self):
        text, ok = QInputDialog().getItem(
            self.w,
            "Select QDS units",
            "Units:",
            ("0", "1", "2"),
            current=self.w.parameters._qds_x_sensor,
            editable=False,
        )
        self.w.parameters._qds_x_sensor = int(text)
        self.w.parameters.writeini()

    def select_qds_y(self):
        text, ok = QInputDialog().getItem(
            self.w,
            "Select QDS units",
            "Units:",
            ("0", "1", "2"),
            current=self.w.parameters._qds_y_sensor,
            editable=False,
        )
        self.w.parameters._qds_y_sensor = int(text)
        self.w.parameters.writeini()

    # ------------------------------------------------------------------
    # Data analysis (offline fitting)
    # ------------------------------------------------------------------

    def scantest(self):
        if self.ui.actionTestFly.isChecked():
            self.ui.actionTestFly.setChecked(True)
        else:
            self.ui.actionTestFly.setChecked(False)
            # self.detector[1] = None

    def fit_wobble_eccentricity(self):
        tp = np.asarray(self.w.mpos)
        rp = np.asarray(self.w.rpos)
        self.fitdata(xd=tp, yd=rp[:, self.w.parameters._qds_x_sensor], dtype="eccent")
        self.fitdata(xd=tp, yd=rp[:, self.w.parameters._qds_y_sensor], dtype="wob")

    def loadscan(self):
        w = QWidget()
        w.resize(320, 240)
        # Set window title
        w.setWindowTitle("Load phi vs QDS[0,1,2] Data")
        fn = QFileDialog.getOpenFileName(
            w,
            "Open File",
            "",
            "Text (*.txt *.dat)",
            None,
            QFileDialog.DontUseNativeDialog,
        )
        filename = fn[0]
        if filename == "":
            return 0
        self.fitdata(
            filename=filename,
            datacolumn=self.w.parameters._qds_x_sensor + 1,
            dtype="eccent",
        )
        self.fitdata(
            filename=filename,
            datacolumn=self.w.parameters._qds_y_sensor + 1,
            dtype="wob",
        )

    def fitdata(self, filename="", datacolumn=2, xd=[], yd=[], dtype="wobble"):
        if self.w.parameters._qds_unit == QDS_UNIT_MM:
            eqn.POSITION_UNIT = eqn.POSITION_UNIT_MM
        if self.w.parameters._qds_unit == QDS_UNIT_UM:
            eqn.POSITION_UNIT = eqn.POSITION_UNIT_UM
        if self.w.parameters._qds_unit == QDS_UNIT_NM:
            eqn.POSITION_UNIT = eqn.POSITION_UNIT_NM
        if len(filename) > 0:
            xd, yd = eqn.loadata(filename=filename, datacolumn=datacolumn)
        else:
            xd, yd = eqn.loadata(xdata=xd, ydata=yd)

        if dtype in "eccentricity":
            popt, pconv = eqn.fit_eccentricity(xd, yd, R=self.w.parameters._qds_R_cyl)
            cv, lb = eqn.get_eccen_fitcurve(xd, popt)
            self.plotfits(xd, yd, cv, lb, ax=1)
        if dtype in "wobble":
            popt, pconv = eqn.fit_wobble(
                xd,
                yd,
                th0=self.w.parameters._qds_th0_vert,
                R=self.w.parameters._qds_R_vert,
            )
            cv, lb = eqn.get_wobble_fitcurve(xd, popt)
            self.plotfits(xd, yd, cv, lb, ax=2)

    def load_plot_eccentricity(self):
        # this is multiple column data where the last column is the sensor data.
        w = QWidget()
        w.resize(320, 240)
        # Set window title
        w.setWindowTitle("Load phi vs QDS Eccentricity(X) Data")
        fn = QFileDialog.getOpenFileName(
            w,
            "Open File",
            "",
            "Text (*.txt *.dat)",
            None,
            QFileDialog.DontUseNativeDialog,
        )
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
        fn = QFileDialog.getOpenFileName(
            w,
            "Open File",
            "",
            "Text (*.txt *.dat)",
            None,
            QFileDialog.DontUseNativeDialog,
        )
        filename = fn[0]
        if filename == "":
            return 0
        self.fitdata(filename=filename, datacolumn=-1, dtype="wob")

    def plotfits(self, xd, yd, curve, lbl, ax=2):
        if ax == 2:
            ax = self.w.ax2
        if ax == 1:
            ax = self.w.ax
        ax.clear()
        ax.plot(np.rad2deg(xd), yd, pen=pg.mkPen("b"))
        ax.plot(np.rad2deg(xd), curve, pen=pg.mkPen("g", style=pg.QtCore.Qt.DashLine))
        ax.setTitle(lbl)
        ylbl = "x-x_mean (mm)" if "xc=" in lbl else "y-y_mean (mm)"
        ax.setLabel("bottom", "phi (deg)")
        ax.setLabel("left", ylbl)
