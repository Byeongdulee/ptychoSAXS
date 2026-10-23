"""The upstream optics: positions, moves, and whether each block is in.

The panel's half of this contract already existed. gui/optics_modes.py
drives in/out sequences through a six-method interface on the panel --
has_block, block_state, block_has_target, move_block, pv_value, pv_factory
-- and its docstring says outright that "nothing here touches a widget of
the panel directly ... the same sequences would run against the full view,
or a test double, unchanged". That interface is what moved here. The panel
keeps the same calls; they cross a socket now.

Classification is not reimplemented. gui/optics_status.py already turns
live positions plus the saved definitions into IN / OUT / BETWEEN /
UNDEFINED, is already Qt-free, and already takes its readings through an
injectable `caget` so it can be driven from anywhere. The server passes its
own readings in.
"""

from ptychosaxs import optics_config

#: Fallback when a controller cannot be built. Keeps the shape of a reading
#: so a panel renders "----" rather than crashing on a missing key.
UNREADABLE = None


class OpticsService:
    """Everything the optics panel asks of the hardware."""

    def __init__(self, devices, pvs, log):
        self._devices = devices
        self._pvs = pvs
        self._log = log

    # -- controllers ---------------------------------------------------------

    @property
    def controllers(self):
        return self._devices.get("optics") or {}

    def _controller_for(self, motor):
        group, index = optics_config.MOTORS[motor]
        controller = self.controllers.get(group)
        if controller is None:
            raise RuntimeError(
                f"the optics group is not open; cannot reach {motor}")
        return controller, controller.motornames[index]

    def motors(self):
        return list(optics_config.MOTORS)

    # -- reads ---------------------------------------------------------------

    def position(self, motor):
        controller, axis = self._controller_for(motor)
        return float(controller.get_pos(axis))

    def positions(self):
        """Every optics motor. A motor that cannot be read reports None
        rather than dropping out, so a panel can show it as unavailable
        instead of silently omitting a row."""
        out = {}
        for motor in optics_config.MOTORS:
            try:
                out[motor] = self.position(motor)
            except Exception:
                self._log.debug("could not read optics motor %s", motor,
                                exc_info=True)
                out[motor] = UNREADABLE
        return out

    def status(self, ini_path=None):
        """IN / OUT / BETWEEN per block, from live positions plus the ini."""
        from ptychosaxs.api import optics_status
        live = self.positions()
        try:
            status = optics_status.read_status(
                ini_path or optics_status.OPTICS_INI_PATH, live=live)
        except Exception:
            self._log.exception("could not classify optics status")
            return {"blocks": {}, "good_for_ptycho": False,
                    "problems": ("status unavailable",)}
        return {
            "blocks": {name: block.state
                       for name, block in status.blocks.items()},
            "good_for_ptycho": bool(
                optics_status.check_good_for_ptycho(status)),
            "problems": tuple(getattr(status, "problems", ()) or ()),
        }

    # -- moves ---------------------------------------------------------------

    def move(self, motor, value, wait=False):
        controller, axis = self._controller_for(motor)
        controller.mv(axis, float(value), wait=wait)
        return self.position(motor)

    def move_rel(self, motor, delta, wait=False):
        controller, axis = self._controller_for(motor)
        controller.mvr(axis, float(delta), wait=wait)
        return self.position(motor)

    def stop(self, motor=None):
        """Stop one motor, or every optics motor.

        Never a job and never queued behind anything: if Stop can wait, the
        Stop button has stopped working.
        """
        targets = [motor] if motor else list(optics_config.MOTORS)
        stopped, failed = [], {}
        for name in targets:
            try:
                controller, axis = self._controller_for(name)
                controller.stop(axis)
                stopped.append(name)
            except Exception as exc:
                # Keep going: one unreachable axis must not prevent the
                # others from being stopped.
                failed[name] = f"{type(exc).__name__}: {exc}"
                self._log.warning("could not stop %s: %s", name, exc)
        return {"stopped": stopped, "failed": failed}

    def set_position(self, motor, value=0.0):
        """Redefine the current position without moving."""
        controller, axis = self._controller_for(motor)
        controller.set_pos(axis, float(value))
        return self.position(motor)


def open_controllers(profile, log, pv_class=None):
    """Build the four optics controllers. Never raises.

    Follows motions_ver2.build_controller's rule: a group that cannot be
    built is reported and left out, because a machine that can reach only
    some of the optics should still bring up the panel.
    """
    if profile.simulated:
        from ptychosaxs.debug import debug_stubs
        return {"opticsbox": debug_stubs.DebugOpticsbox(),
                "OSA": debug_stubs.DebugOSA(),
                "saxs_z": debug_stubs.DebugSaxsZ(),
                "beamstop": debug_stubs.DebugBeamstop()}

    from ptychosaxs import optics
    built = {}
    for name, cls in (("opticsbox", optics.opticsbox), ("OSA", optics.OSA),
                      ("saxs_z", optics.saxs_z), ("beamstop", optics.beamstop)):
        try:
            built[name] = cls(pv_class=pv_class)
        except Exception as exc:
            log.warning("optics group %r could not be built: %s", name, exc)
    return built


def close_controllers(controllers):
    for controller in (controllers or {}).values():
        disconnect = getattr(controller, "disconnect", None)
        if disconnect:
            try:
                disconnect()
            except Exception:
                pass
