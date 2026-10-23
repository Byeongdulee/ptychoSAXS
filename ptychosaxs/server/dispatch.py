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


# ---------------------------------------------------------------------------
# optics
# ---------------------------------------------------------------------------

def _optics(ctx):
    session = ctx.daemon.session
    if session.devices.optics is None:
        raise Rejected(errors.ERR_NOT_READY,
                       "the optics group is not open; no connected client "
                       "has asked for it")
    return session.optics


def _motor(params):
    motor = params.get("motor")
    if not motor:
        raise Rejected(errors.ERR_BAD_PARAMS, "motor is required")
    return motor


def _device_call(fn, *args, **kw):
    """Run a driver call, turning hardware failures into ERR_DEVICE."""
    try:
        return fn(*args, **kw)
    except Rejected:
        raise
    except KeyError as exc:
        raise Rejected(errors.ERR_BAD_PARAMS, f"unknown motor {exc}")
    except Exception as exc:
        raise Rejected(errors.ERR_DEVICE, f"{type(exc).__name__}: {exc}")


@method("optics.motors")
def _optics_motors(ctx, params):
    return {"motors": _optics(ctx).motors()}


@method("optics.positions")
def _optics_positions(ctx, params):
    return {"positions": _optics(ctx).positions()}


@method("optics.position")
def _optics_position(ctx, params):
    motor = _motor(params)
    return {"motor": motor,
            "position": _device_call(_optics(ctx).position, motor)}


@method("optics.status")
def _optics_status(ctx, params):
    return _optics(ctx).status()


@method("optics.move")
def _optics_move(ctx, params):
    motor = _motor(params)
    if "value" not in params:
        raise Rejected(errors.ERR_BAD_PARAMS, "value is required")
    return {"motor": motor,
            "position": _device_call(_optics(ctx).move, motor,
                                     params["value"],
                                     wait=bool(params.get("wait", False)))}


@method("optics.move_rel")
def _optics_move_rel(ctx, params):
    motor = _motor(params)
    if "delta" not in params:
        raise Rejected(errors.ERR_BAD_PARAMS, "delta is required")
    return {"motor": motor,
            "position": _device_call(_optics(ctx).move_rel, motor,
                                     params["delta"],
                                     wait=bool(params.get("wait", False)))}


@method("optics.stop")
def _optics_stop(ctx, params):
    # Deliberately not a job and deliberately not validated against a motor
    # list first: Stop must be the shortest path there is.
    return _optics(ctx).stop(params.get("motor"))


@method("optics.set_position")
def _optics_set_position(ctx, params):
    motor = _motor(params)
    return {"motor": motor,
            "position": _device_call(_optics(ctx).set_position, motor,
                                     params.get("value", 0.0))}


# ---------------------------------------------------------------------------
# raw records
# ---------------------------------------------------------------------------

@method("pv.subscribe")
def _pv_subscribe(ctx, params):
    names = params.get("names") or []
    if not isinstance(names, list):
        raise Rejected(errors.ERR_BAD_PARAMS, "names must be a list")
    values = ctx.daemon.session.pvs.subscribe(ctx.conn.conn_id, names)
    return {"values": values}


@method("pv.unsubscribe")
def _pv_unsubscribe(ctx, params):
    names = params.get("names")
    closed = ctx.daemon.session.pvs.unsubscribe(ctx.conn.conn_id, names)
    return {"closed": closed}


@method("pv.get")
def _pv_get(ctx, params):
    name = params.get("name")
    if not name:
        raise Rejected(errors.ERR_BAD_PARAMS, "name is required")
    return {"name": name, "value": ctx.daemon.session.pvs.get(name)}


@method("pv.put")
def _pv_put(ctx, params):
    name = params.get("name")
    if not name:
        raise Rejected(errors.ERR_BAD_PARAMS, "name is required")
    if "value" not in params:
        raise Rejected(errors.ERR_BAD_PARAMS, "value is required")
    _device_call(ctx.daemon.session.pvs.put, name, params["value"])
    return {"name": name, "written": True}


# ---------------------------------------------------------------------------
# the CRL rig
# ---------------------------------------------------------------------------

def _crl(ctx):
    controller = ctx.daemon.session.devices.crl
    if controller is None:
        raise Rejected(errors.ERR_NOT_READY,
                       "the CRL group is not open; no connected client has "
                       "asked for it")
    return controller


def _axis(params):
    axis = params.get("axis")
    if not axis:
        raise Rejected(errors.ERR_BAD_PARAMS, "axis is required")
    return axis


@method("crl.axes")
def _crl_axes(ctx, params):
    c = _crl(ctx)
    return {"axes": [{"name": n, "unit": c.get_unit(n),
                      "connected": c.is_axis_connected(n)} for n in c.names]}


@method("crl.positions")
def _crl_positions(ctx, params):
    c = _crl(ctx)
    return {"positions": {n: c.get_pos(n) for n in c.names}}


@method("crl.position")
def _crl_position(ctx, params):
    c = _crl(ctx)
    axis = _axis(params)
    return {"axis": axis, "position": _device_call(c.get_pos, axis)}


@method("crl.move")
def _crl_move(ctx, params):
    if "target" not in params:
        raise Rejected(errors.ERR_BAD_PARAMS, "target is required")
    _device_call(_crl(ctx).mv, _axis(params), float(params["target"]),
                 wait=bool(params.get("wait", False)))
    return {"axis": params["axis"], "moving": True}


@method("crl.tweak")
def _crl_tweak(ctx, params):
    _device_call(_crl(ctx).tweak, _axis(params),
                 float(params.get("step", 0.0)),
                 forward=bool(params.get("forward", True)))
    return {"axis": params["axis"]}


@method("crl.set_tweak_step")
def _crl_set_tweak_step(ctx, params):
    _device_call(_crl(ctx).set_tweak_step, _axis(params),
                 float(params.get("step", 0.0)))
    return {"axis": params["axis"]}


@method("crl.stop")
def _crl_stop(ctx, params):
    c = _crl(ctx)
    targets = [params["axis"]] if params.get("axis") else list(c.names)
    stopped, failed = [], {}
    for name in targets:
        try:
            c.stop(name)
            stopped.append(name)
        except Exception as exc:
            failed[name] = f"{type(exc).__name__}: {exc}"
    return {"stopped": stopped, "failed": failed}


@method("crl.get_speed")
def _crl_get_speed(ctx, params):
    axis = _axis(params)
    return {"axis": axis, "speed": _device_call(_crl(ctx).get_speed, axis)}


@method("crl.set_speed")
def _crl_set_speed(ctx, params):
    if "velocity" not in params:
        raise Rejected(errors.ERR_BAD_PARAMS, "velocity is required")
    _device_call(_crl(ctx).set_speed, _axis(params),
                 float(params["velocity"]))
    return {"axis": params["axis"]}


@method("crl.set_position")
def _crl_set_position(ctx, params):
    axis = _axis(params)
    _device_call(_crl(ctx).set_pos, axis, float(params.get("value", 0.0)))
    return {"axis": axis}
