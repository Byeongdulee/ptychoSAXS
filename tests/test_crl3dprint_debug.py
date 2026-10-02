"""
pytest suite for CRL_3dprint's offline contract ONLY.

In scope:
  - EpicsMotorController driven against FakeMotorPV: get_pos/mv/mvr/tweak/
    stop/is_moving/set_speed/get_speed/get_unit and connection reporting.
    The fake sits at the PV boundary, so these exercise the production
    controller logic rather than a parallel stub that could drift from it.
  - FakePV's get/put contract (reused by CRL_3dprint's X-ray-eye code).
  - CRL_3dprint.ini default-creation-on-missing-file, back-filling of
    entries absent from an existing file, and read/write round-trip
    (via _ensure_default_ini).
  - XY in/out JSON export/import schema round-trip (plain dict/json, no
    QFileDialog/Qt widgets involved).
  - The position-proximity/threshold function (_near) used by the status
    pill logic, and the jog-remap .ini parser (_load_jog_map).

Out of scope (explicitly NOT tested here):
  - Any real Channel Access traffic, IOC, or pyepics behavior - no test
    here constructs an epics.PV, so the suite needs neither a network nor
    pyepics installed.
  - Any real Qt widget rendering, QApplication event loop, or button
    click-through-the-actual-.ui-file behavior.
"""
import configparser
import json

import pytest

import CRL_3dprint as crl
from debug_stubs import FakeMotorPV, FakePV
from epics_crl3dprint import MOTOR_SLOTS
from epics_motor import FIELDS, EpicsMotorController
from CRL_3dprint import DEFAULT_FONT_SIZE, _ensure_default_ini, _near


@pytest.fixture
def ctrl():
    """A connected controller backed entirely by in-memory fake records."""
    FakeMotorPV.reset()
    controller = EpicsMotorController(MOTOR_SLOTS, pv_class=FakeMotorPV)
    controller.connect()
    return controller


# ---------------------------------------------------------------------------
# EpicsMotorController against FakeMotorPV
# ---------------------------------------------------------------------------

def test_controller_starts_at_zero(ctrl):
    for name, _prefix, _unit in MOTOR_SLOTS:
        assert ctrl.get_pos(name) == 0.0


def test_controller_mv_absolute(ctrl):
    ctrl.mv("X", 1.5)
    assert ctrl.get_pos("X") == 1.5
    ctrl.mv("TILT", -3.0)
    assert ctrl.get_pos("TILT") == -3.0


def test_controller_mv_with_put_completion(ctrl):
    ctrl.mv("X", 2.25, wait=True)
    assert ctrl.get_pos("X") == 2.25


def test_controller_mvr_relative_accumulates(ctrl):
    ctrl.mv("Y", 1.0)
    ctrl.mvr("Y", 0.25)
    ctrl.mvr("Y", -0.1)
    assert ctrl.get_pos("Y") == pytest.approx(1.15)


def test_controller_tweak_forward_and_reverse(ctrl):
    ctrl.mv("Y", 1.0)
    ctrl.tweak("Y", 0.25, forward=True)
    assert ctrl.get_pos("Y") == pytest.approx(1.25)
    ctrl.tweak("Y", 0.25, forward=False)
    assert ctrl.get_pos("Y") == pytest.approx(1.0)


def test_controller_tweak_publishes_step_to_record(ctrl):
    """The step must reach .TWV, or another client's value would drive the move."""
    ctrl.tweak("X", 0.05, forward=True)
    assert FakeMotorPV("12ideMCS2:m1.TWV").get() == pytest.approx(0.05)


def test_controller_tweak_ignores_sign_of_step(ctrl):
    """Direction comes from forward=, not from the sign of the step."""
    ctrl.tweak("X", -0.25, forward=True)
    assert ctrl.get_pos("X") == pytest.approx(0.25)


def test_controller_set_tweak_step_does_not_move(ctrl):
    ctrl.mv("X", 1.0)
    ctrl.set_tweak_step("X", 0.5)
    assert ctrl.get_pos("X") == 1.0
    assert FakeMotorPV("12ideMCS2:m1.TWV").get() == pytest.approx(0.5)


def test_controller_stop_leaves_axis_ready_to_move(ctrl):
    """SPMG must end on Go, or the axis would ignore every later move."""
    ctrl.stop("X")
    assert FakeMotorPV("12ideMCS2:m1.SPMG").get() == 3
    ctrl.mv("X", 1.0)
    assert ctrl.get_pos("X") == 1.0


def test_controller_is_moving_false_when_done(ctrl):
    assert ctrl.is_moving("X") is False


def test_controller_speed_roundtrip(ctrl):
    """Velocity is written to one record and read back from another, so the
    round trip is what matters - asserting only the written field would miss
    the two being unlinked."""
    assert ctrl.get_speed("X") == 1.0
    ctrl.set_speed("X", 2.5)
    assert FakeMotorPV("12ideMCS2:m1_vCh.A").get() == pytest.approx(2.5)
    assert ctrl.get_speed("X") == pytest.approx(2.5)


