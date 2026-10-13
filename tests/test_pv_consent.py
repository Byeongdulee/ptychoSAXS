"""
pytest suite for consent before writing beamline PVs.

Opening the GUI used to increment the scan number and push it to
12idc:data:fileIndex before the window was usable, with nothing said. On a
shared IOC that renumbers whatever the beamline was doing. These pin the
prompts that now stand in the way, and - just as important - the one case
that is deliberately *not* prompted.

The Qt dialogs are injected, so none of this needs a QApplication.
"""
import pv_consent
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
# Asked once, at startup, and nowhere else
# ---------------------------------------------------------------------------

def test_allowing_is_remembered_and_not_asked_again():
    consent, asker = _consent(outcome=pv_consent.ALLOW)
    assert consent.request() == pv_consent.ALLOW
    assert consent.request() == pv_consent.ALLOW
    assert len(asker.prompts) == 1
    assert consent.granted is True


def test_declining_is_remembered_and_not_asked_again():
    """Declining has to stick, or a scan would re-ask from a worker thread."""
    consent, asker = _consent(outcome=pv_consent.DECLINE)
    assert consent.request() == pv_consent.DECLINE
    assert consent.request() == pv_consent.DECLINE
    assert len(asker.prompts) == 1
    assert consent.granted is False


def test_closing_the_window_is_not_an_answer():
    """Dismissing is distinct from Cancel: the caller exits rather than
    guess, so nothing may be remembered from it."""
    consent, asker = _consent(outcome=pv_consent.CLOSED)
    assert consent.request() == pv_consent.CLOSED
    assert consent.granted is False
    assert consent.asked is False
    consent.request()
    assert len(asker.prompts) == 2


def test_a_configuration_change_asks_again():
    consent, asker = _consent(outcome=pv_consent.ALLOW)
    consent.request()
    consent.revoke()
    consent.request(reason="the configuration changed")
    assert len(asker.prompts) == 2
    assert "configuration changed" in asker.prompts[1]


def test_the_prompt_names_the_pv_and_when_it_is_written():
    consent, asker = _consent(outcome=pv_consent.ALLOW)
    consent.request()
    text = asker.prompts[0]
    assert "12idc:data:fileIndex" in text
    assert "start of each scan" in text
    assert "Closing this window exits" in text


def test_nothing_is_granted_before_being_asked():
    consent, _asker = _consent()
    assert consent.granted is False


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
