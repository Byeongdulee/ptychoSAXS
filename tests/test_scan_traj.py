"""Tests for ptychosaxs/api/scan_traj.py: the scan trajectory csv."""

import csv

import numpy as np
import pytest

from ptychosaxs.api import optics_status, scanplan
from ptychosaxs.api import scan_traj as st


def _traj(kind="fly", axes=None, expt=0.1):
    if axes is None:
        axes = [st.Axis("X", "mm", 1.0, -0.01, 0.01, 0.005),
                st.Axis("Z", "mm", 2.0, -0.01, 0.01, 0.005)]
    return st.ScanTrajectory(sample_name="mysample", scan_number=12, kind=kind,
                             expt=expt, axes=axes)


def test_trajectory_header_is_the_log_header_less_optics_and_detectors():
    header = st.trajectory_header()
    assert "detectors_used" not in header
    for motor in optics_status.US_OPTICS_MOTORS:
        assert motor not in header
    assert header[:2] == ["scan", "completed"]
    assert {"optics_name", "optics_type", "motor positions", "trans1"} <= set(header)


def test_log_header_puts_motor_positions_between_detectors_and_scan_params():
    header = st.log_header()
    assert header[:2] == ["scan", "completed"]
    i = header.index("motor positions")
    assert header[i - 1] == "detectors_used"
    assert header[i + 1] == "scan_center"
    assert header[-2:] == ["optics_name", "optics_type"]


def test_scan_type_round_trips():
    for dim, kind in ((0, "takeshot"), (1, "step"), (2, "fly"), (3, "step")):
        assert st.parse_scan_type(st.scan_type_label(dim, kind)) == (dim, kind)
    assert st.scan_type_label(2, "fly") == "2D flyscan"
    with pytest.raises(st.TrajectoryError):
        st.parse_scan_type("4D wobblescan")


def test_2d_grid_is_snake_ordered_and_matches_scanplan():
    traj = _traj()
    x = scanplan.make_positions(1.0, -0.01, 0.01, 0.005)
    z = scanplan.make_positions(2.0, -0.01, 0.01, 0.005)
    np.testing.assert_allclose(traj.grid(), scanplan.snake_positions(x, z))
    assert traj.n_positions() == 25


def test_3d_grid_repeats_the_xy_snake_at_every_phi():
    traj = _traj(axes=[st.Axis("X", "mm", 0, 0, 0.002, 0.001),
                       st.Axis("Z", "mm", 0, 0, 0.001, 0.001),
                       st.Axis("phi", "deg", 10, 0, 2, 1)])
    grid = traj.grid()
    assert grid.shape == (3 * 2 * 3, 3)
    np.testing.assert_allclose(grid[:6, :2], grid[6:12, :2])
    np.testing.assert_allclose(np.unique(grid[:, 2]), [10, 11, 12])


@pytest.mark.parametrize("axes", [
    [st.Axis("trans1", "mm", 0.5, -0.003, 0.003, 0.001)],
    [st.Axis("X", "mm", 1.0, 0.01, -0.01, 0.005),          # reversed
     st.Axis("Z", "mm", -2.0, -0.01, 0.01, 0.004)],         # non-dividing step
    [st.Axis("X", "mm", 0.0, 0, 0.002, 0.001),
     st.Axis("Z", "mm", 0.0, 0, 0.002, 0.001),
     st.Axis("phi", "deg", 5.0, -1, 1, 0.5)],
])
def test_write_then_read_reproduces_the_same_points(tmp_path, axes):
    traj = _traj(kind="step", axes=axes)
    path = tmp_path / "t.csv"
    st.write_trajectory(path, traj, extra={"monoE": "8.8", "trans1": "0.5"})
    back = st.read_trajectory(path)
    assert back.scan_number == 12
    assert back.sample_name == "mysample"
    assert back.kind == "step"
    assert back.expt == pytest.approx(0.1)
    assert [a.name for a in back.axes] == [a.name for a in axes]
    np.testing.assert_allclose(back.grid(), traj.grid(), atol=1e-6)
    assert back.header["completed"] == "no"
    assert back.header["date"] == ""
    assert back.header["optics_name"] == ""
    assert back.header["monoE"] == "8.8"


def test_written_file_lists_absolute_positions_only(tmp_path):
    path = tmp_path / "t.csv"
    st.write_trajectory(path, _traj())
    rows = list(csv.reader(open(path)))
    assert rows[2] == []
    assert rows[3] == ["X (mm)", "Z (mm)"]
    first = [float(v) for v in rows[4]]
    assert first == pytest.approx([0.99, 1.99])


