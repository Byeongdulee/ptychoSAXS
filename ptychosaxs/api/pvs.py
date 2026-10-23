"""Monitored EPICS PVs, shared by every client that asked for one.

The panels bind line edits straight to records -- a drive field, a tweak
step, the X-ray eye exposure -- and each one used to keep its own PV cache
and poll it on a GUI-thread timer. There were four such caches, all with
the same comment about not reopening a channel per tick, and with two
panels open the IOC served the same records twice.

Here there is one cache. A client subscribes to the names it binds, the
values are pushed to it with everything else, and a write is an RPC. Which
means a bound box in the optics panel updates when the main panel writes
the same record, which it previously could not.

Subscriptions are reference counted per connection, so the last client that
cared about a record is what closes it -- the same rule the device groups
follow.
"""

import threading
import time

#: Cap on a single record read. Matches epics_motor.READ_TIMEOUT_S, and for
#: the same reason: these are served from pyepics' monitor cache, so a read
#: that takes measurable time means the record is not answering.
READ_TIMEOUT_S = 0.2

#: How long a freshly created record may take to connect before we call it
#: unreachable. Channel Access search is asynchronous, so a PV is always
#: briefly unconnected right after it is made; warning then would flag every
#: healthy record at startup and teach everyone to ignore the warning.
CONNECT_GRACE_S = 5.0


class _MemoryPV:
    """Simulated record: remembers what was put to it.

    Deliberately not debug_stubs.FakePV, which reads back 0 for everything.
    A bound line edit polling that would snap to 0 on the first tick and
    hide the behaviour someone is running debug mode to look at.
    """

    connected = True

    def __init__(self, pvname, value=None):
        self.pvname = pvname
        self._value = value

    def get(self, timeout=None):
        # Accepts `timeout` because PVCache.get always passes one; without
        # it every simulated read raised TypeError and came back None, so
        # every bound box in debug mode stayed blank.
        return self._value

    def put(self, value):
        self._value = value


class PVCache:
    def __init__(self, profile, log, pv_class=None):
        self._profile = profile
        self._log = log
        self._pv_class = pv_class
        self._lock = threading.RLock()
        self._pvs = {}                  # name -> PV object
        self._refs = {}                 # name -> {conn_id, ...}
        self._unreachable = set()       # reported once, not every tick
        self._created = {}              # name -> when we made the channel

    # -- construction --------------------------------------------------------

    def _make(self, name):
        if self._pv_class is not None:
            return self._pv_class(name)
        if self._profile.simulated:
            return _MemoryPV(name)
        from epics import PV
        # auto_monitor so a read is served from the local cache rather than
        # a Channel Access round trip; the publisher reads these at 10 Hz.
        return PV(name, auto_monitor=True)

    def _channel(self, name):
        with self._lock:
            pv = self._pvs.get(name)
            if pv is None:
                try:
                    pv = self._make(name)
                except Exception:
                    self._log.exception("could not open PV %s", name)
                    return None
                self._pvs[name] = pv
                self._created[name] = time.monotonic()
            return pv

    # -- subscription --------------------------------------------------------

    def subscribe(self, conn_id, names):
        """Start pushing these records to `conn_id`. Returns current values."""
        for name in names:
            with self._lock:
                self._refs.setdefault(name, set()).add(conn_id)
            self._channel(name)
        return self.snapshot(names)

    def unsubscribe(self, conn_id, names=None):
        """Drop a connection's interest; close records nobody wants."""
        dropped = []
        with self._lock:
            targets = list(self._refs) if names is None else list(names)
            for name in targets:
                holders = self._refs.get(name)
                if not holders:
                    continue
                holders.discard(conn_id)
                if not holders:
                    del self._refs[name]
                    pv = self._pvs.pop(name, None)
                    if pv is not None:
                        dropped.append((name, pv))
        for name, pv in dropped:
            try:
                disconnect = getattr(pv, "disconnect", None)
                if disconnect:
                    disconnect()
            except Exception:
                self._log.debug("PV %s did not disconnect cleanly", name,
                                exc_info=True)
        return len(dropped)

    def subscribed(self):
        with self._lock:
            return sorted(self._refs)

    # -- access --------------------------------------------------------------

    def get(self, name):
        """This record's value, or None. Never blocks for long.

        The connection check is the important half. A record whose IOC is
        down never connects, and pyepics' get() on it blocks for the whole
        timeout -- so reading a handful of dead records on the publisher
        thread stalls the broadcast for every connected client. That is not
        hypothetical: two unreachable X-ray-eye camera records were enough
        to add about a second to every position update in the optics panel.

        Same discipline as epics_motor._read, which this should have
        followed from the start.
        """
        pv = self._channel(name)
        if pv is None:
            return None
        if not getattr(pv, "connected", True):
            age = time.monotonic() - self._created.get(name, 0)
            if age > CONNECT_GRACE_S and name not in self._unreachable:
                self._unreachable.add(name)
                self._log.warning(
                    "record %s has not connected in %.0fs; it reads as None "
                    "until its IOC answers. This no longer slows anything "
                    "down, but whatever binds it will stay blank.",
                    name, age)
            return None
        if name in self._unreachable:
            self._unreachable.discard(name)
            self._log.info("record %s is answering again", name)
        try:
            return pv.get(timeout=READ_TIMEOUT_S)
        except Exception:
            self._log.debug("could not read %s", name, exc_info=True)
            return None

    def put(self, name, value):
        pv = self._channel(name)
        if pv is None:
            raise RuntimeError(f"no channel for {name}")
        if not getattr(pv, "connected", True):
            # A put to a disconnected PV is dropped silently by pyepics, so
            # say so rather than letting a box look like it was written.
            raise RuntimeError(f"{name} is not connected")
        pv.put(value)
        return True

    def snapshot(self, names=None):
        """{name: value} for the subscribed records.

        A record that cannot be read contributes None rather than being
        absent, so a client can tell "unreadable" from "not subscribed".
        """
        with self._lock:
            wanted = list(self._refs) if names is None else list(names)
        return {name: self.get(name) for name in wanted}

    def close_all(self):
        with self._lock:
            pvs = list(self._pvs.items())
            self._pvs.clear()
            self._refs.clear()
        for name, pv in pvs:
            try:
                disconnect = getattr(pv, "disconnect", None)
                if disconnect:
                    disconnect()
            except Exception:
                self._log.debug("PV %s did not disconnect cleanly", name,
                                exc_info=True)
