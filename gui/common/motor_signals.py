"""Qt-backed motion notifications for the panels.

ptychosaxs.motions_ver2 emits axis names and positions through a plain
callable list, because the device layer has to be importable by a headless
server with no Qt installed. Calling a listener directly is fine there and
wrong here: `motors.mv()` runs on a scan worker thread, and a listener that
updates a QLabel must not be called from it.

Installing this in place of the device layer's own signals object restores
queued delivery. Qt's AutoConnection compares the *receiver's* thread
affinity to the emitting thread, so a slot on a widget runs on the GUI
thread no matter which worker emitted.

Duck-compatible with motions_ver2.motorSignals, so the emit sites in the
device layer and in gui/handlers/scan_handler.py did not change.
"""

from PyQt5 import QtCore


class QtMotorSignals(QtCore.QObject):
    AxisNameSignal = QtCore.pyqtSignal(str)
    AxisPosSignal = QtCore.pyqtSignal(float)


def install(pts):
    """Give `pts` Qt signals, carrying over anything already connected.

    Returns the new signals object. Safe to call when pts is None (debug
    mode with no motors) and safe to call twice.
    """
    if pts is None:
        return None
    existing = getattr(pts, "signals", None)
    if isinstance(existing, QtMotorSignals):
        return existing

    signals = QtMotorSignals()
    # The device layer's _Signal keeps its listeners in a plain list; carry
    # them over so anything that subscribed before this ran still fires.
    for attr in ("AxisNameSignal", "AxisPosSignal"):
        old = getattr(existing, attr, None)
        for fn in list(getattr(old, "_subscribers", ())):
            getattr(signals, attr).connect(fn)

    pts.signals = signals
    return signals
