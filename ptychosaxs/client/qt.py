"""Server events as Qt signals.

The only module under ptychosaxs/ that imports Qt, so a script, a test or
the MCP server can drive the instrument with no Qt installed.

Threading: events arrive on the client's reader thread and are emitted from
it. That is safe without any explicit marshalling because Qt's
AutoConnection decides queued-vs-direct by comparing the *receiver's*
thread affinity to the emitting thread, so a slot on a widget runs on the
GUI thread either way. The one requirement is that this object is
constructed on the GUI thread -- which is easy to get wrong, hence the
check in __init__ rather than a line in a docstring nobody reads.

What this replaces: gui/rungui.py's Worker/move/mover QRunnables and the
QThreadPool they ran on, plus the per-panel QTimers that polled hardware.
A call returns immediately and the answer arrives as a signal, so there is
nothing left for a GUI thread to block on.
"""

from PyQt5 import QtCore

from ptychosaxs.client.connection import BeamlineClient


class QtBeamlineClient(QtCore.QObject):
    """A BeamlineClient whose events are pyqtSignals."""

    positionsChanged = QtCore.pyqtSignal(dict, dict, dict)  # pos, moving, connected
    serverStateChanged = QtCore.pyqtSignal(bool, str)       # ready, phase
    messagePosted = QtCore.pyqtSignal(str, str, str)        # level, text, source
    clientsChanged = QtCore.pyqtSignal(list)
    focusRequested = QtCore.pyqtSignal(str)                 # gui_type
    jobStarted = QtCore.pyqtSignal(str, str, dict)          # id, kind, params
    jobProgress = QtCore.pyqtSignal(str, int, int, str)     # id, done, total, phase
    jobFinished = QtCore.pyqtSignal(str, str, str)          # id, state, error
    disconnected = QtCore.pyqtSignal(str)

    def __init__(self, gui_type, label="", parent=None, client=None):
        super().__init__(parent)
        app = QtCore.QCoreApplication.instance()
        if app is not None and QtCore.QThread.currentThread() != app.thread():
            raise RuntimeError(
                "QtBeamlineClient must be constructed on the GUI thread; "
                "its signals rely on the receiver's thread affinity to be "
                "queued correctly")

        self._client = client or BeamlineClient(
            kind="gui", gui_type=gui_type, label=label or gui_type)
        self._wire_events()

    # -- the underlying client ----------------------------------------------

    @property
    def client(self):
        return self._client

    @property
    def motion(self):
        return self._client.motion

    @property
    def system(self):
        return self._client.system

    @property
    def simulated(self):
        return self._client.simulated

    @property
    def ready(self):
        return self._client.ready

    def call(self, method, params=None, **kw):
        return self._client.call(method, params, **kw)

    def connect_to_server(self, *, start_server=True, timeout_s=20.0):
        self._client.connect(start_server=start_server, timeout_s=timeout_s)
        hello = self._client.hello
        self.serverStateChanged.emit(bool(hello.get("ready")),
                                     hello.get("phase", ""))
        return hello

    def close(self):
        self._client.close()

    # -- event plumbing ------------------------------------------------------

    def _wire_events(self):
        c = self._client
        c.on("motion.positions", lambda n, d: self.positionsChanged.emit(
            d.get("positions", {}), d.get("moving", {}),
            d.get("connected", {})))
        c.on("server.state", lambda n, d: self.serverStateChanged.emit(
            bool(d.get("ready")), d.get("phase", "")))
        c.on("message.posted", lambda n, d: self.messagePosted.emit(
            d.get("level", "info"), d.get("text", ""), d.get("source", "")))
        c.on("clients.changed", lambda n, d: self.clientsChanged.emit(
            list(d.get("clients", ()))))
        c.on("client.focus_requested", lambda n, d: self.focusRequested.emit(
            d.get("gui_type", "")))
        c.on("job.started", lambda n, d: self.jobStarted.emit(
            d.get("job_id", ""), d.get("kind", ""), d.get("params", {})))
        c.on("job.progress", lambda n, d: self.jobProgress.emit(
            d.get("job_id", ""), int(d.get("done", 0)),
            int(d.get("total", 0)), d.get("phase", "")))
        c.on("job.finished", lambda n, d: self.jobFinished.emit(
            d.get("job_id", ""), d.get("state", ""), d.get("error", "")))


def attach_single_instance(window, qt_client):
    """Raise `window` when another launch of the same GUI is refused.

    The second process asks the server for focus and exits; this is the end
    that acts on it. Without it the single-instance rule is just a refusal,
    and double-clicking a launcher appears to do nothing.
    """
    def _raise(_gui_type):
        window.setWindowState(
            (window.windowState() & ~QtCore.Qt.WindowMinimized)
            | QtCore.Qt.WindowActive)
        window.raise_()
        window.activateWindow()

    qt_client.focusRequested.connect(_raise)
    return _raise
