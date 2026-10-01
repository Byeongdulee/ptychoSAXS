"""Sample-alignment workflow state machine.

Deliberately Qt-free: this module owns every routing, gating and labelling
decision the sample alignment window makes, so all of that logic can be tested
headlessly (see tests/test_sample_alignment_flow.py) without a QApplication,
a display, or hardware. gui/sample_alignment.py is a thin view over it -- it
renders pages and drives motors, but never decides where a button goes.

The workflow guides an operator through aligning a sample on the tomography
stage. Two branches hang off the "Check movement safe" page:

  sample change -- the tomography centre of rotation is still trusted, so only
                   the trans stages are touched.
  fresh start   -- the centre of rotation is lost, so hexapod X is reset and
                   the full iterative alignment is run.

"Expert" mode merges pages for operators who already know the procedure; it
never changes the route, only how many screens the route is spread across.
"""

# Prefixed to every window title, with the current step's name after a dash.
WINDOW_TITLE_BASE = "Sample Alignment"


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

class Page:
    """QStackedWidget page identifiers.

    POSITION_AND_ROTATION and TRANS_BOTH are the expert-mode merges; the pages
    they replace are simply never visited in that mode (and vice versa).
    """

    RADIOGRAPHY = "radiography"
    MOVEMENT_SAFE = "movement_safe"
    ROUGH_CENTER = "rough_center"
    ROTATION_AXES = "rotation_axes"  # side trip off the rough-centre page
    SET_ROTATION = "set_rotation"
    ROTATION_SAFE = "rotation_safe"
    POSITION_AND_ROTATION = "position_and_rotation"  # expert: rough+rotation+safe
    FIRST_TRANS = "first_trans"
    SECOND_TRANS = "second_trans"
    TRANS_BOTH = "trans_both"  # expert: first+second trans
    COR_KNOWN = "cor_known"
    SMALL_ANGLE_TRANS = "small_angle_trans"
    ITERATIVE = "iterative"
    CHANGE_SAMPLE = "change_sample"


# Human-readable step names. This is the single place they are written: they
# name the destination in every navigation tooltip AND appear after the dash
# in the window title, so editing one here changes both.
PAGE_TITLES = {
    Page.RADIOGRAPHY: "Check radiography mode",
    Page.MOVEMENT_SAFE: "Check movement safe",
    Page.ROUGH_CENTER: "Roughly find sample center",
    Page.ROTATION_AXES: "Align rotation to trans axes",
    Page.SET_ROTATION: "Set rotation",
    Page.ROTATION_SAFE: "Check rotation safe",
    Page.POSITION_AND_ROTATION: "Find sample center and set rotation",
    Page.FIRST_TRANS: "Set first trans",
    Page.SECOND_TRANS: "Set second trans",
    Page.TRANS_BOTH: "Set both trans stages",
    Page.COR_KNOWN: "Is the center of rotation known?",
    Page.SMALL_ANGLE_TRANS: "Set trans with small angles",
    Page.ITERATIVE: "Iterative alignment",
    Page.CHANGE_SAMPLE: "Change sample for better alignment",
}


def window_title(page):
    """"Sample Alignment - <step>" for the title bar."""
    return "%s - %s" % (WINDOW_TITLE_BASE, PAGE_TITLES[page])


# Sentinel destination: the action finishes the workflow and closes the window.
CLOSE = "__close__"


class Branch:
    """Which half of the workflow the operator chose on the movement-safe page.

    SAMPLE_CHANGE trusts the existing centre of rotation and only touches the
    trans stages; FRESH_START re-establishes it, resetting hexapod X and
    running the full iterative alignment.
    """

    SAMPLE_CHANGE = "sample_change"
    FRESH_START = "fresh_start"


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

