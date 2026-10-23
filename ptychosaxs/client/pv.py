"""Client-side mirror of the records the server is pushing.

gui/pv_lineedit.py binds a QLineEdit to an object with `.connected`,
`.get()` and `.put()`. That used to be an epics.PV the panel opened itself,
one cache per panel, polled on a GUI-thread timer. RemotePV is the same
four-member interface backed by the shared server instead: reads come from
the last pv.values event, writes are an RPC.

The win is not only that the panel stops opening channels. Two panels bound
to the same record now see each other's writes, because there is one
subscription behind both of them -- previously each had its own channel and
its own idea of the value.
"""

import threading


class RemotePV:
    """One record, read from the mirror and written through the server."""

    __slots__ = ("pvname", "_cache")

    def __init__(self, pvname, cache):
        self.pvname = pvname
        self._cache = cache

    @property
    def connected(self):
        return self._cache.connected

    def get(self):
        return self._cache.value(self.pvname)

    def put(self, value):
        return self._cache.put(self.pvname, value)

    def wait_for_connection(self, timeout=None):
        """Part of the epics.PV surface that callers reach for.

        There is nothing to wait for here: the server owns the channel and
        subscribing already asked it for the current value. Answering
        immediately is also the point -- gui/handlers/scalar_scan.py calls
        this on the miss path of its PV cache, and a blocking wait there
        would reintroduce exactly the stall that unreachable records used
        to cause.
        """
        return self._cache.connected

    def __repr__(self):
        return f"<RemotePV {self.pvname}={self.get()!r}>"


class RemotePVCache:
    """Every record this client has bound, and their latest values.

    Subscribing is batched per call rather than per record: a panel binds a
    dozen fields while it builds its view, and a round trip each would show
    up as a visible stall on a slow link.
    """

    def __init__(self, client):
        self._client = client
        self._lock = threading.Lock()
        self._values = {}
        self._subscribed = set()
        client.on("pv.values", self._on_values)

    # -- binding -------------------------------------------------------------

    def pv(self, pvname):
        """A PVLineEdit-compatible handle, subscribing on first use."""
        if pvname is None:
            return None
        with self._lock:
            new = pvname not in self._subscribed
            if new:
                self._subscribed.add(pvname)
        if new:
            self._sync([pvname])
        return RemotePV(pvname, self)

    def subscribe_many(self, names):
        """Bind several records in one round trip."""
        names = [n for n in names if n]
        with self._lock:
            fresh = [n for n in names if n not in self._subscribed]
            self._subscribed.update(fresh)
        if fresh:
            self._sync(fresh)

    def _sync(self, names):
        try:
            values = self._client.pv.subscribe(names)
        except Exception:
            # A record that cannot be subscribed is not fatal: the bound box
            # simply shows nothing, which is what it did before it was bound.
            return
        with self._lock:
            self._values.update(values)

    # -- values --------------------------------------------------------------

    def _on_values(self, _name, data):
        with self._lock:
            self._values.update(data.get("values", {}))

    def value(self, pvname):
        with self._lock:
            return self._values.get(pvname)

    def put(self, pvname, value):
        self._client.pv.put(pvname, value)
        # Reflect it immediately rather than waiting for the next event, so
        # a box does not flicker back to its old value between the write and
        # the next publish.
        with self._lock:
            self._values[pvname] = value
        return True

    @property
    def connected(self):
        return self._client.connected

    def names(self):
        with self._lock:
            return sorted(self._subscribed)
