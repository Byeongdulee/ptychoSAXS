"""Scan trajectory files: a scan written down before it runs.

A scan used to exist only for the instant between pressing a button and the
executor reading the widgets. Splitting "decide what to scan" from "scan it"
needs the decision to live somewhere in between, and this is that somewhere:
one csv per scan, written when the trajectory is created and read back when
it is launched.

The file has two parts:

  * a header -- one row of column names and one row of values -- holding
    everything the log csv's row for this scan will hold, less the
    upstream-optics readbacks and the detector list. `completed`, `date`,
    `time`, `optics_name` and `optics_type` start empty (or "no") and are
    filled in when the scan runs;
  * after a blank row, a table with one column per scanned motor holding
    the absolute target of every point, in the order the scan visits them:
    "X (mm)", "Z (mm)".

The file is meant to be hand-editable, and the table is the only statement
of where the scan goes -- there are deliberately no relative columns to
disagree with it. read_trajectory accepts any file it can parse and keeps
the points exactly as listed. The header's scan_center is where the motors
are returned to afterwards (the first listed point if it names no centre for
an axis); the other per-axis header fields are a record, not instructions.

  * A step scan runs the listed points as they are, in file order -- any
    shape, any count.
  * A fly scan is driven as a hexapod trajectory (start, end, velocity), so
    its points must form the regular grid scan_type describes, in scan
    order. check_runnable refuses one that does not, before anything moves.

Pure: no Qt, no hardware, no session. Geometry comes from scanplan so the
points written here are the points the executors compute.
"""

import csv
import re
from dataclasses import dataclass, field

import numpy as np

from ptychosaxs.api import optics_status as _optics_status
from ptychosaxs.api import scanplan

TAKESHOT = "takeshot"
STEP = "step"
FLY = "fly"
KINDS = (TAKESHOT, STEP, FLY)

#: Motors whose positions go in the log's "motor positions" column.
LOGGED_MOTORS = ("X", "Y", "Z", "trans1", "trans2")

#: Columns of the log csv that a trajectory file leaves out: the optics are
#: read at scan time, and which detectors are armed is not decided here.
_NOT_IN_TRAJECTORY = ("detectors_used", *_optics_status.US_OPTICS_MOTORS)

_SCAN_TYPE_RE = re.compile(r"^([123])D (step|fly)scan$")
_POSITION_COLUMN_RE = re.compile(r"^(?P<name>.+) \((?P<unit>[^)]*)\)$")

# Matches the precision of the motor labels on the main panel.
_POSITION_FORMAT = "%.6f"
# Loose enough to survive a %.6f round trip, tight enough to catch an edit.
_POSITION_ATOL = 2e-6


class TrajectoryError(ValueError):
    """A trajectory file that cannot be run, with the reason."""


def log_header():
    """Column names of the per-scan log csv, in order.

    The single definition: ScanExecutorsMixin._get_scan_summary_header
    returns this, and trajectory_header is derived from it, so the two
    files cannot drift apart.
    """
    return [
        "scan",
        "completed",
        "date",
        "time",
        "sample_name",
        "scan_type",
        "ExpTime",
        "n_pos",
        "monoE",
        "phi",
        "detectors_used",
        "motor positions",
        "scan_center",
        "scan_negative_edge",
        "scan_positive_edge",
        "scan_step_size",
        *_optics_status.US_OPTICS_MOTORS,
        "trans1",
        "trans2",
        "optics_name",
        "optics_type",
    ]


def trajectory_header():
    """Header columns of a trajectory file: the log's, less optics/detectors."""
    return [c for c in log_header() if c not in _NOT_IN_TRAJECTORY]


def format_axis_values(axes_params, key):
    """One of the log's per-axis columns, e.g. "X 0.100000 Z -0.020000 ".

    axes_params is a list of dicts with "name" and the field `key` names
    ("p0", "st", "fe" or "step"). The trailing space is how the log has
    always written these, so existing logs keep parsing the same way.
    """
    parts = []
    for ax in axes_params:
        val = ax.get(key, "")
        try:
            formatted = f"{float(val):f}"
        except (TypeError, ValueError):
            formatted = ""
        parts.append(f"{ax['name']} {formatted} ")
    return "".join(parts)


