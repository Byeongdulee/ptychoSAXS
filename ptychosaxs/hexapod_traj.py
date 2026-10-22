"""
Trajectory (fly-scan) transport for the PI C-887 hexapod.

Fly scans are driven by the controller's wave generator - WAV/WSL/WGC/WGO to
build and run the trajectory, TWS/TWC plus CTO to fire one hardware trigger
per bin, DRC/DRR? to read the position record. None of that has an EPICS
interface on 12idbHXP: the motorPIGCS2 driver stubs out profileMove() and
triggerProfile(), no profile-move records are loaded, and the driver contains
no wave-generator commands at all. So trajectories go over GCS, through the
pihexapod library, while step motion goes over EPICS (see epics_hexapod.py).

The library is optional. It is absent from some environments, and when it is
present the controller may still be unreachable. Rather than let that surface
as an AttributeError in the middle of an armed scan, every outcome resolves
to a backend with the full surface and an observable state, and the GUI gates
on can_fly before offering a fly scan at all.

Nothing here imports pihexapod, EPICS or Qt at module scope - the import
happens inside make_trajectory_backend(), so a failure is re-runnable instead
of poisoning the module, and tests can inject their own importer.
"""

import importlib
import socket
import time

import numpy as np

# The C-887's GCS command port.
GCS_PORT = 50000

# The surface scan_handler.py reaches through pts.hexapod. Kept as module
# constants so the tests can parametrise over them and cannot drift from the
# backends.
#
# istraj_running is deliberately absent: the library has no such method.
# scan_handler.py:6201 uses a local variable of that name.
TRAJ_METHODS = (
    "set_traj",
    "set_traj_SNAKE2",
    "analyze_pulse_steps",
    "assign_axis2wavtable",
    "goto_start_pos",
    "run_traj",
    "stop_traj",
    "wait",
    "get_records",
)

TRAJ_ATTRS = (
    "WaveGenID",
    "pulse_number",
    "pulse_step",
    "pulse_positions_index",
    "scantime",
)

# Resolution outcomes, in make_trajectory_backend's order of preference.
STATE_PIHEXAPOD = "pihexapod"      # library imported, device built, surface complete
STATE_SIMULATED = "simulated"      # debug mode
STATE_ABSENT = "absent"            # import failed
STATE_INCOMPATIBLE = "incompatible"  # imported, but missing members we call
STATE_UNREACHABLE = "unreachable"  # complete, but constructing/connecting failed


class TrajectoryUnavailable(RuntimeError):
    """A trajectory operation was attempted with no working transport."""


class TrajectoryBackend:
    """Base for the three real backends. Declares the contract, no behaviour."""

    state = "unknown"
    reason = ""
    available = False


def _forward(name):
    def method(self, *args, **kwargs):
        return getattr(self._dev, name)(*args, **kwargs)
    method.__name__ = name
    method.__doc__ = f"Forwards to pihexapod's {name}()."
    return method


def _forward_attr(name):
    def getter(self):
        return getattr(self._dev, name)
    return property(getter, doc=f"Reads pihexapod's {name}.")


class PiHexapodTrajectory(TrajectoryBackend):
    """The real transport: a thin pass-through to pihexapod.gcs.Hexapod.

    Every method forwards *args/**kwargs untouched. The library owns these
    signatures, so restating them here would only create a second place for
    them to be wrong - a mismatch should surface as pihexapod's own TypeError,
    naming its own parameters.
    """

    available = True
    state = STATE_PIHEXAPOD

    def __init__(self, dev, wave_gen_id, transport="direct", address="", reason=""):
        self._dev = dev
        self._wave_gen_id = wave_gen_id
        self.transport = transport
        self.address = address
        self.reason = reason

    @property
    def WaveGenID(self):
        return self._wave_gen_id

    def disconnect(self):
        disconnect = getattr(self._dev, "disconnect", None)
        if disconnect is not None:
            disconnect()

    def get_records(self, Ndata=0):
        """Read the controller's position record (DRR?).

        Refused on the EPICS transport. IFMT=Hybrid returns one line per
        read, so a scan's worth of record would be one Channel Access round
        trip per point - thousands of them, at ~2.6 ms each, and an attempt
        at 3000 points took the IOC down on 2026-10-19. A fly scan does not
        need this; only fly_result() and plot_qds_hex() do, and they are
        post-hoc. Raising IMAX in the IOC's iocsh/hexapod.cmd so the reply
        arrives in one transaction is what would make it safe.
        """
        if self.transport == "epics":
            raise TrajectoryUnavailable(
                "get_records() is not available over the EPICS transport: "
                f"reading the position record through {self.address}:asyn_1 "
                "costs one Channel Access transaction per scan point. The fly "
                "scan itself does not need it. To enable it, raise IMAX in the "
                "IOC's iocsh/hexapod.cmd so a DRR? reply fits in one read."
            )
        return self._dev.get_records(Ndata)


