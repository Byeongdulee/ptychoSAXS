"""pytest suite for the sample alignment workflow's debug-mode contract.

In scope:
  - alignment_flow.AlignmentFlow: page routing for both branches and both
    modes, the per-page unlock gating, the angle-0 safe-arc decision, phi
    soft-limit checks, the iterative sub-step machine, and the navigation
    tooltip strings.
  - alignment_flow.iterative_script_lines: the progressive black/gray/omitted
    reveal of the iterative instruction script.
  - sample_alignment's pure helpers: eye_is_out (the X-ray-eye polarity this
    workflow and CRL_3dprint both depend on), read_zp_out_positions,
    zp_is_out, and script_html.
  - InstrumentsStub: the debug-mode motor object the window drives, checked
    for the five axes and the mv/mvr/get_pos behaviour it relies on.
  - FakePV: the debug-mode stand-in for the eye and ZP channels.

Out of scope (deliberately NOT tested here):
  - Real hexapod / ACS phi / SmarAct gonio motion, and real EPICS channels.
  - Any Qt widget rendering, QApplication event loop, or click-through of
    sample_alignment.ui. No QApplication is created; sample_alignment is
    imported only for its module-level pure functions, the same way
    test_crl3dprint_debug.py imports CRL_3dprint.
"""
import configparser

import pytest

from alignment_flow import (
    CLOSE,
    ITER_STEPS,
    PAGE_TITLES,
    Action,
    AlignmentFlow,
    Branch,
    Page,
    iterative_script_lines,
)
from debug_stubs import FakePV, InstrumentsStub
from sample_alignment import (
    HEXAPOD_X_COR_DEFAULT,
    PAGE_WIDGETS,
    ZP_OUT_THRESH,
    eye_is_out,
    read_zp_out_positions,
    script_html,
    zp_is_out,
)

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


def _at_rotation_safe(expert=False, branch=Branch.SAMPLE_CHANGE):
    """Drive a flow as far as the rotation-safety decision."""
    flow = AlignmentFlow(expert=expert)
    _unlock_radiography(flow)
    flow.advance(Action.RADIO_CONTINUE)
    _unlock_movement(flow)
    flow.advance(Action.MOVE_FRESH_START if branch == Branch.FRESH_START
                 else Action.MOVE_SAMPLE_CHANGE)
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

    assert _step(flow, Action.ROUGH_CONTINUE) == Page.SET_ROTATION
    flow.record_move("phi")
    assert _step(flow, Action.ROT_CONTINUE) == Page.ROTATION_SAFE

    assert _step(flow, Action.ROTSAFE_360_OK) == Page.FIRST_TRANS
    assert flow.rotation_safe is True

    assert _step(flow, Action.TRANS1_HORIZONTAL) == Page.SECOND_TRANS
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

    flow.advance(Action.ROUGH_CONTINUE)
    flow.record_move("phi")
    flow.advance(Action.ROT_CONTINUE)
    assert _step(flow, Action.ROTSAFE_360_OK) == Page.ITERATIVE


def test_trans2_horizontal_picks_the_other_stage_as_trans_d():
    flow = _at_rotation_safe()
    flow.advance(Action.ROTSAFE_360_OK)
    assert flow.advance(Action.TRANS2_HORIZONTAL) == Page.SECOND_TRANS
    assert flow.trans_h == "trans2"
    assert flow.trans_d == "trans1"


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
    assert flow.xrays_confirmed is False
    assert flow.zp_out is False
    assert flow.moved_z is False
    assert flow.moved_lateral is False
    assert flow.rotation_safe is None
    assert flow.iter_step == 0
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
    # SET_ROTATION and ROTATION_SAFE are skipped entirely.
    assert _step(flow, Action.ROTSAFE_360_OK) == Page.TRANS_BOTH


def test_expert_merges_the_two_trans_pages():
    flow = AlignmentFlow(expert=True)
    flow.advance(Action.RADIO_CONTINUE)
    flow.advance(Action.MOVE_SAMPLE_CHANGE)
    flow.advance(Action.ROTSAFE_360_OK)
    assert flow.page == Page.TRANS_BOTH
    # The horizontal picker records the choice without leaving the page.
    assert flow.advance(Action.TRANS1_HORIZONTAL) == Page.TRANS_BOTH
    assert flow.trans_h == "trans1"
    assert flow.trans_d == "trans2"
    assert flow.advance(Action.TRANS_FINISH) == CLOSE


def test_expert_fresh_start_still_reaches_iterative():
    flow = AlignmentFlow(expert=True)
    flow.advance(Action.RADIO_CONTINUE)
    assert flow.advance(Action.MOVE_FRESH_START) == Page.POSITION_AND_ROTATION
    assert flow.advance(Action.ROTSAFE_360_OK) == Page.ITERATIVE


def test_expert_cor_no_returns_to_the_merged_positioning_page():
    flow = AlignmentFlow(expert=True)
    flow.advance(Action.RADIO_CONTINUE)
    flow.advance(Action.MOVE_SAMPLE_CHANGE)
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
    """Only the radiography and movement-safe gates are dropped."""
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


