"""pytest suite for the sample alignment workflow's debug-mode contract.

In scope:
  - alignment_flow.AlignmentFlow: page routing for both branches and both
    modes, the history stack behind the Back button, the per-page unlock
    gating, the angle-0 safe-arc decision, phi soft-limit checks, the
    iterative sub-step machine, and the navigation tooltip strings.
  - alignment_flow.iterative_script_lines: the progressive black/gray/omitted
    reveal of the iterative instruction script.
  - xray_eye: the command-readback polarity and the shared-state contract
    three GUIs depend on.
  - sample_alignment's pure helpers: read_block_positions and its two
    wrappers, zp_is_out, and script_html.
  - optics_motors' .ini defaults for the SAXS beamstop block and the
    All In/Out toggle.
  - InstrumentsStub: the debug-mode motor object the window drives.

Out of scope (deliberately NOT tested here):
  - Real hexapod / ACS phi / SmarAct gonio motion, and real EPICS channels.
  - Any Qt widget rendering, QApplication event loop, or click-through of
    sample_alignment.ui. No QApplication is created; sample_alignment and
    optics_motors are imported only for their module-level pure functions,
    the same way test_crl3dprint_debug.py imports CRL_3dprint.
"""
import configparser

import pytest

from alignment_flow import (
    CLOSE,
    ITER_STEPS,
    PAGE_TITLES,
    PHI_HIGH_DEFAULT,
    PHI_LOW_DEFAULT,
    WINDOW_TITLE_BASE,
    Action,
    AlignmentFlow,
    Branch,
    Page,
    iterative_script_lines,
    locked_motors,
    window_title,
)
from debug_stubs import FakePV, InstrumentsStub
from optics_motors import INI_DEFAULTS as OPTICS_INI_DEFAULTS
from optics_motors import read_saxsbs_in_all, write_saxsbs_in_all
from ini_utils import ensure_ini_defaults
from sample_alignment import (
    HEXAPOD_X_COR_DEFAULT,
    PAGE_WIDGETS,
    ZP_OUT_THRESH,
    read_block_positions,
    read_saxs_bs_in_positions,
    read_zp_out_positions,
    script_html,
    zp_is_out,
)
from xray_eye import XrayEye, eye_in_from_cmd, eye_is_out

# The five motors every page of the workflow addresses.
ALIGNMENT_MOTORS = ("X", "Z", "trans1", "trans2", "phi")


def _step(flow, action, **kwargs):
    """Advance, asserting the action's button would have been enabled."""
    assert flow.can(action), "%s should be enabled on page %s" % (action, flow.page)
    return flow.advance(action, **kwargs)


def _unlock_radiography(flow):
    flow.xrays_confirmed = True
    flow.zp_out = True


def _unlock_movement(flow):
    flow.record_move("Z")
    flow.record_move("trans1")


def _pick_transh(flow, horizontal="trans1"):
    flow.advance(Action.TRANS1_HORIZONTAL if horizontal == "trans1"
                 else Action.TRANS2_HORIZONTAL)


def _at_positioning(expert=False, branch=Branch.SAMPLE_CHANGE):
    """Drive a flow to the rough-centre page (or the expert merge)."""
    flow = AlignmentFlow(expert=expert)
    _unlock_radiography(flow)
    flow.advance(Action.RADIO_CONTINUE)
    _unlock_movement(flow)
    flow.advance(Action.MOVE_FRESH_START if branch == Branch.FRESH_START
                 else Action.MOVE_SAMPLE_CHANGE)
    return flow


def _at_rotation_safe(expert=False, branch=Branch.SAMPLE_CHANGE,
                      horizontal="trans1"):
    """Drive a flow as far as the rotation-safety decision."""
    flow = _at_positioning(expert=expert, branch=branch)
    _pick_transh(flow, horizontal)
    if not expert:
        flow.advance(Action.ROUGH_CONTINUE)
        flow.record_move("phi")
        flow.advance(Action.ROT_CONTINUE)
    return flow


# ---------------------------------------------------------------------------
# Full walks -- user mode
# ---------------------------------------------------------------------------

def test_user_sample_change_with_full_rotation_reaches_both_trans_pages():
    flow = AlignmentFlow(expert=False)
    assert flow.page == Page.RADIOGRAPHY

    _unlock_radiography(flow)
    assert _step(flow, Action.RADIO_CONTINUE) == Page.MOVEMENT_SAFE

    _unlock_movement(flow)
    assert _step(flow, Action.MOVE_SAMPLE_CHANGE) == Page.ROUGH_CENTER
    assert flow.branch == Branch.SAMPLE_CHANGE
    assert flow.pending_hexapod_x_prompt is False

    _pick_transh(flow, "trans1")
    assert _step(flow, Action.ROUGH_CONTINUE) == Page.SET_ROTATION
    flow.record_move("phi")
    assert _step(flow, Action.ROT_CONTINUE) == Page.ROTATION_SAFE

    assert _step(flow, Action.ROTSAFE_360_OK) == Page.FIRST_TRANS
    assert flow.rotation_safe is True

    assert _step(flow, Action.FIRST_TRANS_CONTINUE) == Page.SECOND_TRANS
    assert flow.trans_h == "trans1"
    assert flow.trans_d == "trans2"

    assert _step(flow, Action.TRANS_FINISH) == CLOSE