def format_motor_positions(positions):
    """The "motor positions" column from {name: value}, in LOGGED_MOTORS order.

    A motor that is absent or unreadable is left out rather than written as
    a fake number.
    """
    params = []
    for name in LOGGED_MOTORS:
        value = positions.get(name)
        if value is None or value == "":
            continue
        params.append({"name": name, "p0": value})
    return format_axis_values(params, "p0")


def scan_type_label(dim, kind):
    """"takeshot", "1D stepscan", "2D flyscan", ..."""
    if kind == TAKESHOT or dim == 0:
        return TAKESHOT
    if kind not in (STEP, FLY):
        raise TrajectoryError(f"Unknown scan kind {kind!r}.")
    return f"{int(dim)}D {kind}scan"


def parse_scan_type(label):
    """(dim, kind) back out of scan_type_label's text."""
    label = (label or "").strip()
    if label == TAKESHOT:
        return 0, TAKESHOT
    match = _SCAN_TYPE_RE.match(label)
    if not match:
        raise TrajectoryError(f"Unrecognised scan_type {label!r}.")
    return int(match.group(1)), match.group(2)


@dataclass
class Axis:
    """One scanned motor: the same p0/st/fe/step the executors take.

    p0 is the absolute centre the relative offsets hang off -- the motor's
    position when the trajectory was created.
    """

    name: str
    unit: str
    p0: float
    st: float
    fe: float
    step: float

    def positions(self):
        return scanplan.make_positions(self.p0, self.st, self.fe, self.step)

    def as_params(self, expt=None):
        """The dict shape _read_motor_params returns, minus motor_index."""
        params = {"name": self.name, "p0": self.p0, "st": self.st,
                  "fe": self.fe, "step": self.step}
        if expt is not None:
            params["expt"] = expt
        return params


