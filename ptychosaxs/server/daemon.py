"""The server process: accept connections, serve methods, know when to stop."""

import itertools
import os
import socket
import threading
import time

from ptychosaxs.api.events import FocusRequested
from ptychosaxs.api.session import BeamlineSession
from ptychosaxs.link import config, wire
from ptychosaxs.server import lifecycle
from ptychosaxs.server.connection import Connection
from ptychosaxs.server.publisher import Publisher
from ptychosaxs.server.registry import ConnectionRegistry


class Daemon:
    def __init__(self, session: BeamlineSession, sock, lock, log,
                 linger_s=None):
        self.session = session
        self.log = log
        self._sock = sock
        self._lock = lock
        self._conns = {}
        self._conn_lock = threading.Lock()
        self._conn_ids = itertools.count(1)
        self._seq = itertools.count(1)
        self._stop = threading.Event()

        self.registry = ConnectionRegistry(log, on_change=self._clients_changed)
        self.policy = lifecycle.ExitPolicy(self.registry, session.jobs, log,
                                           linger_s=linger_s)
        self._publisher = Publisher(session, log)
        self.started_at = time.time()

        # Every event the session publishes goes to every connection.
        session.bus.subscribe(self._broadcast)

    # -- wiring --------------------------------------------------------------

    def next_seq(self):
        return next(self._seq)

    def _broadcast(self, event):
        for conn in self.connections():
            if conn.info is not None and not conn.closed:
                conn.offer(event)

    def connections(self):
        with self._conn_lock:
            return list(self._conns.values())

    def _clients_changed(self, snapshot):
        from ptychosaxs.api.events import ClientsChanged
        self.session.bus.publish(ClientsChanged(tuple(snapshot)))

    # -- connection callbacks ------------------------------------------------

    def on_client_registered(self, conn, info):
        """Open what this client needs. Opening is refcounted, so a group
        several clients want is built once."""
        if info.groups:
            self.session.acquire_for(info.groups)

    def on_connection_closed(self, conn):
        with self._conn_lock:
            self._conns.pop(conn.conn_id, None)
        info = self.registry.unregister(conn.conn_id)
        if info and info.groups:
            # Closes any group no other connected client still needs.
            self.session.release_for(info.groups)

    def on_answer(self, conn, frame):
        """A reply to an ask frame. Prompts arrive with the guard work in a
        later phase; until then an answer is unexpected and simply logged."""
        self.log.debug("unsolicited answer from %s: %s", conn.conn_id, frame)

    def send_focus_request(self, conn_id, gui_type):
        with self._conn_lock:
            conn = self._conns.get(conn_id)
        if conn:
            conn.offer(FocusRequested(gui_type).to_event())

    def hello_result(self, info):
        return {
            "protocol": config.PROTOCOL_VERSION,
            "conn_id": info.conn_id,
            "server_pid": os.getpid(),
            "started_at": self.started_at,
            "ready": self.session.ready,
            "phase": self.session.phase,
            "simulated": self.session.profile.simulated,
            "profile": self.session.profile.describe(),
            "groups": [g.describe() for g in self.session.devices.status()],
            "clients": self.registry.snapshot(),
            "active_jobs": self.session.jobs.snapshot(),
        }

    def status(self):
        st = self.session.status()
        st.update({
            "server_pid": os.getpid(),
            "socket": config.socket_path(),
            "clients": self.registry.snapshot(),
            "shutdown_requested": self.policy.shutdown_requested,
        })
        return st

    # -- running -------------------------------------------------------------

    def serve_forever(self):
        self._publisher.start()
        accept = threading.Thread(target=self._accept_loop,
                                  name="ptys-accept", daemon=True)
        accept.start()
        self.log.info("listening on %s (pid %d)", config.socket_path(),
                      os.getpid())
        try:
            self._supervise()
        except KeyboardInterrupt:
            self.log.info("interrupted; shutting down")
        finally:
            self.shutdown()

    def _accept_loop(self):
        self._sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                client, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            conn_id = f"c{next(self._conn_ids)}"
            conn = Connection(client, conn_id, self)
            with self._conn_lock:
                self._conns[conn_id] = conn
            conn.start()

    def _supervise(self):
        """Heartbeats, reaping, and the exit decision."""
        last_ping = 0.0
        while not self._stop.is_set():
            time.sleep(config.SUPERVISION_TICK_S)

            now = time.monotonic()
            if now - last_ping >= config.HEARTBEAT_S:
                last_ping = now
                for conn in self.connections():
                    if conn.info is not None:
                        conn.send(wire.request(
                            f"srv-ping-{self.next_seq()}", "system.ping"))

            for info, why in self.registry.dead_clients(
                    config.HEARTBEAT_S, config.HEARTBEAT_MISSES):
                self.log.warning("reaping client %s (%s): %s",
                                 info.conn_id, info.label, why)
                with self._conn_lock:
                    conn = self._conns.get(info.conn_id)
                if conn:
                    conn.close(why)
                else:
                    self.registry.unregister(info.conn_id)

            if self.policy.should_exit():
                self.log.info("no clients and nothing running; exiting")
                return

    def shutdown(self):
        if self._stop.is_set():
            return
        self._stop.set()
        self._publisher.stop()
        for conn in self.connections():
            conn.close("server shutting down")
        try:
            self._sock.close()
        except OSError:
            pass
        path = config.socket_path()
        try:
            # Unlink while we still hold the lock, so no client can connect
            # to a socket nobody is accepting on.
            if os.path.lexists(path):
                os.unlink(path)
        except OSError:
            self.log.exception("could not remove %s", path)
        self.session.close()
        if self._lock:
            self._lock.release()
        self.log.info("server stopped")