def test_user_fresh_start_with_full_rotation_reaches_iterative():
    flow = AlignmentFlow(expert=False)
    _unlock_radiography(flow)
    flow.advance(Action.RADIO_CONTINUE)
    _unlock_movement(flow)

    assert _step(flow, Action.MOVE_FRESH_START) == Page.ROUGH_CENTER
    assert flow.branch == Branch.FRESH_START
    # The view consumes this to ask before parking hexapod X.
    assert flow.pending_hexapod_x_prompt is True

    _pick_transh(flow, "trans2")
    flow.advance(Action.ROUGH_CONTINUE)
    flow.record_move("phi")
    flow.advance(Action.ROT_CONTINUE)
    assert _step(flow, Action.ROTSAFE_360_OK) == Page.ITERATIVE


def test_the_picker_records_the_other_stage_as_trans_d():
    flow = _at_positioning()
    _pick_transh(flow, "trans2")
    assert flow.trans_h == "trans2"
    assert flow.trans_d == "trans1"
    # The picker stays on the page -- it records a choice, it does not move.
    assert flow.page == Page.ROUGH_CENTER


# ---------------------------------------------------------------------------
# The "rotation not safe" branches
# ---------------------------------------------------------------------------

def test_sample_change_without_safe_rotation_asks_about_the_cor():
    flow = _at_rotation_safe(branch=Branch.SAMPLE_CHANGE)
    flow.set_phi_low(-10.0)
    flow.set_phi_high(10.0)
    assert flow.rotation_arc_ok() is False

    assert _step(flow, Action.ROTSAFE_CONTINUE) == Page.COR_KNOWN
    assert flow.rotation_safe is False
    assert _step(flow, Action.COR_YES) == Page.SMALL_ANGLE_TRANS
    assert _step(flow, Action.TRANS_FINISH) == CLOSE


def test_unknown_cor_kicks_back_to_a_fresh_start_and_reclears_the_gates():
    flow = _at_rotation_safe(branch=Branch.SAMPLE_CHANGE)
    flow.set_phi_low(-10.0)
    flow.set_phi_high(10.0)
    flow.advance(Action.ROTSAFE_CONTINUE)

    assert _step(flow, Action.COR_NO) == Page.ROUGH_CENTER
    assert flow.branch == Branch.FRESH_START
    assert flow.pending_hexapod_x_prompt is True
    # Second pass through the positioning pages must re-lock their exits.
    assert flow.rotated_once is False
    assert flow.softlimit_set is False
    assert flow.rotation_safe is None
    assert flow.can(Action.ROT_CONTINUE) is False
    assert flow.can(Action.ROTSAFE_CONTINUE) is False
    # The transH choice survives -- it is still the same sample.
    assert flow.trans_h == "trans1"


def test_fresh_start_without_safe_rotation_ends_at_change_sample():
    flow = _at_rotation_safe(branch=Branch.FRESH_START)
    flow.set_phi_low(-10.0)
    flow.set_phi_high(10.0)
    assert _step(flow, Action.ROTSAFE_CONTINUE) == Page.CHANGE_SAMPLE


def test_start_over_resets_run_state_but_keeps_the_saved_limits():
    flow = _at_rotation_safe(branch=Branch.FRESH_START)
    flow.set_phi_low(-30.0)
    flow.set_phi_high(45.0)
    flow.set_phi_zero(5.0)
    flow.advance(Action.ROTSAFE_CONTINUE)
    assert flow.page == Page.CHANGE_SAMPLE

    assert flow.advance(Action.START_OVER) == Page.RADIOGRAPHY
    assert flow.branch is None
    assert flow.horizontal is None
    assert flow.xrays_confirmed is False
    assert flow.zp_out is False
    assert flow.moved_z is False
    assert flow.moved_lateral is False
    assert flow.rotation_safe is None
    assert flow.iter_step == ITER_STEPS[0]
    assert flow.history == ()
    # Soft limits and the zero angle are persisted settings, not run state.
    assert flow.phi_low == -30.0
    assert flow.phi_high == 45.0
    assert flow.phi_zero == 5.0


def test_rotsafe_continue_honours_an_overridden_decision():
    """The view's auto-decide dialog lets the operator take the other branch."""
    flow = _at_rotation_safe(branch=Branch.SAMPLE_CHANGE)
    flow.set_phi_low(-10.0)
    flow.set_phi_high(10.0)
    assert flow.rotation_arc_ok() is False
    # Overriding to "safe" must route as if the arc had been wide enough.
    assert flow.advance(Action.ROTSAFE_CONTINUE, rotation_safe=True) == Page.FIRST_TRANS
    assert flow.rotation_safe is True


# ---------------------------------------------------------------------------
# Expert mode
# ---------------------------------------------------------------------------

def test_expert_merges_the_three_positioning_pages():
    flow = AlignmentFlow(expert=True)
    assert _step(flow, Action.RADIO_CONTINUE) == Page.MOVEMENT_SAFE
    assert _step(flow, Action.MOVE_SAMPLE_CHANGE) == Page.POSITION_AND_ROTATION
    _pick_transh(flow)
    # SET_ROTATION and ROTATION_SAFE are skipped entirely.
    assert _step(flow, Action.ROTSAFE_360_OK) == Page.TRANS_BOTH
    assert _step(flow, Action.TRANS_FINISH) == CLOSE


def test_expert_exits_require_the_horizontal_stage_to_be_picked():
    """Regression: the expert page absorbed the rough-centre step, so its
    exits have to enforce the transH pick that ROUGH_CONTINUE enforces in
    user mode. Without this the iterative page opened with no transH and its
    step 1 -- which drives only transH -- could not be done."""
    flow = _at_positioning(expert=True, branch=Branch.FRESH_START)
    assert flow.page == Page.POSITION_AND_ROTATION
    assert flow.horizontal is None
    assert flow.can(Action.ROTSAFE_360_OK) is False
    flow.set_phi_high(200.0)
    assert flow.can(Action.ROTSAFE_CONTINUE) is False

    _pick_transh(flow, "trans2")
    assert flow.can(Action.ROTSAFE_360_OK) is True
    assert flow.can(Action.ROTSAFE_CONTINUE) is True


