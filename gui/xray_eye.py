"""Shared X-ray eye state for every GUI that commands it.

Three separate GUIs drive the X-ray eye -- the main panel (rungui), the optics
panel (optics_motors, which runs in its own process), and the CRL panel -- and
each used to carry its own copy of the PV code. Two of those copies disagreed
about what the status PV meant, so the In/Out buttons could show opposite
states for the same hardware.

This module is the single implementation. State is shared through the command
record itself: every GUI writes ``usxRIO:Galil2Bo0_CMD``, so reading that
record's value back is how one process learns what another one did. No file,
no registry key, and it works across machines.

XrayEye takes its PV class as a constructor argument, so it has no dependency
on any particular GUI and works the same whether optics_motors was launched
from the main panel or on its own.
"""

# put(1) inserts the eye, put(0) retracts it; the record's value reads back.
EYE_CMD_PV = "usxRIO:Galil2Bo0_CMD"

# Separate status readback. 0 means the eye is OUT of the beam -- this matches
# optics_motors._is_xrayeye_out, the reading its move guard has always used.
# Kept because the status record still exists; XrayEye prefers the command
# readback, which reflects intent immediately rather than lagging the motion.
EYE_STATUS_PV = "usxRIO:Galil2Bo0_STATUS.VAL"


def eye_in_from_cmd(value):
    """True when the last command inserted the eye, None if unreadable."""
    if value is None:
        return None
    try:
        return int(value) != 0
    except (TypeError, ValueError):
        return None


def eye_is_out(status_value):
    """True when EYE_STATUS_PV says the eye is retracted."""
    return status_value == 0


class XrayEye:
    """Read and command the X-ray eye, with state shared between processes.

    `pv_factory` is called with a PV name and must return something with
    ``get()`` and ``put()`` -- ``epics.PV`` normally, ``debug_stubs.FakePV``
    in debug mode.

    Pass ``debug=True`` when the factory produces stubs. FakePV.get() always
    returns 0, so the readback cannot reflect a command; in that mode is_in()
    reports whatever this process last commanded, falling back to the stub's
    own 0 (eye out) before anything has been commanded. State is therefore
    per-process in debug -- the same limitation the ZP status pill documents.
    """

    def __init__(self, pv_factory, debug=False):
        self._pv_factory = pv_factory
        self._debug = bool(debug)
        self._pv_cache = {}
        self._commanded = None  # last command issued from this process

    def _pv(self, name):
        """Cached PV handle -- callers poll, and a fresh epics.PV per poll
        would open a new channel every tick."""
        pv = self._pv_cache.get(name)
        if pv is None:
            pv = self._pv_factory(name)
            self._pv_cache[name] = pv
        return pv

    def is_in(self):
        """True (inserted), False (retracted), or None when unknown."""
        if self._debug and self._commanded is not None:
            # A stub PV cannot remember a put, so once this process has
            # commanded the eye its own record is the only truth available.
            # Before that, fall through to the stub's 0 so the UI still has a
            # definite state (out) instead of leaving both buttons live.
            return self._commanded
        try:
            state = eye_in_from_cmd(self._pv(EYE_CMD_PV).get())
        except Exception:
            return self._commanded
        # An unreadable or malformed value falls back to what this process
        # last did, which is better than reporting "unknown" to the operator.
        return self._commanded if state is None else state

    def is_out(self):
        """The inverse of is_in(), preserving None for unknown."""
        state = self.is_in()
        return None if state is None else not state

    def set_in(self, inserted):
        """Command the eye. Records the intent first so a failed put still
        leaves is_in() reporting what was asked for."""
        self._commanded = bool(inserted)
        self._pv(EYE_CMD_PV).put(1 if inserted else 0)

    def read_status(self):
        """The separate status record, as True (out) / False (in) / None.

        Not used for the button state -- it lags the motion -- but useful for
        showing the hardware's own view alongside the commanded state.
        """
        try:
            return eye_is_out(self._pv(EYE_STATUS_PV).get())
        except Exception:
            return None