def test_controller_speed_is_per_axis(ctrl):
    ctrl.set_speed("X", 2.5)
    assert ctrl.get_speed("Y") == 1.0


def test_controller_unit_comes_from_record(ctrl):
    assert ctrl.get_unit("X") == "mm"
    assert ctrl.get_unit("TILT") == "deg"


def test_controller_independent_axes(ctrl):
    ctrl.mv("X", 1.0)
    assert ctrl.get_pos("Y") == 0.0  # unaffected


def test_controller_reports_connected(ctrl):
    assert ctrl.is_connected() is True
    assert ctrl.disconnected_axes() == []
    for name, _prefix, _unit in MOTOR_SLOTS:
        assert ctrl.is_axis_connected(name) is True


def test_controller_before_connect_has_no_pvs():
    FakeMotorPV.reset()
    controller = EpicsMotorController(MOTOR_SLOTS, pv_class=FakeMotorPV)
    assert controller.is_connected() is False
    with pytest.raises(RuntimeError):
        controller.get_pos("X")


def test_controller_without_pyepics_raises_on_connect():
    """Non-debug startup must fail with a clear message, not an AttributeError."""
    controller = EpicsMotorController(MOTOR_SLOTS, pv_class=None)
    controller._pv_class = None  # as if `from epics import PV` had failed
    with pytest.raises(RuntimeError, match="pyepics"):
        controller.connect()


def test_disconnected_axis_reads_none(ctrl):
    """A dead CA link must read None, never a stale position."""
    for (name, _field), pv in ctrl._pvs.items():
        if name == "X":
            pv.connected = False
    assert ctrl.get_pos("X") is None
    assert ctrl.is_axis_connected("X") is False
    assert ctrl.disconnected_axes() == ["X"]
    assert ctrl.is_connected() is False
    assert ctrl.get_pos("Y") == 0.0  # other axes unaffected


def test_disconnected_axis_falls_back_to_configured_unit(ctrl):
    for (name, _field), pv in ctrl._pvs.items():
        if name == "TILT":
            pv.connected = False
    assert ctrl.get_unit("TILT") == "deg"


# ---------------------------------------------------------------------------
# FakeMotorPV record-name parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("suffix", sorted(FIELDS.values()))
def test_fake_motor_pv_splits_every_field_suffix(suffix):
    """Every suffix the controller uses must be recognised as a whole. A
    suffix the fake does not know is silently treated as part of the record
    name, which would route a write to a record of its own instead of the
    motor - '_vCh.A' is the one most at risk of being mis-split."""
    FakeMotorPV.reset()
    pv = FakeMotorPV("12ideMCS2:m1" + suffix)
    assert (pv._prefix, pv._field) == ("12ideMCS2:m1", suffix)


def test_fake_motor_pv_shares_state_between_fields():
    """A write to .VAL must be visible through .RBV, or the fake would not
    model a motor record at all."""
    FakeMotorPV.reset()
    FakeMotorPV("12ideMCS2:m1.VAL").put(3.0)
    assert FakeMotorPV("12ideMCS2:m1.RBV").get() == pytest.approx(3.0)


def test_fake_motor_pv_axes_are_independent():
    FakeMotorPV.reset()
    FakeMotorPV("12ideMCS2:m1.VAL").put(3.0)
    assert FakeMotorPV("12ideMCS2:m2.RBV").get() == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# FakePV (already-existing shared stub, reused by CRL_3dprint's xray-eye code)
# ---------------------------------------------------------------------------

def test_fakepv_get_returns_zero():
    pv = FakePV("usxRIO:Galil2Bo0_STATUS.VAL")
    assert pv.get() == 0


def test_fakepv_put_does_not_raise():
    pv = FakePV("usxRIO:Galil2Bo0_CMD")
    pv.put(1)  # no exception, no return value asserted


# ---------------------------------------------------------------------------
# .ini default-creation and read/write round-trip
# ---------------------------------------------------------------------------

def test_ensure_default_ini_creates_file_with_expected_sections(tmp_path):
    ini_path = tmp_path / "CRL_3dprint.ini"
    assert not ini_path.exists()
    _ensure_default_ini(str(ini_path))
    assert ini_path.exists()

    cfg = configparser.ConfigParser()
    cfg.read(str(ini_path))
    assert cfg["xy_preset"]["in_0"] == "0.0"
    assert cfg["xy_preset"]["in_1"] == "0.0"
    assert cfg["xy_preset"]["out_0"] == "0.0"
    assert cfg["xy_preset"]["out_1"] == "0.0"
    assert cfg["ui"]["font_size"] == str(DEFAULT_FONT_SIZE)