@dataclass
class ScanTrajectory:
    """A scan, fully specified, independent of any widget.

    `expt` is in seconds. `header` holds the header row's values, keyed by
    column name; trajectory_header() lists the keys a written file carries.
    Axes are ordered fast to slow: X then Z for a 2-D scan, then phi.
    """

    sample_name: str
    scan_number: int
    kind: str
    expt: float
    axes: list = field(default_factory=list)
    header: dict = field(default_factory=dict)
    # Set by read_trajectory: the absolute points as the file lists them
    # (None for a trajectory built in memory), and why they are not the
    # regular grid a fly scan needs ("" if they are).
    listed: object = None
    grid_error: str = ""

    @property
    def dim(self):
        return 0 if self.kind == TAKESHOT else len(self.axes)

    @property
    def scan_type(self):
        return scan_type_label(self.dim, self.kind)

    @property
    def scan_id(self):
        return "S%04d" % int(self.scan_number)

    def numbers_used(self):
        """Scan numbers this scan consumes, starting at scan_number.

        A 3-D scan files every phi slice as a scan of its own (the 2-D
        executors advance the number at the end of each slice), so it takes
        one number per phi position; everything else takes one.
        """
        if self.dim == 3:
            return max(self.axis_counts()[2], 1)
        return 1

    def axis_positions(self):
        """Per-axis absolute positions, fast axis first."""
        return [ax.positions() for ax in self.axes]

    def axis_counts(self):
        """Distinct positions per axis -- of the listed points, if any."""
        if self.listed is not None:
            return [len(_unique_in_order(self.listed[:, i]))
                    for i in range(self.listed.shape[1])]
        return [len(p) for p in self.axis_positions()]

    def n_positions(self):
        """Points to visit: as listed, else the grid product. A takeshot is
        one frame."""
        if self.kind == TAKESHOT:
            return 1
        if self.listed is not None:
            return len(self.listed)
        return int(np.prod(self.axis_counts())) if self.axes else 0

    def points(self):
        """The absolute targets to show: as listed, else as generated."""
        return self.listed if self.listed is not None else self.grid()

    def grid(self):
        """Absolute targets as an (N, dim) array, in traversal order.

        2-D is boustrophedon (Z slow), matching the fly snake path and the
        scan-positions plot; 3-D repeats that XY path at every phi.
        """
        arrays = self.axis_positions()
        if not arrays:
            return np.empty((0, 0))
        if len(arrays) == 1:
            return arrays[0].reshape(-1, 1)
        xy = scanplan.snake_positions(arrays[0], arrays[1])
        if len(arrays) == 2:
            return xy
        slices = [np.column_stack([xy, np.full(len(xy), phi)]) for phi in arrays[2]]
        return np.vstack(slices)

    def validate_header(self):
        """Raise TrajectoryError if the scan-level fields are unusable."""
        if not str(self.sample_name).strip():
            raise TrajectoryError("The sample name is empty.")
        if self.kind not in KINDS:
            raise TrajectoryError(f"Unknown scan kind {self.kind!r}.")
        if not self.expt or self.expt <= 0:
            raise TrajectoryError("The exposure time must be greater than 0.")

    def validate(self):
        """Raise TrajectoryError unless this is something the engine can run."""
        self.validate_header()
        if self.kind == TAKESHOT:
            if self.axes:
                raise TrajectoryError("A takeshot moves no motors.")
            return
        if not 1 <= len(self.axes) <= 3:
            raise TrajectoryError("A scan needs one to three axes.")
        names = [ax.name for ax in self.axes]
        if len(set(names)) != len(names):
            raise TrajectoryError("A motor appears twice: %s." % ", ".join(names))
        for ax in self.axes:
            if ax.step == 0 and ax.st != ax.fe:
                raise TrajectoryError(f"{ax.name}: the step size is 0.")
            if len(ax.positions()) == 0:
                raise TrajectoryError(f"{ax.name}: no positions between from and to.")


# -- reading and writing ------------------------------------------------


def position_columns(traj):
    """Column names of the position table, e.g. ["X (mm)", "Z (mm)"]."""
    return [f"{ax.name} ({ax.unit})" for ax in traj.axes]


def header_values(traj, extra=None):
    """The header row for `traj`, filling what the trajectory itself knows.

    `extra` supplies the readbacks taken when the trajectory was created
    (monoE, phi, motor positions, trans1, trans2) and anything else the
    caller wants to override. Run-time fields start blank.
    """
    params = [ax.as_params() for ax in traj.axes]
    values = {c: "" for c in trajectory_header()}
    values.update({
        "scan": traj.scan_id,
        "completed": "no",
        "sample_name": traj.sample_name,
        "scan_type": traj.scan_type,
        "ExpTime": repr(float(traj.expt)),
        "n_pos": str(traj.n_positions()),
        "scan_center": format_axis_values(params, "p0"),
        "scan_negative_edge": format_axis_values(params, "st"),
        "scan_positive_edge": format_axis_values(params, "fe"),
        "scan_step_size": format_axis_values(params, "step"),
    })
    for key, value in (traj.header or {}).items():
        if key in values:
            values[key] = value
    for key, value in (extra or {}).items():
        if key in values:
            values[key] = "" if value is None else str(value)
    return values