for _name in TRAJ_METHODS:
    if _name == "get_records":  # overridden above, transport-dependent
        continue
    setattr(PiHexapodTrajectory, _name, _forward(_name))
for _name in TRAJ_ATTRS:
    if _name != "WaveGenID":  # supplied from the module, not the device
        setattr(PiHexapodTrajectory, _name, _forward_attr(_name))
del _name


class UnavailableTrajectory(TrajectoryBackend):
    """Full shape, every member raises.

    A Special Case object rather than a null object: it keeps the facade's
    forwarding uniform, but nothing silently succeeds. A fly scan that
    "worked" against a do-nothing stub would arm the detector, collect frames
    at unknown positions and write them to a real data file - worse than a
    clear failure.
    """

    available = False

    def __init__(self, state, reason):
        self.state = state
        self.reason = reason

    def _fail(self, member):
        raise TrajectoryUnavailable(
            f"{member}: the hexapod trajectory transport is unavailable "
            f"({self.state}) - {self.reason}. Fly scans need the pihexapod "
            f"library and a reachable C-887; step scans are unaffected."
        )


def _unavailable_method(name):
    def method(self, *args, **kwargs):
        self._fail(name)
    method.__name__ = name
    return method


def _unavailable_attr(name):
    def getter(self):
        self._fail(name)
    return property(getter)


for _name in TRAJ_METHODS:
    setattr(UnavailableTrajectory, _name, _unavailable_method(_name))
for _name in TRAJ_ATTRS:
    setattr(UnavailableTrajectory, _name, _unavailable_attr(_name))
del _name


class SimulatedTrajectory(TrajectoryBackend):
    """Offline stand-in used in debug mode and by the test stubs.

    pulse_number is computed, not faked: the arithmetic mirrors the real
    hardware's phantom-trigger and even-row quirks (see
    gui/handlers/scan_handler.py's _adjust_axis_length) so the fly-scan code
    paths that reconcile predicted against actual trigger counts can be
    exercised without a controller.
    """

    available = True
    state = STATE_SIMULATED
    reason = "simulated hexapod (debug mode)"

    WaveGenID = {"X": 1, "Y": 2, "Z": 3, "U": 4, "V": 5, "W": 6}

    def __init__(self):
        self.pulse_number = 0
        self.pulse_step = 0.0
        self.pulse_positions_index = []
        self.scantime = 0.0

    def set_traj(self, axis, total_time, distance, start_pos, direction,
                 step_time, samples) -> None:
        print(f"[DEBUG] SimulatedTrajectory.set_traj({axis}, "
              f"total_time={total_time}, distance={distance}, start_pos={start_pos})")
        self.pulse_step = step_time
        self.pulse_number = round(total_time / step_time) if step_time else 0
        self.scantime = total_time
        self.pulse_positions_index = list(range(self.pulse_number))

    def set_traj_SNAKE2(self, step_time, Xst, Xdist, Xstep, Yst, Yfe, Ystep) -> None:
        print(f"[DEBUG] SimulatedTrajectory.set_traj_SNAKE2(step_time={step_time}, "
              f"X=[{Xst}, {Xst + Xdist}], Xstep={Xstep}, Y=[{Yst}, {Yfe}], Ystep={Ystep})")
        self.pulse_step = step_time
        Xfe = Xst + Xdist
        x_step = -abs(Xstep) if Xst > Xfe else abs(Xstep)
        y_step = -abs(Ystep) if Yst > Yfe else abs(Ystep)
        n_x = len(np.arange(Xst, Xfe + x_step / 2, x_step)) + 1  # phantom trigger
        n_y = len(np.arange(Yst, Yfe + y_step / 2, y_step))
        n_y += n_y % 2  # pad to even
        self.pulse_number = n_x * n_y
        self.scantime = self.pulse_number * step_time
        self.pulse_positions_index = list(range(self.pulse_number))

    def analyze_pulse_steps(self):
        return 1, 1

    def assign_axis2wavtable(self, axis, wave_id) -> None:
        print(f"[DEBUG] SimulatedTrajectory.assign_axis2wavtable({axis}, {wave_id})")

    def goto_start_pos(self, axis) -> None:
        print(f"[DEBUG] SimulatedTrajectory.goto_start_pos({axis})")

    def run_traj(self, axis, wait=False) -> None:
        print(f"[DEBUG] SimulatedTrajectory.run_traj({axis})")

    def istraj_running(self) -> bool:
        return False

    def stop_traj(self) -> None:
        print("[DEBUG] SimulatedTrajectory.stop_traj()")

    def wait(self) -> bool:
        print("[DEBUG] SimulatedTrajectory.wait()")
        return True

    def get_records(self, Ndata=0):
        return []