class Action:
    """One per button that changes flow state.

    UNLOCK_ALL is the odd one out: it is the footer's view-local escape hatch
    that enables whatever motor rows the current page has disabled, for that
    visit only. It is listed here for completeness but never navigates --
    destination() returns None for it.
    """

    RADIO_CONTINUE = "radio_continue"
    MOVE_FRESH_START = "move_fresh_start"
    MOVE_SAMPLE_CHANGE = "move_sample_change"
    ROUGH_CONTINUE = "rough_continue"
    NEED_ROTATION = "need_rotation"
    ROTAXES_DONE = "rotaxes_done"
    ROT_CONTINUE = "rot_continue"
    ROTSAFE_360_OK = "rotsafe_360_ok"
    ROTSAFE_CONTINUE = "rotsafe_continue"
    TRANS1_HORIZONTAL = "trans1_horizontal"
    TRANS2_HORIZONTAL = "trans2_horizontal"
    FIRST_TRANS_CONTINUE = "first_trans_continue"
    TRANS_FINISH = "trans_finish"
    COR_YES = "cor_yes"
    COR_NO = "cor_no"
    ITER_NEXT = "iter_next"
    ITER_BACK = "iter_back"
    ITER_FINISH = "iter_finish"
    UNLOCK_ALL = "unlock_all"
    GO_BACK = "go_back"
    START_OVER = "start_over"


# Actions that move the user to another page. The view gives exactly these a
# destination tooltip; everything else keeps a descriptive one.
NAV_ACTIONS = frozenset({
    Action.RADIO_CONTINUE,
    Action.MOVE_FRESH_START,
    Action.MOVE_SAMPLE_CHANGE,
    Action.ROUGH_CONTINUE,
    Action.NEED_ROTATION,
    Action.ROTAXES_DONE,
    Action.ROT_CONTINUE,
    Action.ROTSAFE_360_OK,
    Action.ROTSAFE_CONTINUE,
    Action.FIRST_TRANS_CONTINUE,
    Action.TRANS_FINISH,
    Action.COR_YES,
    Action.COR_NO,
    Action.ITER_NEXT,
    Action.ITER_BACK,
    Action.ITER_FINISH,
    Action.GO_BACK,
    Action.START_OVER,
})


# ---------------------------------------------------------------------------
# Iterative alignment sub-steps
# ---------------------------------------------------------------------------

# Sub-step ids. Step 0 (choosing which trans stage is horizontal) is no longer
# one of them -- that question is asked once, on the rough-centre page -- so
# the iterative page opens at step 1 with both stages already labelled. 23 is
# the combined "steps 2 & 3" loop the operator repeats until satisfied.
ITER_STEPS = (1, 23, 4)

# The on-screen script, revealed progressively. There is no step 0 here:
# working out which trans stage is horizontal has already happened, back on
# the rough-centre page, so transH and transD are known by the time this page
# is reached.
ITER_PREAMBLE = (
    "Iterative alignment of rotation, X-rays, and sample\n"
    "The goal here is to simultaneously set the center of rotation, X-ray beam   "
    "path, and sample at the same point in 3d space. This is accomplished with   "
    "an iterative algorithm, requiring just the phi, X, and transH - the trans   "
    "stage you identified as horizontal.",
)

ITER_STEP_LINES = {
    1: (
        " ",
        "Step 1: Roughly place the sample at the center of the X-ray eye using   "
        "transH, mark that spot.", " ",
    ),
    23: (
        " "
        "Step 2a: Rotate phi 0 -> 180 deg.",
        "Step 2b: Use transH to move the sample halfway back to the mark.",
        "Step 2c: Rotate phi -> 0 deg, make sure sample moves less, otherwise   "
        "try again.", " ",
        "Step 3: Use X to bring the sample back to the mark.",
        "Repeat steps 2-3 until satisfied.", " ",
    ),
    4: (
        " "
        "Step 4: Align transD, the unused trans motor, by rotating phi by "
        "<= 90 deg.",
    ),
}

# Phi tweak step on the iterative page: a fixed 180 deg for the step 2a/2c
# flips, dropping to an editable 10 deg for the small step-4 rotations.
ITER_PHI_STEP = 180.0
ITER_PHI_STEP_FINAL = 10.0

