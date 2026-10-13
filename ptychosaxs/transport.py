"""
Which driver each stage is built on.

Only the hexapod is genuinely reachable two ways: the same GCS protocol
either travels over TCP to the C-887, or the stage is driven through the
12idbHXP motor records instead. That choice is a *transport*, and it is the
hexapod's alone.

Everything else simply has one driver or another, with no shared protocol
underneath and nothing being tunnelled:

  phi    acspy, straight at the ACS controller. There is no IOC for the
         tomography rotation stage, so there is nothing to choose.
  gonio  the 12ideMCS1 motor records, or the SmarAct MCS2 SDK through
         ptychosaxs/smaract_gonio.py. One boolean, and EPICS is not
         involved at all when the SDK is selected.

Whatever is asked for, a stage whose driver cannot be built here says so
and falls back to a simulated stand-in - a machine that can reach only some
of the hardware should still bring up the GUI and drive the rest.

Policy only. The objects are built in motions_ver2, where the hardware
imports live; nothing here imports Qt, pyepics or any vendor library.
"""

import os

EPICS = "epics"
DIRECT = "direct"
SIMULATED = "simulated"

STAGES = ("hexapod", "phi", "gonio")

# What each stage can be built as, for the builders and their tests to agree
# on. Add to a tuple here when a driver becomes real.
CHOICES = {
    "hexapod": (EPICS, DIRECT),
    "phi": (DIRECT,),
    "gonio": (EPICS, DIRECT),
}

# rungui exports its two switches here. Environment variables rather than
# imports, for the same reason the DEBUG_* flags use them: motions_ver2 is
# imported long before anything holds a reference to rungui's globals.
ENV_HEXAPOD_TRANSPORT = "PTYCHOSAXS_HEXAPOD_TRANSPORT"
ENV_GONIO_SMARACT = "PTYCHOSAXS_GONIO_SMARACT"

DEFAULT_HEXAPOD_TRANSPORT = EPICS

DESCRIPTION = {
    ("hexapod", EPICS): "the 12idbHXP motor records",
    ("hexapod", DIRECT): "pihexapod/pipython over TCP to the C-887",
    ("phi", DIRECT): "acspy over ethernet to the ACS controller",
    ("gonio", EPICS): "the 12ideMCS1 motor records",
    ("gonio", DIRECT): "the SmarAct MCS2 SDK (ptychosaxs.smaract_gonio)",
}


class TransportUnavailable(RuntimeError):
    """A stage's chosen driver cannot be built on this machine.

    Raised by whichever builder tried, caught by
    motions_ver2.build_controller, which substitutes a simulated stand-in
    and explains why. Defined here so both sides share one class: if the
    raise and the except are different classes, one stage losing its driver
    takes the GUI down with it.
    """


def _truthy(value) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def resolve(stage, simulated=False, env=None):
    """(driver, reason) - what this stage should actually be built as.

    `reason` is empty when the request was honoured, and otherwise explains
    the substitution in terms someone at the beamline can act on. It never
    raises: a mistyped switch should cost one stage, not the whole GUI.
    """
    env = os.environ if env is None else env

    if stage not in CHOICES:
        return SIMULATED, f"{stage!r} is not a known stage"
    if simulated:
        return SIMULATED, ""

    if stage == "hexapod":
        want = env.get(ENV_HEXAPOD_TRANSPORT) or DEFAULT_HEXAPOD_TRANSPORT
        if want in CHOICES["hexapod"] or want == SIMULATED:
            return want, ""
        return SIMULATED, (
            f"{want!r} is not a hexapod transport - use {EPICS!r} or "
            f"{DIRECT!r}"
        )

    if stage == "gonio":
        return (DIRECT if _truthy(env.get(ENV_GONIO_SMARACT, "0")) else EPICS), ""

    # phi has one driver and no switch.
    return DIRECT, ""


def describe(stage, driver) -> str:
    """One phrase naming what a driver talks to, for logs and dialogs."""
    if driver == SIMULATED:
        return "a simulated stand-in"
    return DESCRIPTION.get((stage, driver), driver)
