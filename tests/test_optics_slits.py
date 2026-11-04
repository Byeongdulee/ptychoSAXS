"""The optics panel's Slits registry and the enable checkboxes.

Qt-free like the rest of the suite: the slit classification, the [slits]
ini round trip, the .ui files as XML, the simulated server's .VAL -> .RBV
echo, and OpticsModes' slit step against a fake panel.
"""

import logging
import os
import xml.etree.ElementTree as ET

import pytest

from gui.optics import optics_modes
from gui.optics.optics_modes import OpticsModes
from gui.optics_motors import (
    INI_DEFAULTS,
    SLITS,
    SLIT_PRESETS,
    UI_DIR,
    VIEW_LEVELS,
    classify_slits,
    read_slit_presets,
    write_slit_presets,
)
from ptychosaxs.api.pvs import PVCache
from ptychosaxs.api.profile import Profile

LOG = logging.getLogger("test")


def _widths(value):
    return {key: value for key in SLITS}


# -- classification ------------------------------------------------------

def test_slits_at_a_preset_within_10um_read_as_that_preset():
    presets = {"ptycho": _widths(0.100), "SAXS": _widths(0.500)}
    rbvs = _widths(0.100)
    rbvs["MH2_V"] = 0.110
    assert classify_slits(rbvs, presets) == "ptycho"
    assert classify_slits(_widths(0.495), presets) == "SAXS"


def test_one_slit_beyond_tolerance_is_no_preset():
    presets = {"ptycho": _widths(0.100), "SAXS": _widths(0.500)}
    rbvs = _widths(0.100)
    rbvs["CL_H"] = 0.111
    assert classify_slits(rbvs, presets) == "----"


def test_an_undefined_width_or_unreadable_slit_never_matches():
    presets = {"ptycho": _widths(0.100), "SAXS": _widths(None)}
    assert classify_slits(_widths(None), {"ptycho": _widths(None)}) == "----"
    rbvs = _widths(0.100)
    rbvs["CL_V"] = None
    assert classify_slits(rbvs, presets) == "----"
    partial = _widths(0.100)
    partial["MH2_H"] = None
    assert classify_slits(_widths(0.100), {"ptycho": partial}) == "----"


def test_identical_presets_read_as_ptycho():
    presets = {"ptycho": _widths(0.2), "SAXS": _widths(0.2)}
    assert classify_slits(_widths(0.2), presets) == "ptycho"


# -- persistence ---------------------------------------------------------

def test_slit_presets_round_trip_through_the_ini(tmp_path):
    path = str(tmp_path / "optics_motors.ini")
    assert read_slit_presets(path) == {p: _widths(None) for p in SLIT_PRESETS}
    presets = {"ptycho": _widths(0.1), "SAXS": _widths(None)}
    presets["SAXS"]["CL_H"] = 1.25
    write_slit_presets(presets, path)
    assert read_slit_presets(path) == presets


def test_ini_defaults_carry_every_slit_width_blank():
    section = INI_DEFAULTS["slits"]
    assert len(section) == len(SLITS) * len(SLIT_PRESETS)
    assert set(section.values()) == {""}


# -- the .ui files -------------------------------------------------------

def _widgets(name):
    return ET.parse(os.path.join(UI_DIR, name)).getroot().iter("widget")


@pytest.mark.parametrize("view", VIEW_LEVELS)
def test_no_view_has_an_enable_push_button_left(view):
    assert not [w.get("name") for w in _widgets(view)
                if w.get("name", "").endswith("_enable")
                and w.get("class") != "QCheckBox"]


@pytest.mark.parametrize("view", VIEW_LEVELS)
def test_enable_checkboxes_have_no_text(view):
    for widget in _widgets(view):
        if widget.get("class") == "QCheckBox" and widget.get("name").endswith("_enable"):
            text = widget.find("property[@name='text']/string")
            assert text is None or not (text.text or "").strip(), widget.get("name")


def test_the_user_view_can_unlock_saxs_slit_definition():
    root = ET.parse(os.path.join(UI_DIR, "optics_motors_user.ui")).getroot()
    assert root.find(".//action[@name='actionEnable_SAXS_slit_definition']") is not None
    assert root.find(".//widget[@name='pushButton_slits_define_SAXS']") is not None


# -- simulated server ----------------------------------------------------

def test_a_simulated_drive_arrives_at_its_readback():
    pvs = PVCache(Profile(simulated=True), LOG)
    pvs.subscribe("c1", ["12idc:CL_SlitHsize.RBV"])
    pvs.put("12idc:CL_SlitHsize.VAL", 0.25)
    assert pvs.snapshot()["12idc:CL_SlitHsize.RBV"] == 0.25


def test_a_real_drive_is_not_echoed():
    puts = {}

    class Recording:
        connected = True

        def __init__(self, name):
            self.name = name

        def put(self, value):
            puts[self.name] = value

    pvs = PVCache(Profile(simulated=False), LOG, pv_class=Recording)
    pvs.put("12idc:CL_SlitHsize.VAL", 0.25)
    assert puts == {"12idc:CL_SlitHsize.VAL": 0.25}


# -- mode sequences ------------------------------------------------------

class _FakePanel:
    ui = None

    def __init__(self, defined):
        self.defined = defined
        self.slit_moves = []
        self.block_moves = []

    def slits_has_target(self, preset):
        return preset in self.defined

    def move_slits(self, preset):
        self.slit_moves.append(preset)
        return True


@pytest.fixture
def modes(monkeypatch):
    def make(defined):
        panel = _FakePanel(defined)
        m = OpticsModes(panel)
        m.warnings = []
        monkeypatch.setattr(m, "_warn", m.warnings.append)
        monkeypatch.setattr(m, "_move_blocks",
                            lambda keys, into, what: panel.block_moves.append(
                                (tuple(keys), into)) or True)
        monkeypatch.setattr(m, "_retract_xrayeye_if_in", lambda: True)
        monkeypatch.setattr(m, "_insert_saxsbs", lambda preamble: True)
        panel.block_state = lambda key: None
        return m, panel
    return make


def test_ptycho_mode_sets_ptycho_slits_before_the_optics(modes):
    m, panel = modes({"ptycho"})
    m.ptycho_mode()
    assert panel.slit_moves == ["ptycho"]
    assert panel.block_moves == [(tuple(optics_modes.PTYCHO_BLOCKS), True)]


def test_saxs_mode_sets_saxs_slits(modes):
    m, panel = modes({"SAXS"})
    m.saxs_mode()
    assert panel.slit_moves == ["SAXS"]
    assert panel.block_moves == [(tuple(optics_modes.PTYCHO_BLOCKS), False)]


@pytest.mark.parametrize("mode", ["ptycho_mode", "xray_eye_mode", "saxs_mode"])
def test_undefined_slits_stop_a_mode_before_anything_moves(modes, mode):
    m, panel = modes(set())
    panel.xray_eye = None   # xray_eye_mode must refuse before touching it
    getattr(m, mode)()
    assert panel.slit_moves == [] and panel.block_moves == []
    assert len(m.warnings) == 1 and "slit" in m.warnings[0]
