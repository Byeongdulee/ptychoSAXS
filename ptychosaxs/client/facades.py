"""Typed call sites, so a GUI never spells a method name by hand.

Thin on purpose: each one is a single call(). What they buy is that
tests/test_api_contract.py can assert every facade method maps to a name in
ptychosaxs.link.methods and that every name has a facade -- so a typo is a
failing test rather than an ERR_UNKNOWN_METHOD at the beamline.
"""


class _Facade:
    #: filled in by subclasses; checked by the contract test
    METHODS = ()

    def __init__(self, client):
        self._c = client


class SystemFacade(_Facade):
    METHODS = ("system.status", "system.clients", "system.ping",
               "system.request_focus", "system.shutdown")

    def status(self):
        return self._c.call("system.status")

    def clients(self):
        return self._c.call("system.clients")["clients"]

    def ping(self):
        return self._c.call("system.ping")

    def request_focus(self, gui_type):
        return self._c.call("system.request_focus", {"gui_type": gui_type})

    def shutdown(self):
        return self._c.call("system.shutdown")


class MotionFacade(_Facade):
    METHODS = ("motion.axes", "motion.positions", "motion.position")

    def axes(self):
        return self._c.call("motion.axes")["axes"]

    def positions(self):
        return self._c.call("motion.positions")["positions"]

    def position(self, axis):
        return self._c.call("motion.position", {"axis": axis})["position"]


class OpticsFacade(_Facade):
    METHODS = ("optics.motors", "optics.positions", "optics.position",
               "optics.status", "optics.move", "optics.move_rel",
               "optics.stop", "optics.set_position")

    def motors(self):
        return self._c.call("optics.motors")["motors"]

    def positions(self):
        return self._c.call("optics.positions")["positions"]

    def position(self, motor):
        return self._c.call("optics.position", {"motor": motor})["position"]

    def status(self):
        return self._c.call("optics.status")

    def move(self, motor, value, wait=False):
        return self._c.call("optics.move",
                            {"motor": motor, "value": value, "wait": wait})

    def move_rel(self, motor, delta, wait=False):
        return self._c.call("optics.move_rel",
                            {"motor": motor, "delta": delta, "wait": wait})

    def stop(self, motor=None):
        # Short timeout: Stop that hangs is Stop that does not work.
        return self._c.call("optics.stop", {"motor": motor}, timeout_s=5.0)

    def set_position(self, motor, value=0.0):
        return self._c.call("optics.set_position",
                            {"motor": motor, "value": value})


class PvFacade(_Facade):
    """Records a panel binds a line edit straight to."""

    METHODS = ("pv.subscribe", "pv.unsubscribe", "pv.get", "pv.put")

    def subscribe(self, names):
        return self._c.call("pv.subscribe", {"names": list(names)})["values"]

    def unsubscribe(self, names=None):
        return self._c.call("pv.unsubscribe", {"names": names})

    def get(self, name):
        return self._c.call("pv.get", {"name": name})["value"]

    def put(self, name, value):
        return self._c.call("pv.put", {"name": name, "value": value})


class CrlFacade(_Facade):
    METHODS = ("crl.axes", "crl.positions", "crl.position", "crl.move",
               "crl.tweak", "crl.set_tweak_step", "crl.stop",
               "crl.get_speed", "crl.set_speed", "crl.set_position")

    def axes(self):
        return self._c.call("crl.axes")["axes"]

    def positions(self):
        return self._c.call("crl.positions")["positions"]

    def position(self, axis):
        return self._c.call("crl.position", {"axis": axis})["position"]

    def move(self, axis, target, wait=False):
        return self._c.call("crl.move",
                            {"axis": axis, "target": target, "wait": wait})

    def tweak(self, axis, step=0.0, forward=True):
        return self._c.call("crl.tweak",
                            {"axis": axis, "step": step, "forward": forward})

    def set_tweak_step(self, axis, step):
        return self._c.call("crl.set_tweak_step", {"axis": axis, "step": step})

    def stop(self, axis=None):
        return self._c.call("crl.stop", {"axis": axis}, timeout_s=5.0)

    def get_speed(self, axis):
        return self._c.call("crl.get_speed", {"axis": axis})["speed"]

    def set_speed(self, axis, velocity):
        return self._c.call("crl.set_speed",
                            {"axis": axis, "velocity": velocity})

    def set_position(self, axis, value=0.0):
        return self._c.call("crl.set_position", {"axis": axis, "value": value})


ALL_FACADES = (SystemFacade, MotionFacade, OpticsFacade, PvFacade, CrlFacade)


def facade_methods():
    out = set()
    for cls in ALL_FACADES:
        out.update(cls.METHODS)
    return out
