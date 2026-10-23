"""Output-path construction for scans, on Windows and on Linux.

Two different paths name one scan folder: the GUI path this process opens
with os.makedirs/h5py, and the EPICS path written into a detector's FilePath
PV. These used to be open-coded in four places plus a fifth, incompatible
variant inside the master-file builder, which is how a Linux run ended up
writing frames to /net/micdata/data2/net/micdata/data2/... and failing to
create the master file at all.

The Windows cases here pin the behaviour that must not change.
"""
import os

import pytest

from gui.handlers.scan_handler import ScanHandler


# Real paths from the 12ID-C console and workstation.
WIN_WF = r"Z:\12IDC\2026_Data\2026_3\02_test"
LINUX_WF = "/net/micdata/data2/12IDC/2026_Data/2026_3/02_test"
BASEPATH = "/net/micdata/data2/"


class _FakeLineEdit:
    def __init__(self, value):
        self._value = value

    def text(self):
        return self._value


class _FakeUi:
    def __init__(self, working_folder):
        self.edit_workingfolder = _FakeLineEdit(working_folder)


class _FakeWindow:
    def __init__(self, on_linux):
        self._on_linux = on_linux

    def running_on_linux(self):
        return self._on_linux


class _Handler:
    """Minimal stand-in exposing just what the path helpers touch.

    The helpers under test are taken unbound off ScanHandler, so this
    exercises the real implementations without building a live handler (which
    would need a QApplication and a connected IOC).
    """

    def __init__(self, working_folder, on_linux):
        self.ui = _FakeUi(working_folder)
        self.w = _FakeWindow(on_linux)
        # These helpers have moved into ptychosaxs.api.scan_exec, where they
        # reach the window through a ScanContext rather than through self.w
        # and self.ui directly. The double mirrors the real handler, which
        # builds the same thing in its __init__.
        from ptychosaxs.api.scans import ScanContext

        self.ctx = ScanContext(window=self.w)
        self.ctx.ui = self.ui

    _working_folder = ScanHandler._working_folder
    # staticmethod() re-applied: attribute access has already unwrapped the
    # descriptor, and a plain function assigned here would bind as an
    # instance method and swallow `base`.
    _join_posix = staticmethod(ScanHandler._join_posix)
    _gui_scan_folder = ScanHandler._gui_scan_folder
    _epics_scan_folder = ScanHandler._epics_scan_folder


# ---------------------------------------------------------------------------
# EPICS path: what the detector IOC is told. Always Linux.
# ---------------------------------------------------------------------------

def test_epics_path_on_linux_uses_the_working_folder_as_the_basepath():
    """The regression. The Working folder is already absolute, so prepending
    a basepath to it produced the doubled path the frames actually landed in."""
    h = _Handler(LINUX_WF, on_linux=True)
    assert h._epics_scan_folder(BASEPATH, "ptycho", "S0004") == (
        "/net/micdata/data2/12IDC/2026_Data/2026_3/02_test/ptycho/S0004"
    )


def test_epics_path_on_linux_ignores_the_configured_basepath():
    h = _Handler(LINUX_WF, on_linux=True)
    with_base = h._epics_scan_folder(BASEPATH, "ptycho", "S0004")
    without_base = h._epics_scan_folder("/somewhere/else/", "ptycho", "S0004")
    assert with_base == without_base


def test_epics_path_on_windows_swaps_the_drive_letter_for_the_basepath():
    h = _Handler(WIN_WF, on_linux=False)
    assert h._epics_scan_folder(BASEPATH, "ptycho", "S0004") == (
        "/net/micdata/data2/12IDC/2026_Data/2026_3/02_test/ptycho/S0004"
    )


def test_epics_path_on_windows_handles_a_basepath_without_a_trailing_slash():
    h = _Handler(WIN_WF, on_linux=False)
    assert h._epics_scan_folder("/net/micdata/data2", "SAXS", "S0001") == (
        "/net/micdata/data2/12IDC/2026_Data/2026_3/02_test/SAXS/S0001"
    )


def test_epics_path_never_contains_a_backslash_or_a_double_slash():
    for wf, on_linux in ((WIN_WF, False), (LINUX_WF, True)):
        h = _Handler(wf, on_linux)
        path = h._epics_scan_folder(BASEPATH, "ptycho", "S0004")
        assert "\\" not in path
        assert "//" not in path