def test_takeshot_round_trips_without_a_table(tmp_path):
    path = tmp_path / "t.csv"
    st.write_trajectory(path, _traj(kind="takeshot", axes=[]))
    back = st.read_trajectory(path)
    assert back.kind == "takeshot" and back.dim == 0 and back.n_positions() == 1


def _rewrite(path, edit):
    rows = list(csv.reader(open(path)))
    rows = edit(rows)
    with open(path, "w", newline="") as fh:
        csv.writer(fh).writerows(rows)


@pytest.mark.parametrize("edit", [
    lambda rows: [*rows[:6], [rows[6][0][:-1] + "9", rows[6][1]], *rows[7:]],  # nudge one point
    lambda rows: rows[:-3],                                                  # drop the tail
    lambda rows: [*rows, ["5.0", "5.0"]],                                    # add a point
])
def test_a_hand_edited_step_scan_runs_its_points_as_listed(tmp_path, edit):
    path = tmp_path / "t.csv"
    st.write_trajectory(path, _traj(kind="step"))
    _rewrite(path, edit)
    back = st.read_trajectory(path)
    st.check_runnable(back)                     # step: any points
    listed = np.array([[float(c) for c in r] for r in list(csv.reader(open(path)))[4:] if r])
    np.testing.assert_allclose(back.points(), listed)
    assert back.n_positions() == len(listed)


def test_a_hand_edited_fly_scan_off_the_grid_loads_but_will_not_run(tmp_path):
    path = tmp_path / "t.csv"
    st.write_trajectory(path, _traj(kind="fly"))
    _rewrite(path, lambda rows: rows[:-3])
    back = st.read_trajectory(path)             # loads: it parses
    assert back.grid_error
    with pytest.raises(st.TrajectoryError, match="regular grid"):
        st.check_runnable(back)


def test_a_fly_grid_rewritten_by_hand_runs_as_the_new_grid(tmp_path):
    """Replacing the table with a different regular grid is a valid edit."""
    path = tmp_path / "t.csv"
    st.write_trajectory(path, _traj(kind="fly"))
    other = _traj(kind="fly", axes=[st.Axis("X", "mm", 3.0, 0, 0.004, 0.002),
                                    st.Axis("Z", "mm", 4.0, 0, 0.002, 0.002)])
    table = [["%.6f" % v for v in p] for p in other.grid()]
    _rewrite(path, lambda rows: rows[:4] + table)
    back = st.read_trajectory(path)
    st.check_runnable(back)
    np.testing.assert_allclose(back.grid(), other.grid(), atol=1e-6)


def test_scan_center_is_the_return_position_not_a_point_shift(tmp_path):
    path = tmp_path / "t.csv"
    st.write_trajectory(path, _traj(kind="step"))
    st.update_header(path, scan_center="X 7.000000 Z 8.000000 ")
    back = st.read_trajectory(path)
    assert [ax.p0 for ax in back.axes] == [7.0, 8.0]
    np.testing.assert_allclose(back.points(), _traj().grid(), atol=1e-6)


def test_random_csv_is_refused(tmp_path):
    path = tmp_path / "t.csv"
    path.write_text("a,b,c\n1,2,3\n")
    with pytest.raises(st.TrajectoryError):
        st.read_trajectory(path)


def test_invalid_trajectories_are_not_written(tmp_path):
    with pytest.raises(st.TrajectoryError):
        st.write_trajectory(tmp_path / "a.csv", _traj(expt=0))
    traj = _traj()
    traj.sample_name = " "
    with pytest.raises(st.TrajectoryError):
        st.write_trajectory(tmp_path / "b.csv", traj)
    assert not list(tmp_path.iterdir())


def test_update_header_changes_only_named_cells(tmp_path):
    path = tmp_path / "t.csv"
    st.write_trajectory(path, _traj())
    before = list(csv.reader(open(path)))
    st.update_header(path, completed="yes", date="2026-11-03", bogus="x")
    after = list(csv.reader(open(path)))
    assert after[2:] == before[2:]
    back = st.read_trajectory(path)
    assert back.header["completed"] == "yes"
    assert back.header["date"] == "2026-11-03"
    assert "bogus" not in after[0]


def test_motor_positions_column_skips_missing_motors():
    text = st.format_motor_positions({"X": 1.5, "Z": -2, "trans2": None})
    assert text == "X 1.500000 Z -2.000000 "


def test_estimate_matches_the_large_panel_formula():
    traj = _traj()
    est = st.estimate(traj, overhead_step=0.5, fly_period=0.12)
    assert est.counts == [5, 5]
    assert est.n_total == 25
    assert est.step_seconds == pytest.approx(25 * 0.6)
    n_fly = scanplan.count_positions(traj.axis_positions(), scanplan.FLY_SNAKE)
    assert est.fly_seconds == pytest.approx(n_fly * 0.12)