def test_360_ok_is_always_available():
    flow = AlignmentFlow(expert=False)
    assert flow.can(Action.ROTSAFE_360_OK) is True


# ---------------------------------------------------------------------------
# The angle-0 safe-arc decision and soft limits
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("low,high,zero,expected", [
    (-540.0, 540.0, 0.0, True),     # the defaults
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

def _at_iterative():
    flow = _at_rotation_safe(branch=Branch.FRESH_START)
    flow.advance(Action.ROTSAFE_360_OK)
    assert flow.page == Page.ITERATIVE
    return flow


def test_iterative_step0_offers_only_the_horizontal_picker():
    flow = _at_iterative()
    state = flow.iterative_state()
    assert state["step"] == 0
    assert state["motors"] == frozenset({"trans1", "trans2"})
    assert state["prompt"] == "Horizontal motor is:"
    assert state["btn_a"]["text"] == "trans1"
    assert state["btn_b"]["text"] == "trans2"
    assert state["btn_a"]["action"] == Action.TRANS1_HORIZONTAL
    assert state["btn_b"]["action"] == Action.TRANS2_HORIZONTAL
    assert state["back"]["enabled"] is False
    assert state["finish"]["enabled"] is False
    assert state["phi_step"] == 180.0
    assert state["phi_editable"] is False


def test_iterative_picker_advances_to_step1_and_names_the_stages():
    flow = _at_iterative()
    assert flow.advance(Action.TRANS2_HORIZONTAL) == Page.ITERATIVE
    state = flow.iterative_state()
    assert state["step"] == 1
    assert state["trans_h"] == "trans2"
    assert state["trans_d"] == "trans1"
    # Only transH moves while the operator marks the centre.
    assert state["motors"] == frozenset({"trans2"})
    assert state["prompt"] == ""
    assert state["btn_a"]["text"] == "Center found"
    assert state["btn_b"]["text"] == "Unlock all"
    assert state["btn_b"]["action"] == Action.UNLOCK_ALL
    assert state["back"]["enabled"] is True
    assert state["finish"]["enabled"] is False


def test_iterative_steps_2_and_3_enable_everything_except_trans_d():
    flow = _at_iterative()
    flow.advance(Action.TRANS1_HORIZONTAL)
    flow.advance(Action.ITER_NEXT)
    state = flow.iterative_state()
    assert state["step"] == 23
    assert state["motors"] == frozenset({"phi", "X", "trans1"})
    assert "trans2" not in state["motors"]
    assert state["btn_a"]["text"] == "Steps 2 & 3 complete"
    assert state["phi_step"] == 180.0
    assert state["finish"]["enabled"] is False


def test_iterative_step4_enables_everything_except_x_and_frees_the_phi_step():
    flow = _at_iterative()
    flow.advance(Action.TRANS1_HORIZONTAL)
    flow.advance(Action.ITER_NEXT)
    flow.advance(Action.ITER_NEXT)
    state = flow.iterative_state()
    assert state["step"] == 4
    assert state["motors"] == frozenset({"phi", "trans1", "trans2"})
    assert "X" not in state["motors"]
    assert state["phi_step"] == 10.0
    assert state["phi_editable"] is True
    assert state["btn_a"]["text"] == ""
    assert state["btn_a"]["enabled"] is False
    assert state["btn_a"]["action"] is None
    assert state["finish"]["enabled"] is True
    assert flow.can(Action.ITER_FINISH) is True
    assert flow.advance(Action.ITER_FINISH) == CLOSE


def test_iterative_back_walks_the_steps_in_reverse():
    flow = _at_iterative()
    flow.advance(Action.TRANS1_HORIZONTAL)
    flow.advance(Action.ITER_NEXT)
    flow.advance(Action.ITER_NEXT)
    assert flow.iter_step == 4
    for expected in (23, 1, 0):
        assert flow.advance(Action.ITER_BACK) == Page.ITERATIVE
        assert flow.iter_step == expected
    # Back at the picker, the transH/transD choice is undone.
    assert flow.trans_h is None
    assert flow.trans_d is None
    assert flow.can(Action.ITER_BACK) is False


def test_iterative_finish_is_locked_until_the_last_step():
    flow = _at_iterative()
    for step in ITER_STEPS[:-1]:
        assert flow.iter_step == step
        assert flow.can(Action.ITER_FINISH) is False
        flow.advance(Action.TRANS1_HORIZONTAL if step == 0 else Action.ITER_NEXT)
    assert flow.iter_step == ITER_STEPS[-1]
    assert flow.can(Action.ITER_FINISH) is True


def test_iterative_next_is_not_offered_at_the_first_or_last_step():
    flow = _at_iterative()
    assert flow.can(Action.ITER_NEXT) is False  # step 0 uses the picker
    flow.advance(Action.TRANS1_HORIZONTAL)
    assert flow.can(Action.ITER_NEXT) is True
    flow.advance(Action.ITER_NEXT)
    assert flow.can(Action.ITER_NEXT) is True
    flow.advance(Action.ITER_NEXT)
    assert flow.can(Action.ITER_NEXT) is False  # step 4 is terminal


# ---------------------------------------------------------------------------
# The progressively revealed instruction script
# ---------------------------------------------------------------------------

def test_script_shows_the_current_step_black_and_the_next_one_gray():
    lines = iterative_script_lines(0)
    assert [colour for _text, colour in lines] == ["black", "black", "gray"]
    assert "Step 0:" in lines[1][0]
    assert "Step 1:" in lines[2][0]


def test_script_omits_steps_beyond_the_next_one():
    text = " ".join(line for line, _colour in iterative_script_lines(0))
    assert "Step 2a" not in text
    assert "Step 4" not in text


def test_script_grays_the_whole_next_group():
    lines = iterative_script_lines(1)
    greyed = [line for line, colour in lines if colour == "gray"]
    assert len(greyed) == 5  # steps 2a, 2b, 2c, 3 and the "repeat" line
    assert greyed[0].startswith("Step 2a")
    assert greyed[-1].startswith("Repeat steps 2-3")


def test_script_at_step23_grays_only_step4():
    lines = iterative_script_lines(23)
    greyed = [line for line, colour in lines if colour == "gray"]
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
    markup = script_html(iterative_script_lines(0))
    assert '<span style="color:#888888;">' in markup
    assert '<span style="color:#000000;">' in markup


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
    flow.advance(Action.TRANS1_HORIZONTAL)
    assert flow.destination_label(Action.TRANS_FINISH) == (
        "Finishes the alignment and closes this window")


def test_tooltip_for_back_one_step_warns_it_does_not_undo_moves():
    flow = _at_iterative()
    label = flow.destination_label(Action.ITER_BACK)
    assert "does not undo any motor moves" in label


def test_non_navigating_actions_have_no_destination_tooltip():
    flow = _at_iterative()
    assert flow.destination_label(Action.UNLOCK_ALL) == ""


def _all_pages():
    return [value for name, value in vars(Page).items()
            if not name.startswith("_")]


def test_every_page_has_a_title_for_tooltips_to_name():
    for page in _all_pages():
        assert page in PAGE_TITLES, "%s has no title" % page


def test_every_page_maps_to_a_widget_in_the_ui():
    """A page added to the flow without a matching QStackedWidget page would
    otherwise only fail when the operator navigated onto it."""
    for page in _all_pages():
        assert page in PAGE_WIDGETS, "%s has no .ui page" % page
    assert len(set(PAGE_WIDGETS.values())) == len(PAGE_WIDGETS)


# ---------------------------------------------------------------------------
# X-ray eye polarity
# ---------------------------------------------------------------------------

def test_eye_status_zero_means_out():
    """Regression guard for the inverted read fixed in CRL_3dprint."""
    assert eye_is_out(0) is True
    assert eye_is_out(1) is False


def test_debug_mode_reports_the_eye_as_out():
    """FakePV.get() returns 0, so debug mode starts with the eye retracted."""
    assert eye_is_out(FakePV("usxRIO:Galil2Bo0_STATUS.VAL").get()) is True


def test_fake_pv_put_never_raises():
    FakePV("usxRIO:Galil2Bo0_CMD").put(1)
    FakePV("usxRIO:Galil2Bo0_CMD").put(0)


# ---------------------------------------------------------------------------
# Zone plate Out positions
# ---------------------------------------------------------------------------

def _write_zp_ini(path, **zp):
    parser = configparser.ConfigParser()
    if zp:
        parser["zp"] = {key: str(value) for key, value in zp.items()}
    with open(str(path), "w") as handle:
        parser.write(handle)
    return str(path)


def test_zp_out_positions_are_read_from_the_optics_ini(tmp_path):
    path = _write_zp_ini(tmp_path / "optics_motors.ini",
                         out_0="0.4999", out_1="0.0000")
    assert read_zp_out_positions(path) == (pytest.approx(0.4999),
                                           pytest.approx(0.0))


def test_zp_out_positions_are_unknown_when_unset(tmp_path):
    """optics_motors seeds out_0/out_1 as empty strings on a fresh install."""
    path = _write_zp_ini(tmp_path / "optics_motors.ini", out_0="", out_1="")
    assert read_zp_out_positions(path) == (None, None)


def test_zp_out_positions_are_unknown_without_a_zp_section(tmp_path):
    path = _write_zp_ini(tmp_path / "optics_motors.ini")
    assert read_zp_out_positions(path) == (None, None)


def test_zp_out_positions_are_unknown_when_the_file_is_missing(tmp_path):
    assert read_zp_out_positions(str(tmp_path / "nope.ini")) == (None, None)


def test_zp_out_positions_are_unknown_when_only_one_axis_is_saved(tmp_path):
    path = _write_zp_ini(tmp_path / "optics_motors.ini", out_0="0.5")
    assert read_zp_out_positions(path) == (None, None)


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
