"""The canonical list of methods the server exposes.

One place both ends agree on. The server asserts that every @method it
registers is named here; the client asserts that every facade call maps to a
name here; tests/test_api_contract.py asserts the two sets are equal. Without
this, a typo in a facade is a runtime ERR_UNKNOWN_METHOD on a beamline at 2am
instead of a failing test.

This is the same discipline tests/test_transport.py already applies to the
driver tables: a stage in CHOICES with no builder is "a switch with nothing
behind it", and a method in one list but not the other is the same bug.

BLOCKING marks the methods that are wrapped as jobs. Everything else must
return in milliseconds, because it runs inline on the connection's reader
thread -- see ptychosaxs/server/dispatch.py.
"""

# --- system -----------------------------------------------------------------
SYSTEM = {
    "system.hello":          "Handshake. Must be the first frame on a connection.",
    "system.ping":           "Liveness probe. Answers even before devices are ready.",
    "system.status":         "Server pid, uptime, readiness, simulation state, clients, jobs.",
    "system.clients":        "Who is currently connected.",
    "system.request_focus":  "Ask the GUI holding a slot to raise its window.",
    "system.shutdown":       "Ask the server to exit. Refused while a fly scan is running.",
}

# --- motion (read-only in Phase 1) ------------------------------------------
MOTION = {
    "motion.axes":       "Every configured axis: name, unit, controller, connected.",
    "motion.positions":  "Current position of every axis, from the monitor cache.",
    "motion.position":   "Current position of one axis.",
}

ALL = {**SYSTEM, **MOTION}
NAMES = frozenset(ALL)

# Methods that may take longer than a few milliseconds and are therefore run
# as jobs rather than inline. Empty in Phase 1: everything here is a read.
BLOCKING = frozenset()

# Methods answerable before BeamlineSession has finished building devices.
# Everything else gets ERR_NOT_READY until then.
PRE_READY = frozenset({
    "system.hello", "system.ping", "system.status",
    "system.clients", "system.request_focus", "system.shutdown",
})


def is_known(name: str) -> bool:
    return name in NAMES
