"""Compatibility shim: optics status classification moved into the API.

It was always Qt-free and hardware-free; it now lives in
ptychosaxs/api/optics_status.py because the server classifies optics state
too, and a device-layer module importing from gui/ is backwards.

Kept for one phase so the panels and tests that import
`gui.optics_status` keep working while they are migrated.
"""

from ptychosaxs.api.optics_status import *  # noqa: F401,F403
from ptychosaxs.api.optics_status import (  # noqa: F401
    BETWEEN, IN, OUT, UNDEFINED,
    MOTORS, PV_BASES, OPTICS_INI_PATH, US_OPTICS_MOTORS,
    BlockStatus, OpticsBlockSpec, OpticsStatus,
    check_good_for_ptycho, classify_block, classify_optic,
    read_definitions, read_live_positions, read_status,
)