def test_user_mode_rotation_exits_are_not_gated_on_transh():
    """On the user route the pick was already enforced one page earlier."""
    flow = _at_rotation_safe(branch=Branch.SAMPLE_CHANGE)
    assert flow.page == Page.ROTATION_SAFE
    assert flow.can(Action.ROTSAFE_360_OK) is True


def test_expert_fresh_start_still_reaches_iterative_with_transh_set():
    flow = _at_positioning(expert=True, branch=Branch.FRESH_START)
    _pick_transh(flow, "trans1")
    assert flow.advance(Action.ROTSAFE_360_OK) == Page.ITERATIVE
    assert flow.trans_h == "trans1"
    assert flow.iterative_state()["motors"] == frozenset({"trans1"})


def test_expert_cor_no_returns_to_the_merged_positioning_page():
    flow = _at_positioning(expert=True, branch=Branch.SAMPLE_CHANGE)
    _pick_transh(flow)
    flow.set_phi_low(-10.0)
    flow.set_phi_high(10.0)
    assert flow.advance(Action.ROTSAFE_CONTINUE) == Page.COR_KNOWN
    assert flow.advance(Action.COR_NO) == Page.POSITION_AND_ROTATION


def test_expert_mode_drops_the_unlock_gating():
    flow = AlignmentFlow(expert=True)
    assert flow.can(Action.RADIO_CONTINUE) is True
    assert flow.can(Action.MOVE_SAMPLE_CHANGE) is True
    assert flow.can(Action.MOVE_FRESH_START) is True


def test_expert_mode_still_requires_a_soft_limit_before_continue():
    flow = _at_rotation_safe(expert=True)
    assert flow.can(Action.ROTSAFE_CONTINUE) is False
    flow.set_phi_high(200.0)
    assert flow.can(Action.ROTSAFE_CONTINUE) is True


def test_start_applies_a_new_mode_and_returns_to_the_first_page():
    """The view only calls start() on a fresh open, which is what keeps a
    mid-alignment checkbox change from reshuffling pages underneath."""
    flow = AlignmentFlow(expert=False)
    flow.advance(Action.RADIO_CONTINUE)
    assert flow.start(expert=True) == Page.RADIOGRAPHY
    assert flow.expert is True
    assert flow.advance(Action.MOVE_SAMPLE_CHANGE) == Page.POSITION_AND_ROTATION


def test_start_without_an_explicit_mode_keeps_the_current_one():
    flow = AlignmentFlow(expert=True)
    flow.start()
    assert flow.expert is True


# ---------------------------------------------------------------------------
# The Back button's history stack
# ---------------------------------------------------------------------------

def test_back_is_unavailable_on_the_first_page():
    flow = AlignmentFlow()
    assert flow.history == ()
    assert flow.previous_page() is None
    assert flow.can(Action.GO_BACK) is False


def test_history_records_every_forward_move():
    flow = _at_rotation_safe(branch=Branch.SAMPLE_CHANGE)
    assert flow.history == (Page.RADIOGRAPHY, Page.MOVEMENT_SAFE,
                            Page.ROUGH_CENTER, Page.SET_ROTATION)


def test_back_retraces_the_path_in_reverse():
    flow = _at_rotation_safe(branch=Branch.SAMPLE_CHANGE)
    for expected in (Page.SET_ROTATION, Page.ROUGH_CENTER,
                     Page.MOVEMENT_SAFE, Page.RADIOGRAPHY):
        assert _step(flow, Action.GO_BACK) == expected
    assert flow.can(Action.GO_BACK) is False


def test_the_picker_does_not_push_history():
    """Recording transH stays on the page, so Back must not land on it."""
    flow = _at_positioning()
    before = flow.history
    _pick_transh(flow)
    assert flow.history == before


def test_back_retraces_a_cor_kick_back_rather_than_the_graph():
    """The graph says ROUGH_CENTER comes from MOVEMENT_SAFE, but this run
    reached it from the COR question -- Back has to follow the real path."""
    flow = _at_rotation_safe(branch=Branch.SAMPLE_CHANGE)
    flow.set_phi_low(-10.0)
    flow.set_phi_high(10.0)
    flow.advance(Action.ROTSAFE_CONTINUE)
    flow.advance(Action.COR_NO)
    assert flow.page == Page.ROUGH_CENTER
    assert flow.advance(Action.GO_BACK) == Page.COR_KNOWN


def test_back_does_not_rewind_flow_state():
    flow = _at_rotation_safe(branch=Branch.FRESH_START, horizontal="trans2")
    flow.set_phi_low(-33.0)
    flow.advance(Action.GO_BACK)
    assert flow.branch == Branch.FRESH_START
    assert flow.trans_h == "trans2"
    assert flow.phi_low == -33.0
    assert flow.softlimit_set is True


def test_back_tooltip_names_the_page_and_says_nothing_is_undone():
    flow = _at_positioning()
    label = flow.destination_label(Action.GO_BACK)
    assert PAGE_TITLES[Page.MOVEMENT_SAFE] in label
    assert "no motor moves" in label


# ---------------------------------------------------------------------------
# The rotation-axes detour
# ---------------------------------------------------------------------------

def test_need_rotation_detours_and_done_comes_back():
    flow = _at_positioning()
    _pick_transh(flow)
    assert _step(flow, Action.NEED_ROTATION) == Page.ROTATION_AXES
    assert _step(flow, Action.ROTAXES_DONE) == Page.ROUGH_CENTER
    # The detour leaves the page's own state untouched.
    assert flow.trans_h == "trans1"
    assert flow.can(Action.ROUGH_CONTINUE) is True