class AsynGcsTrajectory(TrajectoryBackend):
    """Reserved, deliberately not implemented.

    GCS can in principle be tunnelled through the IOC's asyn record
    (12idbHXP:asyn_1, bound to the same socket the motor driver uses), and
    pihexapod already does this when given a base PV instead of an IP - see
    the "epics" transport in make_trajectory_backend. Writing a corrected
    transport here instead would need four things that are not true today:

      1. .AOUT is DBF_STRING, 40 bytes. "WAV 1 X LIN ..." and
         "DRR? 1 N 1 2 ... 12" do not fit; .BOUT (DBF_CHAR) plus .NOWT is the
         only way to send them.
      2. .BINP is DBF_CHAR[256]. IMAX and OMAX are 256 and are declared
         special(SPC_NOMOD) in asynRecord.dbd, so they cannot be raised at
         runtime - only by editing the IOC's iocsh/hexapod.cmd and
         restarting.

         NOTE, measured 2026-10-19: the 256-byte cap is per *read*, not per
         reply, and with IFMT=Hybrid + IEOS="\n" each read returns one line.
         A twelve-column DRR? data line measures 124 bytes, so it fits - the
         earlier claim here that get_records() "cannot work through a
         256-byte buffer" was wrong. What actually rules the read loop out is
         cost and stability: one CA round trip per line (~2.6 ms/line best
         case, so minutes for a real scan), and a 3000-point DRR? drain
         attempted over CA took the IOC down. Raising IMAX/OMAX so the reply
         arrives in one transaction is the prerequisite, not line width.
      3. TMOD is a record field, not a per-transaction argument. Response-less
         GCS commands would each burn the 1 s TMOT unless TMOD is flipped to
         Write first, and that flip is two transactions with an unlocked gap
         between them.
      4. The port is shared with the driver's poller (100 ms while moving).
         Individual transactions are atomic under the asyn port lock, but a
         stray response in that gap would be read by the poller.

    Items 1-2 are IOC-side. Until they are fixed, implementing this would be
    guesswork validatable only on live hardware.
    """

    available = False
    state = "asyn-gcs"
    reason = "not implemented - see the class docstring"


def _read_gcs_reply(sock, timeout):
    """Read one complete GCS reply off an open socket.

    Every reply line ends with "\\n". In a multi-line reply, every line
    except the last carries a space immediately before that newline - the
    trailing space is the controller's "more to come" marker, and its
    absence is the only reliable end-of-reply signal. Reading until the
    socket goes quiet instead would work but costs a full timeout on every
    exchange, which is the mistake the asyn path made.
    """
    sock.settimeout(timeout)
    buf = b""
    while True:
        chunk = sock.recv(4096)
        if not chunk:  # peer closed
            break
        buf += chunk
        if buf.endswith(b"\n"):
            lines = buf.split(b"\n")
            # lines[-1] is "" because buf ends in the separator, so the
            # last complete line is lines[-2].
            if len(lines) >= 2 and not lines[-2].endswith(b" "):
                break
    return buf.decode("ascii", "replace")


