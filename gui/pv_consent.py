"""
Asking, once, before this GUI writes the scan number to the beamline.

12idc:data:fileIndex is shared, and writing it renumbers runs for anything
else reading it. The GUI used to write it on startup without saying so.
Now it asks at startup and nowhere else, and the answer governs every
write for the rest of the session - including the ones a scan makes, which
happen on worker threads where no dialog could be shown anyway.

Three outcomes, because closing a dialog is not the same as answering it:

  ALLOW    write the PV when a scan starts.
  DECLINE  never write it; the GUI keeps its own scan number locally.
  CLOSED   the window was dismissed without an answer - the caller shuts
           the GUI down rather than guess.

The Qt dialog is injected (`ask`) so the policy can be tested without a
QApplication.
"""

from ptychosaxs import pv_audit

ALLOW = "allow"
DECLINE = "decline"
CLOSED = "closed"


def _qt_ask(parent, title, text) -> str:
    """Ok / Cancel / dismissed, told apart by which button was clicked.

    QMessageBox.question cannot distinguish Cancel from the title-bar X -
    both come back as Cancel - so the buttons are added explicitly and
    clickedButton() is read directly. It returns None when the dialog was
    closed without pressing either.
    """
    from PyQt5.QtWidgets import QMessageBox

    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(text)
    ok = box.addButton(QMessageBox.Ok)
    cancel = box.addButton(QMessageBox.Cancel)
    box.setDefaultButton(cancel)
    box.exec_()
    clicked = box.clickedButton()
    if clicked is ok:
        return ALLOW
    if clicked is cancel:
        return DECLINE
    return CLOSED


class PvWriteConsent:
    """Session-scoped consent for writing the scan number."""

    def __init__(self, parent=None, ask=None):
        self._parent = parent
        self._ask = ask or _qt_ask
        self._granted = False
        self._asked = False

    @property
    def granted(self) -> bool:
        """Whether writes are allowed. False until asked and answered Ok."""
        return self._granted

    @property
    def asked(self) -> bool:
        return self._asked

    def revoke(self):
        """Call on a configuration change, so the next request asks again."""
        self._granted = False
        self._asked = False

    def request(self, reason="starting up") -> str:
        """Ask once and remember the answer. Returns ALLOW, DECLINE or CLOSED.

        CLOSED is not remembered: the caller is expected to shut down, and
        if it does not, the next request asks again rather than inheriting
        a non-answer.
        """
        if self._asked:
            return ALLOW if self._granted else DECLINE
        text = (
            f"ptychoSAXS is {reason}.\n\n"
            f"It can write one beamline PV:\n\n"
            f"    {pv_audit.consent_summary()}\n\n"
            f"It is written at the start of each scan. Writing it renumbers "
            f"scans for everything else reading this PV.\n\n"
            f"OK allows it. Cancel means it is never written - the GUI runs "
            f"and keeps its scan number locally. Closing this window exits."
        )
        outcome = self._ask(self._parent, "Allow writes to beamline PVs?", text)
        if outcome == CLOSED:
            return CLOSED
        self._asked = True
        self._granted = outcome == ALLOW
        if not self._granted:
            print(f"Declined: {pv_audit.SCAN_NUMBER_PV} will never be written.")
        return outcome