def test_the_detour_page_has_a_title_and_is_reachable_from_the_expert_merge():
    assert Page.ROTATION_AXES in PAGE_TITLES
    flow = _at_positioning(expert=True)
    assert flow.destination(Action.NEED_ROTATION) == Page.ROTATION_AXES


# ---------------------------------------------------------------------------
# User-mode gating
# ---------------------------------------------------------------------------

def test_radiography_needs_both_xrays_and_zp_out():
    flow = AlignmentFlow(expert=False)
    assert flow.can(Action.RADIO_CONTINUE) is False
    flow.xrays_confirmed = True
    assert flow.can(Action.RADIO_CONTINUE) is False
    flow.zp_out = True
    assert flow.can(Action.RADIO_CONTINUE) is True


def test_movement_safe_needs_z_and_a_lateral_move():
    flow = AlignmentFlow(expert=False)
    assert flow.can(Action.MOVE_FRESH_START) is False
    flow.record_move("Z")
    assert flow.can(Action.MOVE_FRESH_START) is False
    flow.record_move("trans2")
    assert flow.can(Action.MOVE_FRESH_START) is True
    assert flow.can(Action.MOVE_SAMPLE_CHANGE) is True


@pytest.mark.parametrize("lateral", ["X", "trans1", "trans2"])
def test_any_lateral_motor_satisfies_the_movement_gate(lateral):
    flow = AlignmentFlow(expert=False)
    flow.record_move("Z")
    flow.record_move(lateral)
    assert flow.can(Action.MOVE_SAMPLE_CHANGE) is True


def test_rotating_phi_does_not_satisfy_the_movement_gate():
    flow = AlignmentFlow(expert=False)
    flow.record_move("Z")
    flow.record_move("phi")
    assert flow.can(Action.MOVE_SAMPLE_CHANGE) is False


def test_rough_centre_continue_needs_the_horizontal_stage():
    flow = _at_positioning()
    assert flow.can(Action.ROUGH_CONTINUE) is False
    _pick_transh(flow)
    assert flow.can(Action.ROUGH_CONTINUE) is True


def test_set_rotation_needs_one_rotation():
    flow = AlignmentFlow(expert=False)
    assert flow.can(Action.ROT_CONTINUE) is False
    flow.record_move("phi")
    assert flow.can(Action.ROT_CONTINUE) is True


@pytest.mark.parametrize("setter", ["set_phi_low", "set_phi_high"])
def test_rotation_safe_needs_one_soft_limit(setter):
    flow = AlignmentFlow(expert=False)
    assert flow.can(Action.ROTSAFE_CONTINUE) is False
    getattr(flow, setter)(12.0)
    assert flow.can(Action.ROTSAFE_CONTINUE) is True


def test_setting_the_zero_angle_alone_does_not_unlock_continue():
    flow = AlignmentFlow(expert=False)
    flow.set_phi_zero(30.0)
    assert flow.can(Action.ROTSAFE_CONTINUE) is False


# ---------------------------------------------------------------------------
# Which motors each page locks
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("page", [
    Page.MOVEMENT_SAFE, Page.ROUGH_CENTER, Page.POSITION_AND_ROTATION])
def test_x_is_locked_wherever_the_sample_is_being_centred(page):
    """Moving X takes the sample off the centre of rotation, so it needs the
    footer's Unlock all (and its confirmation) first."""
    assert locked_motors(page) == frozenset({"X"})


@pytest.mark.parametrize("page", [
    Page.RADIOGRAPHY, Page.SET_ROTATION, Page.ROTATION_SAFE,
    Page.ROTATION_AXES, Page.FIRST_TRANS, Page.SECOND_TRANS,
    Page.TRANS_BOTH, Page.SMALL_ANGLE_TRANS, Page.ITERATIVE])
def test_other_pages_lock_no_fixed_motor_up_front(page):
    assert locked_motors(page) == frozenset()


def test_z_is_never_locked():
    """Vertical motion does not disturb the centre of rotation."""
    for page in PAGE_TITLES:
        assert "Z" not in locked_motors(page)


@pytest.mark.parametrize("horizontal,downstream", [
    ("trans1", "trans2"), ("trans2", "trans1")])
def test_set_first_trans_locks_the_downstream_stage(horizontal, downstream):
    """That page aligns transH only -- transD comes later, on its own page."""
    flow = _at_rotation_safe(horizontal=horizontal)
    flow.advance(Action.ROTSAFE_360_OK)
    assert flow.page == Page.FIRST_TRANS
    assert flow.locked_rows() == frozenset({downstream})
    assert horizontal not in flow.locked_rows()


def test_locked_rows_resolves_trans_d_only_where_it_matters():
    flow = _at_rotation_safe(horizontal="trans1")
    # The static X locks still come through unchanged...
    assert flow.locked_rows(Page.MOVEMENT_SAFE) == frozenset({"X"})
    # ...and pages that align both stages lock neither.
    assert flow.locked_rows(Page.SMALL_ANGLE_TRANS) == frozenset()
    assert flow.locked_rows(Page.TRANS_BOTH) == frozenset()


def test_first_trans_locks_nothing_before_the_stage_is_picked():
    """No transH yet means no transD to lock -- the guard must not guess."""
    flow = AlignmentFlow()
    assert flow.trans_d is None
    assert flow.locked_rows(Page.FIRST_TRANS) == frozenset()


# ---------------------------------------------------------------------------
# The angle-0 safe-arc decision and soft limits
# ---------------------------------------------------------------------------

