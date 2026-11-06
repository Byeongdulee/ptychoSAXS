"""
gui/handlers/traj_handler.py
Two-step scanning: write a scan trajectory csv, then run a scan from one.

A scan used to go straight from the scan button to the executors. Now every
scan launched from the panel goes through a trajectory file
(ptychosaxs/api/scan_traj.py):

  1. write  -- the trajectory is built (from the user view's form, or from
               the large view's per-motor fields) and written as a csv into
               the folder the scan's master file will go in;
  2. run    -- a trajectory file is read back, its points checked, and the
               matching scan entry point is started with its parameters
               taken from the file instead of the widgets
               (ScanHandler.run_plan).

The large view runs both steps back to back (write_and_run). The user view
exposes them separately, with a simple FIFO queue on the run side: a run
requested while a scan is going waits its turn.
"""

import datetime
import glob
import html
import os
from collections import deque

import numpy as np
from PyQt5.QtCore import QObject, Qt, QTimer
from PyQt5.QtWidgets import (
    QFileDialog, QLabel, QLineEdit, QMessageBox, QPushButton, QVBoxLayout,
)
# After PyQt5: imported first, pyqtgraph probes the other Qt bindings, and a
# broken one in the environment (PySide6 here) makes the import fail.
import pyqtgraph as pg

from ptychosaxs.api import scan_traj, scanplan
from ptychosaxs.api.scan_config import DETECTOR_READOUTTIME
from ptychosaxs.api.scan_traj import FLY, STEP, TAKESHOT, Axis, ScanTrajectory, TrajectoryError

# The user view's scan-dimension combo, in display order.
DIM_CHOICES = (("takeshot", 0), ("1", 1), ("2", 2), ("3", 3))
DEFAULT_DIM_TEXT = "2"
KIND_CHOICES = (STEP, FLY)

# Motor sets offered for 2-D and 3-D scans from the user view. Unusual
# combinations stay on the large view's axis selectors and Extra Scans.
MOTORS_2D = ("X", "Z")
MOTORS_3D = ("X", "Z", "phi")

# Trajectory files sit next to the master file and share its stem.
TRAJ_SUFFIX = "_traj.csv"

SETTINGS = "userView/scanTraj/"


def _fmt_time(seconds):
    return scanplan.format_duration(seconds)


