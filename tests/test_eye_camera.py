"""X-ray eye takeshot / Start / Stop sequencing (gui/optics/eye_camera.py).

Driven against in-memory records -- no EPICS, no server. QTimer needs only
a QCoreApplication, so this runs headlessly.
"""

import time

import pytest
from PyQt5.QtCore import QCoreApplication, QEventLoop, QTimer

from gui.optics import eye_camera
from gui.optics.eye_camera import SKIP_ACQUIRING_MSG, EyeCamera
from gui.optics.xray_eye import (
    EYE_ACQUIRE_PV, EYE_ACQUIRE_RBV_PV, EYE_EXPOSURE_RBV_PV, EYE_IMAGE_MODE_PV,
    FAST_SHUTTER_CLOSE_PV, FAST_SHUTTER_OPEN_PV, IMAGE_MODE_CONTINUOUS,
    IMAGE_MODE_SINGLE, TAKESHOT_STEP_DELAY_MS,
)


@pytest.fixture(scope="module", autouse=True)
def app():
    yield QCoreApplication.instance() or QCoreApplication([])


class Records:
    """PV factory recording every put, with a timestamp."""

    def __init__(self, values=None, fail_on=()):
        self.values = dict(values or {})
        self.puts = []
        self.fail_on = set(fail_on)

    def __call__(self, name):
        records = self

        class _PV:
            def get(self):
                return records.values.get(name)

            def put(self, value):
                if name in records.fail_on:
                    raise RuntimeError("%s is not connected" % name)
                records.puts.append((name, value, time.monotonic()))
                records.values[name] = value
        return _PV()

    def names(self):
        return [p[0] for p in self.puts]


def run_until(predicate, timeout=5.0):
    loop = QEventLoop()
    deadline = time.monotonic() + timeout

    def tick():
        if predicate() or time.monotonic() > deadline:
            loop.quit()
    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(5)
    loop.exec_()
    timer.stop()
    return predicate()


def make(records, debug=False, delay=TAKESHOT_STEP_DELAY_MS):
    cam = EyeCamera(records, debug=debug, step_delay_ms=delay)
    messages = []
    cam.message.connect(messages.append)
    return cam, messages


def test_takeshot_sequence_and_delays():
    rec = Records({EYE_ACQUIRE_RBV_PV: 0, EYE_EXPOSURE_RBV_PV: 0.0})
    cam, messages = make(rec)
    assert cam.take_shot()
    assert run_until(lambda: not cam.busy)
    assert rec.names() == [EYE_IMAGE_MODE_PV, FAST_SHUTTER_OPEN_PV,
                           EYE_ACQUIRE_PV, FAST_SHUTTER_CLOSE_PV]
    assert rec.puts[0][1] == IMAGE_MODE_SINGLE
    t_open, t_trig, t_close = (p[2] for p in rec.puts[1:])
    # Each step at least the configured delay after the previous one.
    assert t_trig - t_open >= 0.09
    assert t_close - t_trig >= 0.09
    assert messages[-1] == "X-ray eye takeshot done"


def test_shutter_stays_open_until_camera_reports_idle():
    rec = Records({EYE_ACQUIRE_RBV_PV: 0, EYE_EXPOSURE_RBV_PV: 0.05})
    cam, _ = make(rec, delay=10)
    cam.take_shot()
    # Simulate the camera going busy once triggered, idle 300 ms later.
    assert run_until(lambda: EYE_ACQUIRE_PV in rec.names())
    rec.values[EYE_ACQUIRE_RBV_PV] = 1
    t_trig = time.monotonic()
    QTimer.singleShot(300, lambda: rec.values.__setitem__(EYE_ACQUIRE_RBV_PV, 0))
    assert run_until(lambda: not cam.busy)
    t_close = rec.puts[-1][2]
    assert rec.puts[-1][0] == FAST_SHUTTER_CLOSE_PV
    assert t_close - t_trig >= 0.3


def test_acquiring_camera_skips_without_touching_shutter():
    rec = Records({EYE_ACQUIRE_RBV_PV: 1})
    cam, messages = make(rec)
    assert not cam.take_shot()
    assert rec.puts == []
    assert messages == [SKIP_ACQUIRING_MSG]


def test_unreadable_camera_skips_outside_debug_only():
    rec = Records()  # Acquire_RBV reads None
    cam, messages = make(rec)
    assert not cam.take_shot()
    assert rec.puts == []
    assert "not answering" in messages[-1]

    cam, _ = make(rec, debug=True, delay=10)
    assert cam.take_shot()
    assert run_until(lambda: not cam.busy)
    assert rec.names()[-1] == FAST_SHUTTER_CLOSE_PV


def test_second_press_while_running_is_ignored():
    rec = Records({EYE_ACQUIRE_RBV_PV: 0})
    cam, messages = make(rec, delay=50)
    assert cam.take_shot()
    assert not cam.take_shot()
    assert "already in progress" in messages[-1]
    assert run_until(lambda: not cam.busy)
    assert rec.names().count(FAST_SHUTTER_OPEN_PV) == 1


def test_failed_trigger_still_closes_shutter():
    rec = Records({EYE_ACQUIRE_RBV_PV: 0}, fail_on={EYE_ACQUIRE_PV})
    cam, messages = make(rec, delay=10)
    cam.take_shot()
    assert run_until(lambda: not cam.busy)
    assert rec.names() == [EYE_IMAGE_MODE_PV, FAST_SHUTTER_OPEN_PV,
                           FAST_SHUTTER_CLOSE_PV]
    assert "failed to trigger" in messages[-1]


def test_failed_shutter_open_still_sends_close():
    rec = Records({EYE_ACQUIRE_RBV_PV: 0}, fail_on={FAST_SHUTTER_OPEN_PV})
    cam, _ = make(rec, delay=10)
    assert not cam.take_shot()
    assert rec.names() == [EYE_IMAGE_MODE_PV, FAST_SHUTTER_CLOSE_PV]
    assert not cam.busy


def test_stuck_camera_times_out_and_closes(monkeypatch):
    monkeypatch.setattr(eye_camera, "TIMEOUT_MARGIN_S", 0.2)
    rec = Records({EYE_ACQUIRE_RBV_PV: 0})
    cam, messages = make(rec, delay=10)
    cam.take_shot()
    assert run_until(lambda: EYE_ACQUIRE_PV in rec.names())
    rec.values[EYE_ACQUIRE_RBV_PV] = 1  # never goes idle
    assert run_until(lambda: not cam.busy)
    assert rec.names()[-1] == FAST_SHUTTER_CLOSE_PV
    assert "timeout" in messages[-1]


def test_start_and_stop():
    rec = Records()
    cam, _ = make(rec)
    assert cam.start()
    assert rec.puts[0][:2] == (EYE_IMAGE_MODE_PV, IMAGE_MODE_CONTINUOUS)
    assert rec.puts[1][:2] == (EYE_ACQUIRE_PV, 1)
    assert cam.stop()
    assert rec.puts[2][:2] == (EYE_ACQUIRE_PV, 0)
    assert FAST_SHUTTER_OPEN_PV not in rec.names()