def test_default_soft_limits_are_plus_minus_270():
    flow = AlignmentFlow()
    assert flow.phi_low == PHI_LOW_DEFAULT == -270.0
    assert flow.phi_high == PHI_HIGH_DEFAULT == 270.0
    assert flow.phi_zero == 0.0
    # 270 deg either side of zero clears the 180 deg a full alignment needs.
    assert flow.rotation_arc_ok() is True


@pytest.mark.parametrize("low,high,zero,expected", [
    (-270.0, 270.0, 0.0, True),     # the defaults
    (-10.0, 10.0, 0.0, False),      # far too narrow either way
    (-300.0, 10.0, 0.0, True),      # 300 deg available below zero
    (10.0, 300.0, 0.0, True),       # 300 deg available above zero
    (0.0, 200.0, 100.0, False),     # 200 deg wide, but only 100 either side
    (0.0, 200.0, 10.0, True),       # same width, zero offset -> 190 above
    (-180.0, 0.0, 0.0, True),       # exactly 180 below zero
    (0.0, 180.0, 0.0, True),        # exactly 180 above zero
    (0.0, 179.9, 0.0, False),       # just short
])
def test_rotation_arc_is_measured_from_the_zero_angle(low, high, zero, expected):
    flow = AlignmentFlow()
    flow.phi_low = low
    flow.phi_high = high
    flow.phi_zero = zero
    assert flow.rotation_arc_ok() is expected


def test_phi_in_limits_includes_the_boundaries():
    flow = AlignmentFlow()
    flow.phi_low = -90.0
    flow.phi_high = 90.0
    assert flow.phi_in_limits(0.0) is True
    assert flow.phi_in_limits(-90.0) is True
    assert flow.phi_in_limits(90.0) is True
    assert flow.phi_in_limits(90.001) is False
    assert flow.phi_in_limits(-90.001) is False


def test_relative_phi_target_is_checked_against_the_debug_motor_position():
    """The window computes a relative move's target from pts.get_pos()."""
    pts = InstrumentsStub()
    pts.mv("phi", 85.0)
    flow = AlignmentFlow()
    flow.phi_low = -90.0
    flow.phi_high = 90.0
    assert flow.phi_in_limits(pts.get_pos("phi") + 3.0) is True
    assert flow.phi_in_limits(pts.get_pos("phi") + 10.0) is False


def test_setting_a_limit_from_a_live_position_records_it():
    pts = InstrumentsStub()
    pts.mv("phi", -47.5)
    flow = AlignmentFlow()
    flow.set_phi_low(pts.get_pos("phi"))
    assert flow.phi_low == pytest.approx(-47.5)
    assert flow.softlimit_set is True


# ---------------------------------------------------------------------------
# Iterative alignment sub-steps
# ---------------------------------------------------------------------------

def _at_iterative(horizontal="trans1"):
    flow = _at_rotation_safe(branch=Branch.FRESH_START, horizontal=horizontal)
    flow.advance(Action.ROTSAFE_360_OK)
    assert flow.page == Page.ITERATIVE
    return flow


def test_iterative_has_no_step_zero():
    """Choosing the horizontal stage moved to the rough-centre page, so the
    iterative page opens on step 1 with transH already known."""
    assert ITER_STEPS == (1, 23, 4)
    flow = _at_iterative()
    assert flow.iter_step == 1
    assert flow.can(Action.ITER_BACK) is False


def test_iterative_step1_drives_only_transh():
    flow = _at_iterative(horizontal="trans2")
    state = flow.iterative_state()
    assert state["step"] == 1
    assert state["trans_h"] == "trans2"
    assert state["trans_d"] == "trans1"
    assert state["motors"] == frozenset({"trans2"})
    assert state["btn_a"]["text"] == "Center found"
    assert state["btn_a"]["action"] == Action.ITER_NEXT
    assert state["finish"]["enabled"] is False
    assert state["phi_step"] == 180.0
    assert state["phi_editable"] is False


def test_iterative_steps_2_and_3_enable_everything_except_trans_d():
    flow = _at_iterative(horizontal="trans1")
    flow.advance(Action.ITER_NEXT)
    state = flow.iterative_state()
    assert state["step"] == 23
    assert state["motors"] == frozenset({"phi", "X", "trans1"})
    assert "trans2" not in state["motors"]
    assert state["btn_a"]["text"] == "Steps 2 & 3 complete"
    assert state["phi_step"] == 180.0
    assert state["finish"]["enabled"] is False


def test_iterative_step4_aligns_only_trans_d_and_frees_the_phi_step():
    flow = _at_iterative(horizontal="trans1")
    flow.advance(Action.ITER_NEXT)
    flow.advance(Action.ITER_NEXT)
    state = flow.iterative_state()
    assert state["step"] == 4
    # transH is already set from steps 1-3 and must not be disturbed, and X
    # is finished -- only phi and transD move.
    assert state["motors"] == frozenset({"phi", "trans2"})
    assert "trans1" not in state["motors"]
    assert "X" not in state["motors"]
    assert state["phi_step"] == 10.0
    assert state["phi_editable"] is True
    assert state["btn_a"]["text"] == ""
    assert state["btn_a"]["enabled"] is False
    assert state["btn_a"]["action"] is None
    assert state["finish"]["enabled"] is True
    assert flow.advance(Action.ITER_FINISH) == CLOSE


def test_iterative_unlock_all_is_not_a_page_button_any_more():
    """It moved to the footer, which acts on whichever page is showing."""
    state = _at_iterative().iterative_state()
    assert "btn_b" not in state