class TrajHandler:
    def __init__(self, window) -> None:
        self.w = window
        self.queue = deque()        # trajectory paths waiting to run
        self.running = None         # dict for the trajectory scan in progress
        self.loaded = None          # (path, ScanTrajectory) in the run panel
        self._pending_outcome = None  # set between a scan ending and _finish
        self._loaded_plot = None

    @property
    def ui(self):
        return self.w.ui

    @property
    def sh(self):
        return self.w.scan_handler

    def _own(self, cls, name):
        """A widget of the current view itself, never one borrowed from the
        large view behind it (see gui/widgets/layered_window.py)."""
        return QObject.findChild(self.ui, cls, name)

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------

    def _unit(self, name):
        try:
            return self.w.motorunits[self.w.motornames.index(name)]
        except ValueError:
            return ""

    def _connected(self, name):
        try:
            return bool(self.w.motorconnected[self.w.motornames.index(name)])
        except ValueError:
            return False

    def _current_pos(self, name):
        """A motor's position now: the device first, the panel label second."""
        try:
            return float(self.w.pts.get_pos(name))
        except Exception:
            return float(self.w.get_motorpos(name))

    def _readbacks(self):
        """The header values a trajectory records at creation time."""
        sh = self.sh
        extra = {"monoE": sh._get_mono_energy(),
                 "motor positions": sh._get_logged_motor_positions()}
        trans1, trans2 = sh._get_gonio_trans_positions()
        extra["trans1"], extra["trans2"] = trans1, trans2
        if self._connected("phi"):
            try:
                extra["phi"] = str(self._current_pos("phi"))
            except Exception:
                pass
        return extra

    def fly_period(self, expt):
        """Per-trigger time of a fly scan, as update_scan_estimate computes it."""
        fly_acq = getattr(self.w.parameters, "_fly_acq_time", self.sh.OVERHEAD_FLY)
        return max(fly_acq, expt + DETECTOR_READOUTTIME, self.sh.OVERHEAD_FLY)

    def estimate(self, traj):
        fly_1d = scanplan.FLY_HEXAPOD_1D
        if traj.axes and traj.axes[0].name not in getattr(self.w.pts.hexapod, "axes", ()):
            fly_1d = scanplan.FLY_PHI
        return scan_traj.estimate(
            traj, overhead_step=self.sh.OVERHEAD_STEP,
            fly_period=self.fly_period(traj.expt), fly_1d_kind=fly_1d,
            fly_slice_overhead=self.sh.OVERHEAD_FLY3D_PHI)

    # ------------------------------------------------------------------
    # Where trajectory files go
    # ------------------------------------------------------------------

    def _master_folder_type(self):
        """The folder the first active area detector's master file goes in.

        Same rule as ScanExecutorsMixin._get_master_file_path_with_number,
        so the trajectory lands beside the master file. With no area
        detector selected (e.g. a debug session) it falls back to ptycho/.
        """
        detectors = getattr(self.w, "detector", [])
        for i, tp in ((0, "S"), (1, "W")):
            if i < len(detectors) and detectors[i] is not None:
                mode = self.w.detector_mode[i]
                if self.w.is_ptychomode and mode in ("", "ptycho"):
                    return "ptycho"
                return tp + "AXS"
        return "ptycho"

    def trajectory_path(self, traj):
        folder = self.sh._gui_scan_folder(self._master_folder_type(), traj.scan_id)
        name = "%s_%04d%s" % (traj.sample_name, int(traj.scan_number), TRAJ_SUFFIX)
        return os.path.join(folder, name)

    def trajectory_root(self):
        """Where the Load... dialog opens: <working folder>/ptycho."""
        return os.path.join(self.sh._working_folder(), "ptycho")

    def _folder_types(self):
        """Folder types a trajectory can sit in, the current one first."""
        first = self._master_folder_type()
        return [first] + [t for t in ("ptycho", "SAXS", "WAXS") if t != first]

    def trajectory_for_number(self, number):
        """The trajectory file for scan `number`, or None.

        Looks in the scan's folder under each folder type, current one first;
        if a folder somehow holds several, the newest wins.
        """
        for folder_type in self._folder_types():
            folder = self.sh._gui_scan_folder(folder_type, "S%04d" % int(number))
            found = glob.glob(os.path.join(folder, "*" + TRAJ_SUFFIX))
            if found:
                return max(found, key=os.path.getmtime)
        return None

    def trajectory_numbers(self):
        """Every scan number with a trajectory file, ascending."""
        numbers = set()
        root = self.sh._working_folder()
        for folder_type in self._folder_types():
            pattern = os.path.join(root, folder_type, "S[0-9]*", "*" + TRAJ_SUFFIX)
            for path in glob.glob(pattern):
                name = os.path.basename(os.path.dirname(path))
                if name[1:].isdigit():
                    numbers.add(int(name[1:]))
        return sorted(numbers)

    # ------------------------------------------------------------------
    # Building trajectories
    # ------------------------------------------------------------------

    def trajectory_from_params(self, kind, axes_params, expt):
        """A ScanTrajectory from _read_motor_params-style dicts."""
        axes = [Axis(name=p["name"], unit=self._unit(p["name"]), p0=float(p["p0"]),
                     st=float(p["st"]), fe=float(p["fe"]), step=float(p["step"]))
                for p in axes_params]
        return ScanTrajectory(sample_name=self.w.parameters.scan_name,
                              scan_number=int(self.w.parameters.scan_number),
                              kind=kind, expt=float(expt), axes=axes)

    def trajectory_from_large(self, kind, motor_indices):
        """Read the large view's fields for these motors, prompting exactly
        as a direct scan launch would. None if cancelled or unreadable."""
        sh = self.sh
        if kind == TAKESHOT:
            widget = self.ui.findChild(QLineEdit, "ed_lup_1_t")
            try:
                expt = float(widget.text()) if widget is not None else 0.0
            except ValueError:
                expt = 0.0
            return self.trajectory_from_params(TAKESHOT, [], expt)
        try:
            params = [sh._read_motor_params(i) for i in motor_indices]
        except (ValueError, TypeError):
            # check_start_position's Cancel lands here as float(None).
            QMessageBox.warning(self.w.ui, "Error", "Check scan parameters.")
            return None
        return self.trajectory_from_params(kind, params, params[0]["expt"])

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def write(self, traj, advance=False, confirm_overwrite=True):
        """Write `traj` beside its master file. Returns the path, or None.

        Makes the scan folder (the master file itself is only written when
        the scan starts). A finished scan's file is never overwritten; an
        unrun one is, after asking if confirm_overwrite. With advance, the
        scan number moves past every number this scan will use (one per phi
        slice for 3-D), so the next trajectory gets a free folder.
        """
        try:
            traj.validate()
        except TrajectoryError as exc:
            self.w.report_error(f"Trajectory not written: {exc}")
            QMessageBox.warning(self.w.ui, "Scan trajectory", str(exc))
            return None
        path = self.trajectory_path(traj)
        if os.path.exists(path):
            try:
                done = scan_traj.read_trajectory(path).header.get("completed", "")
            except TrajectoryError:
                done = ""
            if done not in ("", "no"):
                QMessageBox.warning(
                    self.w.ui, "Scan trajectory",
                    f"{traj.scan_id} has already been run ({done}).\n"
                    "Change the scan number and write again.")
                return None
            if confirm_overwrite and QMessageBox.question(
                    self.w.ui, "Scan trajectory",
                    f"A trajectory for {traj.scan_id} already exists:\n{path}\n\n"
                    "Overwrite it?") != QMessageBox.Yes:
                return None
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            scan_traj.write_trajectory(path, traj, extra=self._readbacks())
        except (OSError, TrajectoryError) as exc:
            self.w.report_error(f"Could not write {path}: {exc}")
            QMessageBox.warning(self.w.ui, "Scan trajectory",
                                f"Could not write the trajectory:\n{exc}")
            return None
        print(f"Wrote scan trajectory: {path}")
        self.ui.statusbar.showMessage(f"Wrote {os.path.basename(path)}", 10000)
        if advance:
            self.w.parameters.scan_number = int(traj.scan_number) + traj.numbers_used()
            self.w.update_scannumber()
            self.w.update_scanname()
            self.w.parameters.writeini()
        return path

    # ------------------------------------------------------------------
    # Loading (user view's run panel)
    # ------------------------------------------------------------------

    def load(self, path, quiet=False):
        """Read `path` into the run panel. Leaves the panel alone if the file
        cannot be parsed.

        Anything parsable loads, so it can be inspected; whether it can be
        run (a regular grid, connected motors) is checked by start(), and
        flagged in the status bar here.
        """
        try:
            traj = scan_traj.read_trajectory(path)
        except TrajectoryError as exc:
            if not quiet:
                QMessageBox.warning(self.w.ui, "Load scan",
                                    f"Not a usable scan trajectory:\n{path}\n\n{exc}")
            return False
        self.loaded = (path, traj)
        self.refresh_loaded()
        self.show_loaded_number()
        problem = self._run_problem(traj)
        if problem and not quiet:
            self.ui.statusbar.showMessage(f"Loaded, but it cannot be run: {problem}", 30000)
        return True

    def _run_problem(self, traj):
        """Why `traj` cannot be run here, or "" if it can."""
        try:
            scan_traj.check_runnable(traj)
        except TrajectoryError as exc:
            return str(exc)
        missing = [ax.name for ax in traj.axes if not self._connected(ax.name)]
        if missing:
            return "motor(s) not connected: %s." % ", ".join(missing)
        return ""

    def show_loaded_number(self):
        """edit_load_scannumber always shows the loaded trajectory's number."""
        edit = self._own(QLineEdit, "edit_load_scannumber")
        if edit is not None:
            edit.setText("" if self.loaded is None else str(self.loaded[1].scan_number))

    def load_number(self, number):
        """Load the trajectory written for scan `number`."""
        path = self.trajectory_for_number(number)
        if path is None:
            self.ui.statusbar.showMessage("No scan trajectory for S%04d." % int(number), 10000)
            self.show_loaded_number()
            return False
        if not self.load(path):
            self.show_loaded_number()
            return False
        return True

    def load_number_from_edit(self):
        """edit_load_scannumber's Enter."""
        edit = self._own(QLineEdit, "edit_load_scannumber")
        try:
            number = int(edit.text())
        except (AttributeError, ValueError):
            self.show_loaded_number()
            return
        self.load_number(number)

    def step_loaded_number(self, direction):
        """-1/+1: load the nearest lower/higher scan that has a trajectory.

        Nearest rather than exactly +-1, so numbers with no trajectory of
        their own -- the extra ones a 3-D scan uses for its phi slices, or
        ones skipped by hand -- are stepped over.
        """
        numbers = self.trajectory_numbers()
        if self.loaded is not None:
            current = self.loaded[1].scan_number
        else:
            edit = self._own(QLineEdit, "edit_load_scannumber")
            try:
                current = int(edit.text())
            except (AttributeError, ValueError):
                current = int(self.w.parameters.scan_number)
        if direction > 0:
            candidates = [n for n in numbers if n > current]
            target = candidates[0] if candidates else None
        else:
            candidates = [n for n in numbers if n < current]
            target = candidates[-1] if candidates else None
        if target is None:
            self.ui.statusbar.showMessage(
                "No %s scan trajectory." % ("later" if direction > 0 else "earlier"), 10000)
            return
        self.load_number(target)

    def browse(self):
        start = self.trajectory_root()
        if not os.path.isdir(start):
            start = self.sh._working_folder()
        path, _ = QFileDialog.getOpenFileName(
            self.w.ui, "Load scan trajectory", start, "Scan trajectories (*.csv)")
        if path:
            self.load(path)

    def refresh_loaded(self):
        """Redraw the run panel from self.loaded."""
        if self.loaded is None:
            self.plot_loaded(None)   # still the empty black box
            return
        path, traj = self.loaded
        label = self._own(QLabel, "label_loadSinfo")
        if label is not None:
            # Same lines as the .ui's placeholder text, with each value
            # highlighted so it stands out from its caption.
            def value(text):
                return ('<span style="background-color: cyan;">&nbsp;%s&nbsp;</span>'
                        % html.escape(str(text)))
            label.setTextFormat(Qt.RichText)
            label.setText("Loaded scan info<br>num: %s<br>sample: %s<br>scan type: %s"
                          % (value(traj.scan_number), value(traj.sample_name),
                             value(_describe(traj))))
            label.setToolTip(path)
        self.show_feedback("rs", traj)
        self.plot_loaded(traj)
        self.refresh_run_button()

    @staticmethod
    def master_file_for(path):
        """The master file already in this trajectory's scan folder, or None.

        It is written when a scan starts, so its presence means the folder's
        scan has been run (or started) and running again would overwrite it.
        """
        found = sorted(glob.glob(os.path.join(os.path.dirname(path), "*_master.h5")))
        return found[0] if found else None

    # label_load_scanCheck states, coloured like label_scanCheck's.
    _LOAD_CHECK = {
        "none": ("No scan\ntrajectory", "background-color: #fffde7; color: #000000"),
        "ready": ("Scan\nready!", "background-color: #C6EFCE; color: #000000"),
        "full": ("Scan folder full,\nmake a new one.",
                 "background-color: rgb(255, 199, 206); color: #000000"),
    }

    def refresh_run_button(self):
        """Grey out Run Loaded Scan when the loaded scan's folder already has
        a master file, and say which state it is in on label_load_scanCheck:
        no trajectory, ready to run, or already run."""
        have_csv = self.loaded is not None and os.path.exists(self.loaded[0])
        master = self.master_file_for(self.loaded[0]) if have_csv else None
        label = self._own(QLabel, "label_load_scanCheck")
        if label is not None:
            text, style = self._LOAD_CHECK[
                "none" if not have_csv else "full" if master else "ready"]
            label.setText(text)
            label.setStyleSheet(style)
        button = self._own(QPushButton, "pushButton_run_scan")
        if button is None:
            return
        if getattr(self.w, "scans_blocked", False):
            button.setEnabled(False)
            return
        button.setEnabled(master is None)
        button.setToolTip(
            "This scan folder already has a master file (%s), so it has been "
            "run. Load or write another trajectory." % os.path.basename(master)
            if master else "")

    def plot_loaded(self, traj):
        """Nominal path in verticalLayout_loaded_scan_path, on black. traj
        None leaves the plot empty, so the box is there before any load."""
        layout = self._own(QVBoxLayout, "verticalLayout_loaded_scan_path")
        if layout is None:
            return
        plot = self._loaded_plot
        if plot is None or plot.parent() is None:
            plot = pg.PlotWidget()
            plot.setBackground("k")
            for axis in ("bottom", "left"):
                plot.getAxis(axis).enableAutoSIPrefix(False)
            layout.addWidget(plot)
            self._loaded_plot = plot
        plot.clear()
        plot.setTitle(None)
        if traj is None:
            return
        if traj.kind == TAKESHOT:
            plot.setTitle("takeshot - no motion")
            return
        # The points as listed, so an irregular (unrunnable) file still shows
        # exactly what it contains. 3-D draws every phi slice's XY path on
        # top of one another.
        grid = traj.points()
        if traj.dim == 1:
            xs, ys = np.arange(len(grid)), grid[:, 0]
            plot.setLabel("bottom", "point")
            plot.setLabel("left", "%s (%s)" % (traj.axes[0].name, traj.axes[0].unit))
            plot.setAspectLocked(False)
        else:
            xs, ys = grid[:, 0], grid[:, 1]
            plot.setLabel("bottom", "%s (%s)" % (traj.axes[0].name, traj.axes[0].unit))
            plot.setLabel("left", "%s (%s)" % (traj.axes[1].name, traj.axes[1].unit))
            plot.setAspectLocked(True)
        plot.plot(x=xs, y=ys, pen=pg.mkPen("r", width=1), symbol="o", symbolSize=4,
                  symbolBrush=pg.mkBrush("r"), symbolPen=pg.mkPen(None))
        plot.plot(x=xs[:2], y=ys[:2], pen=None, symbol="x", symbolSize=10,
                  symbolBrush=pg.mkBrush(None), symbolPen=pg.mkPen("c", width=2))

    def show_feedback(self, prefix, traj):
        """Fill label_<prefix>_N1..3 / _Ntot / _estT, like the large view's
        Nx/Ny/Ntot/estT labels. traj None blanks them."""

        def put(name, text):
            label = self._own(QLabel, f"label_{prefix}_{name}")
            if label is not None:
                label.setText(text)

        if traj is None:
            for i in (1, 2, 3):
                put(f"N{i}", f"N{i}\n-")
            put("Ntot", "Ntot: -")
            put("estT", "-\n-")
            return
        est = self.estimate(traj)
        for i in (1, 2, 3):
            count = est.counts[i - 1] if i <= len(est.counts) else None
            put(f"N{i}", f"N{i}\n{count if count is not None else '-'}")
        put("Ntot", f"Ntot: {est.n_total}")
        put("estT", f"{_fmt_time(est.step_seconds)}\n{_fmt_time(est.fly_seconds)}")

    # ------------------------------------------------------------------
    # Running
    # ------------------------------------------------------------------

    def run_loaded(self):
        """pushButton_run_scan: run the loaded trajectory, or queue it."""
        if self.loaded is None:
            QMessageBox.information(self.w.ui, "Run scan", "Load a scan trajectory first.")
            return
        path, _ = self.loaded
        self.request_run(path)

    def request_run(self, path):
        self._settle()
        if (self.running is not None and self.running["path"] == path) or path in self.queue:
            QMessageBox.information(self.w.ui, "Run scan",
                                    "That trajectory is already running or queued.")
            return
        if self.w.isscan or self.running is not None or self.queue:
            self.queue.append(path)
            self.ui.statusbar.showMessage(
                f"Queued {os.path.basename(path)} ({len(self.queue)} waiting)", 10000)
            return
        self.start(path)

    def start(self, path, allow_existing_master=False):
        """Run the trajectory at `path` now. True if the scan started.

        Refused if the scan folder already has a master file -- that scan has
        run, and running it again would overwrite it -- unless
        allow_existing_master, which the write-then-run path passes: there
        the trajectory was written a moment ago for the panel's current scan
        number, exactly as a direct scan launch always behaved.

        Re-read from disk rather than trusting anything cached, so what runs
        is the file as it is now. The scan takes the file's sample name and
        scan number; once it ends the panel's number is put back to whichever
        is later, the panel's or the scan's own successor.
        """
        self._settle()
        try:
            traj = scan_traj.read_trajectory(path)
        except TrajectoryError as exc:
            self.w.report_error(f"Cannot run {os.path.basename(path)}: {exc}")
            return False
        done = traj.header.get("completed", "")
        if done not in ("", "no"):
            self.w.report_error(f"{traj.scan_id} was already run ({done}); not running it again.")
            return False
        problem = self._run_problem(traj)
        master = None if allow_existing_master else self.master_file_for(path)
        if master and not problem:
            problem = f"its folder already has a master file ({os.path.basename(master)})."
        if problem:
            self.w.report_error(f"Cannot run {traj.scan_id}: {problem}")
            return False
        indices = [self.w.motornames.index(ax.name) for ax in traj.axes]

        previous_number = int(self.w.parameters.scan_number)
        self.ui.edit_scanname.setText(traj.sample_name)
        self.w.parameters.scan_number = int(traj.scan_number)
        self.w.update_scannumber()
        self.w.update_scanname()

        # A step scan visits the file's points exactly as listed (see
        # ScanExecutorsMixin._planned_points); a fly scan runs the grid its
        # axes describe, which check_runnable has confirmed they are.
        custom = self._sync_step_metadata(path, traj)
        plan = {"expt": traj.expt,
                "motor_indices": indices,
                "points": traj.points() if traj.kind == STEP else None,
                "custom": custom,
                "axes": {i: dict(ax.as_params(traj.expt), motor_index=i)
                         for i, ax in zip(indices, traj.axes)}}
        started = self.sh.run_plan(plan, lambda: self._dispatch(traj, indices))
        if not started:
            self.w.parameters.scan_number = max(previous_number, int(traj.scan_number))
            self.w.update_scannumber()
            self.w.update_scanname()
            return False

        now = datetime.datetime.now()
        identity = getattr(self.sh, "_cached_optics_identity", None) or {}
        try:
            scan_traj.update_header(path, date=now.strftime("%Y-%m-%d"),
                                    time=now.strftime("%H:%M:%S"),
                                    optics_name=identity.get("optics_name", ""),
                                    optics_type=identity.get("optics_type", ""))
        except (OSError, TrajectoryError) as exc:
            print(f"Warning: could not stamp {path}: {exc}")
        self.running = {"path": path, "traj": traj, "previous_number": previous_number}
        self.refresh_run_button()   # its master file exists now
        return True

    def _sync_step_metadata(self, path, traj):
        """Make a step trajectory's header describe the points it will visit.

        The header's n_pos and per-axis fields are written once, when the
        trajectory is created; a hand edit to the table leaves them
        describing the old grid. So at run time n_pos is set to the number
        of listed points, and if those points are not a regular grid in scan
        order, scan_step_size becomes "custom" -- in this file and, through
        the plan, in the log row. Returns whether the scan is custom.
        """
        if traj.kind != STEP:
            return False
        custom = bool(traj.grid_error)
        updates = {}
        n = str(traj.n_positions())
        if traj.header.get("n_pos", "") != n:
            updates["n_pos"] = n
        if custom and traj.header.get("scan_step_size", "") != "custom":
            updates["scan_step_size"] = "custom"
        if updates:
            try:
                scan_traj.update_header(path, **updates)
                traj.header.update(updates)
            except (OSError, TrajectoryError) as exc:
                print(f"Warning: could not update {path}: {exc}")
        return custom

    def _dispatch(self, traj, idx):
        w = self.w
        if traj.kind == TAKESHOT:
            return self.sh.takeshot()
        fly = traj.kind == FLY
        if traj.dim == 1:
            return w.fly(idx[0]) if fly else w.stepscan(idx[0])
        if traj.dim == 2:
            return w.fly2d(idx[0], idx[1], snake=True) if fly else w.stepscan2d(idx[0], idx[1])
        return (w.fly3d(idx[0], idx[1], idx[2], snake=True) if fly
                else w.stepscan3d(idx[0], idx[1], idx[2]))

    def write_and_run(self, kind, motor_indices=()):
        """The large view's scan buttons: write the trajectory, run it."""
        if self.w.isscan:
            QMessageBox.warning(self.w.ui, "Scan", "A scan is already running.")
            return
        # takeshot deliberately skips the name prompt, as it always has.
        if kind != TAKESHOT and not self.sh._confirm_scan_name():
            return
        traj = self.trajectory_from_large(kind, motor_indices)
        if traj is None:
            return
        path = self.write(traj, advance=False, confirm_overwrite=False)
        if path:
            self.start(path, allow_existing_master=True)

    def takeshot_now(self):
        """The user view's takeshot: one frame now, ignoring the loaded scan."""
        if self.w.isscan:
            QMessageBox.warning(self.w.ui, "Takeshot", "A scan is already running.")
            return
        try:
            expt = self.w.user_view.exposure_seconds()
        except ValueError:
            QMessageBox.warning(self.w.ui, "Takeshot", "Exposure time must be a number.")
            return
        if QMessageBox.question(
                self.w.ui, "Takeshot",
                "Take a single camera image now (%g s exposure)?" % expt) != QMessageBox.Yes:
            return
        self.w.update_scanname()
        traj = self.trajectory_from_params(TAKESHOT, [], expt)
        path = self.write(traj, advance=False, confirm_overwrite=False)
        if path:
            self.start(path, allow_existing_master=True)

    # ------------------------------------------------------------------
    # Completion
    # ------------------------------------------------------------------

    def scan_ended(self, outcome):
        """A scan left the Scanning state: outcome "yes", "partial" or "error".

        Deferred one event-loop turn, because scandone advances the scan
        number *after* setting the status this is triggered from.
        """
        if self.running is None:
            return
        self._pending_outcome = outcome
        QTimer.singleShot(0, lambda: self._finish(outcome))

    def _settle(self):
        """Finish an ended scan's bookkeeping now if it is still pending --
        a macro can ask for its next scan before the deferred call runs."""
        if self.running is not None and self._pending_outcome is not None:
            self._finish(self._pending_outcome)

    def _finish(self, outcome):
        self._pending_outcome = None
        run, self.running = self.running, None
        if run is None:
            return
        self.sh.clear_plan()
        try:
            scan_traj.update_header(run["path"], completed=outcome)
        except (OSError, TrajectoryError) as exc:
            print(f"Warning: could not mark {run['path']} {outcome}: {exc}")
        number = max(run["previous_number"], int(self.w.parameters.scan_number))
        if number != int(self.w.parameters.scan_number):
            self.w.parameters.scan_number = number
            self.w.update_scannumber()
            self.w.update_scanname()
            self.w.parameters.writeini()
        if self.loaded is not None and self.loaded[0] == run["path"]:
            self.load(run["path"], quiet=True)

        if outcome != "yes":
            if self.queue:
                n = len(self.queue)
                self.queue.clear()
                self.w.report_error(f"Scan queue cleared: {n} queued scan(s) not run.")
            return
        while self.queue:
            if self.start(self.queue.popleft()):
                return



def _describe(traj):
    """"2D flyscan (X, Z)" for the run panel's info label."""
    if traj.kind == TAKESHOT:
        return TAKESHOT
    return "%s (%s)" % (traj.scan_type, ", ".join(ax.name for ax in traj.axes))
