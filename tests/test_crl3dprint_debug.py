"""
pytest suite for CRL_3dprint's debug-mode contract ONLY.

In scope:
  - DebugSmaractCRLController: get_pos/mv/mvr/stop/set_pos/is_moving/
    set_speed/get_speed behavior and in-memory state tracking.
  - FakePV's get/put contract (reused by CRL_3dprint's X-ray-eye code).
  - CRL_3dprint.ini default-creation-on-missing-file and read/write
    round-trip (via _ensure_default_ini).
  - XY in/out JSON export/import schema round-trip (plain dict/json, no
    QFileDialog/Qt widgets involved).
  - The position-proximity/threshold function (_near) used by the status
    pill logic.

Out of scope (explicitly NOT tested here):
  - Real CRLAxisController / SmaractMCS2Controller / smaract.ctl SDK.
  - Any real Qt widget rendering, QApplication event loop, or button
    click-through-the-actual-.ui-file behavior.
  - EPICS PV behavior beyond FakePV's own get/put contract.
"""
import configparser
import json

import pytest

from debug_stubs import DebugSmaractCRLController, FakePV
from CRL_3dprint import DEFAULT_FONT_SIZE, _ensure_default_ini, _near

_MOTOR_SLOTS = [("X", 0, "mm"), ("Y", 1, "mm"), ("TILT", 2, "deg"), ("PITCH", 3, "deg")]


# ---------------------------------------------------------------------------
# DebugSmaractCRLController
# ---------------------------------------------------------------------------

def test_debug_controller_starts_at_zero():
    ctrl = DebugSmaractCRLController(motor_slots=_MOTOR_SLOTS)
    for name, _ch, _unit in _MOTOR_SLOTS:
        assert ctrl.get_pos(name) == 0.0


def test_debug_controller_mv_absolute():
    ctrl = DebugSmaractCRLController(motor_slots=_MOTOR_SLOTS)
    ctrl.mv("X", 1.5)
    assert ctrl.get_pos("X") == 1.5
    ctrl.mv("TILT", -3.0)
    assert ctrl.get_pos("TILT") == -3.0


def test_debug_controller_mvr_relative_accumulates():
    ctrl = DebugSmaractCRLController(motor_slots=_MOTOR_SLOTS)
    ctrl.mv("Y", 1.0)
    ctrl.mvr("Y", 0.25)
    ctrl.mvr("Y", -0.1)
    assert ctrl.get_pos("Y") == pytest.approx(1.15)


def test_debug_controller_set_pos_returns_new_value():
    ctrl = DebugSmaractCRLController(motor_slots=_MOTOR_SLOTS)
    result = ctrl.set_pos("PITCH", 42.0)
    assert result == 42.0
    assert ctrl.get_pos("PITCH") == 42.0


def test_debug_controller_stop_and_is_moving_no_raise():
    ctrl = DebugSmaractCRLController(motor_slots=_MOTOR_SLOTS)
    ctrl.stop("X")  # must not raise
    assert ctrl.is_moving("X") is False


def test_debug_controller_connect_disconnect_state():
    ctrl = DebugSmaractCRLController(motor_slots=_MOTOR_SLOTS)
    assert ctrl.is_connected() is False
    ctrl.connect()
    assert ctrl.is_connected() is True
    ctrl.disconnect()
    assert ctrl.is_connected() is False


def test_debug_controller_speed_roundtrip_is_fixed_stub_values():
    ctrl = DebugSmaractCRLController(motor_slots=_MOTOR_SLOTS)
    ctrl.set_speed("X", vel=10, acc=20)  # must not raise
    assert ctrl.get_speed("X") == (1.0, 10.0)


def test_debug_controller_independent_axes():
    ctrl = DebugSmaractCRLController(motor_slots=_MOTOR_SLOTS)
    ctrl.mv("X", 1.0)
    assert ctrl.get_pos("Y") == 0.0  # unaffected


def test_debug_controller_calibrate_no_raise():
    ctrl = DebugSmaractCRLController(motor_slots=_MOTOR_SLOTS)
    ctrl.calibrate("X")  # must not raise


def test_debug_controller_find_reference_resets_position():
    ctrl = DebugSmaractCRLController(motor_slots=_MOTOR_SLOTS)
    ctrl.mv("TILT", 3.5)
    ctrl.find_reference("TILT")
    assert ctrl.get_pos("TILT") == 0.0


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


def test_ensure_default_ini_does_not_overwrite_existing_file(tmp_path):
    ini_path = tmp_path / "CRL_3dprint.ini"
    cfg = configparser.ConfigParser()
    cfg["xy_preset"] = {"in_0": "1.234", "in_1": "0", "out_0": "0", "out_1": "0"}
    with open(ini_path, "w") as f:
        cfg.write(f)

    _ensure_default_ini(str(ini_path))

    cfg2 = configparser.ConfigParser()
    cfg2.read(str(ini_path))
    assert cfg2["xy_preset"]["in_0"] == "1.234"  # untouched


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