# Motor rows each page starts with disabled, keyed by page. X is locked
# wherever the sample is being centred, because moving it takes the sample off
# the centre of rotation -- the footer's "Unlock all" button, behind a
# confirmation, is the only way to enable it. The iterative page is absent
# here because it locks per sub-step; iterative_state() reports that instead.
PAGE_LOCKED_MOTORS = {
    Page.MOVEMENT_SAFE: frozenset({"X"}),
    Page.ROUGH_CENTER: frozenset({"X"}),
    Page.POSITION_AND_ROTATION: frozenset({"X"}),
}

# Pages that align transH and must leave the downstream stage alone. Which
# real motor that is only becomes known once transH is picked, so it cannot
# live in the static table above -- see AlignmentFlow.locked_rows.
PAGES_LOCKING_TRANS_D = frozenset({Page.FIRST_TRANS})


def locked_motors(page):
    """Row keys `page` disables regardless of the transH choice."""
    return PAGE_LOCKED_MOTORS.get(page, frozenset())


# Rotating this far either side of the zero angle is enough for the 180 deg
# flip that the trans and iterative alignments both depend on.
SAFE_ARC_DEG = 180.0

# Phi soft limits and zero angle, used until the operator sets their own.
PHI_LOW_DEFAULT = -270.0
PHI_HIGH_DEFAULT = 270.0
PHI_ZERO_DEFAULT = 0.0


def iterative_script_lines(step):
    """Return [(text, colour)] for the iterative page's instruction label.

    Everything up to and including `step` is black, the next step is gray, and
    later steps are omitted entirely -- so the operator sees where they are and
    what is coming, without the full wall of text up front.
    """
    lines = [(text, "black") for text in ITER_PREAMBLE]
    index = ITER_STEPS.index(step)
    for reached in ITER_STEPS[: index + 1]:
        lines.extend((text, "black") for text in ITER_STEP_LINES[reached])
    if index + 1 < len(ITER_STEPS):
        upcoming = ITER_STEPS[index + 1]
        lines.extend((text, "gray") for text in ITER_STEP_LINES[upcoming])
    return lines


# ---------------------------------------------------------------------------
# The state machine
# ---------------------------------------------------------------------------

