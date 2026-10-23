"""Scan geometry, pinned to what the GUI produced before it moved.

Every expected value here was captured by running the original
ScanHandler methods, in place, before a line of them was lifted into
ptychosaxs/api/scanplan.py. So these are not a description of what the
functions ought to do -- they are a record of what the instrument has
actually been scanning with, which is the only safe thing to preserve when
moving code nothing previously tested.

Several look wrong and are not. Each such case carries its reason, because
the next person to read them will otherwise "fix" one.

The other half of the job: ScanHandler still exposes the old private
methods, and the last test here asserts they give the same answers, so the
7,000-line file can keep calling them while it is migrated.
"""

import numpy as np
import pytest

from ptychosaxs.api import scanplan


# -- signed_step -------------------------------------------------------------

@pytest.mark.parametrize("start,end,step,expected", [
    (0, 1, 0.25, 0.25),
    (1, 0, 0.25, -0.25),      # direction wins over the sign passed in
    (0, 1, -0.25, 0.25),      # ... in both directions
    (0, 0, 0.1, 0.1),
    (0, 0, 0, 1.0),           # nowhere to go and no step: 1.0, not a div-by-zero
    (-1, 1, 0.5, 0.5),
    (2, -2, 0, -4),           # a zero step becomes the whole span
])
def test_signed_step(start, end, step, expected):
    assert scanplan.signed_step(start, end, step) == pytest.approx(expected)


# -- make_positions ----------------------------------------------------------

@pytest.mark.parametrize("p0,st,fe,step,expected", [
    (0, -1, 1, 0.5, [-1.0, -0.5, 0.0, 0.5, 1.0]),
    (10, -0.05, 0.05, 0.01,
     [9.95, 9.96, 9.97, 9.98, 9.99, 10.0, 10.01, 10.02, 10.03, 10.04, 10.05]),
    (0, 0, 1, 0, [0.0, 1.0]),               # zero step still yields two points
    (0, 1, -1, 0.25,
     [1.0, 0.75, 0.5, 0.25, 0.0, -0.25, -0.5, -0.75, -1.0]),
    (5, 0, 0, 0.1, [5.0]),                  # zero span is one point, not none
])
def test_make_positions(p0, st, fe, step, expected):
    got = scanplan.make_positions(p0, st, fe, step)
    assert got == pytest.approx(expected)


def test_the_endpoint_survives_floating_point_when_the_step_divides_the_span():
    """The half-step in the arange bound is what guarantees this. Without
    it, floating point drops the last point for roughly half of these."""
    for step in (0.01, 0.02, 0.05, 0.1):
        got = scanplan.make_positions(0, 0, 1, step)
        assert got[-1] == pytest.approx(1.0), f"step {step} lost its endpoint"


def test_a_step_that_does_not_divide_the_span_stops_short():
    """Not a rounding bug: 0.3 does not divide 1.0, so the scan ends at 0.9
    and never reaches the requested end. Recorded because it looks like one
    and is the behaviour the instrument has always had."""
    got = scanplan.make_positions(0, 0, 1, 0.3)
    assert got.tolist() == pytest.approx([0.0, 0.3, 0.6, 0.9])


# -- snake_positions ---------------------------------------------------------

def test_snake_alternates_direction_every_row():
    got = scanplan.snake_positions([0, 1, 2], [10, 20])
    assert got.tolist() == [[0, 10], [1, 10], [2, 10],
                            [2, 20], [1, 20], [0, 20]]


# -- adjust_axis_length ------------------------------------------------------

@pytest.mark.parametrize("n,axis,kind,expected", [
    # step: untouched
    (0, 0, "step", 0), (1, 0, "step", 1), (5, 0, "step", 5), (5, 1, "step", 5),
    # fly_snake fast axis: one phantom trigger per line
    (0, 0, "fly_snake", 1), (1, 0, "fly_snake", 2),
    (3, 0, "fly_snake", 4), (4, 0, "fly_snake", 5), (5, 0, "fly_snake", 6),
    # fly_snake slow axis: rounded up to even
    (0, 1, "fly_snake", 0), (1, 1, "fly_snake", 2),
    (3, 1, "fly_snake", 4), (4, 1, "fly_snake", 4), (5, 1, "fly_snake", 6),
    # fly_phi fast axis: intervals, not points
    (0, 0, "fly_phi", 0), (1, 0, "fly_phi", 0),
    (3, 0, "fly_phi", 2), (5, 0, "fly_phi", 4),
    # fly_phi slow axis and fly_hexapod_1d: untouched
    (3, 1, "fly_phi", 3), (3, 0, "fly_hexapod_1d", 3),
])
def test_adjust_axis_length(n, axis, kind, expected):
    assert scanplan.adjust_axis_length(n, axis, kind) == expected