def test_iterative_back_walks_the_steps_in_reverse():
    flow = _at_iterative()
    flow.advance(Action.ITER_NEXT)
    flow.advance(Action.ITER_NEXT)
    assert flow.iter_step == 4
    for expected in (23, 1):
        assert flow.advance(Action.ITER_BACK) == Page.ITERATIVE
        assert flow.iter_step == expected
    assert flow.can(Action.ITER_BACK) is False
    # Stepping back through the iterative page never unpicks transH -- that
    # choice belongs to an earlier page now.
    assert flow.trans_h == "trans1"


def test_iterative_next_is_not_offered_at_the_last_step():
    flow = _at_iterative()
    assert flow.can(Action.ITER_NEXT) is True
    flow.advance(Action.ITER_NEXT)
    assert flow.can(Action.ITER_NEXT) is True
    flow.advance(Action.ITER_NEXT)
    assert flow.can(Action.ITER_NEXT) is False


def test_iterative_falls_back_to_both_stages_if_transh_is_somehow_unset():
    """Belt and braces: enabling nothing would strand the operator."""
    flow = AlignmentFlow()
    flow.page = Page.ITERATIVE
    flow.iter_step = 1
    assert flow.trans_h is None
    assert flow.iterative_state()["motors"] == frozenset({"trans1", "trans2"})


# ---------------------------------------------------------------------------
# The progressively revealed instruction script
# ---------------------------------------------------------------------------

def test_script_never_mentions_a_step_zero():
    for step in ITER_STEPS:
        text = " ".join(line for line, _colour in iterative_script_lines(step))
        assert "Step 0" not in text


def test_script_shows_the_current_step_black_and_the_next_one_gray():
    lines = iterative_script_lines(1)
    assert [colour for _text, colour in lines][:2] == ["black", "black"]
    assert "Step 1:" in lines[1][0]
    greyed = [line for line, colour in lines if colour == "gray"]
    assert len(greyed) == 5  # steps 2a, 2b, 2c, 3 and the "repeat" line
    assert greyed[0].startswith("Step 2a")


def test_script_omits_steps_beyond_the_next_one():
    text = " ".join(line for line, _colour in iterative_script_lines(1))
    assert "Step 4" not in text


def test_script_at_step23_grays_only_step4():
    greyed = [line for line, colour in iterative_script_lines(23)
              if colour == "gray"]
    assert len(greyed) == 1
    assert greyed[0].startswith("Step 4")


def test_script_at_the_last_step_is_entirely_black():
    lines = iterative_script_lines(4)
    assert all(colour == "black" for _line, colour in lines)
    assert any("Step 4" in line for line, _colour in lines)


def test_script_grows_monotonically():
    lengths = [len(iterative_script_lines(step)) for step in ITER_STEPS]
    assert lengths == sorted(lengths)


def test_script_html_escapes_markup_characters():
    """Step 4's text contains "<= 90 deg", which must not become a tag."""
    markup = script_html(iterative_script_lines(4))
    assert "&lt;= 90 deg" in markup
    assert "<= 90" not in markup
    assert '<span style="color:#000000;">' in markup


def test_script_html_colours_gray_lines_differently():
    markup = script_html(iterative_script_lines(1))
    assert '<span style="color:#888888;">' in markup
    assert '<span style="color:#000000;">' in markup


# ---------------------------------------------------------------------------
# Window title and page coverage
# ---------------------------------------------------------------------------

def _all_pages():
    return [value for name, value in vars(Page).items()
            if not name.startswith("_")]


def test_window_title_is_the_base_plus_the_step_name():
    assert window_title(Page.RADIOGRAPHY) == (
        "%s - %s" % (WINDOW_TITLE_BASE, PAGE_TITLES[Page.RADIOGRAPHY]))
    assert window_title(Page.ITERATIVE).endswith(PAGE_TITLES[Page.ITERATIVE])


def test_every_page_has_a_title_for_the_tooltips_and_the_window_bar():
    for page in _all_pages():
        assert page in PAGE_TITLES, "%s has no title" % page
        assert window_title(page).startswith(WINDOW_TITLE_BASE)


def test_every_page_maps_to_a_widget_in_the_ui():
    """A page added to the flow without a matching QStackedWidget page would
    otherwise only fail when the operator navigated onto it."""
    for page in _all_pages():
        assert page in PAGE_WIDGETS, "%s has no .ui page" % page
    assert len(set(PAGE_WIDGETS.values())) == len(PAGE_WIDGETS)


# ---------------------------------------------------------------------------
# Navigation tooltips
# ---------------------------------------------------------------------------

def test_tooltip_names_the_destination_page():
    flow = AlignmentFlow(expert=False)
    assert flow.destination_label(Action.RADIO_CONTINUE) == (
        "Goes to: %s" % PAGE_TITLES[Page.MOVEMENT_SAFE])


def test_tooltip_reflects_the_mode():
    user = AlignmentFlow(expert=False)
    expert = AlignmentFlow(expert=True)
    assert user.destination_label(Action.MOVE_SAMPLE_CHANGE) == (
        "Goes to: %s" % PAGE_TITLES[Page.ROUGH_CENTER])
    assert expert.destination_label(Action.MOVE_SAMPLE_CHANGE) == (
        "Goes to: %s" % PAGE_TITLES[Page.POSITION_AND_ROTATION])


def test_tooltip_reflects_the_branch_on_a_conditional_exit():
    narrow = (-10.0, 10.0)

    sample_change = _at_rotation_safe(branch=Branch.SAMPLE_CHANGE)
    sample_change.phi_low, sample_change.phi_high = narrow
    assert sample_change.destination_label(Action.ROTSAFE_CONTINUE) == (
        "Goes to: %s" % PAGE_TITLES[Page.COR_KNOWN])

    fresh = _at_rotation_safe(branch=Branch.FRESH_START)
    fresh.phi_low, fresh.phi_high = narrow
    assert fresh.destination_label(Action.ROTSAFE_CONTINUE) == (
        "Goes to: %s" % PAGE_TITLES[Page.CHANGE_SAMPLE])


