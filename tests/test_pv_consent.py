"""
pytest suite for the scan-number PV write policy.

Opening the GUI increments the scan number and pushes it to
12idc:data:fileIndex. On a shared IOC that renumbers whatever the beamline
was doing. A startup prompt used to gate this; it was removed by request, so
what is pinned here now is that writes are unconditional and that *nothing*
is asked - a dialog creeping back in is the regression worth catching.

The pv_audit inventory of what this GUI touches outlived the prompt, and the
bulk of this file exercises it.

The Qt dialog hook is injected, so none of this needs a QApplication.
"""
from gui import pv_consent
from ptychosaxs import pv_audit


class _Asker:
    """Stands in for the Qt dialog, recording what it was shown."""

    def __init__(self, outcome=pv_consent.ALLOW):
        self.outcome = outcome
        self.prompts = []

    def ask(self, parent, title, text):
        self.prompts.append(text)
        return self.outcome


def _consent(**kw):
    asker = _Asker(**kw)
    return pv_consent.PvWriteConsent(ask=asker.ask), asker


# ---------------------------------------------------------------------------
# The inventory the prompt is built from
# ---------------------------------------------------------------------------

def test_file_index_is_the_only_pv_this_gui_writes():
    assert pv_audit.written_pvs() == ["12idc:data:fileIndex"]


def test_user_dir_is_read_but_never_written():
    assert "12idc:data:userDir" in pv_audit.by_access(pv_audit.READS)
    assert "12idc:data:userDir" not in pv_audit.written_pvs()


def test_the_summary_is_just_the_pv_name():
    """The prompt names one PV and nothing else - the inventory of what is
    read or untouched was noise in a dialog people have to read."""
    assert pv_audit.consent_summary() == "12idc:data:fileIndex"


def test_every_pv_the_user_listed_is_still_accounted_for():
    """The inventory stays, even though the prompt no longer recites it."""
    listed = {
        "UserName", "GUPNumber", "ESAFNumber", "absIntScaleFactor",
        "absIntStandard", "QRcode", "comment", "userDir", "specFile",
        "fileIndex", "fileIndexExt", "SampleThickness", "SampleName",
        "SampleTemperature", "heater", "phdName",
    }
    assert {suffix for suffix, _a, _w in pv_audit.DATA_PVS} == listed


# ---------------------------------------------------------------------------
# Nothing is asked, and writes are always allowed
# ---------------------------------------------------------------------------

def test_writes_are_granted_before_anything_is_requested():
    """rungui reads `granted` from worker threads that never call request();
    it has to be true from construction, not merely after a request."""
    consent, _asker = _consent()
    assert consent.granted is True
    assert consent.asked is True


def test_no_dialog_is_ever_shown():
    """The regression this file exists to catch: a prompt creeping back."""
    consent, asker = _consent()
    consent.request()
    consent.request(reason="the configuration changed")
    assert asker.prompts == []


def test_requesting_always_allows():
    consent, _asker = _consent()
    assert consent.request() == pv_consent.ALLOW
    assert consent.request() == pv_consent.ALLOW
    assert consent.granted is True


def test_closed_is_never_returned():
    """rungui exits the process on CLOSED, so this path must stay dead even
    though the branch is still in place."""
    consent, _asker = _consent(outcome=pv_consent.CLOSED)
    assert consent.request() != pv_consent.CLOSED


def test_a_configuration_change_does_not_revoke():
    consent, asker = _consent()
    consent.revoke()
    assert consent.granted is True
    assert consent.request() == pv_consent.ALLOW
    assert asker.prompts == []


# ---------------------------------------------------------------------------
# What the answer governs
# ---------------------------------------------------------------------------

def test_there_is_no_per_write_dialog():
    """One prompt at startup is the whole interaction; a dialog per write
    was removed, and scans could not show one anyway."""
    assert not hasattr(pv_consent.PvWriteConsent, "confirm_scan_number")


def test_the_pv_is_written_only_at_the_start_of_a_scan():
    sites = {name for name, _fn, _why in pv_audit.WRITE_SITES}
    assert sites == {"Scan start", "3-D slice start"}


# ---------------------------------------------------------------------------
# Where a fresh session's scan number comes from
# ---------------------------------------------------------------------------

def test_the_ioc_value_is_adopted_as_is():
    """fileIndex is the beamline's record of the last index used, so a
    fresh session continues from it rather than from its own ini."""
    assert pv_audit.startup_scan_number(ioc_value=42, ini_value=7) == 42


def test_a_zero_from_the_ioc_is_still_adopted():
    """0 is a real index; treating it as 'unreadable' would silently fall
    back to the ini."""
    assert pv_audit.startup_scan_number(ioc_value=0, ini_value=7) == 0


def test_an_unreadable_pv_falls_back_to_the_ini_and_advances():
    """The +1 is the point: without it a relaunch reuses the number the
    previous session already wrote files under."""
    assert pv_audit.startup_scan_number(ioc_value=None, ini_value=7) == 8


def test_a_float_from_the_ioc_is_coerced():
    """The ini stores these as floats and pyepics may hand back one too;
    the widgets and file names need an int."""
    assert pv_audit.startup_scan_number(ioc_value=42.0, ini_value=7.0) == 42
    assert pv_audit.startup_scan_number(ioc_value=None, ini_value=7.0) == 8


def test_reading_at_startup_is_recorded_as_not_a_write():
    startup = next(s for s in pv_audit.LOCAL_ONLY if s[0] == "GUI startup")
    assert "Never writes" in startup[2]


def test_startup_and_completion_are_recorded_as_local_only():
    local = {name for name, _fn, _why in pv_audit.LOCAL_ONLY}
    assert "GUI startup" in local
    assert "Scan finished" in local