def probe_direct_gcs(address, port=GCS_PORT, command="*IDN?", timeout=3.0):
    """Ask the C-887 who it is over a plain TCP socket.

    No EPICS and no pipython. GCS is line-oriented ASCII over TCP - the
    same bytes probe_asyn_gcs pushes through the IOC's asyn record - so a
    socket is the whole transport. pipython's GCSDevice loads PI's
    libpi_pi_gcs2.so even for a TCP connection, and that library's absence
    is what makes the "direct" transport report itself unavailable on
    Linux; none of it is needed to speak the protocol.

    Note this opens a *second* connection: the IOC holds one of its own to
    the same controller. A refusal here is itself informative - it means
    the controller will not accept the direct transport alongside the IOC.

    Returns (ok, report) - report is a multi-line string meant to be shown
    verbatim, whichever way it went.
    """
    lines = [f"address: {address}:{port}", f"command: {command}",
             "transport: raw TCP socket (no EPICS, no pipython)"]
    sock = None
    started = time.monotonic()
    try:
        sock = socket.create_connection((address, port), timeout=timeout)
        sock.sendall((command + "\n").encode("ascii"))
        reply = _read_gcs_reply(sock, timeout)
    except OSError as exc:
        lines.append(f"FAIL: {type(exc).__name__}: {exc}")
        return False, "\n".join(lines)
    finally:
        if sock is not None:
            sock.close()

    elapsed = time.monotonic() - started
    lines.append(f"bytes read: {len(reply)}")
    lines.append(f"elapsed: {elapsed * 1000:.0f} ms")
    lines.append(f"reply: {reply.strip()!r}")
    if not reply.strip():
        lines.append("FAIL: connected, but the controller sent nothing back.")
        return False, "\n".join(lines)
    lines.append("OK: GCS reaches the hexapod over a direct socket.")
    return True, "\n".join(lines)


def probe_asyn_gcs(ioc_prefix="12idbHXP", command="*IDN?", timeout=3.0):
    """Send one short GCS query through the IOC's asyn record, return the reply.

    The one question items 1-4 above cannot answer from the outside: does a
    GCS command written to 12idbHXP:asyn_1 actually reach the C-887 and come
    back? "*IDN?" is chosen because it is 5 characters - it fits .AOUT's 40
    bytes with room to spare, so a failure here is the transport, not the
    truncation of item 1.

    The reply is read from .BINP rather than .AINP: .AINP is DBF_STRING (40
    bytes) and the controller's identification string is longer than that, so
    an ASCII read would report a truncated success. This leaves the record in
    Binary input format, which is what pihexapod's own transport sets at
    connect() anyway.

    Returns (ok, report) - report is a multi-line string meant to be shown
    verbatim, whichever way it went.
    """
    try:
        from epics import PV, caget, caput
    except ImportError as exc:
        return False, f"pyepics is not importable: {exc}"

    base = f"{ioc_prefix}:asyn_1"
    lines = [f"record:  {base}", f"command: {command}"]

    cnct = PV(f"{base}.CNCT")
    if not cnct.wait_for_connection(timeout=timeout):
        lines.append("FAIL: the asyn record itself does not connect - "
                     "the IOC is down or the prefix is wrong.")
        return False, "\n".join(lines)
    if cnct.get() != 1:
        lines.append("FAIL: the record is there but .CNCT is 0 - the IOC is "
                     "up and the controller behind it is not.")
        return False, "\n".join(lines)

    # Hybrid input, not Binary. All three formats matter here:
    #   ASCII  - reply goes to .AINP, a 40-byte DBF_STRING. The C-887's
    #            identification string is 75 bytes, so this truncates and
    #            reports the truncation as a success.
    #   Binary - reply goes to the 256-byte .BINP, but binary means "raw
    #            bytes, no terminator interpretation", so IEOS is ignored.
    #            The read collects the reply and then burns the full .TMOT
    #            waiting for bytes that never come: every exchange takes a
    #            second and ends in a MAJOR alarm, success and failure alike.
    #   Hybrid  - .BINP *and* EOS processing. This is the one we want.
    # Measured on 12idbHXP: Binary 1.25 s and MAJOR, Hybrid 0.027 s and clean.
    #
    # Writes are put-completion because they must all land before .AOUT
    # processes; IEOS in particular is silently ineffective if it arrives late.
    caput(f"{base}.OFMT", "ASCII", wait=True, timeout=timeout)
    caput(f"{base}.IFMT", "Hybrid", wait=True, timeout=timeout)
    caput(f"{base}.OEOS", "", wait=True, timeout=timeout)
    caput(f"{base}.IEOS", "\\n", wait=True, timeout=timeout)
    caput(f"{base}.TMOD", "Write/Read", wait=True, timeout=timeout)

    caput(f"{base}.AOUT", command + "\\n", wait=True, timeout=timeout)

    # NORD, not NRRD: NRRD is how many bytes to ask for (0 = until EOS or
    # timeout), NORD is how many actually arrived.
    nord = int(caget(f"{base}.NORD") or 0)
    raw = caget(f"{base}.BINP")
    reply = ""
    if raw is not None and nord:
        reply = bytes(int(b) for b in raw[:nord]).decode(
            "ascii", "replace").strip()

    lines.append(f"bytes read: {nord}")
    lines.append(f"reply: {reply!r}")
    errs = caget(f"{base}.ERRS", as_string=True)
    if errs:
        lines.append(f"record error field: {errs}")
    if not reply:
        lines.append("FAIL: nothing came back. The record transacted but the "
                     "controller did not answer within its .TMOT.")
        return False, "\n".join(lines)
    lines.append("OK: GCS commands reach the hexapod over EPICS.")
    return True, "\n".join(lines)