def test_tooltip_for_the_360_ok_exit_also_follows_the_branch():
    sample_change = _at_rotation_safe(branch=Branch.SAMPLE_CHANGE)
    assert sample_change.destination_label(Action.ROTSAFE_360_OK) == (
        "Goes to: %s" % PAGE_TITLES[Page.FIRST_TRANS])

    fresh = _at_rotation_safe(branch=Branch.FRESH_START)
    assert fresh.destination_label(Action.ROTSAFE_360_OK) == (
        "Goes to: %s" % PAGE_TITLES[Page.ITERATIVE])


def test_tooltip_for_a_terminal_action_says_it_closes():
    flow = _at_rotation_safe()
    flow.advance(Action.ROTSAFE_360_OK)
    flow.advance(Action.FIRST_TRANS_CONTINUE)
    assert flow.destination_label(Action.TRANS_FINISH) == (
        "Finishes the alignment and closes this window")


def test_tooltip_for_back_one_step_warns_it_does_not_undo_moves():
    flow = _at_iterative()
    assert "does not undo any motor moves" in flow.destination_label(
        Action.ITER_BACK)


def test_non_navigating_actions_have_no_destination_tooltip():
    flow = _at_iterative()
    assert flow.destination_label(Action.UNLOCK_ALL) == ""


# ---------------------------------------------------------------------------
# X-ray eye: shared state through the command readback
# ---------------------------------------------------------------------------

class _MemoryPV:
    """A bo record stand-in that reads back whatever was written to it."""

    def __init__(self, name, store):
        self._name = name
        self._store = store

    def get(self):
        return self._store.get(self._name)

    def put(self, value):
        self._store[self._name] = value


def _memory_pv_factory():
    """A PV class and the record store behind it, so two XrayEye instances
    can stand in for two processes sharing one IOC."""
    store = {}
    return (lambda name: _MemoryPV(name, store)), store


class _RaisingPV:
    def __init__(self, name):
        self._name = name

    def get(self):
        raise RuntimeError("no channel access")

    def put(self, value):
        raise RuntimeError("no channel access")


def test_eye_command_readback_polarity():
    assert eye_in_from_cmd(1) is True
    assert eye_in_from_cmd(0) is False
    assert eye_in_from_cmd(None) is None
    assert eye_in_from_cmd("nonsense") is None


def test_eye_status_zero_means_out():
    """Regression guard for the inverted read that used to be in CRL_3dprint."""
    assert eye_is_out(0) is True
    assert eye_is_out(1) is False


def test_eye_state_round_trips_through_the_command_record():
    factory, _store = _memory_pv_factory()
    eye = XrayEye(factory)
    assert eye.is_in() is None  # nothing commanded yet
    eye.set_in(True)
    assert eye.is_in() is True
    assert eye.is_out() is False
    eye.set_in(False)
    assert eye.is_in() is False
    assert eye.is_out() is True


def test_eye_state_is_shared_between_two_processes():
    """The whole point: the optics GUI runs in its own process, and both see
    the same record."""
    factory, _store = _memory_pv_factory()
    optics_gui = XrayEye(factory)
    alignment_window = XrayEye(factory)

    optics_gui.set_in(True)
    assert alignment_window.is_in() is True
    alignment_window.set_in(False)
    assert optics_gui.is_in() is False


def test_eye_falls_back_to_the_local_command_when_the_pv_is_unreadable():
    eye = XrayEye(_RaisingPV)
    assert eye.is_in() is None
    try:
        eye.set_in(True)
    except RuntimeError:
        pass  # the put fails, but the intent was recorded first
    assert eye.is_in() is True


def test_eye_in_debug_mode_tracks_only_what_this_process_commanded():
    """FakePV.get() always returns 0, so the readback cannot reflect a
    command -- debug mode reports the local state instead."""
    eye = XrayEye(FakePV, debug=True)
    eye.set_in(True)
    assert eye.is_in() is True
    eye.set_in(False)
    assert eye.is_in() is False


def test_eye_in_debug_mode_starts_out_rather_than_unknown():
    """An unknown state leaves both In and Out live. The stub reports 0, so
    debug mode opens with a definite "out" and exactly one button enabled."""
    eye = XrayEye(FakePV, debug=True)
    assert eye.is_in() is False
    assert eye.is_out() is True


def test_fake_pv_put_never_raises():
    FakePV("usxRIO:Galil2Bo0_CMD").put(1)
    FakePV("usxRIO:Galil2Bo0_CMD").put(0)


# ---------------------------------------------------------------------------
# Optics positions read out of the optics GUI's .ini
# ---------------------------------------------------------------------------

def _write_optics_ini(path, **sections):
    parser = configparser.ConfigParser()
    for name, entries in sections.items():
        parser[name] = {key: str(value) for key, value in entries.items()}
    with open(str(path), "w") as handle:
        parser.write(handle)
    return str(path)


def test_zp_out_positions_are_read_from_the_optics_ini(tmp_path):
    path = _write_optics_ini(tmp_path / "optics_motors.ini",
                             zp={"out_0": "0.4999", "out_1": "0.0000"})
    assert read_zp_out_positions(path) == (pytest.approx(0.4999),
                                           pytest.approx(0.0))


def test_saxs_beamstop_in_positions_are_read_from_the_optics_ini(tmp_path):
    path = _write_optics_ini(tmp_path / "optics_motors.ini",
                             SAXSbs={"in_0": "1.25", "in_1": "-0.5"})
    # (vertical, horizontal) -- 12ideSFT:m4 is the horizontal one, in_1.
    assert read_saxs_bs_in_positions(path) == (pytest.approx(1.25),
                                               pytest.approx(-0.5))