def test_a_one_point_phi_fly_collects_nothing():
    """Not a bug. phi flies at constant velocity and triggers on a clock,
    so one point spans no interval and there is nothing to record."""
    assert scanplan.count_positions([np.zeros(1)], "fly_phi") == 0


# -- count_positions ---------------------------------------------------------

@pytest.mark.parametrize("lengths,kind,expected", [
    ([5], "step", 5),
    ([5, 3], "step", 15),
    ([5, 3], "fly_snake", 24),     # (5+1) * (3+1)
    ([5, 4], "fly_snake", 24),     # (5+1) * 4   -- already even
    ([5], "fly_phi", 4),
    ([1], "fly_phi", 0),
])
def test_count_positions(lengths, kind, expected):
    arrays = [np.zeros(n) for n in lengths]
    assert scanplan.count_positions(arrays, kind) == expected


# -- extend_axis_positions ---------------------------------------------------

@pytest.mark.parametrize("coords,step,axis,kind,expected", [
    ([0.0, 1.0, 2.0], 1.0, 0, "fly_snake", [0.0, 1.0, 2.0, 3.0]),
    ([0.0, 1.0, 2.0], 1.0, 1, "fly_snake", [0.0, 1.0, 2.0, 3.0]),
    ([0.0, 1.0], 0.5, 0, "step", [0.0, 1.0]),      # nothing to add
])
def test_extend_axis_positions(coords, step, axis, kind, expected):
    got = scanplan.extend_axis_positions(np.asarray(coords), step, axis, kind)
    assert got.tolist() == pytest.approx(expected)


@pytest.mark.parametrize("kind", ["step", "fly_snake", "fly_hexapod_1d"])
def test_an_extended_array_is_exactly_as_long_as_the_count_predicts(kind):
    """The invariant the two functions exist to share. If they drift, a
    scan arms a different number of triggers than it writes points for."""
    for n in range(0, 7):
        coords = np.arange(float(n))
        for axis in (0, 1):
            extended = scanplan.extend_axis_positions(coords, 1.0, axis, kind)
            assert len(extended) == scanplan.adjust_axis_length(n, axis, kind)


def test_fly_phi_is_the_one_case_where_extend_and_count_disagree():
    """Recorded, not fixed.

    extend_axis_positions only ever appends, and fly_phi is the only kind
    that *reduces* a count -- so on the fast axis the array keeps n points
    while the count says n-1. extend_axis_positions' own docstring claimed
    the two always agree, which is untrue here.

    Left exactly as it is. Whether it can be reached depends on whether a
    fly_phi scan ever goes through the 2-D grid builder, and changing scan
    geometry on a working instrument on the strength of reading the code is
    not a trade worth making. This test exists so the discrepancy is known
    and deliberate rather than discovered during a beamtime.
    """
    for n in range(1, 7):
        coords = np.arange(float(n))
        extended = scanplan.extend_axis_positions(coords, 1.0, 0, "fly_phi")
        assert len(extended) == n
        assert scanplan.adjust_axis_length(n, 0, "fly_phi") == n - 1


# -- format_duration ---------------------------------------------------------

@pytest.mark.parametrize("seconds,expected", [
    (0.5, "0.5 s"), (59.9, "59.9 s"),
    (300, "300.0 s"),        # the switch is above five minutes, not at it
    (300.1, "5.0 min"), (3600, "60.0 min"),
])
def test_format_duration(seconds, expected):
    assert scanplan.format_duration(seconds) == expected


# -- the handler still agrees ------------------------------------------------

def test_the_scan_handler_still_gives_the_same_answers():
    """ScanHandler keeps its private planners while it is migrated; they
    must stay a thin pass-through rather than a second implementation."""
    from gui.handlers.scan_handler import ScanHandler

    h = ScanHandler.__new__(ScanHandler)   # these touch no instance state

    assert h._signed_step(2, -2, 0) == scanplan.signed_step(2, -2, 0)
    assert h._make_positions(0, -1, 1, 0.5).tolist() == \
        scanplan.make_positions(0, -1, 1, 0.5).tolist()
    assert h._snake_positions([0, 1], [5]).tolist() == \
        scanplan.snake_positions([0, 1], [5]).tolist()
    assert h._adjust_axis_length(3, 0, "fly_snake") == \
        scanplan.adjust_axis_length(3, 0, "fly_snake")
    assert h._compute_n_positions([np.zeros(5), np.zeros(3)], "fly_snake") == \
        scanplan.count_positions([np.zeros(5), np.zeros(3)], "fly_snake")
    assert h._fmt_time(300.1) == scanplan.format_duration(300.1)
    assert h._extend_axis_positions(np.asarray([0.0, 1.0, 2.0]), 1.0, 0,
                                    "fly_snake").tolist() == \
        scanplan.extend_axis_positions(np.asarray([0.0, 1.0, 2.0]), 1.0, 0,
                                       "fly_snake").tolist()