def _describe_gaps(missing, wave_gen_id):
    parts = []
    if missing:
        parts.append("pihexapod.gcs.Hexapod is missing " + ", ".join(missing))
    if wave_gen_id is None:
        parts.append("pihexapod.gcs has no WaveGenID")
    return "; ".join(parts)


def _default_importer():
    return importlib.import_module("pihexapod.gcs")


def make_trajectory_backend(address="", ioc_prefix="", *, importer=None,
                            prefer="auto", log=print, epics_ready=None):
    """Resolve a trajectory backend, reporting why when there isn't one.

    prefer:
      "auto"      - direct GCS over TCP to `address`, then, if that fails,
                    pihexapod's own EPICS passthrough via `ioc_prefix`.
      "direct"    - TCP only.
      "epics"     - the asyn passthrough only.
      "simulated" - never touch the library.
      "none"      - never fly.

    epics_ready: callable returning (ok, reason), asked before the EPICS
    passthrough is accepted. Constructing pihexapod's EPICS transport
    succeeds as long as the asyn *record* exists, which says nothing about
    whether the controller behind it is reachable or whether replies will
    fit in the record's buffer - so without this check a dead controller
    reports a working fly transport and fails later, mid-scan.

    The library is imported here rather than at module scope so that an
    ImportError is a reportable state instead of a module that cannot load,
    and so tests can supply `importer` without installing anything.
    """
    if prefer == "simulated":
        return SimulatedTrajectory()
    if prefer == "none":
        return UnavailableTrajectory(STATE_ABSENT, "fly scans disabled by configuration")

    importer = importer or _default_importer
    try:
        gcs = importer()
    except Exception as exc:
        return _report(log, STATE_ABSENT, f"{type(exc).__name__}: {exc}")

    # Validate the surface before anyone depends on it. A stale install that
    # imports cleanly but lacks set_traj_SNAKE2 would otherwise fail deep
    # inside a scan, after the detector is armed.
    hexapod_cls = getattr(gcs, "Hexapod", None)
    if hexapod_cls is None:
        return _report(log, STATE_INCOMPATIBLE, "pihexapod.gcs has no Hexapod class")
    missing = [name for name in TRAJ_METHODS if not hasattr(hexapod_cls, name)]
    wave_gen_id = getattr(gcs, "WaveGenID", None)
    if missing or wave_gen_id is None:
        return _report(log, STATE_INCOMPATIBLE, _describe_gaps(missing, wave_gen_id))

    attempts = []
    if prefer in ("auto", "direct"):
        attempts.append(("direct", address or getattr(gcs, "IP", "")))
    if prefer in ("auto", "epics"):
        attempts.append(("epics", ioc_prefix))

    failures = []
    for transport, target in attempts:
        label = f"{target}:asyn_1" if transport == "epics" else target
        if not target:
            failures.append(f"{transport}: no address configured")
            continue
        if transport == "epics":
            ok, why = (True, "") if epics_ready is None else epics_ready()
            if not ok:
                failures.append(f"epics ({label}): {why}")
                continue
        try:
            dev = hexapod_cls(target)
        except Exception as exc:
            failures.append(f"{transport} ({label}): {type(exc).__name__}: {exc}")
            continue
        if transport == "epics":
            log("hexapod: falling back to the GCS-over-EPICS transport "
                f"({target}:asyn_1). This is experimental: the library's own "
                "transport writes commands through the 40-byte .AOUT field, "
                "which truncates a WAV or DRR? command, and its reads are "
                "capped by the record's IMAX.")
        else:
            log(f"hexapod: trajectory transport is {transport} ({target}).")
        return PiHexapodTrajectory(dev, wave_gen_id, transport=transport,
                                   address=target)

    return _report(log, STATE_UNREACHABLE, "; ".join(failures))


def _report(log, state, reason):
    log(f"hexapod: trajectory transport unavailable ({state}) - {reason}. "
        f"Fly scans are disabled; step scans are unaffected.")
    return UnavailableTrajectory(state, reason)