def test_positions_are_unknown_when_unset(tmp_path):
    """optics_motors seeds these as empty strings on a fresh install."""
    path = _write_optics_ini(tmp_path / "optics_motors.ini",
                             zp={"out_0": "", "out_1": ""})
    assert read_zp_out_positions(path) == (None, None)


def test_positions_are_unknown_without_the_section(tmp_path):
    path = _write_optics_ini(tmp_path / "optics_motors.ini")
    assert read_zp_out_positions(path) == (None, None)
    assert read_saxs_bs_in_positions(path) == (None, None)


def test_positions_are_unknown_when_the_file_is_missing(tmp_path):
    assert read_zp_out_positions(str(tmp_path / "nope.ini")) == (None, None)


def test_positions_are_unknown_when_only_one_axis_is_saved(tmp_path):
    path = _write_optics_ini(tmp_path / "optics_motors.ini",
                             SAXSbs={"in_0": "0.5"})
    assert read_saxs_bs_in_positions(path) == (None, None)


def test_read_block_positions_handles_both_kinds(tmp_path):
    path = _write_optics_ini(
        tmp_path / "optics_motors.ini",
        bs={"in_0": "1", "in_1": "2", "out_0": "3", "out_1": "4"})
    assert read_block_positions(path, "bs", "in") == (1.0, 2.0)
    assert read_block_positions(path, "bs", "out") == (3.0, 4.0)


@pytest.mark.parametrize("readback,expected", [
    (0.5, True),
    (0.504, True),      # inside the 0.005 tolerance
    (0.496, True),
    (0.51, False),      # outside it
    (0.0, False),
])
def test_zp_is_out_uses_the_optics_gui_tolerance(readback, expected):
    # Exact-boundary readbacks are left out on purpose: 0.5 + ZP_OUT_THRESH is
    # 0.5050000000000000044 in binary floating point, so "on the tolerance" is
    # not a well-defined case to assert on.
    assert ZP_OUT_THRESH == 0.005
    assert zp_is_out(readback, 0.5) is expected


def test_zp_is_out_is_false_when_either_value_is_unknown():
    assert zp_is_out(None, 0.5) is False
    assert zp_is_out(0.5, None) is False


# ---------------------------------------------------------------------------
# The optics GUI's SAXS beamstop block and All In/Out toggle
# ---------------------------------------------------------------------------

def test_optics_ini_defaults_cover_the_saxs_beamstop_block():
    assert OPTICS_INI_DEFAULTS["SAXSbs"] == {
        "in_0": "", "in_1": "", "out_0": "", "out_1": ""}


def test_all_in_out_toggle_defaults_to_excluding_the_saxs_beamstop():
    """All Out retracting a beamstop an experiment relies on is a worse
    surprise than having to move it by hand."""
    assert OPTICS_INI_DEFAULTS["options"]["saxsbs_in_all"] == "0"


def test_saxsbs_in_all_round_trips_through_the_ini(tmp_path):
    path = str(tmp_path / "optics_motors.ini")
    assert read_saxsbs_in_all(path) is False  # missing file
    write_saxsbs_in_all(True, path)
    assert read_saxsbs_in_all(path) is True
    write_saxsbs_in_all(False, path)
    assert read_saxsbs_in_all(path) is False


def test_new_defaults_backfill_without_disturbing_saved_positions(tmp_path):
    """An existing installation's .ini must gain the new entries and keep
    everything it already had."""
    path = _write_optics_ini(
        tmp_path / "optics_motors.ini",
        zp={"out_0": "0.4999", "out_1": "0.0000"},
        osa={"in_0": "0.2266", "in_1": "-0.3668"})

    assert ensure_ini_defaults(path, OPTICS_INI_DEFAULTS) is True

    cfg = configparser.ConfigParser()
    cfg.read(path)
    assert cfg["zp"]["out_0"] == "0.4999"      # untouched
    assert cfg["osa"]["in_1"] == "-0.3668"     # untouched
    assert cfg["osa"]["out_0"] == ""           # back-filled
    assert cfg.has_section("SAXSbs")           # new block
    assert cfg["SAXSbs"]["in_1"] == ""
    assert cfg["options"]["saxsbs_in_all"] == "0"


# ---------------------------------------------------------------------------
# The debug-mode motor object the window drives
# ---------------------------------------------------------------------------

def test_debug_stub_exposes_every_motor_the_workflow_uses():
    pts = InstrumentsStub()
    for name in ALIGNMENT_MOTORS:
        assert name in pts.motornames, "%s missing from the debug stub" % name


def test_debug_stub_motors_start_at_zero():
    pts = InstrumentsStub()
    for name in ALIGNMENT_MOTORS:
        assert pts.get_pos(name) == 0.0


def test_debug_stub_tweaks_accumulate():
    """Each << / >> press is one mvr of the row's step size."""
    pts = InstrumentsStub()
    for _ in range(4):
        pts.mvr("trans1", 0.25)
    assert pts.get_pos("trans1") == pytest.approx(1.0)
    pts.mvr("trans1", -0.25)
    assert pts.get_pos("trans1") == pytest.approx(0.75)


def test_debug_stub_absolute_move_parks_hexapod_x_at_the_cor_default():
    pts = InstrumentsStub()
    pts.mv("X", HEXAPOD_X_COR_DEFAULT)
    assert pts.get_pos("X") == pytest.approx(1.3)


def test_debug_stub_reports_every_motor_connected():
    pts = InstrumentsStub()
    for name in ALIGNMENT_MOTORS:
        assert pts.isconnected(name) is True