def test_ensure_default_ini_keeps_saved_values_and_backfills_missing(tmp_path):
    """An older .ini keeps every value it already has; only entries it is
    missing (here: whole sections, and one key inside a section it does have)
    are added from the defaults."""
    ini_path = tmp_path / "CRL_3dprint.ini"
    cfg = configparser.ConfigParser()
    cfg["xy_preset"] = {"in_0": "1.234", "in_1": "0", "out_0": "0"}  # no out_1
    with open(ini_path, "w") as f:
        cfg.write(f)

    _ensure_default_ini(str(ini_path))

    cfg2 = configparser.ConfigParser()
    cfg2.read(str(ini_path))
    assert cfg2["xy_preset"]["in_0"] == "1.234"  # untouched
    assert cfg2["xy_preset"]["out_1"] == "0.0"  # back-filled key
    assert cfg2["ui"]["font_size"] == str(DEFAULT_FONT_SIZE)  # back-filled section
    assert "jog_remap_xy" in cfg2
    assert "scalar_scan" in cfg2


def test_ini_read_modify_write_preserves_other_sections(tmp_path):
    ini_path = tmp_path / "CRL_3dprint.ini"
    _ensure_default_ini(str(ini_path))

    cfg = configparser.ConfigParser()
    cfg.read(str(ini_path))
    cfg["xy_preset"]["in_0"] = "3.14"
    with open(ini_path, "w") as f:
        cfg.write(f)

    cfg2 = configparser.ConfigParser()
    cfg2.read(str(ini_path))
    assert cfg2["xy_preset"]["in_0"] == "3.14"
    assert cfg2["ui"]["font_size"] == str(DEFAULT_FONT_SIZE)  # preserved


# ---------------------------------------------------------------------------
# XY in/out JSON export/import schema round-trip (pure dict/json, no Qt)
# ---------------------------------------------------------------------------

def test_xy_json_export_import_roundtrip(tmp_path):
    data = {"xy": {"in": {"X": 1.111, "Y": -2.222}, "out": {"X": 0.0, "Y": 0.0}}}
    path = tmp_path / "crl3dprint_xy_positions.json"
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

    with open(path) as f:
        loaded = json.load(f)

    assert loaded == data
    assert loaded["xy"]["in"]["X"] == pytest.approx(1.111)
    assert loaded["xy"]["out"]["Y"] == pytest.approx(0.0)


def test_xy_json_import_rejects_malformed_file(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not valid json")
    with pytest.raises(json.JSONDecodeError):
        with open(path) as f:
            json.load(f)


# ---------------------------------------------------------------------------
# Position-proximity / status-threshold math
# ---------------------------------------------------------------------------

def test_near_within_threshold_true():
    assert _near(1.0000, 1.0049) is True


def test_near_outside_threshold_false():
    assert _near(1.0000, 1.0060) is False


def test_near_none_values_false():
    assert _near(None, 1.0) is False
    assert _near(1.0, None) is False


# ---------------------------------------------------------------------------
# Jog-remap .ini parsing
# ---------------------------------------------------------------------------

def test_load_jog_map_falls_back_without_a_section(monkeypatch, tmp_path):
    monkeypatch.setattr(crl, "_CRL_INI", str(tmp_path / "missing.ini"))
    assert crl._load_jog_map("jog_remap_xy", crl.XY_JOG_MAP) == crl.XY_JOG_MAP


def test_load_jog_map_reads_saved_values(monkeypatch, tmp_path):
    ini = tmp_path / "CRL_3dprint.ini"
    cfg = configparser.ConfigParser()
    cfg["jog_remap_xy"] = {"pb_xy_left_axis": "Y", "pb_xy_left_sign": "1"}
    with open(ini, "w") as f:
        cfg.write(f)
    monkeypatch.setattr(crl, "_CRL_INI", str(ini))

    result = crl._load_jog_map("jog_remap_xy", crl.XY_JOG_MAP)
    assert result["pb_xy_left"] == ("Y", 1)
    assert result["pb_xy_right"] == crl.XY_JOG_MAP["pb_xy_right"]  # untouched


def test_load_jog_map_rejects_unknown_axis(monkeypatch, tmp_path):
    """An axis name the controller has no record for must not reach a move -
    it would fail deep in the PV lookup rather than here."""
    ini = tmp_path / "CRL_3dprint.ini"
    cfg = configparser.ConfigParser()
    cfg["jog_remap_xy"] = {"pb_xy_left_axis": "NOPE", "pb_xy_left_sign": "-1"}
    with open(ini, "w") as f:
        cfg.write(f)
    monkeypatch.setattr(crl, "_CRL_INI", str(ini))

    assert crl._load_jog_map("jog_remap_xy", crl.XY_JOG_MAP)["pb_xy_left"] == ("X", -1)


def test_load_jog_map_rejects_non_numeric_sign(monkeypatch, tmp_path):
    ini = tmp_path / "CRL_3dprint.ini"
    cfg = configparser.ConfigParser()
    cfg["jog_remap_xy"] = {"pb_xy_left_axis": "X", "pb_xy_left_sign": "up"}
    with open(ini, "w") as f:
        cfg.write(f)
    monkeypatch.setattr(crl, "_CRL_INI", str(ini))

    assert crl._load_jog_map("jog_remap_xy", crl.XY_JOG_MAP)["pb_xy_left"] == ("X", -1)
