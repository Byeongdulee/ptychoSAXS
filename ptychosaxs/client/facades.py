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


ALL_FACADES = (SystemFacade, MotionFacade)


def facade_methods():
    out = set()
    for cls in ALL_FACADES:
        out.update(cls.METHODS)
    return out
