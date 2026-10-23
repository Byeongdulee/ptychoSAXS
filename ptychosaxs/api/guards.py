"""Questions that must be answered before a scan may start.

A guard is the *decision* to ask, separated from the asking. The check
functions here are pure -- give them numbers and they return a Guard or
None -- and the client renders whichever Guard comes back as the dialog it
always was. That split is what lets a scan be started by something with no
screen: a macro, the CLI, an MCP tool. Those install an answer policy
instead of a dialog, which generalises the special case the GUI already
had, where _confirm_large_scan skipped its dialog when macro_running was
set.

The safety limits live here too. MAX_VELOCITY and MIN_VELOCITY were class
attributes on a Qt handler, which meant the numbers that stop the hexapod
being commanded past what it will accept could only be read by importing a
GUI.
"""

from dataclasses import dataclass, field

# --- policy -----------------------------------------------------------------

#: Confirm before starting a scan larger than this many positions.
LARGE_SCAN_THRESHOLD = 250

#: Maximum fly velocity per axis, in that axis's own units -- mm/s for the
#: hexapod, deg/s for phi. Exceeding the controller's own limit makes it
#: reject the move outright, mid-scan, so this is checked before anything
#: starts moving.
MAX_VELOCITY = {
    "X": 0.2,
    "Z": 0.2,
    "phi": 37.0,
}

#: Minimum fly velocity per axis, same units and keys.
MIN_VELOCITY = {
    "X": 0.00024,
    "Z": 0.00024,
    "phi": 0.01,
}

CONFIRM = "confirm"   # a question: proceeding is one of the answers
WARN = "warn"         # a refusal: there is nothing to agree to


@dataclass(frozen=True)
class Guard:
    """One thing a human would have to be told, or asked, before a scan."""

    key: str                       # stable id, for auto-answer policies
    kind: str                      # CONFIRM | WARN
    title: str
    text: str
    choices: tuple = ("OK",)
    default: str = "OK"
    proceed: str = ""              # the choice that means "go ahead"

    def allows(self, choice) -> bool:
        """Whether `choice` means the scan may start."""
        return bool(self.proceed) and choice == self.proceed

    @property
    def blocks(self) -> bool:
        """A WARN has no proceeding answer; it always stops the scan."""
        return not self.proceed

    def describe(self) -> dict:
        return {"key": self.key, "kind": self.kind, "title": self.title,
                "text": self.text, "choices": list(self.choices),
                "default": self.default, "proceed": self.proceed}


def _confirm(key, title, text):
    return Guard(key=key, kind=CONFIRM, title=title, text=text,
                 choices=("Proceed", "Cancel"), default="Cancel",
                 proceed="Proceed")


def _warn(key, title, text):
    return Guard(key=key, kind=WARN, title=title, text=text,
                 choices=("OK",), default="OK", proceed="")


# --- the checks -------------------------------------------------------------

def large_scan(n_positions, per_point_s, overhead_s,
               threshold=LARGE_SCAN_THRESHOLD, unattended=False):
    """Confirm a long scan before committing the beam time to it."""
    n_positions = int(round(n_positions))
    if n_positions <= threshold or unattended:
        return None
    from ptychosaxs.api.scanplan import format_duration
    estimate = n_positions * (per_point_s + overhead_s)
    return _confirm(
        "large_scan", "Large Scan",
        f"This scan has {n_positions} positions.\n"
        f"Estimated time: {format_duration(estimate)}\n\n"
        "Proceed with scan?")


def multi_pulse_needs_hdf(pulses_per_step, hdf_plugin_enabled):
    """Several frames per position have to be aggregated into one file."""
    if pulses_per_step > 1 and not hdf_plugin_enabled:
        return _warn(
            "multi_pulse_needs_hdf", "Check HDF Plugin",
            f"Pulses per step is set to {pulses_per_step}.\n"
            "HDF5 plugin must be enabled for multi-pulse per step scans.")
    return None


def saxs_detector_mode(mode):
    if mode != "scan":
        return _warn("saxs_det_mode", "Check SAXS Detector",
                     "Set SAXS detector to scan mode")
    return None


def velocity_limits(velocities, maximums=None, minimums=None):
    """Refuse a fly scan the stage would reject once it was underway.

    Checked before anything moves, because the controller rejecting a move
    command mid-scan leaves a half-collected dataset and a stage somewhere
    nobody chose.
    """
    maximums = MAX_VELOCITY if maximums is None else maximums
    minimums = MIN_VELOCITY if minimums is None else minimums
    for axis, vel in velocities.items():
        speed = abs(vel)
        ceiling = maximums.get(axis)
        floor = minimums.get(axis)
        if ceiling is not None and speed > ceiling:
            return _warn(
                "velocity_too_high", "Velocity Limit Exceeded",
                f"{axis} velocity would be {speed:.4f}, exceeding the "
                f"configured maximum of {ceiling:.4f}.\n"
                "Reduce the scan range or increase the exposure time.")
        if floor is not None and speed < floor:
            return _warn(
                "velocity_too_low", "Velocity Limit Exceeded",
                f"{axis} velocity would be {speed:.4f}, below the "
                f"configured minimum of {floor:.4f}.\n"
                "Increase the scan range or reduce the exposure time.")
    return None


# --- answering them unattended ----------------------------------------------

@dataclass
class AnswerPolicy:
    """How a client with no screen answers guards.

    `proceed_through` names the guard keys it is willing to agree to.
    Anything else gets its default, which for a CONFIRM is Cancel -- so the
    safe behaviour is the one you get by saying nothing.
    """

    proceed_through: frozenset = field(default_factory=frozenset)
    log=None

    def answer(self, guard: Guard) -> str:
        if guard.key in self.proceed_through and guard.proceed:
            choice = guard.proceed
        else:
            choice = guard.default
        if self.log:
            self.log("guard %r auto-answered %r: %s",
                     guard.key, choice, guard.title)
        return choice


#: What a macro run answers: it may agree to a long scan, because running a
#: lot of scans unattended is the point of it, but it may not wave away a
#: warning about the detector or the stage.
MACRO_POLICY = AnswerPolicy(proceed_through=frozenset({"large_scan"}))

#: The default for anything else headless: agree to nothing.
STRICT_POLICY = AnswerPolicy()
