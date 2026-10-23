"""One connected client: a reader thread, a writer thread, and a queue.

The queue conflates. Positions go out at 10 Hz to everyone, and a client
that stops draining -- a GUI stuck in a modal dialog, a laptop that slept --
must not accumulate a backlog it will then render frame by frame. A new
`motion.positions` replaces the pending one rather than queueing behind it,
so a slow client gets the current truth late instead of every stale truth in
order. Events that mean something individually (a job finishing, a message)
are never conflated.

If even the conflated queue overflows, the connection is closed with a
counted log line rather than blocking the publisher. That is the rule
qds_bridge already applies to its sample stream: lose data visibly, never
lose time.
"""

import threading
from collections import OrderedDict

from ptychosaxs.link import config, errors, methods, wire
from ptychosaxs.server import registry as reg
from ptychosaxs.server.dispatch import HANDLERS, Rejected


class _OutQueue:
    """Bounded, conflating, thread-safe."""

    def __init__(self, maxsize):
        self._items = OrderedDict()       # key -> frame
        self._plain = []                  # frames with no conflation key
        self._cv = threading.Condition()
        self._max = maxsize
        self._closed = False
        self.dropped = 0

    def put(self, frame, key=None):
        with self._cv:
            if self._closed:
                return True
            if key is not None:
                self._items[key] = frame          # replaces any pending one
            else:
                if len(self._plain) + len(self._items) >= self._max:
                    self.dropped += 1
                    return False                   # caller closes us
                self._plain.append(frame)
            self._cv.notify()
            return True

    def get(self, timeout=0.25):
        with self._cv:
            if not self._plain and not self._items and not self._closed:
                self._cv.wait(timeout)
            if self._plain:
                return self._plain.pop(0)
            if self._items:
                _key, frame = self._items.popitem(last=False)
                return frame
            return None

    def close(self):
        with self._cv:
            self._closed = True
            self._cv.notify_all()

    @property
    def closed(self):
        return self._closed


