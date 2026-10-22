"""Unit tests for gui/optics_status.py.

No Qt, no EPICS, no hardware -- live positions are injected, and the ini is
written to a tmp_path. conftest.py already puts gui/ on sys.path.
"""

import json
import textwrap

import pytest

import optics_status as os_mod
from optics_status import (
    BETWEEN, IN, OUT, UNDEFINED, BLOCKS, classify_block, classify_optic,
    check_good_for_ptycho, read_definitions, read_status,
)


# -- optic name classification -------------------------------------------

@pytest.mark.parametrize("name,expected_type", [
    ("sRZP 50nm", "square_RZP"),
    ("square zone plate 40nm", "square_RZP"),
    ("cRZP 30nm", "circle_RZP"),
    ("circle 30nm", "circle_RZP"),
    ("tRZP 20nm", "triangle_RZP"),
    ("triangle 20nm", "triangle_RZP"),
    ("RZP 15nm", "RZP"),
    ("random 15nm", "RZP"),
    ("ZP 50nm", "regular_ZP"),
    ("zone plate", "regular_ZP"),
    ("regular optic", "regular_ZP"),
    ("mirror", "unknown_optic"),
    ("", "unknown_optic"),
])
def test_optic_type(name, expected_type):
    assert classify_optic(name)[0] == expected_type


def test_specific_rzp_beats_generic():
    """sRZP contains both "rzp" and "zp"; rule order must pick square."""
    assert classify_optic("sRZP")[0] == "square_RZP"
    assert classify_optic("cRZP")[0] == "circle_RZP"
    assert classify_optic("tRZP")[0] == "triangle_RZP"


@pytest.mark.parametrize("name,expected_ozw", [
    ("sRZP 50nm", 50),
    ("sRZP 50 nm", 50),          # a space before the unit is still a width
    ("ZP 200NM", 200),           # case-insensitive
    ("optic 1", 1),              # no "nm" at all -> unknown
    ("optic 1nm", 1),            # 1 is not > OZW_MIN, so not a width
    ("optic 4nm", 1),            # boundary: 4 is rejected
    ("optic 5nm", 5),            # boundary: 5 is accepted
    ("ZP 1.530nm", 1),           # must not read 530 out of a decimal
    ("", 1),
])
def test_optic_ozw(name, expected_ozw):
    assert classify_optic(name)[2] == expected_ozw


def test_optic_name_is_passed_through_stripped():
    assert classify_optic("  sRZP 50nm  ")[1] == "sRZP 50nm"


def test_classify_optic_handles_none():
    assert classify_optic(None) == ("unknown_optic", "", 1)


# -- block/ini wiring ------------------------------------------------------

def test_saxsbs_section_name_is_not_the_key():
    """The SAXS beamstop is keyed "saxsbs" but stored under "SAXSbs"."""
    assert BLOCKS["saxsbs"].section == "SAXSbs"


def test_osa_motor_order_is_ver_then_hor():
    """in_0/in_1 mean OSA_Y/OSA_X -- getting this backwards reads plausibly
    but wrongly, so pin it."""
    assert BLOCKS["osa"].motors == ["OSA_Y", "OSA_X"]


def test_every_block_motor_has_a_pv():
    for spec in BLOCKS.values():
        for motor in spec.motors:
            assert motor in os_mod.PV_BASES
            assert motor in os_mod.MOTORS


def test_us_optics_motors_all_have_pvs():
    for motor in os_mod.US_OPTICS_MOTORS:
        assert motor in os_mod.PV_BASES


# -- definitions reading ---------------------------------------------------

def _write_ini(tmp_path, body):
    path = tmp_path / "optics_motors.ini"
    path.write_text(textwrap.dedent(body))
    return str(path)


def test_missing_ini_is_not_an_error(tmp_path):
    defs, present, error = read_definitions(str(tmp_path / "nope.ini"))
    assert present is False and error is None
    assert all(v is None for v in defs["bs"]["in"])


def test_blank_values_read_as_none(tmp_path):
    path = _write_ini(tmp_path, """
        [bs]
        in_0 =
        in_1 =
        out_0 = 1.5
        out_1 =
    """)
    defs, _, _ = read_definitions(path)
    assert defs["bs"]["in"] == [None, None]
    assert defs["bs"]["out"] == [1.5, None]


def test_corrupt_positions_json_does_not_raise(tmp_path):
    path = _write_ini(tmp_path, """
        [zp]
        positions = {"half written
    """)
    os_mod._last_good_definitions = None
    defs, present, error = read_definitions(path)
    assert present is True and error is not None
    assert defs["zp"]["positions"] == {}


# -- classification --------------------------------------------------------

def _defs(**over):
    base = {spec.key: {"in": [None] * len(spec.motors),
                       "out": [None] * len(spec.motors),
                       "positions": {}}
            for spec in BLOCKS.values()}
    base.update(over)
    return base


def test_in_match():
    defs = _defs(bs={"in": [1.0, 2.0], "out": [9.0, 9.0], "positions": {}})
    st = classify_block(BLOCKS["bs"], {"BS_ver": 1.0, "BS_hor": 2.0}, defs)
    assert st.state == IN and st.label == "In"


def test_out_match_within_tolerance():
    defs = _defs(bs={"in": [1.0, 2.0], "out": [9.0, 9.0], "positions": {}})
    st = classify_block(BLOCKS["bs"], {"BS_ver": 9.004, "BS_hor": 8.997}, defs)
    assert st.state == OUT