def write_trajectory(path, traj, extra=None):
    """Write `traj` to `path` and return the header values written.

    Validates first, so nothing unrunnable reaches the disk.
    """
    traj.validate()
    values = header_values(traj, extra)
    columns = trajectory_header()
    with open(path, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(columns)
        writer.writerow([values[c] for c in columns])
        writer.writerow([])
        if traj.kind != TAKESHOT:
            writer.writerow(position_columns(traj))
            for point in traj.grid():
                writer.writerow([_POSITION_FORMAT % v for v in point])
    traj.header = dict(values)
    return values


def _unique_in_order(values):
    """Distinct values in first-seen order, merging %.6f round-off."""
    out = []
    for v in values:
        if not any(abs(v - u) <= _POSITION_ATOL for u in out):
            out.append(float(v))
    return np.array(out)


def _parse_axis_values(text):
    """{"X": 0.1, "Z": -0.02} from a header cell like "X 0.100000 Z -0.020000 "."""
    tokens = (text or "").split()
    out = {}
    for name, value in zip(tokens[0::2], tokens[1::2]):
        try:
            out[name] = float(value)
        except ValueError:
            pass
    return out


def _axis_from_column(name, unit, values, centre=None):
    """Rebuild one Axis from its listed positions.

    `centre` (from the header's scan_center) is only where the motor goes
    back to afterwards; it never moves a listed point. Without one, the
    first listed position stands in.
    """
    u = _unique_in_order(values)
    p0 = float(u[0]) if centre is None else float(centre)
    step = float(round(u[1] - u[0], 9)) if len(u) > 1 else 0.0
    return Axis(name=name, unit=unit, p0=p0, st=float(u[0] - p0),
                fe=float(u[-1] - p0), step=step)


def read_trajectory(path):
    """Load a trajectory file. Raises TrajectoryError only if it cannot be
    parsed (or its header is unusable).

    The points are kept as listed. The axes are rebuilt from them, then
    regenerated and compared point by point; if they are not that regular
    grid, the reason goes in grid_error -- which only matters to a fly scan
    (see check_runnable).
    """
    try:
        with open(path, newline="") as fh:
            rows = list(csv.reader(fh))
    except OSError as exc:
        raise TrajectoryError(f"Could not read {path}: {exc}") from exc
    except (csv.Error, UnicodeDecodeError) as exc:
        raise TrajectoryError(f"{path} is not a readable csv: {exc}") from exc

    if len(rows) < 2 or not rows[0]:
        raise TrajectoryError("No header: this is not a scan trajectory file.")
    columns, raw_values = rows[0], rows[1]
    missing = [c for c in ("scan", "sample_name", "scan_type", "ExpTime")
               if c not in columns]
    if missing:
        raise TrajectoryError("Not a scan trajectory file (missing %s)."
                              % ", ".join(missing))
    header = {c: (raw_values[i] if i < len(raw_values) else "")
              for i, c in enumerate(columns)}

    dim, kind = parse_scan_type(header["scan_type"])
    match = re.fullmatch(r"S(\d+)", header["scan"].strip())
    if not match:
        raise TrajectoryError(f"Unrecognised scan id {header['scan']!r}.")
    try:
        expt = float(header["ExpTime"])
    except ValueError:
        raise TrajectoryError(f"Unreadable ExpTime {header['ExpTime']!r}.") from None

    traj = ScanTrajectory(sample_name=header["sample_name"],
                          scan_number=int(match.group(1)), kind=kind,
                          expt=expt, header=header)
    traj.validate_header()
    if kind == TAKESHOT:
        return traj

    table = [r for r in rows[2:] if any(cell.strip() for cell in r)]
    if len(table) < 2:
        raise TrajectoryError("The position table is missing or empty.")
    pos_columns, data = table[0], table[1:]
    parsed = [_POSITION_COLUMN_RE.match(c.strip()) for c in pos_columns]
    if not all(parsed):
        raise TrajectoryError("Unrecognised position columns: %s." % ", ".join(pos_columns))
    try:
        values = np.array([[float(c) for c in r] for r in data], dtype=float)
    except ValueError as exc:
        raise TrajectoryError(f"Non-numeric position: {exc}") from None
    if values.ndim != 2 or values.shape[1] != len(pos_columns):
        raise TrajectoryError("Ragged position table.")

    names = [m.group("name").strip() for m in parsed]
    if len(set(names)) != len(names):
        raise TrajectoryError("A motor has more than one column: %s." % ", ".join(names))
    if len(names) != dim:
        raise TrajectoryError(
            f"scan_type says {dim}D but the table has {len(names)} motor column(s).")

    centres = _parse_axis_values(header.get("scan_center", ""))
    for i, (name, m) in enumerate(zip(names, parsed)):
        traj.axes.append(_axis_from_column(name, m.group("unit").strip(),
                                           values[:, i], centres.get(name)))
    traj.listed = values

    try:
        traj.validate()
        expected = traj.grid()
        if expected.shape != traj.listed.shape or not np.allclose(
                expected, traj.listed, atol=_POSITION_ATOL):
            traj.grid_error = "the points are not a regular %dD grid in scan order" % dim
    except TrajectoryError as exc:
        traj.grid_error = str(exc)
    return traj


def check_runnable(traj):
    """Raise TrajectoryError if the scan engine cannot run `traj`.

    Step scans run whatever points are listed. Fly scans are hexapod
    trajectories and need the regular grid scan_type describes.
    """
    traj.validate_header()
    if traj.kind == TAKESHOT:
        return
    if traj.n_positions() == 0:
        raise TrajectoryError("There are no positions to visit.")
    if traj.kind != FLY:
        return
    if traj.listed is None:
        traj.validate()
    elif traj.grid_error:
        raise TrajectoryError(
            "A fly scan needs a regular grid (%s). Restore the grid, or change "
            "scan_type to a stepscan to visit these points as listed." % traj.grid_error)


def update_header(path, **updates):
    """Rewrite named header cells in place; the position table is untouched.

    Used to stamp date/time/optics when a scan starts and `completed` when
    it ends. Unknown column names are ignored rather than appended, so this
    can never reshape a file.
    """
    with open(path, newline="") as fh:
        rows = list(csv.reader(fh))
    if len(rows) < 2:
        raise TrajectoryError(f"{path} has no header to update.")
    columns, values = rows[0], rows[1]
    values = values + [""] * (len(columns) - len(values))
    for key, value in updates.items():
        if key in columns:
            values[columns.index(key)] = "" if value is None else str(value)
    rows[1] = values
    with open(path, "w", newline="") as fh:
        csv.writer(fh).writerows(rows)


# -- estimates ------------------------------------------------------------


@dataclass
class Estimate:
    """What the feedback panels show for one trajectory."""

    counts: list        # per-axis point counts, fast axis first
    n_total: int        # nominal points
    step_seconds: float
    fly_seconds: float


def estimate(traj, overhead_step, fly_period, fly_1d_kind=scanplan.FLY_HEXAPOD_1D,
             fly_slice_overhead=0.0):
    """Point counts and step/fly durations, the way the large panel estimates.

    fly_period is the per-trigger time (see ScanHandler.update_scan_estimate:
    max(fly_acq_time, expt + readout, minimum)). fly_1d_kind is the
    scanplan kind a 1-D fly over this axis runs as -- the caller knows
    whether it is a hexapod axis. A 3-D fly pays fly_slice_overhead per
    phi slice on top of the snake XY count.
    """
    if traj.kind == TAKESHOT:
        return Estimate([], 1, traj.expt, traj.expt)
    counts = traj.axis_counts()
    n_total = traj.n_positions()
    step_seconds = n_total * (traj.expt + overhead_step)
    if traj.grid_error:
        # Not a grid, so no snake/phantom-point rule applies; a flat count
        # is the honest estimate.
        return Estimate(counts, n_total, step_seconds, n_total * fly_period)
    arrays = traj.axis_positions()
    if len(arrays) == 1:
        n_fly = scanplan.count_positions(arrays, fly_1d_kind)
        fly_seconds = n_fly * fly_period
    else:
        n_fly = scanplan.count_positions(arrays[:2], scanplan.FLY_SNAKE)
        n_slices = counts[2] if len(counts) > 2 else 1
        fly_seconds = n_fly * n_slices * fly_period
        if len(counts) > 2:
            fly_seconds += n_slices * fly_slice_overhead
    return Estimate(counts, n_total, step_seconds, fly_seconds)
