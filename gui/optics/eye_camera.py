"""Acquisition control for the X-ray eye camera: takeshot, Start, Stop.

takeshot exposes one frame with the fast shutter open only around it:

    shutter open --delay--> Acquire=1 --(exposure done)--delay--> shutter close

Each step is scheduled with a one-shot QTimer, so the panel keeps
repainting and taking server events while the shot is in flight, instead
of being frozen for the exposure. "Exposure done" is the camera's
Acquire_RBV dropping back to 0, waited for at least the exposure time and
at most the exposure time plus TIMEOUT_MARGIN_S, so a camera that never
reports idle still gets its shutter closed.

Once the shutter has been opened, every path out -- success, a failed put,
a timeout -- ends in the close step.

Like XrayEye, this takes its PV factory as an argument and knows nothing
about any particular GUI. All PV names and the step delay live in
xray_eye.py.
"""

import time

from PyQt5.QtCore import QObject, QTimer, pyqtSignal

from gui.optics.xray_eye import (
    EYE_ACQUIRE_PV, EYE_ACQUIRE_RBV_PV, EYE_EXPOSURE_PV, EYE_EXPOSURE_RBV_PV,
    EYE_IMAGE_MODE_PV, FAST_SHUTTER_CLOSE_PV, FAST_SHUTTER_OPEN_PV,
    IMAGE_MODE_CONTINUOUS, IMAGE_MODE_SINGLE, TAKESHOT_STEP_DELAY_MS,
)

# How often to look at Acquire_RBV once the exposure time has passed. The
# server publishes at about 10 Hz, so polling faster gains nothing.
POLL_MS = 50

# Extra time allowed beyond the exposure for the camera to report idle
# before the shutter is closed anyway.
TIMEOUT_MARGIN_S = 5.0

SKIP_ACQUIRING_MSG = "X-ray eye acquiring, skipping takeshot"


