"""The client end of the link.

One reader thread, owned here. Replies resolve Futures keyed by request id;
events go to glob-matched subscribers; the server's heartbeat is answered
without the caller ever seeing it.

Nothing in this file blocks on hardware, so a GUI needs no worker threads of
its own: a long operation returns a job id immediately and its progress
arrives as events. That is what retires the QThreadPool/Worker/move/mover
machinery in gui/rungui.py.
"""

import fnmatch
import itertools
import socket
import threading
from concurrent.futures import Future

from ptychosaxs.link import config, errors, wire
from ptychosaxs.client import facades


class BeamlineClient:
    def __init__(self, socket_path=None, *, kind="cli", gui_type=None,
                 label=""):
        self.socket_path = socket_path or config.socket_path()
        self.kind = kind
        self.gui_type = gui_type
        self.label = label or gui_type or kind

        self._sock = None
        self._reader = None
        self._ids = itertools.count(1)
        self._pending = {}
        self._subs = []
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._prompt_handler = None

        self.hello = {}
        self.motion = facades.MotionFacade(self)
        self.system = facades.SystemFacade(self)
        self.optics = facades.OpticsFacade(self)
        self.pv = facades.PvFacade(self)
        self.crl = facades.CrlFacade(self)

    # -- connection ----------------------------------------------------------

    def connect(self, *, start_server=True, timeout_s=20.0):
        from ptychosaxs.client.launcher import ensure_server
        self._sock = ensure_server(self.socket_path,
                                   start_server=start_server,
                                   timeout_s=timeout_s)
        self._closed.clear()
        self._reader = threading.Thread(target=self._read_loop,
                                        name="ptys-client-reader", daemon=True)
        self._reader.start()
        self.hello = self.call("system.hello", {
            "protocol": config.PROTOCOL_VERSION,
            "kind": self.kind,
            "gui_type": self.gui_type,
            "label": self.label,
        }, timeout_s=timeout_s)
        return self

    def close(self):
        if self._closed.is_set():
            return
        self._closed.set()
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for fut in pending:
            if not fut.done():
                fut.set_exception(ConnectionError("connection closed"))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    @property
    def connected(self):
        return self._sock is not None and not self._closed.is_set()

    @property
    def ready(self):
        return bool(self.hello.get("ready"))

    @property
    def simulated(self):
        return bool(self.hello.get("simulated"))

    @property
    def profile(self):
        return self.hello.get("profile", {})

    # -- requests ------------------------------------------------------------

    def call(self, method, params=None, *, timeout_s=10.0):
        """Send a request and wait for its reply. Raises ServerError."""
        if self._sock is None:
            raise ConnectionError("not connected")
        req_id = f"c-{next(self._ids)}"
        fut = Future()
        with self._lock:
            self._pending[req_id] = fut
        try:
            wire.send(self._sock, wire.request(req_id, method, params))
        except OSError as exc:
            with self._lock:
                self._pending.pop(req_id, None)
            raise ConnectionError(f"could not send {method}: {exc}") from exc
        return fut.result(timeout=timeout_s)

    # -- events --------------------------------------------------------------

    def on(self, pattern, fn):
        """Subscribe to events whose name matches a glob ("job.*", "*")."""
        entry = (pattern, fn)
        with self._lock:
            self._subs.append(entry)

        def unsubscribe():
            with self._lock:
                if entry in self._subs:
                    self._subs.remove(entry)
        return unsubscribe

    def set_prompt_handler(self, fn):
        """fn(ask_frame) -> chosen string. Used by guard dialogs."""
        self._prompt_handler = fn

    def answer(self, ask_id, choice):
        wire.send(self._sock, wire.answer(ask_id, choice))

    # -- reader --------------------------------------------------------------

    def _read_loop(self):
        try:
            for frame in wire.FrameReader(self._sock):
                if self._closed.is_set():
                    break
                self._on_frame(frame)
        except errors.ProtocolError:
            pass
        except Exception:
            pass
        finally:
            self.close()

    def _on_frame(self, frame):
        kind = frame.get("t")
        if kind == wire.T_REP:
            with self._lock:
                fut = self._pending.pop(frame.get("id"), None)
            if fut is None or fut.done():
                return
            if frame.get("ok"):
                fut.set_result(frame.get("result", {}))
            else:
                fut.set_exception(errors.from_reply(frame.get("error", {})))
        elif kind == wire.T_EVT:
            self._dispatch_event(frame)
        elif kind == wire.T_REQ:
            # The server's heartbeat. Answering it is what keeps this
            # client's slot and its device groups alive.
            if frame.get("method") == "system.ping":
                try:
                    wire.send(self._sock, wire.ok(frame.get("id"), {}))
                except OSError:
                    pass
        elif kind == wire.T_ASK:
            self._dispatch_prompt(frame)

    def _dispatch_event(self, frame):
        name = frame.get("event", "")
        with self._lock:
            subs = [fn for pattern, fn in self._subs
                    if fnmatch.fnmatchcase(name, pattern)]
        for fn in subs:
            try:
                fn(name, frame.get("data", {}))
            except Exception:
                pass

    def _dispatch_prompt(self, frame):
        handler = self._prompt_handler
        if handler is None:
            self.answer(frame.get("id"), frame.get("default", ""))
            return
        try:
            choice = handler(frame)
        except Exception:
            choice = frame.get("default", "")
        self.answer(frame.get("id"), choice)