def test_just_outside_tolerance_is_between():
    defs = _defs(bs={"in": [1.0, 2.0], "out": [9.0, 9.0], "positions": {}})
    st = classify_block(BLOCKS["bs"], {"BS_ver": 9.02, "BS_hor": 9.0}, defs)
    assert st.state == BETWEEN and st.label == "----"


def test_no_definitions_is_undefined_not_between():
    st = classify_block(BLOCKS["bs"], {"BS_ver": 1.0, "BS_hor": 2.0}, _defs())
    assert st.state == UNDEFINED and st.label == "undefined"


def test_all_none_named_position_never_matches():
    """The stock fresh-install default is {"test position 1": [null, null]}.
    It must read UNDEFINED, not IN."""
    defs = _defs(zp={"in": [], "out": [None, None],
                     "positions": {"test position 1": [None, None]}})
    st = classify_block(BLOCKS["zp"], {"ZP_ver": 0.0, "ZP_hor": 0.0}, defs)
    assert st.state == UNDEFINED


def test_named_position_reports_its_name():
    defs = _defs(zp={"in": [], "out": [9.0, 9.0],
                     "positions": {"sRZP 50nm": [1.0, 2.0]}})
    st = classify_block(BLOCKS["zp"], {"ZP_ver": 1.0, "ZP_hor": 2.0}, defs)
    assert st.state == IN and st.name == "sRZP 50nm"
    assert st.label == "sRZP 50nm"      # name instead of "In"


def test_overlapping_named_positions_flagged_ambiguous():
    defs = _defs(zp={"in": [], "out": [9.0, 9.0],
                     "positions": {"a": [1.0, 2.0], "b": [1.0, 2.0]}})
    st = classify_block(BLOCKS["zp"], {"ZP_ver": 1.0, "ZP_hor": 2.0}, defs)
    assert st.state == IN and st.ambiguous is True


def test_unreadable_motor_is_undefined():
    defs = _defs(bs={"in": [1.0, 2.0], "out": [9.0, 9.0], "positions": {}})
    st = classify_block(BLOCKS["bs"], {"BS_ver": None, "BS_hor": None}, defs)
    assert st.state == UNDEFINED


def test_partial_readback_does_not_match():
    """One axis readable, one not -- must not claim a match on the half it
    can see."""
    defs = _defs(bs={"in": [1.0, 2.0], "out": [9.0, 9.0], "positions": {}})
    st = classify_block(BLOCKS["bs"], {"BS_ver": 1.0, "BS_hor": None}, defs)
    assert st.state == BETWEEN


# -- the ptycho rule -------------------------------------------------------

def _status_from(live, tmp_path, ini_body):
    return read_status(_write_ini(tmp_path, ini_body), live=live)


GOOD_INI = """
    [bs]
    in_0 = 1.0
    in_1 = 1.0
    out_0 = 9.0
    out_1 = 9.0
    [zp]
    out_0 = 9.0
    out_1 = 9.0
    positions = {"sRZP 50nm": [2.0, 2.0]}
    [osa]
    in_0 = 3.0
    in_1 = 3.0
    out_0 = 9.0
    out_1 = 9.0
    [SAXSbs]
    in_0 = 4.0
    in_1 = 4.0
    out_0 = 8.0
    out_1 = 8.0
    [ztrans]
    out_0 = 9.0
    positions = {"focus": [5.0]}
"""

GOOD_LIVE = {"BS_ver": 1.0, "BS_hor": 1.0, "ZP_ver": 2.0, "ZP_hor": 2.0,
             "OSA_Y": 3.0, "OSA_X": 3.0, "SAXSBS_ver": 8.0, "SAXSBS_hor": 8.0,
             "BSZP_Ztrans": 5.0}


def test_good_state_passes(tmp_path):
    status = _status_from(GOOD_LIVE, tmp_path, GOOD_INI)
    ok, problems = check_good_for_ptycho(status)
    assert ok is True and problems == []
    assert status.zp_name == "sRZP 50nm"


def test_saxs_bs_inserted_is_a_problem(tmp_path):
    live = dict(GOOD_LIVE, SAXSBS_ver=4.0, SAXSBS_hor=4.0)
    ok, problems = check_good_for_ptycho(_status_from(live, tmp_path, GOOD_INI))
    assert ok is False
    assert any("SAXS BS" in p and "Out" in p for p in problems)


def test_zp_retracted_is_a_problem(tmp_path):
    live = dict(GOOD_LIVE, ZP_ver=9.0, ZP_hor=9.0)
    ok, problems = check_good_for_ptycho(_status_from(live, tmp_path, GOOD_INI))
    assert ok is False
    assert any(p.startswith("ZP is Out") for p in problems)


def test_ztrans_is_not_part_of_the_rule(tmp_path):
    """BSZP_Ztrans is reported but must not gate a scan."""
    live = dict(GOOD_LIVE, BSZP_Ztrans=123.0)
    ok, _ = check_good_for_ptycho(_status_from(live, tmp_path, GOOD_INI))
    assert ok is True


def test_undefined_block_is_reported_as_a_problem(tmp_path):
    ok, problems = check_good_for_ptycho(_status_from(GOOD_LIVE, tmp_path, "[bs]\n"))
    assert ok is False
    assert any("no saved" in p for p in problems)


def test_read_status_without_epics_is_all_undefined(tmp_path):
    status = read_status(_write_ini(tmp_path, GOOD_INI),
                         caget=lambda pv: (_ for _ in ()).throw(RuntimeError("no IOC")))
    assert status.epics_ok is False
    assert all(b.state == UNDEFINED for b in status.blocks.values())
