"""What the server is driving: real hardware, or simulated stand-ins.

One switch, not six. The GUI used to carry six hand-edited DEBUG_* booleans
plus a --debug level that recombined them, and exported the result into the
environment for the device layer to read at import. That arrangement produced
exactly the bug you would predict: rungui exported PTYCHOSAXS_DEBUG_TOMO_PHI
while motions_ver2 read PTYCHOSAXS_DEBUG_ACS, so the phi debug flag silently
did nothing for as long as it has existed.

Now the server owns the switch and the names live in one place. The
per-controller variables still work as an escape hatch for someone debugging
a single stage, but nothing in a GUI sets them any more.

Resolution happens before the device layer is imported, because
motions_ver2, interferometers and transport all read os.environ at module
scope.
"""

import os
from dataclasses import dataclass, field

# Per-controller simulation switches, read at import by the device layer.
DEBUG_VARS = (
    "PTYCHOSAXS_DEBUG_HEXAPOD",
    "PTYCHOSAXS_DEBUG_TOMO_PHI",
    "PTYCHOSAXS_DEBUG_GONIO",
    "PTYCHOSAXS_DEBUG_QDS",
    "PTYCHOSAXS_DEBUG_DG645",
    "PTYCHOSAXS_DEBUG_EPICS",
)

# Driver selection, owned by ptychosaxs/transport.py.
ENV_HEXAPOD_TRANSPORT = "PTYCHOSAXS_HEXAPOD_TRANSPORT"
ENV_GONIO_SMARACT = "PTYCHOSAXS_GONIO_SMARACT"
ENV_PHI_ACS = "PTYCHOSAXS_PHI_ACS"


def _truthy(value) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Profile:
    """How this server was asked to talk to the instrument."""

    simulated: bool = False
    hexapod_transport: str = "epics"
    gonio_smaract: bool = False
    phi_acs: bool = False
    # Set when a stage was asked for and could not be built. Filled in by
    # DeviceRegistry, not by the constructor.
    degraded: tuple = field(default_factory=tuple)

    @classmethod
    def from_env(cls, env=None, simulated=None):
        env = os.environ if env is None else env
        return cls(
            simulated=(bool(simulated) if simulated is not None
                       else any(_truthy(env.get(v, "0")) for v in DEBUG_VARS)),
            hexapod_transport=env.get(ENV_HEXAPOD_TRANSPORT) or "epics",
            gonio_smaract=_truthy(env.get(ENV_GONIO_SMARACT, "0")),
            phi_acs=_truthy(env.get(ENV_PHI_ACS, "0")),
        )

    # Modules that read these variables at import time. Setting a variable
    # after one of them is imported has no effect whatsoever.
    LATE_IMPORT_SENTINELS = (
        "ptychosaxs.motions_ver2",
        "ptychosaxs.interferometers",
    )

    def apply_to_env(self, env=None, warn=None) -> None:
        """Write this profile into the environment.

        Must run before the first import of ptychosaxs.motions_ver2 or
        ptychosaxs.interferometers: both read os.environ at module scope, so
        a variable set afterwards is simply ignored and the stage is built
        for real while the caller believes it asked for a simulation. That
        failure is silent and it writes fabricated positions into real data
        files, so it is checked rather than documented.
        """
        import sys as _sys
        env = os.environ if env is None else env
        if env is os.environ:
            late = [m for m in self.LATE_IMPORT_SENTINELS if m in _sys.modules]
            if late:
                msg = ("profile applied after %s was already imported; its "
                       "module-scope flags are already fixed and this "
                       "profile will not take effect" % ", ".join(late))
                if warn:
                    warn(msg)
                else:
                    import warnings
                    warnings.warn(msg, RuntimeWarning, stacklevel=2)
        for var in DEBUG_VARS:
            env[var] = "1" if self.simulated else "0"
        env[ENV_HEXAPOD_TRANSPORT] = (
            "simulated" if self.simulated else self.hexapod_transport)
        env[ENV_GONIO_SMARACT] = "1" if self.gonio_smaract else "0"
        env[ENV_PHI_ACS] = "1" if self.phi_acs else "0"

    def describe(self) -> dict:
        """The form that goes over the wire in the hello reply.

        A GUI renders its caution banner from this rather than from its own
        module globals, so a panel can never claim the hardware is real while
        the server it is driving is simulated.
        """
        return {
            "simulated": self.simulated,
            "hexapod_transport": self.hexapod_transport,
            "gonio_smaract": self.gonio_smaract,
            "phi_acs": self.phi_acs,
            "degraded": list(self.degraded),
        }

    def summary(self) -> str:
        if self.simulated:
            return "SIMULATED - no hardware is connected"
        bits = [f"hexapod={self.hexapod_transport}",
                f"gonio={'smaract' if self.gonio_smaract else 'epics'}",
                f"phi={'acs' if self.phi_acs else 'epics'}"]
        if self.degraded:
            bits.append("degraded=" + ",".join(self.degraded))
        return "real hardware (" + ", ".join(bits) + ")"
