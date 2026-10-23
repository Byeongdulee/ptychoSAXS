"""The one-button beamline configurations behind the user optics panel.

optics_motors_user.ui gives a user three buttons -- Ptycho Mode, X-ray Eye
Mode, SAXS Mode -- in place of the per-block In/Out controls the staff views
carry. Each one is a short sequence of moves with a safety question or two in
the middle, and those sequences live here rather than in optics_motors.py for
two reasons: the panel file is already long, and the order these things happen
in is the part most likely to be revised, so it is worth having in one place
that reads top to bottom.

Nothing here touches a widget of the panel directly. OpticsModes talks to the
panel through the small interface documented on its constructor, so the same
sequences would run against the full view, or a test double, unchanged.

The waiting is the awkward part. The ptychosaxs controllers move without
waiting (`mv(..., wait=False)`), so "after the ZP, BS and OSA are in" has to
be a poll of the status the panel is already computing every tick. wait_for()
below does that on a nested event loop, which keeps the panel's readbacks
live -- and its Stop All button usable -- while a sequence is part way
through.
"""

import time

from PyQt5.QtCore import QEventLoop, Qt, QTimer
from PyQt5.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QVBoxLayout,
)

from gui import optics_status
from gui.pv_lineedit import PVLineEdit
from gui.xray_eye import EYE_EXPOSURE_PV, EYE_EXPOSURE_RBV_PV

# Below this the optics can be retracted with the beam on the detector; at or
# above it the SAXS beamstop goes in first. keV, read from PHOTON_ENERGY_PV.
PHOTON_ENERGY_PV = "12ida2:EnCalc"
SAXSBS_ENERGY_KEV = 12.0

# How long a sequence waits for a block to report that it arrived before it
# gives up and says so. Generous: BSZP_Ztrans aside, these are short moves,
# but a stage that has to be homed first can take a while.
MOVE_TIMEOUT_S = 120.0

# Between retracting and re-inserting the X-ray eye on a retry. The eye is
# pneumatic and occasionally does not seat; backing it off and letting it
# settle is what clears that.
EYE_RETRY_PAUSE_MS = 2000

# Optics that go in for ptychography and out for everything else, in the
# order they are commanded. Keys are optics_status.BLOCKS keys.
PTYCHO_BLOCKS = ["zp", "bs", "osa"]
SAXSBS_BLOCK = "saxsbs"


def fmt_seconds(value):
    """Exposure times, which are seconds and must never see the panel's
    mm/um conversion -- %g keeps 0.002 as 0.002 and 1.0 as 1."""
    return "" if value is None else "%g" % value


# ---------------------------------------------------------------------------
# Waiting without freezing the panel
# ---------------------------------------------------------------------------
def pause(milliseconds):
    """Wait, with the event loop still running, so readbacks keep updating."""
    loop = QEventLoop()
    QTimer.singleShot(milliseconds, loop.quit)
    loop.exec_()


def wait_for(predicate, parent, message, timeout=MOVE_TIMEOUT_S, poll_ms=100):
    """Pump the event loop until `predicate()` is true.

    Shows a modal, cancellable progress dialog for as long as it waits, so
    the operator can see what is being waited on and back out of it. Returns
    True when the condition was met, False on cancel or timeout.
    """
    if predicate():
        return True

    dialog = QProgressDialog(message, "Cancel", 0, 0, parent)
    dialog.setWindowTitle("Moving")
    dialog.setWindowModality(Qt.ApplicationModal)
    dialog.setMinimumDuration(0)
    dialog.setAutoClose(False)
    dialog.setAutoReset(False)
    dialog.show()

    deadline = time.monotonic() + timeout
    loop = QEventLoop()
    outcome = {"ok": False}

    def tick():
        if predicate():
            outcome["ok"] = True
            loop.quit()
        elif dialog.wasCanceled() or time.monotonic() >= deadline:
            loop.quit()

    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(poll_ms)
    try:
        loop.exec_()
    finally:
        timer.stop()
        dialog.close()
    return outcome["ok"]


# ---------------------------------------------------------------------------
# Exposure time controls
# ---------------------------------------------------------------------------
class ExposureRow(QHBoxLayout):
    """x0.1 | exposure time | x10, bound to the X-ray eye camera.

    The same three widgets the panel carries as pb_xeExp_stepDown / ed_xeExp /
    pb_xeExp_stepUp, built in code because the verify dialog is built in code.
    Both go through PVLineEdit, so typing and Enter behave identically in the
    dialog and on the panel.

    `poll()` has to be driven by the owner; the dialog below runs a timer for
    it, the panel folds it into its own refresh tick.
    """

    def __init__(self, pv_factory, initial=None):
        super(ExposureRow, self).__init__()
        self.edit = QLineEdit()
        self.edit.setAlignment(Qt.AlignCenter)
        self.edit.setToolTip("Exposure: %s" % EYE_EXPOSURE_PV)

        self.binding = PVLineEdit(self.edit, [pv_factory(EYE_EXPOSURE_PV)],
                                  fmt_seconds, float)
        if initial is not None:
            self.edit.setText(fmt_seconds(initial))

        down = QPushButton("0.1x")
        up = QPushButton("10x")
        for button, factor in ((down, 0.1), (up, 10.0)):
            # Not autoDefault: in a dialog that would make Enter in the
            # exposure box press a button instead of sending the value.
            button.setAutoDefault(False)
            button.clicked.connect(
                lambda _checked=False, f=factor: self.binding.scale(f))

        self.addWidget(down)
        self.addWidget(self.edit)
        self.addWidget(up)

    def poll(self):
        self.binding.poll()