def test_epics_path_tolerates_an_empty_working_folder():
    """An unset Working folder must not yield "//ptycho" or a bare "/"."""
    h = _Handler("", on_linux=True)
    assert h._epics_scan_folder(BASEPATH, "ptycho", "S0001") == "ptycho/S0001"


# ---------------------------------------------------------------------------
# GUI path: what this process opens.
# ---------------------------------------------------------------------------

def test_gui_path_on_linux_is_absolute_and_matches_the_epics_path():
    """On Linux both sides name the same folder, so the master file lands
    beside the detector frames rather than under the launch directory."""
    h = _Handler(LINUX_WF, on_linux=True)
    gui = h._gui_scan_folder("ptycho", "S0004")
    assert gui.startswith("/")
    assert gui == h._epics_scan_folder(BASEPATH, "ptycho", "S0004")


def test_gui_path_on_windows_keeps_the_drive_letter():
    h = _Handler(WIN_WF, on_linux=False)
    gui = h._gui_scan_folder("ptycho", "S0004")
    assert gui.startswith("Z:")
    # os.path.join uses the host separator; on Windows that is a backslash,
    # which is what the console produced before the helpers existed.
    assert gui == os.path.join(WIN_WF, "ptycho", "S0004")


def test_gui_path_accepts_an_explicit_working_folder_for_worker_threads():
    """3-D slices and the master-file builders pass the folder captured at
    scan start rather than reading the Qt widget off the GUI thread."""
    h = _Handler("/not/used", on_linux=True)
    assert h._gui_scan_folder("ptycho", "S0007", LINUX_WF) == (
        LINUX_WF + "/ptycho/S0007"
    )


# ---------------------------------------------------------------------------
# The specific defects that lost the master file.
# ---------------------------------------------------------------------------

def test_master_folder_on_linux_is_not_turned_into_a_relative_path():
    """The old builder ran .strip("/\\") over the Working folder, which ate
    the leading "/" and made os.makedirs create a tree under the CWD."""
    h = _Handler(LINUX_WF, on_linux=True)
    assert os.path.isabs(h._gui_scan_folder("ptycho", "S0004"))


def test_master_folder_on_linux_has_real_separators_not_literal_backslashes():
    """The old builder joined with "\\", so h5py was handed a single
    directory component named "02_test\\ptycho\\S0004" that did not exist."""
    h = _Handler(LINUX_WF, on_linux=True)
    folder = h._gui_scan_folder("ptycho", "S0004")
    assert "\\" not in folder
    assert os.path.dirname(folder + "/x.h5") == folder


@pytest.mark.parametrize("folder_type", ["ptycho", "SAXS", "WAXS", "positions", "DANTE"])
def test_every_folder_type_round_trips_on_both_platforms(folder_type):
    linux = _Handler(LINUX_WF, on_linux=True)
    windows = _Handler(WIN_WF, on_linux=False)
    assert linux._epics_scan_folder(BASEPATH, folder_type, "S0002").endswith(
        "/02_test/%s/S0002" % folder_type
    )
    assert windows._epics_scan_folder(BASEPATH, folder_type, "S0002").endswith(
        "/02_test/%s/S0002" % folder_type
    )


# ---------------------------------------------------------------------------
# Step-scan exposure period
# ---------------------------------------------------------------------------

def test_step_detector_period_is_never_below_the_detector_floor():
    period = ScanHandler._step_detector_period(None, 0.002)
    assert period >= 0.03


def test_step_detector_period_leaves_room_for_readout():
    """A long exposure must not be given a period shorter than itself, or the
    camera rejects triggers and the scan reads it as a frame timeout."""
    expt = 0.5
    period = ScanHandler._step_detector_period(None, expt)
    assert period > expt


def test_step_detector_period_matches_the_dg645_cycle_period():
    """The two are compared by the detector: a camera period longer than the
    trigger spacing silently drops frames."""
    for expt in (0.001, 0.002, 0.05, 0.25):
        assert ScanHandler._step_detector_period(None, expt) == round(
            max(expt + 0.020, 0.03), 6
        )