class AlignmentFlow:
    """Routing, gating and labelling for the sample alignment workflow."""

    def __init__(self, expert=False):
        self.expert = bool(expert)
        # Soft limits and the zero angle persist across runs (the view loads
        # them from QSettings), so start() must not reset them.
        self.phi_low = PHI_LOW_DEFAULT
        self.phi_high = PHI_HIGH_DEFAULT
        self.phi_zero = PHI_ZERO_DEFAULT
        self.start()

    # -- lifecycle ---------------------------------------------------------

    def start(self, expert=None):
        """Reset to the first page and clear all per-run state.

        Expert mode is only re-read here -- at the start of a run -- so that
        toggling the checkbox mid-alignment never reshuffles pages underneath
        the operator.
        """
        if expert is not None:
            self.expert = bool(expert)
        self.page = Page.RADIOGRAPHY
        self._history = []
        self.branch = None
        self.horizontal = None  # "trans1" or "trans2", picked on the rough page
        self.rotation_safe = None
        # Radiography page unlock chain
        self.xrays_confirmed = False
        self.camera_issue = False
        self.zp_out = False
        # Movement-safe page unlock chain
        self.moved_z = False
        self.moved_lateral = False
        # Set-rotation / rotation-safe unlock chains
        self.rotated_once = False
        self.softlimit_set = False
        # Iterative page
        self.iter_step = ITER_STEPS[0]
        # Set when the fresh-start branch is entered; the view consumes it to
        # ask permission before moving hexapod X to its default.
        self.pending_hexapod_x_prompt = False
        return self.page

    # -- history -----------------------------------------------------------

    @property
    def history(self):
        """Pages visited on the way here, oldest first. Read-only view."""
        return tuple(self._history)

    def previous_page(self):
        """Where Back would land, or None on the first page."""
        return self._history[-1] if self._history else None

    def back(self):
        """Pop one page off the history.

        Only the page changes: the branch, the transH choice and the soft
        limits are all left alone, and no motor is moved back. Retracing a
        path is for re-reading a screen, not for undoing work.
        """
        if self._history:
            self.page = self._history.pop()
        return self.page

    # -- soft limits -------------------------------------------------------

    def rotation_arc_ok(self):
        """True if a 180 deg arc fits either side of the zero angle.

        The downstream trans and iterative alignments both need a 0 -> 180 deg
        flip, so a usable range is one that reaches 180 deg away from zero in
        at least one direction -- not merely one that is 180 deg wide.
        """
        return ((self.phi_high - self.phi_zero) >= SAFE_ARC_DEG
                or (self.phi_zero - self.phi_low) >= SAFE_ARC_DEG)

    def phi_in_limits(self, target):
        return self.phi_low <= target <= self.phi_high

    def set_phi_low(self, value):
        self.phi_low = float(value)
        self.softlimit_set = True

    def set_phi_high(self, value):
        self.phi_high = float(value)
        self.softlimit_set = True

    def set_phi_zero(self, value):
        self.phi_zero = float(value)

    # -- motion bookkeeping (drives the unlock chains) ---------------------

    def record_move(self, motor):
        """Note that `motor` was tweaked, for the pages that gate on it."""
        if motor == "Z":
            self.moved_z = True
        elif motor in ("X", "trans1", "trans2"):
            self.moved_lateral = True
        elif motor == "phi":
            self.rotated_once = True

    # -- gating ------------------------------------------------------------

    def can(self, action):
        """True if `action`'s button should be enabled right now.

        Expert mode drops the radiography and movement-safe unlock chains --
        the pages are still shown, they just are not locked.
        """
        if action == Action.RADIO_CONTINUE:
            return self.expert or (self.xrays_confirmed and self.zp_out)
        if action in (Action.MOVE_FRESH_START, Action.MOVE_SAMPLE_CHANGE):
            return self.expert or (self.moved_z and self.moved_lateral)
        if action == Action.ROUGH_CONTINUE:
            # Every later page labels its rows transH/transD, so the choice
            # has to be made before leaving this page.
            return self.horizontal is not None
        if action == Action.ROT_CONTINUE:
            return self.rotated_once
        if action == Action.ROTSAFE_360_OK:
            return self._transh_chosen_if_needed()
        if action == Action.ROTSAFE_CONTINUE:
            return self.softlimit_set and self._transh_chosen_if_needed()
        if action in (Action.GO_BACK, Action.ROTAXES_DONE):
            return bool(self._history)
        if action == Action.ITER_BACK:
            return self.iter_step != ITER_STEPS[0]
        if action == Action.ITER_FINISH:
            return self.iter_step == ITER_STEPS[-1]
        if action == Action.ITER_NEXT:
            return self.iter_step != ITER_STEPS[-1]
        return True

    def locked_rows(self, page=None):
        """Row keys `page` starts with disabled, with transD resolved.

        The static table cannot name the downstream trans stage, because
        which one it is depends on the transH pick made at runtime.
        """
        page = self.page if page is None else page
        locked = set(locked_motors(page))
        if page in PAGES_LOCKING_TRANS_D and self.trans_d:
            locked.add(self.trans_d)
        return frozenset(locked)

    def _transh_chosen_if_needed(self):
        """Guard the exits of the expert merged page.

        In user mode the transH picker lives on the rough-centre page and
        ROUGH_CONTINUE enforces it. The expert page absorbs that step, so its
        own exits have to enforce it instead -- otherwise the iterative page
        opens with no horizontal stage chosen and its step 1, which drives
        only transH, has nothing to enable.
        """
        if self.page != Page.POSITION_AND_ROTATION:
            return True
        return self.horizontal is not None

    # -- routing -----------------------------------------------------------

    def _positioning_page(self):
        """The page that starts a positioning pass, for the current mode.

        In expert mode the rough-centre, set-rotation and rotation-safe pages
        are one merged screen, so both the movement-safe branch buttons and a
        COR_NO kick-back land there instead of on ROUGH_CENTER.
        """
        return (Page.POSITION_AND_ROTATION if self.expert else Page.ROUGH_CENTER)

    def _after_rotation_safe(self, rotation_safe):
        """Resolve the four-way rotation-safe x branch outcome."""
        if rotation_safe:
            if self.branch == Branch.FRESH_START:
                return Page.ITERATIVE
            return Page.TRANS_BOTH if self.expert else Page.FIRST_TRANS
        if self.branch == Branch.FRESH_START:
            return Page.CHANGE_SAMPLE
        return Page.COR_KNOWN

    def destination(self, action, rotation_safe=None):
        """Pure routing: the page `action` leads to, without applying it.

        `rotation_safe` overrides the computed arc decision -- the view passes
        the operator's answer when they override the auto-decision dialog.
        Returns CLOSE for terminal actions, or None for non-navigating ones.
        """
        if action in (Action.GO_BACK, Action.ROTAXES_DONE):
            return self.previous_page()
        if action == Action.RADIO_CONTINUE:
            return Page.MOVEMENT_SAFE
        if action in (Action.MOVE_FRESH_START, Action.MOVE_SAMPLE_CHANGE):
            return self._positioning_page()
        if action == Action.ROUGH_CONTINUE:
            return Page.SET_ROTATION
        if action == Action.NEED_ROTATION:
            return Page.ROTATION_AXES
        if action == Action.ROT_CONTINUE:
            return Page.ROTATION_SAFE
        if action == Action.ROTSAFE_360_OK:
            return self._after_rotation_safe(True)
        if action == Action.ROTSAFE_CONTINUE:
            if rotation_safe is None:
                rotation_safe = self.rotation_arc_ok()
            return self._after_rotation_safe(rotation_safe)
        if action == Action.FIRST_TRANS_CONTINUE:
            return Page.SECOND_TRANS
        if action in (Action.TRANS1_HORIZONTAL, Action.TRANS2_HORIZONTAL):
            return self.page  # the picker records a choice, it does not move
        if action == Action.TRANS_FINISH:
            return CLOSE
        if action == Action.COR_YES:
            return Page.SMALL_ANGLE_TRANS
        if action == Action.COR_NO:
            # The centre of rotation is lost -- restart on the fresh-start
            # branch, which resets hexapod X before re-centring.
            return self._positioning_page()
        if action in (Action.ITER_NEXT, Action.ITER_BACK):
            return Page.ITERATIVE
        if action == Action.ITER_FINISH:
            return CLOSE
        if action == Action.START_OVER:
            return Page.RADIOGRAPHY
        return None

    def destination_label(self, action):
        """Tooltip text for a navigating button.

        Recomputed on every page change, so branch-dependent exits always name
        the page the operator will actually land on.
        """
        if action not in NAV_ACTIONS:
            return ""
        target = self.destination(action)
        if target == CLOSE:
            return "Finishes the alignment and closes this window"
        if target is None:
            return ""
        if action in (Action.GO_BACK, Action.ROTAXES_DONE):
            return ("Goes back to: %s (nothing is undone and no motor moves)"
                    % PAGE_TITLES[target])
        if action == Action.ITER_BACK:
            return ("Goes back one step of the iterative alignment "
                    "(does not undo any motor moves)")
        if action == Action.ITER_NEXT:
            return "Goes to: the next step of the iterative alignment"
        if target == self.page:
            return ""
        return "Goes to: %s" % PAGE_TITLES[target]

    # -- transitions -------------------------------------------------------

    def advance(self, action, rotation_safe=None):
        """Apply `action`: update state and move to its destination page.

        Returns the new page, or CLOSE when the workflow is finished.
        """
        if action == Action.START_OVER:
            return self.start()
        if action in (Action.GO_BACK, Action.ROTAXES_DONE):
            return self.back()

        target = self.destination(action, rotation_safe=rotation_safe)
        if target is None:
            return self.page

        if action in (Action.MOVE_FRESH_START, Action.COR_NO):
            self.branch = Branch.FRESH_START
            self.pending_hexapod_x_prompt = True
            # Re-entering the positioning pages from COR_NO must clear the
            # gates those pages set, or their Continue buttons stay unlocked
            # from the first pass through.
            self.rotated_once = False
            self.softlimit_set = False
            self.rotation_safe = None
        elif action == Action.MOVE_SAMPLE_CHANGE:
            self.branch = Branch.SAMPLE_CHANGE
        elif action == Action.ROTSAFE_360_OK:
            self.rotation_safe = True
        elif action == Action.ROTSAFE_CONTINUE:
            self.rotation_safe = (self.rotation_arc_ok() if rotation_safe is None
                                  else bool(rotation_safe))
        elif action == Action.TRANS1_HORIZONTAL:
            self.horizontal = "trans1"
        elif action == Action.TRANS2_HORIZONTAL:
            self.horizontal = "trans2"
        elif action == Action.ITER_NEXT:
            index = ITER_STEPS.index(self.iter_step)
            if index + 1 < len(ITER_STEPS):
                self.iter_step = ITER_STEPS[index + 1]
        elif action == Action.ITER_BACK:
            index = ITER_STEPS.index(self.iter_step)
            if index > 0:
                self.iter_step = ITER_STEPS[index - 1]

        if target != CLOSE and target != self.page:
            self._history.append(self.page)
            self.page = target
        return target

    # -- iterative page ----------------------------------------------------

    @property
    def trans_h(self):
        """The trans motor chosen as horizontal, or None before it is picked."""
        return self.horizontal

    @property
    def trans_d(self):
        """The other trans motor -- the one aligned last, in step 4."""
        if self.horizontal == "trans1":
            return "trans2"
        if self.horizontal == "trans2":
            return "trans1"
        return None

    def iterative_state(self):
        """Widget state for the iterative page at the current sub-step.

        Every button on that page exists from the start; only the label,
        enabled state and wired action change as the operator advances.
        `motors` is in terms of the stable row keys (trans1/trans2), not the
        transH/transD display labels.
        """
        step = self.iter_step
        trans_h = self.trans_h
        trans_d = self.trans_d

        if step == 1:
            # transH is always known by now (its picker gates every route to
            # this page). If it somehow is not, offer both trans stages
            # rather than enabling nothing and stranding the operator.
            motors = {trans_h} if trans_h else {"trans1", "trans2"}
        elif step == 23:
            # Everything except transD: phi and X do the alignment, transH
            # takes up the halfway correction.
            motors = {"phi", "X"} | ({trans_h} if trans_h
                                     else {"trans1", "trans2"})
        else:
            # Step 4 aligns transD with small phi steps. transH is already
            # set from steps 1-3 and must not be disturbed, and X is done.
            motors = {"phi"} | ({trans_d} if trans_d
                                else {"trans1", "trans2"})

        state = {
            "step": step,
            "motors": frozenset(motors),
            "phi_step": ITER_PHI_STEP_FINAL if step == 4 else ITER_PHI_STEP,
            "phi_editable": step == 4,
            "trans_h": trans_h,
            "trans_d": trans_d,
            "back": {"enabled": self.can(Action.ITER_BACK)},
            "finish": {"enabled": self.can(Action.ITER_FINISH)},
        }

        if step == 1:
            state["btn_a"] = {
                "text": "Center found",
                "enabled": True,
                "action": Action.ITER_NEXT,
            }
        elif step == 23:
            # Plain "&" here -- the view escapes it to "&&" before setText(),
            # since Qt reads a lone "&" in button text as a mnemonic marker.
            state["btn_a"] = {
                "text": "Steps 2 & 3 complete",
                "enabled": True,
                "action": Action.ITER_NEXT,
            }
        else:  # step 4 -- nothing left to advance to, only Finished!
            state["btn_a"] = {"text": "", "enabled": False, "action": None}

        return state
