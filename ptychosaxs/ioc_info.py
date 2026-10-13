"""
Who is serving the PVs this GUI depends on, and since when.

Every stage and detector here lives behind an IOC, and an IOC that restarted
mid-session is invisible from the GUI: the records still answer, autosave has
put plausible numbers back, and nothing says the positions are from before
the restart. Worse, an IOC can be up while the controller behind it is not -
which is a different fault with the same symptom, six axes reading zero.

This module collects both facts for every IOC the GUI talks to. It is kept
separate from the dialog that shows them so it can be tested with a fake PV
class, like the motor controllers are.

Reads are bounded and never raise: an unreachable IOC is a row saying so, not
an exception on the GUI thread.
"""

import time

try:
    from .epics_hexapod import ASYN_CNCT_PV, ASYN_CONNECTED
except ImportError:
    from epics_hexapod import ASYN_CNCT_PV, ASYN_CONNECTED

# devIocStats fields, where the IOC loads it. iocAdminSoft.db publishes these
# under the IOC's own prefix.
_HOSTNAME = ":HOSTNAME"
_STARTED = ":STARTTOD"
_ENGINEER = ":ENGINEER"
_UPTIME = ":UPTIME"

READ_TIMEOUT_S = 0.5

# One shared budget across every IOC, not one per IOC - see collect().
CONNECT_TIMEOUT_S = 1.5

# (prefix, what it serves, a PV known to exist there). The probe PV is only
# needed for IOCs without devIocStats - pyepics can still name the host
# serving any connected channel, which is the one fact worth having.
IOC_TABLE = [
    ("12idbHXP", "Hexapod (PI C-887)", "12idbHXP:m1.RBV"),
    ("12ideMCS1", "Goniometer (trans/tilt)", "12ideMCS1:m1.RBV"),
    ("12ideMCS2", "CRL 3D-print stage", "12ideMCS2:m1.RBV"),
    ("12ideSFT", "Soft motors", "12ideSFT:m4.RBV"),
    ("12idcACS1", "ACS stages", "12idcACS1:m1.RBV"),
    ("12idcUC8", "UC8 stages", "12idcUC8:m1.RBV"),
    ("12idc", "Beamline / scaler", "12idc:3820:scaler1.CNT"),
    ("12ida2", "Mono / energy", "12ida2:EnCalc"),
    ("12idcPIL", "Pilatus detector", "12idcPIL:cam1:Acquire"),
    ("12idcDAN", "Dante detector", "12idcDAN:cam1:Acquire"),
    ("12idbPE", "Perkin-Elmer detector", "12idbPE:cam1:Acquire"),
]


def _pv_factory(pv_class=None):
    if pv_class is not None:
        return pv_class
    from epics import PV  # imported late: this module is useful without CA

    return PV


def _read(pv_class, name, connect_timeout=CONNECT_TIMEOUT_S):
    """A bounded read that answers None instead of raising or blocking."""
    try:
        pv = pv_class(name)
        wait = getattr(pv, "wait_for_connection", None)
        if wait is not None:
            wait(timeout=connect_timeout)
        if not pv.connected:
            return None, pv
        return pv.get(timeout=READ_TIMEOUT_S), pv
    except Exception:
        return None, None


def _as_text(value):
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("latin-1", "replace").strip() or None
    text = str(value).strip()
    return text or None


def ioc_status(prefix, role="", probe_pv="", pv_class=None,
               connect_timeout=CONNECT_TIMEOUT_S):
    """One IOC's identity: host, start time, engineer, uptime.

    devIocStats is the good path. Where it is not loaded - three of the IOCs
    here - fall back to the host pyepics reports for a channel known to exist
    on that IOC, which still answers the question that matters most.
    """
    pv_class = _pv_factory(pv_class)
    row = {
        "prefix": prefix,
        "role": role,
        "reachable": False,
        "host": None,
        "started": None,
        "engineer": None,
        "uptime": None,
        "source": None,
    }

    host, _pv = _read(pv_class, prefix + _HOSTNAME, connect_timeout)
    if host is not None:
        row.update(
            reachable=True,
            source="devIocStats",
            host=_as_text(host),
            started=_as_text(_read(pv_class, prefix + _STARTED, connect_timeout)[0]),
            engineer=_as_text(_read(pv_class, prefix + _ENGINEER, connect_timeout)[0]),
            uptime=_as_text(_read(pv_class, prefix + _UPTIME, connect_timeout)[0]),
        )
        return row

    if probe_pv:
        _value, pv = _read(pv_class, probe_pv, connect_timeout)
        if pv is not None and getattr(pv, "connected", False):
            row.update(
                reachable=True,
                source="channel host (no devIocStats)",
                host=_as_text(getattr(pv, "host", None)),
            )
    return row


def collect(table=None, pv_class=None):
    """Every IOC in the table, in order. Never raises.

    The channels are created first and then given one shared wait, rather
    than a connect timeout each. Channel Access searches run concurrently,
    so waiting on each in turn would make the dialog freeze for the timeout
    times the number of IOCs - worst exactly when several are down, which is
    when someone is most likely to open it.
    """
    table = table or IOC_TABLE
    pv_class = _pv_factory(pv_class)
    warmed = []
    for prefix, _role, probe in table:
        names = [prefix + field for field in
                 (_HOSTNAME, _STARTED, _ENGINEER, _UPTIME)]
        names.append(probe)
        for name in names:
            if not name:
                continue
            try:
                warmed.append(pv_class(name))
            except Exception:
                pass
    deadline = time.monotonic() + CONNECT_TIMEOUT_S
    for pv in warmed:
        wait = getattr(pv, "wait_for_connection", None)
        remaining = deadline - time.monotonic()
        if wait is None or remaining <= 0:
            break
        try:
            wait(timeout=remaining)
        except Exception:
            pass
    return [
        ioc_status(prefix, role, probe, pv_class=pv_class, connect_timeout=0.0)
        for prefix, role, probe in table
    ]


def hexapod_link_status(pv_class=None):
    """Whether the hexapod IOC can actually reach its controller.

    Separate from the table because it is a different question: the IOC can
    be perfectly healthy while the C-887 behind it is unplugged, and then
    every hexapod readback is undefined rather than a position.
    """
    pv_class = _pv_factory(pv_class)
    value, pv = _read(pv_class, ASYN_CNCT_PV)
    if pv is None or not getattr(pv, "connected", False):
        return None  # cannot tell - the hexapod IOC itself is not answering
    return value == ASYN_CONNECTED


def format_report(rows, link_ok=None):
    """Plain text for the dialog, one line per IOC."""
    width = max((len(r["prefix"]) for r in rows), default=0)
    lines = []
    for row in rows:
        name = row["prefix"].ljust(width)
        if not row["reachable"]:
            lines.append(f"{name}  unreachable                 {row['role']}")
            continue
        started = row["started"] or "start time unknown"
        lines.append(f"{name}  {row['host']}  {started}  {row['role']}")
    if link_ok is not None:
        lines.append("")
        lines.append(
            "Hexapod controller link: "
            + ("up" if link_ok else "DOWN - the IOC is running but cannot reach "
                                   "the C-887, so its readbacks are not positions")
        )
    return "\n".join(lines)