class XrayEyeCheckDialog(QDialog):
    """"Is X-ray Eye in?", with the exposure controls needed to answer it.

    The operator answers by looking at the camera, and the camera is often
    unreadable until the exposure is right -- so the controls for fixing that
    belong in the question, not on a screen behind it.

    ask() returns YES, NO, or ABORT (the window was closed).
    """

    YES, NO, ABORT = "yes", "no", "abort"
    POLL_MS = 200

    def __init__(self, parent, pv_factory, initial_exposure=None):
        super(XrayEyeCheckDialog, self).__init__(parent)
        self.setWindowTitle("X-ray Eye")
        self._answer = self.ABORT

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Is X-ray Eye in? Please verify on camera."))

        label = QLabel("X-ray eye exposure time")
        font = label.font()
        font.setBold(True)
        label.setFont(font)
        layout.addWidget(label)

        self.exposure = ExposureRow(pv_factory, initial=initial_exposure)
        layout.addLayout(self.exposure)

        set_exposure = QPushButton("Set exposure time")
        set_exposure.setAutoDefault(False)
        set_exposure.clicked.connect(self.exposure.binding.commit)
        layout.addWidget(set_exposure)

        answers = QHBoxLayout()
        yes, no = QPushButton("Yes"), QPushButton("No")
        for button, answer in ((yes, self.YES), (no, self.NO)):
            # Both explicitly non-default, so the only thing Enter can do in
            # this dialog is send the exposure time. Answering "the eye is
            # in" is not something a stray keystroke should be able to do.
            button.setAutoDefault(False)
            button.setDefault(False)
            button.clicked.connect(
                lambda _checked=False, a=answer: self._answer_with(a))
            answers.addWidget(button)
        layout.addLayout(answers)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.exposure.poll)

    def _answer_with(self, answer):
        self._answer = answer
        self.accept()

    def ask(self):
        self._timer.start(self.POLL_MS)
        # Focus the exposure box rather than a button, both so the operator
        # can fix the image straight away and so Enter has somewhere harmless
        # to go.
        self.exposure.edit.setFocus(Qt.OtherFocusReason)
        self.exposure.edit.selectAll()
        try:
            self.exec_()
        finally:
            self._timer.stop()
        return self._answer