class Connection:
    """A client's socket plus the two threads that serve it."""

    def __init__(self, sock, conn_id, daemon):
        self.sock = sock
        self.conn_id = conn_id
        self.daemon = daemon
        self.log = daemon.log
        self.info = None                 # ClientInfo, set at handshake
        self._out = _OutQueue(config.OUT_QUEUE_MAX)
        self._closed = threading.Event()
        self._threads = []

    # -- lifetime ------------------------------------------------------------

    def start(self):
        for target, name in ((self._read_loop, "reader"),
                             (self._write_loop, "writer")):
            t = threading.Thread(target=target, name=f"conn-{self.conn_id}-{name}",
                                 daemon=True)
            t.start()
            self._threads.append(t)

    def close(self, reason=""):
        if self._closed.is_set():
            return
        self._closed.set()
        self._out.close()
        try:
            self.sock.shutdown(__import__("socket").SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        if reason:
            # A connection that never said hello is almost always the
            # launcher's liveness probe, which connects and drops. Logging
            # those at INFO buries the real ones.
            level = self.log.info if self.info is not None else self.log.debug
            level("connection %s closed: %s", self.conn_id, reason)
        self.daemon.on_connection_closed(self)

    @property
    def closed(self):
        return self._closed.is_set()

    # -- outbound ------------------------------------------------------------

    def offer(self, event):
        """Queue an event. Never blocks; closes the connection if hopeless."""
        key = getattr(event, "conflate_key", None)
        frame = wire.event(self.daemon.next_seq(), event.name, event.data)
        if not self._out.put(frame, key):
            self.log.warning(
                "connection %s is not draining (%d events dropped); closing",
                self.conn_id, self._out.dropped)
            self.close("outbound queue overflow")

    def send(self, frame):
        self._out.put(frame)

    def send_now(self, frame):
        """Write one frame straight to the socket, bypassing the queue.

        For the replies we send immediately before closing -- a rejected
        handshake, a protocol error. Queueing those races the close: the
        writer thread finds the queue shut and the client sees the socket
        drop with no explanation, so a duplicate-GUI refusal arrives as
        ConnectionError instead of the error that says which pid holds the
        slot. Safe to do directly because nothing else writes to a
        connection that has not completed its handshake.
        """
        try:
            wire.send(self.sock, frame)
        except OSError:
            pass

    def _write_loop(self):
        while not self._closed.is_set():
            frame = self._out.get()
            if frame is None:
                continue
            try:
                wire.send(self.sock, frame)
            except (OSError, BrokenPipeError):
                self.close("write failed")
                return

    # -- inbound -------------------------------------------------------------

    def _read_loop(self):
        try:
            for frame in wire.FrameReader(self.sock):
                if self._closed.is_set():
                    break
                self._on_frame(frame)
        except errors.ProtocolError as exc:
            self.log.warning("connection %s protocol error: %s",
                             self.conn_id, exc)
        except Exception:
            if not self._closed.is_set():
                self.log.exception("connection %s reader failed", self.conn_id)
        finally:
            self.close("peer closed")

    def _on_frame(self, frame):
        kind = frame.get("t")
        if kind == wire.T_ANS:
            self.daemon.on_answer(self, frame)
            return
        if kind == wire.T_REP:
            # The client answering our heartbeat. This is what distinguishes
            # a live-but-idle client from a frozen one holding its slot and
            # its device groups open.
            self.daemon.registry.touch(self.conn_id)
            return
        if kind != wire.T_REQ:
            self.send(wire.error(frame.get("id"), errors.ERR_PROTOCOL,
                                 f"server does not accept {kind!r} frames"))
            return

        req_id = frame.get("id")
        name = frame.get("method")
        params = frame.get("params") or {}

        if self.info is None:
            if name != "system.hello":
                self.send_now(wire.error(
                    req_id, errors.ERR_PROTOCOL,
                    "the first frame on a connection must be system.hello"))
                self.close("no handshake")
                return
            self._handshake(req_id, params)
            return

        self.daemon.registry.touch(self.conn_id)
        self._invoke(req_id, name, params)

    def _handshake(self, req_id, params):
        proto = params.get("protocol", config.PROTOCOL_VERSION)
        if not (config.MIN_PROTOCOL_VERSION <= proto <= config.PROTOCOL_VERSION):
            self.send_now(wire.error(
                req_id, errors.ERR_PROTOCOL,
                f"client speaks protocol {proto}, this server speaks "
                f"{config.MIN_PROTOCOL_VERSION}..{config.PROTOCOL_VERSION}; "
                f"this usually means a daemon left over from an older "
                f"checkout - stop it with 'ptychosaxs-server --stop'"))
            self.close("protocol mismatch")
            return

        kind = params.get("kind", "cli")
        gui_type = params.get("gui_type")
        info = reg.ClientInfo(self.conn_id, kind, gui_type,
                              reg.peer_pid(self.sock) or params.get("pid"),
                              params.get("label", ""))
        try:
            self.daemon.registry.register(info)
        except reg.DuplicateGui as exc:
            self.send_now(wire.error(
                req_id, errors.ERR_DUPLICATE_GUI, str(exc),
                {"gui_type": gui_type, "pid": exc.holder.pid,
                 "conn_id": exc.holder.conn_id,
                 "since": exc.holder.connected_at}))
            self.close("duplicate gui")
            return

        self.info = info
        self.daemon.on_client_registered(self, info)
        self.send(wire.ok(req_id, self.daemon.hello_result(info)))

    def _invoke(self, req_id, name, params):
        handler = HANDLERS.get(name)
        if handler is None:
            self.send(wire.error(req_id, errors.ERR_UNKNOWN_METHOD,
                                 f"no such method {name!r}"))
            return
        if not self.daemon.session.ready and name not in methods.PRE_READY:
            self.send(wire.error(
                req_id, errors.ERR_NOT_READY,
                f"the server is still starting ({self.daemon.session.phase})"))
            return
        try:
            result = handler.fn(_Ctx(self.daemon, self), params)
        except Rejected as exc:
            self.send(wire.error(req_id, exc.code, exc.message, exc.data))
        except Exception as exc:
            self.log.exception("method %s failed", name)
            self.send(wire.error(req_id, errors.ERR_INTERNAL,
                                 f"{type(exc).__name__}: {exc}"))
        else:
            self.send(wire.ok(req_id, result))


class _Ctx:
    """What a handler is given: the daemon and the connection that asked."""
    __slots__ = ("daemon", "conn")

    def __init__(self, daemon, conn):
        self.daemon = daemon
        self.conn = conn

    @property
    def session(self):
        return self.daemon.session
