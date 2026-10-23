"""The hardware exception types scan execution catches.

One definition, imported by everyone. These have debug-mode stand-ins --
when a device is simulated its real module is never imported, so the
exception class it would have provided has to come from somewhere -- and
that made them easy to define twice. Two modules each building their own
`class DG645_Error(Exception)` produces two unrelated classes, and an
`except DG645_Error` in one then silently fails to catch the one raised
through the other. Nothing would look wrong until a delay generator
faulted mid-scan and the handler that was supposed to catch it did not.

Which flavour you get is decided here, once, from the same environment
flags the device layer reads.
"""

import os

_DEBUG_DEVICES = (os.environ.get("PTYCHOSAXS_DEBUG_EPICS") == "1"
                  or os.environ.get("PTYCHOSAXS_DEBUG_DEVICES") == "1")

# tools/dg645.py is the only module here that needs InstrumentKit, so it
# has a flag of its own -- that is what lets the GUI run with real
# detectors and softglue in an environment without that package.
_DEBUG_DG645 = _DEBUG_DEVICES or os.environ.get("PTYCHOSAXS_DEBUG_DG645") == "1"

if _DEBUG_DEVICES:
    class DET_MIN_READOUT_Error(Exception):
        """Simulated stand-in: the detector cannot read out that fast."""

    class DET_OVER_READOUT_SPEED_Error(Exception):
        """Simulated stand-in: requested period is under the readout floor."""

    class SOFTGLUE_Setup_Error(Exception):
        """Simulated stand-in: softglue could not be configured."""
else:
    from ptychosaxs.tools.detectors import (  # noqa: F401
        DET_MIN_READOUT_Error,
        DET_OVER_READOUT_SPEED_Error,
    )
    from ptychosaxs.tools.softglue import SOFTGLUE_Setup_Error  # noqa: F401

if _DEBUG_DG645:
    class DG645_Error(Exception):
        """Simulated stand-in: the delay generator refused a command."""
else:
    from ptychosaxs.tools.dg645 import DG645_Error  # noqa: F401
