"""Beamline configuration the scan machinery reads.

Record names and timing constants, as plain data. They were class- and
module-level attributes inside gui/handlers/scan_handler.py, which meant
the PVs a scan counts on and the timeouts it waits for could only be read
by importing a Qt handler -- and that a headless scan runner could not
have them at all.

Same move as MAX_VELOCITY in ptychosaxs/api/guards.py, and for the same
reason: configuration that governs the instrument should not live inside
a view.
"""

#: Hexapod fly modes. Mirrored from rungui, where the panel's menu sets
#: which one a fly scan uses.
HEXAPOD_FLYMODE_WAVELET = 0
HEXAPOD_FLYMODE_STANDARD = 1

# --- detectors --------------------------------------------------------------

#: Detector readout floor, in seconds, used when working out whether a
#: requested exposure period is achievable.
DETECTOR_READOUTTIME = 0.02

#: What a detector reports when it never started. Distinguished from zero
#: frames, which is a detector that started and collected nothing.
DETECTOR_NOT_STARTED_ERROR = -1

# --- the step-scan scaler ---------------------------------------------------

#: Channels counted at each 1-D step-scan position, saved next to the
#: Struck MCS csv.
#:
#: Deliberately *not* the same scaler as gui/scalar_scan.py's: that window
#: counts on 12idc:3820:scaler1, this counts on 12idc:scaler1. They look
#: alike and are different instruments, which is worth saying out loud
#: because the names differ by four characters.
SCAN_SCALAR_PVS = {
    "IC": "12idc:scaler1.S2",
    "BS2": "12idc:scaler1.S3",
    "BS": "12idc:scaler1.S4",
    "IfCRL": "12idc:scaler1.S5",
}

#: 0 = OneShot, 1 = AutoCount (free-running).
SCALER_CONT_PV = "12idc:scaler1.CONT"

#: Preset count time.
SCALER_TP_PV = "12idc:scaler1.TP"

#: Count start -- put 1; reads back 0 when the count is done.
SCALER_CNT_PV = "12idc:scaler1.CNT"

#: Added to the count time to get the ceiling for one OneShot count, so a
#: scaler that never reports done fails the step rather than the scan.
COUNT_DONE_TIMEOUT_MARGIN_S = 5.0

#: How often .CNT is polled while waiting for it to drop back to zero.
COUNT_POLL_INTERVAL_S = 0.01
