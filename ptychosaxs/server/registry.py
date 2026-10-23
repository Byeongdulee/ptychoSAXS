"""Who is connected, what they need open, and who may not connect twice.

Reference counting happens at two levels and they are not the same thing.
The *server* lives while any client is connected. A *device group* lives
while any connected client declared it needs one. A GUI that only drives the
optics never causes the hexapod to be opened, and closing it closes the
optics PVs but leaves the main panel's stages alone.

Client identity is taken from the kernel, not from the client. SO_PEERCRED
gives us the peer's real pid, so "optics_motors is already open as pid
12345" is something we know rather than something we were told, and it
doubles as a liveness probe through /proc that does not depend on the socket
noticing.
"""

import os
import socket
import struct
import threading
import time

# What each GUI needs open. A GUI not listed here gets nothing, which is the
# right default: an unknown client should not be able to power up a stage.
#
# Phase 1 implements the `motion` group only; the rest are declared as each
# GUI migrates, so that every device's construction leaves gui/rungui.py
# module scope exactly once.
REQUIREMENTS = {
    "rungui": ("motion", "optics"),
    "optics_motors": ("optics",),
    "crl_3dprint": ("crl",),
    "sample_alignment": ("motion",),
}

# Kinds that may not have two live connections of the same gui_type.
EXCLUSIVE_KINDS = frozenset({"gui"})

# Kinds exempt from the single-instance rule: no window, no ini file to own,
# and refusing a second CLI invocation would break scripting.
SHARED_KINDS = frozenset({"cli", "mcp", "observer"})


def peer_pid(sock):
    """The connecting process's pid, from the kernel. None if unavailable."""
    try:
        buf = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                              struct.calcsize("3i"))
        pid, _uid, _gid = struct.unpack("3i", buf)
        return pid or None
    except (OSError, AttributeError, struct.error):
        return None


def pid_alive(pid) -> bool:
    if not pid:
        return False
    return os.path.exists(f"/proc/{pid}")


class ClientInfo:
    def __init__(self, conn_id, kind, gui_type, pid, label):
        self.conn_id = conn_id
        self.kind = kind
        self.gui_type = gui_type
        self.pid = pid
        self.label = label or gui_type or kind
        self.connected_at = time.time()
        self.last_seen = self.connected_at
        self.missed_pings = 0

    @property
    def groups(self):
        return REQUIREMENTS.get(self.gui_type, ()) if self.gui_type else ()

    def describe(self):
        return {"conn_id": self.conn_id, "kind": self.kind,
                "gui_type": self.gui_type, "pid": self.pid,
                "label": self.label, "connected_at": self.connected_at,
                "uptime_s": round(time.time() - self.connected_at, 1)}


class DuplicateGui(Exception):
    """A live connection already holds this gui_type."""

    def __init__(self, holder):
        self.holder = holder
        super().__init__(
            f"{holder.gui_type} is already open (pid {holder.pid})")


class ConnectionRegistry:
    def __init__(self, log, on_change=None):
        self._log = log
        self._lock = threading.RLock()
        self._clients = {}
        self._holders = {}          # gui_type -> conn_id
        self._on_change = on_change

    # -- registration --------------------------------------------------------

    def register(self, info: ClientInfo):
        with self._lock:
            if info.kind in EXCLUSIVE_KINDS and info.gui_type:
                holder_id = self._holders.get(info.gui_type)
                holder = self._clients.get(holder_id) if holder_id else None
                if holder is not None:
                    # Verify before refusing: a GUI killed with SIGKILL must
                    # not lock its own type out for the life of the server.
                    if pid_alive(holder.pid):
                        raise DuplicateGui(holder)
                    self._log.warning(
                        "%s was held by pid %s which is gone; reaping",
                        info.gui_type, holder.pid)
                    self._remove_locked(holder_id)
                self._holders[info.gui_type] = info.conn_id
            self._clients[info.conn_id] = info
        self._log.info("client %s connected (%s, pid %s)",
                       info.conn_id, info.label, info.pid)
        self._changed()
        return info

    def unregister(self, conn_id):
        with self._lock:
            info = self._remove_locked(conn_id)
        if info:
            self._log.info("client %s disconnected (%s)", conn_id, info.label)
            self._changed()
        return info

    def _remove_locked(self, conn_id):
        info = self._clients.pop(conn_id, None)
        if info and info.gui_type and self._holders.get(info.gui_type) == conn_id:
            del self._holders[info.gui_type]
        return info

    def _changed(self):
        if self._on_change:
            try:
                self._on_change(self.snapshot())
            except Exception:
                self._log.exception("clients-changed hook failed")

    # -- queries -------------------------------------------------------------

    def get(self, conn_id):
        with self._lock:
            return self._clients.get(conn_id)

    def holder_of(self, gui_type):
        with self._lock:
            cid = self._holders.get(gui_type)
            return self._clients.get(cid) if cid else None

    def live_count(self):
        with self._lock:
            return len(self._clients)

    def snapshot(self):
        with self._lock:
            return [c.describe() for c in self._clients.values()]

    def all(self):
        with self._lock:
            return list(self._clients.values())

    def required_groups(self):
        """Union of what every connected client needs open."""
        with self._lock:
            out = set()
            for c in self._clients.values():
                out.update(c.groups)
            return out

    # -- liveness ------------------------------------------------------------

    def touch(self, conn_id):
        with self._lock:
            info = self._clients.get(conn_id)
            if info:
                info.last_seen = time.time()
                info.missed_pings = 0

    def dead_clients(self, heartbeat_s, max_misses):
        """Connections that have stopped answering, or whose process is gone.

        A socket only reports a peer that *closed*. A frozen or SIGSTOPped
        GUI holds its slot open forever, which would keep its device groups
        open and block server shutdown, so liveness is checked three ways:
        EOF on the socket, missed pings, and /proc.
        """
        now = time.time()
        stale = []
        with self._lock:
            for info in self._clients.values():
                if info.pid and not pid_alive(info.pid):
                    stale.append((info, "process is gone"))
                elif now - info.last_seen > heartbeat_s * (max_misses + 1):
                    stale.append((info, "stopped answering pings"))
        return stale