# ---------------------------------------------------------------------------
# The sequences
# ---------------------------------------------------------------------------
class OpticsModes:
    """Ptycho / X-ray Eye / SAXS mode, driven through a panel interface.

    `panel` must provide:

      ui                      the widget to parent dialogs to
      xray_eye                an xray_eye.XrayEye
      has_block(key)          whether this view carries that block at all
      block_state(key)        optics_status IN/OUT/... for a block, or None
      block_has_target(key, kind)   whether a saved "in"/"out" exists to move to
      move_block(key, into)   command a block in (True) or out (False)
      pv_value(name)          a PV as a float, or None if unreadable
      pv_factory(name)        a PV object for PVLineEdit, or None
    """

    def __init__(self, panel):
        self.panel = panel

    # -- helpers -----------------------------------------------------------

    def _warn(self, text):
        QMessageBox.warning(self.panel.ui, "Optics mode", text)

    def _blocks_are(self, keys, state):
        return lambda: all(self.panel.block_state(k) == state for k in keys)

    def _missing_targets(self, keys, kind):
        """Blocks with nowhere to go -- no saved position for `kind`.

        Checked up front rather than discovered halfway through: a sequence
        that moved two optics and then found the third had no saved position
        would leave the beamline in a state nobody asked for.
        """
        return [optics_status.BLOCKS[k].title for k in keys
                if not self.panel.block_has_target(k, kind)]

    def _move_blocks(self, keys, into, what):
        """Command every present block and wait for all of them to arrive."""
        kind = "in" if into else "out"
        present = [k for k in keys if self.panel.has_block(k)]
        missing = self._missing_targets(present, kind)
        if missing:
            self._warn("No saved %s position for: %s.\nNothing has been moved."
                       % (kind.capitalize(), ", ".join(missing)))
            return False

        for key in present:
            self.panel.move_block(key, into)

        wanted = optics_status.IN if into else optics_status.OUT
        if wait_for(self._blocks_are(present, wanted), self.panel.ui,
                    "Moving %s %s..." % (what, kind)):
            return True
        self._warn("%s did not report %s.\nStopping here -- check the motors."
                   % (what.capitalize(), kind.capitalize()))
        return False

    def _retract_xrayeye_if_in(self):
        """Offer to pull the eye out, if it is in. True unless the operator
        said no -- an eye that was already out is not a refusal."""
        if self.panel.xray_eye.is_in() is not True:
            return True
        reply = QMessageBox.question(
            self.panel.ui, "X-ray Eye", "OK to remove the X-ray eye?",
            QMessageBox.Ok | QMessageBox.Cancel, QMessageBox.Ok)
        if reply != QMessageBox.Ok:
            return False
        self.panel.xray_eye.set_in(False)
        return True

    def _insert_saxsbs(self, preamble):
        """Put the SAXS beamstop in and wait for it. False if it never got
        there, or if this view has no SAXS beamstop block at all."""
        if not self.panel.has_block(SAXSBS_BLOCK):
            self._warn("This view has no SAXS beamstop control.")
            return False
        QMessageBox.information(self.panel.ui, "SAXS beamstop", preamble)
        return self._move_blocks([SAXSBS_BLOCK], True, "SAXS beamstop")

    # -- Ptycho Mode -------------------------------------------------------

    def ptycho_mode(self):
        """ZP, BS and OSA in; then clear the eye and the SAXS beamstop.

        The two retractions come last and are asked about separately. Both
        uncover the detector, so neither happens until the optics that are
        supposed to be in the beam actually are.
        """
        if not self._move_blocks(PTYCHO_BLOCKS, True, "ZP, BS and OSA"):
            return

        self._retract_xrayeye_if_in()

        if self.panel.block_state(SAXSBS_BLOCK) == optics_status.IN:
            reply = QMessageBox.question(
                self.panel.ui, "SAXS beamstop",
                "The SAXS beamstop is in. OK to remove it?",
                QMessageBox.Ok | QMessageBox.Cancel, QMessageBox.Ok)
            # Cancel leaves the beamstop where it is and stops there: the
            # optics are already in, which is the part Ptycho Mode is for.
            if reply == QMessageBox.Ok:
                self._move_blocks([SAXSBS_BLOCK], False, "SAXS beamstop")

    # -- X-ray Eye Mode ----------------------------------------------------

    def xray_eye_mode(self):
        """Eye in, verified by the operator, then the optics out of the way.

        The verification is not a formality -- the eye does fail to insert --
        so the sequence will not retract a single optic until someone has
        looked at the camera and said the eye is there.
        """
        self.panel.xray_eye.set_in(True)

        while True:
            answer = XrayEyeCheckDialog(
                self.panel.ui, self.panel.pv_factory,
                initial_exposure=self.panel.pv_value(EYE_EXPOSURE_RBV_PV)).ask()
            if answer == XrayEyeCheckDialog.YES:
                break
            if answer != XrayEyeCheckDialog.NO:
                return  # dialog closed -- treat as "stop", eye left as it is
            if not self._retry_xrayeye():
                return

        self._retract_optics_for_eye()

    def _retry_xrayeye(self):
        """Offer the out-and-in-again that usually seats a stuck eye."""
        reply = QMessageBox.question(
            self.panel.ui, "X-ray Eye",
            "Retry?\nX-ray eye sometimes fails to insert.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if reply != QMessageBox.Yes:
            return False
        self.panel.xray_eye.set_in(False)
        pause(EYE_RETRY_PAUSE_MS)
        self.panel.xray_eye.set_in(True)
        # Second pause for the same reason as the first: asking "is it in?"
        # before the eye has had time to travel just collects a wrong answer.
        pause(EYE_RETRY_PAUSE_MS)
        return True

    def _retract_optics_for_eye(self):
        """Clear ZP, BS and OSA, behind the SAXS beamstop if the energy is
        high enough to need it.

        An unreadable energy takes the beamstop branch. The cost of inserting
        a beamstop that was not needed is a wasted minute; the cost of
        skipping one that was is a detector.
        """
        energy = self.panel.pv_value(PHOTON_ENERGY_PV)
        if energy is None:
            if not self._insert_saxsbs(
                    "Could not read the photon energy (%s).\n"
                    "Inserting SAXS beamstop before removing optics."
                    % PHOTON_ENERGY_PV):
                return
        elif energy >= SAXSBS_ENERGY_KEV:
            if not self._insert_saxsbs(
                    "Photon energy is %.3f keV.\n"
                    "Inserting SAXS beamstop before removing optics." % energy):
                return

        self._move_blocks(PTYCHO_BLOCKS, False, "ZP, BS and OSA")

    # -- SAXS Mode ---------------------------------------------------------

    def saxs_mode(self):
        """SAXS beamstop in, then ZP, BS and OSA out from behind it."""
        if not self._insert_saxsbs("Inserting SAXS beamstop before removing "
                                   "optics."):
            return
        self._move_blocks(PTYCHO_BLOCKS, False, "ZP, BS and OSA")
