"""The method table, and the rule about what may run where.

Every handler is registered against a name in ptychosaxs.link.methods, which
is the list the client's facades are checked against too. A method that
exists on one side and not the other is the same class of bug as a stage in
transport.CHOICES with no builder behind it, and tests/test_api_contract.py
rejects it for the same reason.

`blocking` is the load-bearing flag. A handler declared non-blocking runs
inline on its connection's reader thread, which is only safe because EPICS
reads come from pyepics' monitor cache -- EpicsMotorController creates its
readable fields with auto_monitor=True and its docstring states reads are
safe from any thread. Anything that can take longer than a few milliseconds
must be blocking, and is wrapped as a job whose reply is a job id.
"""

import time

from ptychosaxs.link import errors, methods

HANDLERS = {}


class Handler:
    __slots__ = ("fn", "name", "blocking")

    def __init__(self, fn, name, blocking):
        self.fn = fn
        self.name = name
        self.blocking = blocking


def method(name, blocking=False):
    """Register a handler. The name must be in link.methods."""
    if name not in methods.NAMES:
        raise KeyError(
            f"{name!r} is not in ptychosaxs.link.methods; add it there so "
            f"the client and the contract test can see it too")
    if blocking and name not in methods.BLOCKING:
        raise ValueError(f"{name!r} is blocking but not listed in "
                         f"methods.BLOCKING")

    def register(fn):
        HANDLERS[name] = Handler(fn, name, blocking)
        return fn
    return register


class Rejected(Exception):
    """A handler refusing, with a code the client can switch on."""

    def __init__(self, code, message, data=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data or {}


# ---------------------------------------------------------------------------
# system
# ---------------------------------------------------------------------------

@method("system.hello")
def _hello(ctx, params):
    # The handshake is handled by the connection itself, because it has to
    # happen before anything else is allowed; this entry exists so the name
    # resolves and so an attempt to say hello twice gets a clear answer.
    raise Rejected(errors.ERR_PROTOCOL,
                   "hello has already been exchanged on this connection")


@method("system.ping")
def _ping(ctx, params):
    return {"pong": time.time()}


@method("system.status")
def _status(ctx, params):
    return ctx.daemon.status()


@method("system.clients")
def _clients(ctx, params):
    return {"clients": ctx.daemon.registry.snapshot()}


@method("system.request_focus")
def _request_focus(ctx, params):
    """Ask the GUI already holding a slot to raise its window.

    The second launch of a GUI calls this, then exits. Without it the
    single-instance rule is a refusal; with it, double-clicking the icon
    brings the window you wanted to the front, which is what was meant.
    """
    gui_type = params.get("gui_type")
    if not gui_type:
        raise Rejected(errors.ERR_BAD_PARAMS, "gui_type is required")
    holder = ctx.daemon.registry.holder_of(gui_type)
    if holder is None:
        return {"raised": False, "reason": "nothing holds that slot"}
    ctx.daemon.send_focus_request(holder.conn_id, gui_type)
    return {"raised": True, "pid": holder.pid}


@method("system.shutdown")
def _shutdown(ctx, params):
    """Ask the server to exit.

    Refused outright while a fly scan is running: the trajectory is in the
    C-887's wave generator and there is no way to stop it from here, so the
    honest answer is "wait", not a shutdown that abandons a moving stage.
    """
    blocking = ctx.daemon.policy.blocking_reason()
    if blocking:
        raise Rejected(errors.ERR_FLY_IN_PROGRESS, blocking,
                       {"jobs": ctx.daemon.session.jobs.snapshot()})
    ctx.daemon.policy.request_shutdown()
    return {"shutting_down": True}


# ---------------------------------------------------------------------------
# motion (read-only in Phase 1)
# ---------------------------------------------------------------------------

def _devices(ctx):
    devices = ctx.daemon.session.devices
    if devices.motion is None:
        raise Rejected(errors.ERR_NOT_READY,
                       "the motion group is not open; no connected client "
                       "has asked for it")
    return devices


@method("motion.axes")
def _axes(ctx, params):
    return {"axes": [a.describe() for a in _devices(ctx).axes()]}


@method("motion.positions")
def _positions(ctx, params):
    devices = _devices(ctx)
    return {"positions": devices.positions(), "moving": devices.moving()}


@method("motion.position")
def _position(ctx, params):
    axis = params.get("axis")
    if not axis:
        raise Rejected(errors.ERR_BAD_PARAMS, "axis is required")
    try:
        return {"axis": axis, "position": _devices(ctx).position(axis)}
    except KeyError:
        raise Rejected(errors.ERR_BAD_PARAMS, f"unknown axis {axis!r}")
    except Rejected:
        raise
    except Exception as exc:
        raise Rejected(errors.ERR_DEVICE, f"{type(exc).__name__}: {exc}")


def missing_handlers():
    """Names in link.methods with no handler. Used by the contract test."""
    return sorted(methods.NAMES - set(HANDLERS))
