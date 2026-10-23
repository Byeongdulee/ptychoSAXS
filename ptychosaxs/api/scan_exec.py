"""Scan execution, moving out of the Qt handler one tranche at a time.

These methods came from gui/handlers/scan_handler.py unchanged except for
one rename: `self.w` became `self.ctx`. That is the whole trick. Walking
the call graph showed the eight executors and the helpers they reach need
40 names from the GUI window, and that those names are overwhelmingly
state rather than widgets -- messages, pts, parameters, motornames,
softglue, dg645, detector and a dozen flags. ScanContext (in
ptychosaxs/api/scans.py) presents exactly those, session first and window
second, so a method can move before the state it uses has, and each name
can migrate to BeamlineSession afterwards without touching an executor
again.

ScanHandler inherits this, so every one of its several hundred internal
callers is unaffected by a method moving. Class attributes the methods
reach for -- OVERHEAD_FLY, MAX_VELOCITY and the rest -- still resolve
through self on the handler.

Moved in small commits on purpose. None of this can be meaningfully
exercised without beam, so when something turns out to be wrong the useful
thing is a small diff to point at rather than a green test run.
"""

import csv
import os
import pathlib
import threading
import time
import traceback

import numpy as np

from ptychosaxs.api import optics_status as _optics_status
from ptychosaxs.api import scanplan  # noqa: F401  (used by moved methods)
from ptychosaxs.api.scan_config import (
    COUNT_DONE_TIMEOUT_MARGIN_S,
    COUNT_POLL_INTERVAL_S,
    DETECTOR_NOT_STARTED_ERROR,
    DETECTOR_READOUTTIME,
    SCALER_CNT_PV,
    SCALER_CONT_PV,
    SCALER_TP_PV,
    SCAN_SCALAR_PVS,
)

try:  # pragma: no cover - absent only where pyepics is not installed
    import epics
    from epics import caget
except ImportError:  # pragma: no cover
    epics = None
    caget = None

try:  # pragma: no cover
    import h5py
except ImportError:  # pragma: no cover
    h5py = None


class ScanExecutorsMixin:
    """The parts of scan execution that have left the GUI handler."""

    def _scaler_oneshot_begin(self, exp_time):
        """Put the scaler into OneShot mode with `exp_time` as its preset,
        mirroring ScalarScanWindow._start_scan.

        Returns True if the scaler is ours for the rest of the scan, in which
        case _scaler_oneshot_restore must run on every exit path.
        """
        try:
            import epics
        except ImportError:
            print("Warning: pyepics not available, scaler counts not collected")
            return False
        try:
            epics.caput(SCALER_CONT_PV, 0)  # OneShot, for the duration of this scan only
            epics.caput(SCALER_TP_PV, exp_time)
        except Exception as e:
            print(f"Warning: could not put scaler into OneShot mode: {e}")
            return False
        return True

    def _scaler_oneshot_count(self, exp_time):
        """Trigger one OneShot count, block until the scaler reports it done,
        then read the channels once.

        Mirrors _ScanWorker._wait_for_count_done / _read_scalars_into in
        scalar_scan.py. Returns one row of floats in SCAN_SCALAR_PVS order,
        using nan for anything that could not be read so the csv keeps one row
        per scan position even when the scaler misbehaves.
        """
        nan_row = [float("nan")] * len(SCAN_SCALAR_PVS)
        timeout = exp_time + COUNT_DONE_TIMEOUT_MARGIN_S
        try:
            import epics

            # Put-completion: the scaler record acknowledges .CNT only once the
            # count has finished, so this blocks for the whole exposure. A plain
            # non-blocking put would return before the IOC had even started
            # counting, and since the record zeroes .S1-.SN at count start, the
            # read below would then return 0 for every channel.
            epics.caput(SCALER_CNT_PV, 1, wait=True, timeout=timeout)

            # Backstop in case this IOC does not honour put-completion on .CNT.
            # use_monitor=False forces a fresh CA get rather than a possibly
            # stale monitor-cached value.
            deadline = time.monotonic() + timeout
            while epics.caget(SCALER_CNT_PV, use_monitor=False):
                if time.monotonic() > deadline:
                    print("Warning: timed out waiting for scaler count to finish")
                    return nan_row
                time.sleep(COUNT_POLL_INTERVAL_S)
        except Exception as e:
            print(f"Warning: scaler count failed: {e}")
            return nan_row

        row = []
        for name, pv_name in SCAN_SCALAR_PVS.items():
            try:
                value = epics.caget(pv_name, use_monitor=False)
            except Exception as e:
                print(f"Warning: could not read {name} ({pv_name}): {e}")
                value = None
            row.append(float("nan") if value is None else float(value))
        return row

    def _scaler_oneshot_restore(self):
        """Put the scaler back into AutoCount - the counterpart to
        _scaler_oneshot_begin, so a stopped or errored scan never leaves the
        scaler stuck out of free-running for other clients."""
        try:
            import epics

            epics.caput(SCALER_CONT_PV, 1)
        except Exception as e:
            print(f"Warning: could not restore scaler to free-running: {e}")