def test_old_log_csv_migrates_by_column_name(tmp_path):
    """"motor positions" is inserted mid-header, so a positional append
    would shift every later value under the wrong name."""
    from ptychosaxs.api.scan_exec import ScanExecutorsMixin

    old_header = [c for c in st.log_header() if c not in ("motor positions", "optics_type")]
    row = {c: f"v_{c}" for c in old_header}
    path = tmp_path / "log.csv"
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(old_header)
        w.writerow([row[c] for c in old_header])

    ScanExecutorsMixin._migrate_scan_summary_header(ScanExecutorsMixin(), path)

    header, values = list(csv.reader(open(path)))
    assert header == st.log_header()
    got = dict(zip(header, values))
    assert got["motor positions"] == "" and got["optics_type"] == ""
    for c in old_header:
        assert got[c] == f"v_{c}"


def test_3d_reserves_one_scan_number_per_phi_slice():
    traj = _traj(axes=[st.Axis("X", "mm", 0, 0, 0.002, 0.001),
                       st.Axis("Z", "mm", 0, 0, 0.001, 0.001),
                       st.Axis("phi", "deg", 0, 0, 4, 1)])
    assert traj.numbers_used() == 5
    assert _traj().numbers_used() == 1


def test_executors_take_the_listed_points_for_their_axes():
    from ptychosaxs.api.scan_exec import ScanExecutorsMixin

    m = ScanExecutorsMixin()
    pts = np.array([[0.0, 1.0, 10.0], [0.5, 1.0, 10.0], [0.2, 2.0, 20.0]])
    m._plan = {"motor_indices": [0, 2, 6], "points": pts}
    np.testing.assert_allclose(m._planned_points([0, 2], phi=20.0), [[0.2, 2.0]])
    np.testing.assert_allclose(m._planned_points([0, 2]), pts[:2, :2])   # first slice
    np.testing.assert_allclose(m._planned_slices([0, 2, 6]), [10.0, 20.0])
    assert m._planned_points([1]) is None                                # other axes
    m._plan = {"motor_indices": [0, 2], "points": None}                  # fly: grid
    assert m._planned_points([0, 2]) is None


def test_running_an_off_grid_step_scan_rewrites_n_pos_and_marks_it_custom(tmp_path):
    from gui.handlers.traj_handler import TrajHandler

    path = tmp_path / "t.csv"
    st.write_trajectory(path, _traj(kind="step"))
    _rewrite(path, lambda rows: [*rows, ["5.0", "5.0"], ["6.0", "6.0"]])
    traj = st.read_trajectory(path)
    assert TrajHandler._sync_step_metadata(None, path, traj) is True
    back = st.read_trajectory(path)
    assert back.header["n_pos"] == "27"
    assert back.header["scan_step_size"] == "custom"


def test_running_an_on_grid_step_scan_keeps_its_step_size(tmp_path):
    from gui.handlers.traj_handler import TrajHandler

    path = tmp_path / "t.csv"
    st.write_trajectory(path, _traj(kind="step"))
    traj = st.read_trajectory(path)
    assert TrajHandler._sync_step_metadata(None, path, traj) is False
    assert st.read_trajectory(path).header["scan_step_size"] == "X 0.005000 Z 0.005000 "


class _FlickeringPilatus:
    """Armed drops for a moment mid-acquisition, as during readout."""
    _prefix = "S12-PILATUS1:cam1:"

    def __init__(self, acquiring):
        self.Acquire_RBV = 1 if acquiring else 0
        self._armed_reads = iter([0, 0, 1])
        self.arm_calls = 0

    @property
    def Armed(self):
        return next(self._armed_reads, 1)

    def Arm(self):
        self.arm_calls += 1


@pytest.mark.parametrize("acquiring,expected_arms", [(True, 0), (False, 2)])
def test_a_running_acquisition_is_not_re_armed(acquiring, expected_arms, monkeypatch):
    from types import SimpleNamespace
    from ptychosaxs.api import scan_exec
    from ptychosaxs.api.scan_exec import ScanExecutorsMixin

    monkeypatch.setattr(scan_exec.time, "sleep", lambda s: None)
    det = _FlickeringPilatus(acquiring)
    m = ScanExecutorsMixin()
    m.ctx = SimpleNamespace(detector=[det], parameters=SimpleNamespace(_pulses_per_step=1))
    timed_out, _ = m.is_arming_detecotors_timedout()
    assert not timed_out
    assert det.arm_calls == expected_arms
