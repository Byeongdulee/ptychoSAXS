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

# --- optics -----------------------------------------------------------------
OPTICS = {
    "optics.motors":     "Every configured optics motor, in panel order.",
    "optics.positions":  "Current position of every optics motor.",
    "optics.position":   "Current position of one optics motor.",
    "optics.status":     "IN / OUT / BETWEEN per optics block, plus the ptycho verdict.",
    "optics.move":       "Move one optics motor to an absolute position.",
    "optics.move_rel":   "Move one optics motor by a relative step.",
    "optics.stop":       "Stop one optics motor, or all of them. Never queued.",
    "optics.set_position": "Redefine a motor's current position without moving.",
}

# --- raw records ------------------------------------------------------------
# For line edits bound directly to a field (.VAL, .TWV, the eye exposure).
PV = {
    "pv.subscribe":   "Start receiving these records in the pv.values event.",
    "pv.unsubscribe": "Stop receiving them; closes any nobody else wants.",
    "pv.get":         "Read one record now.",
    "pv.put":         "Write one record.",
}

# --- the CRL rig ------------------------------------------------------------
# A second EpicsMotorController-backed rig, addressed by axis name. Kept as
# its own family rather than folded into motion.* because it is a separate
# instrument on a separate IOC, opened only for the panel that drives it.
CRL = {
    "crl.axes":            "Every CRL axis: name, unit, connected.",
    "crl.positions":       "Current position of every CRL axis.",
    "crl.position":        "Current position of one CRL axis.",
    "crl.move":            "Move one CRL axis to an absolute position.",
    "crl.tweak":           "Step one CRL axis by its tweak value.",
    "crl.set_tweak_step":  "Set one CRL axis's tweak step.",
    "crl.stop":            "Stop one CRL axis, or all of them. Never queued.",
    "crl.get_speed":       "Read one CRL axis's velocity.",
    "crl.set_speed":       "Set one CRL axis's velocity.",
    "crl.set_position":    "Redefine a CRL axis's position without moving.",
}

# --- scans ------------------------------------------------------------------
# Long operations. start returns a job id immediately and progress is
# broadcast to every client, so a second panel can watch a scan the first
# one began.
SCAN = {
    "scan.plan":   "Point count and time estimate for a scan. Pure; runs nothing.",
    "scan.check":  "The guards that apply to a planned scan, as data.",
    "scan.start":  "Submit a scan. Returns a job id immediately.",
    "scan.stop":   "Ask a running step scan to stop. A fly scan cannot be asked.",
    "scan.active": "Scans currently running.",
}

ALL = {**SYSTEM, **MOTION, **OPTICS, **PV, **CRL, **SCAN}
NAMES = frozenset(ALL)

# Methods that may take longer than a few milliseconds and are therefore run
# as jobs rather than inline. Empty in Phase 1: everything here is a read.
# scan.start is submitted as a job rather than run inline, so it is not
# listed here -- it returns a job id in milliseconds like any other method.
BLOCKING = frozenset()

# Methods answerable before BeamlineSession has finished building devices.
# Everything else gets ERR_NOT_READY until then.
PRE_READY = frozenset({
    "system.hello", "system.ping", "system.status",
    "system.clients", "system.request_focus", "system.shutdown",
})

#: Methods that move hardware. Collected so a read-only client (the MCP
#: server by default) can be refused in one place rather than per method.
MUTATING = frozenset({
    "optics.move", "optics.move_rel", "optics.set_position", "pv.put",
    "crl.move", "crl.tweak", "crl.set_tweak_step", "crl.set_speed",
    "crl.set_position",
    "scan.start", "scan.stop",
})


def is_known(name: str) -> bool:
    return name in NAMES
