"""The upstream optics, after the move off epicsmotor.

What has to stay true: the optics panel addresses a motor as (group, index)
and the index must still reach the same physical record. Everything else
about the driver changed.
"""

import pytest

from ptychosaxs import optics_config as oc
from ptychosaxs.debug.debug_stubs import FakeMotorPV
from ptychosaxs.optics import OSA, beamstop, opticsbox, saxs_z

GROUPS = {"opticsbox": opticsbox, "OSA": OSA,
          "saxs_z": saxs_z, "beamstop": beamstop}


@pytest.fixture(autouse=True)
def clean_records():
    FakeMotorPV.reset()
    yield
    FakeMotorPV.reset()


def ctrl(group):
    return GROUPS[group](pv_class=FakeMotorPV)


@pytest.mark.parametrize("group", sorted(GROUPS))
def test_axis_order_is_the_order_the_panel_indexes(group):
    assert ctrl(group).motornames == oc.GROUPS[group]


def test_every_configured_motor_belongs_to_exactly_one_group():
    seen = [name for names in oc.GROUPS.values() for name in names]
    assert sorted(seen) == sorted(oc.PV_BASES)
    assert len(seen) == len(set(seen))


def test_the_panels_group_index_addressing_reaches_the_right_record():
    """gui/optics_motors._axis() does controller.motornames[index]; this is
    the invariant that makes that safe."""
    for name, (group, index) in oc.MOTORS.items():
        assert ctrl(group).motornames[index] == name


@pytest.mark.parametrize("name,base", sorted(oc.PV_BASES.items()))
def test_a_move_writes_the_drive_field_of_that_motors_record(name, base):
    group, _index = oc.MOTORS[name]
    controller = ctrl(group)
    controller.mv(name, 1.5)
    written = [pv for pv, _v in FakeMotorPV.history()]
    assert f"{base}.VAL" in written, (
        f"moving {name} should write {base}.VAL, wrote {written}")


def test_a_move_reads_back_through_the_readback_field():
    c = ctrl("opticsbox")
    c.mv("ZP_ver", 2.25)
    assert c.get_pos("ZP_ver") == pytest.approx(2.25)


def test_a_relative_move_uses_the_records_own_relative_field():
    """Not read-then-write: .RLV lets the IOC do the arithmetic, so two
    relative moves racing cannot both read the same start position."""
    c = ctrl("opticsbox")
    c.mv("ZP_ver", 1.0)
    c.mvr("ZP_ver", 0.5)
    assert f"{oc.PV_BASES['ZP_ver']}.RLV" in [pv for pv, _ in FakeMotorPV.history()]
    assert c.get_pos("ZP_ver") == pytest.approx(1.5)


def test_mvr_accepts_the_wait_keyword_the_panel_passes():
    ctrl("opticsbox").mvr("BS_ver", 0.1, wait=False)


def test_motorunits_are_available_without_blowing_up():
    assert len(ctrl("OSA").motorunits) == len(oc.GROUPS["OSA"])


# -- the duplication this replaced -------------------------------------------

def test_the_status_module_and_the_device_layer_share_one_table():
    """These were two hand-maintained copies, with a comment warning they
    "must stay in step ... a wrong entry silently logs the wrong motor"."""
    from gui import optics_status
    assert optics_status.PV_BASES is oc.PV_BASES
    assert optics_status.MOTORS is oc.MOTORS


@pytest.mark.parametrize("group", sorted(GROUPS))
def test_debug_and_real_controllers_agree_on_axis_names(group):
    """Debug mode must not point the panel's buttons at different stages."""
    from ptychosaxs.debug import debug_stubs
    stub_cls = {"opticsbox": debug_stubs.DebugOpticsbox,
                "OSA": debug_stubs.DebugOSA,
                "saxs_z": debug_stubs.DebugSaxsZ,
                "beamstop": debug_stubs.DebugBeamstop}[group]
    assert stub_cls().motornames == ctrl(group).motornames
