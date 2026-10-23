"""One poller for the whole instrument.

This replaces every per-GUI hardware timer. The worst of them,
gui/optics_motors.py's updatepos, runs at 10 Hz on the GUI thread and reads
every motor in the panel plus every bound PV; with two panels open the IOC
served the same data twice. Here one thread reads and every client gets the
same conflated snapshot.

It must never block on a device lock. A direct-driver move holds the stage
lock for the length of one command, which is short, but a stage whose
controller has wedged would otherwise stall the readout for every connected
panel. A read that fails leaves the previous value standing.
"""

import threading

from ptychosaxs.link import config
from ptychosaxs.api.events import (
    MotionPositions, OpticsPositions, OpticsStatus, PvValues,
)


class Publisher:
    #: Publish optics classification every Nth tick. At 10 Hz that is twice
    #: a second, which is faster than anyone moves a beamstop by hand.
    STATUS_EVERY = 5

    def __init__(self, session, log, hz=None):
        self._session = session
        self._log = log
        self._period = 1.0 / (hz or config.PUBLISH_HZ)
        self._stop = threading.Event()
        self._thread = None
        self._status_countdown = 0

    def start(self):
        self._thread = threading.Thread(target=self._loop, name="ptys-publish",
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.wait(self._period):
            try:
                self._tick()
            except Exception:
                # A publisher that dies takes every panel's readout with it.
                self._log.exception("publisher tick failed")

    def _tick(self):
        self._publish_motion()
        self._publish_optics()
        self._publish_pvs()

    def _publish_motion(self):
        devices = self._session.devices
        if devices.motion is None:
            return
        positions = devices.positions()
        if not positions:
            return
        self._session.bus.publish(MotionPositions(
            positions=positions,
            moving=devices.moving(),
            connected={a.name: a.connected for a in devices.axes()},
        ))

    def _publish_optics(self):
        if self._session.devices.optics is None:
            return
        self._session.bus.publish(
            OpticsPositions(positions=self._session.optics.positions()))

        # Classification is comparatively expensive -- it re-reads the ini
        # and compares every block -- and it changes far more slowly than a
        # position does, so it goes out at a fraction of the rate.
        self._status_countdown -= 1
        if self._status_countdown <= 0:
            self._status_countdown = self.STATUS_EVERY
            status = self._session.optics.status()
            self._session.bus.publish(OpticsStatus(
                blocks=status["blocks"],
                good_for_ptycho=status["good_for_ptycho"],
                problems=tuple(status["problems"]),
            ))

    def _publish_pvs(self):
        values = self._session.pvs.snapshot()
        if values:
            self._session.bus.publish(PvValues(values=values))
