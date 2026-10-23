"""Answering the questions control logic has to stop and ask.

A guard is a question the server must have answered before it can continue
-- "this scan is 40000 points, proceed?". It cannot be an event, because
events are one-way and the scan is blocked waiting.

The split: the *decision to ask* lives server-side with the control logic,
and the *asking* stays in the GUI as the QMessageBox it already is. That
keeps the 58 QMessageBox calls in scan_handler looking the same to an
operator while moving the logic behind them out of the view.

An unattended client cannot show a dialog, so it installs an AutoAnswer
policy instead. That generalises what the GUI already special-cases:
_confirm_large_scan skips its dialog when macro_running is set.
"""


class AutoAnswer:
    """Answer prompts from a fixed policy. For macros, the CLI and MCP."""

    def __init__(self, default=None, answers=None, log=None):
        self.default = default
        self.answers = dict(answers or {})
        self.log = log

    def __call__(self, ask):
        kind = ask.get("kind", "confirm")
        choice = self.answers.get(kind, self.default or ask.get("default", ""))
        if self.log:
            self.log("auto-answered %r with %r: %s",
                     ask.get("title", kind), choice, ask.get("text", ""))
        return choice


def qt_prompt_handler(parent=None):
    """A prompt handler that shows the dialog the GUI always showed.

    Returns a callable for BeamlineClient.set_prompt_handler. It must run on
    the GUI thread, so it marshals through a queued signal rather than
    showing a dialog from the reader thread.
    """
    from PyQt5 import QtCore, QtWidgets

    class _Asker(QtCore.QObject):
        asked = QtCore.pyqtSignal(dict, object)

        def __init__(self):
            super().__init__(parent)
            self.asked.connect(self._show, QtCore.Qt.BlockingQueuedConnection)

        def _show(self, ask, box):
            dialog = QtWidgets.QMessageBox(parent)
            dialog.setWindowTitle(ask.get("title", "ptychoSAXS"))
            dialog.setText(ask.get("text", ""))
            buttons = {}
            for choice in ask.get("choices", ["OK"]):
                buttons[dialog.addButton(choice,
                                         QtWidgets.QMessageBox.ActionRole)] = choice
            dialog.exec_()
            box.append(buttons.get(dialog.clickedButton(),
                                   ask.get("default", "")))

    asker = _Asker()

    def handle(ask):
        box = []
        asker.asked.emit(ask, box)
        return box[0] if box else ask.get("default", "")

    return handle