class EyeCamera(QObject):
    """takeshot / Start / Stop for whichever camera is the X-ray eye.

    `pv_factory(name)` must return an object with get() and put(). Pass
    ``debug=True`` when it produces simulated records. Those read None for
    Acquire_RBV, which outside debug mode means the camera is not
    answering and makes takeshot refuse to open the shutter.

    Progress and problems are reported through `message`. The caller
    decides where they go; the optics panel sends them to its status bar.
    """

    message = pyqtSignal(str)

    def __init__(self, pv_factory, debug=False,
                 step_delay_ms=TAKESHOT_STEP_DELAY_MS, parent=None):
        super(EyeCamera, self).__init__(parent)
        self._pv_factory = pv_factory
        self._debug = bool(debug)
        self.step_delay_ms = int(step_delay_ms)
        self._pv_cache = {}
        self._busy = False
        self._shutter_open = False
        self._deadline = 0.0

    # -- records -------------------------------------------------------

    PV_NAMES = (EYE_ACQUIRE_PV, EYE_ACQUIRE_RBV_PV, EYE_IMAGE_MODE_PV,
                EYE_EXPOSURE_PV, EYE_EXPOSURE_RBV_PV,
                FAST_SHUTTER_OPEN_PV, FAST_SHUTTER_CLOSE_PV)

    def connect_records(self):
        """Bind every record up front. A server-backed factory subscribes
        on first use, and a channel opened only when the button is pressed
        would not have connected in time for the acquiring check."""
        for name in self.PV_NAMES:
            self._pv(name)

    def _pv(self, name):
        pv = self._pv_cache.get(name)
        if pv is None:
            pv = self._pv_factory(name)
            self._pv_cache[name] = pv
        return pv

    def _get(self, name):
        try:
            return self._pv(name).get()
        except Exception:
            return None

    def _put(self, name, value):
        self._pv(name).put(value)

    def is_acquiring(self):
        """True / False from Acquire_RBV, or None when it cannot be read."""
        value = self._get(EYE_ACQUIRE_RBV_PV)
        if value is None:
            return None
        try:
            return int(value) != 0
        except (TypeError, ValueError):
            return None

    def exposure_s(self):
        """What the camera accepted, else the setpoint, else 0."""
        for name in (EYE_EXPOSURE_RBV_PV, EYE_EXPOSURE_PV):
            try:
                value = float(self._get(name))
            except (TypeError, ValueError):
                continue
            if value >= 0:
                return value
        return 0.0

    @property
    def busy(self):
        return self._busy

    # -- Start / Stop ----------------------------------------------------

    def start(self):
        """Continuous mode, then acquire."""
        try:
            self._put(EYE_IMAGE_MODE_PV, IMAGE_MODE_CONTINUOUS)
            self._put(EYE_ACQUIRE_PV, 1)
        except Exception as exc:
            self.message.emit("X-ray eye start failed: %s" % exc)
            return False
        self.message.emit("X-ray eye acquiring (continuous)")
        return True

    def stop(self):
        try:
            self._put(EYE_ACQUIRE_PV, 0)
        except Exception as exc:
            self.message.emit("X-ray eye stop failed: %s" % exc)
            return False
        self.message.emit("X-ray eye stopped")
        return True

    # -- takeshot --------------------------------------------------------

    def take_shot(self):
        """Begin the sequence. Returns False if it did not start; the
        reason has already been sent through `message`."""
        if self._busy:
            self.message.emit("X-ray eye takeshot already in progress")
            return False

        # Checked before the shutter is touched: a running camera, or one
        # that cannot be read, means no shutter movement at all.
        acquiring = self.is_acquiring()
        if acquiring:
            self.message.emit(SKIP_ACQUIRING_MSG)
            return False
        if acquiring is None and not self._debug:
            self.message.emit("X-ray eye camera not answering (%s), skipping takeshot"
                              % EYE_ACQUIRE_RBV_PV)
            return False

        self._busy = True
        try:
            # Single, so a camera left in continuous by Start takes one
            # frame rather than running on after the shutter shuts.
            self._put(EYE_IMAGE_MODE_PV, IMAGE_MODE_SINGLE)
            # Treated as open before the put, so even a put that fails
            # partway is followed by a close.
            self._shutter_open = True
            self._put(FAST_SHUTTER_OPEN_PV, 1)
        except Exception as exc:
            self._abort("X-ray eye takeshot failed opening shutter: %s" % exc)
            return False
        self.message.emit("X-ray eye takeshot: shutter open")
        QTimer.singleShot(self.step_delay_ms, self._trigger)
        return True

    def _trigger(self):
        try:
            self._put(EYE_ACQUIRE_PV, 1)
        except Exception as exc:
            self._abort("X-ray eye takeshot failed to trigger: %s" % exc)
            return
        exposure = self.exposure_s()
        self._deadline = time.monotonic() + exposure + TIMEOUT_MARGIN_S
        self.message.emit("X-ray eye takeshot: exposing %g s" % exposure)
        # Acquire_RBV is not looked at before the exposure is over. Until
        # the server has published the camera going busy, it would still
        # read the 0 from before the trigger.
        QTimer.singleShot(int(round(exposure * 1000)), self._wait_for_idle)

    def _wait_for_idle(self):
        if self.is_acquiring():
            if time.monotonic() < self._deadline:
                QTimer.singleShot(POLL_MS, self._wait_for_idle)
                return
            self._finish_after_delay(
                "X-ray eye takeshot: camera still busy after timeout, shutter closed")
            return
        self._finish_after_delay("X-ray eye takeshot done")

    def _finish_after_delay(self, text):
        QTimer.singleShot(self.step_delay_ms, lambda: self._close(text))

    def _abort(self, text):
        """Something failed. Close at once if the shutter may be open."""
        if self._shutter_open:
            self._close(text)
        else:
            self._busy = False
            self.message.emit(text)

    def _close(self, text):
        try:
            self._put(FAST_SHUTTER_CLOSE_PV, 1)
        except Exception as exc:
            text = "X-ray eye takeshot: could not close fast shutter: %s" % exc
        else:
            self._shutter_open = False
        finally:
            self._busy = False
        self.message.emit(text)
