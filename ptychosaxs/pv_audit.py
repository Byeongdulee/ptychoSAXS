"""
The beamline data PVs, and which of them this GUI writes.

Opening the GUI used to increment the scan number and push it to the IOC
before anyone had agreed to it, which is how a fresh launch could silently
renumber someone else's run. This module is the inventory behind the
consent prompts in gui/pv_consent.py: what exists, what this program
touches, and when.

Data only - no Qt, no pyepics - so the inventory can be asserted against
the code in a test rather than drifting from it.
"""

WRITES = "writes"        # this GUI puts to it
READS = "reads"          # this GUI only cagets it
UNTOUCHED = "untouched"  # named for completeness; nothing here touches it

BEAMLINE_PREFIX = "12idc:"

# Everything under 12idc:data: that the beamline keeps run metadata in. The
# access column is this codebase's, established by grepping for each name -
# "untouched" means some *other* program writes it, not that it is unused.
DATA_PVS = [
    ("UserName", UNTOUCHED, ""),
    ("GUPNumber", UNTOUCHED, ""),
    ("ESAFNumber", UNTOUCHED, ""),
    ("absIntScaleFactor", UNTOUCHED, ""),
    ("absIntStandard", UNTOUCHED, ""),
    ("QRcode", UNTOUCHED, ""),
    ("comment", UNTOUCHED, ""),
    ("userDir", READS,
     "read by tools/detectors.py to find the Pilatus file-writer directory. "
     "tools/files.py:folder_ready() would write it, but nothing calls it."),
    ("specFile", UNTOUCHED, ""),
    ("fileIndex", WRITES,
     "the scan number. The only one of these this GUI writes."),
    ("fileIndexExt", UNTOUCHED, ""),
    ("SampleThickness", UNTOUCHED, ""),
    ("SampleName", UNTOUCHED, ""),
    ("SampleTemperature", UNTOUCHED, ""),
    ("heater", UNTOUCHED, ""),
    ("phdName", UNTOUCHED, ""),
]

SCAN_NUMBER_PV = BEAMLINE_PREFIX + "data:fileIndex"

# Every path that writes SCAN_NUMBER_PV. Both are the start of a scan, and
# that is the whole list on purpose: the PV should name the scan that is
# running, so startup, scan completion and the +/- buttons leave it alone.
WRITE_SITES = [
    ("Scan start", "scan_handler.run_stop_issued",
     "every scan: 1-D fly, step scans, and the 2-D fly executors."),
    ("3-D slice start", "scan_handler._advance_scan_number_safe",
     "once per phi slice, before any frames are taken for it."),
]

# Things that move the scan number without writing the PV, listed so the
# absence is deliberate rather than an oversight.
LOCAL_ONLY = [
    ("GUI startup", "rungui.ptyco_main_control.__init__",
     "reads fileIndex and adopts it as this session's scan number, or "
     "advances the ini value when the PV cannot be read. Never writes."),
    ("Scan finished", "scan_handler.update_scannumber via scandone",
     "refreshes the widgets only."),
    ("Scan number +/- buttons", "rungui._scan_number_plus_one / _minus_one",
     "local bookkeeping; the next scan start publishes the result."),
]


def startup_scan_number(ioc_value, ini_value) -> int:
    """Which scan number a fresh session should start from.

    The IOC's fileIndex is the beamline's own record of the last index
    used, so adopting it keeps this GUI in step with anything else
    numbering scans - including a second copy of this GUI.

    When it cannot be read, fall back to the ini and advance. That +1
    matters: without it a relaunch would reuse the number the previous
    session had already written files under.
    """
    if ioc_value is None:
        return int(ini_value) + 1
    return int(ioc_value)


def pvname(suffix) -> str:
    return f"{BEAMLINE_PREFIX}data:{suffix}"


def by_access(access) -> list:
    return [pvname(s) for s, a, _why in DATA_PVS if a == access]


def written_pvs() -> list:
    return by_access(WRITES)


def consent_summary() -> str:
    """The one line the startup prompt needs: the PV this GUI can write."""
    return SCAN_NUMBER_PV
