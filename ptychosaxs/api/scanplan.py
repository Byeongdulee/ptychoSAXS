"""Scan geometry: where the points are, and how many of them there are.

Pure functions. No hardware, no widgets, no session state -- give them
numbers and they give you numbers back. That is the whole point: this is
the part of a scan that can be checked without an instrument, and pinning
it is what makes the rest of the scan code safe to move.

Lifted verbatim out of gui/handlers/scan_handler.py, which still calls
them. The behaviour is pinned by tests/test_scanplan_golden.py against
values captured from the original implementation before it moved, edge
cases included -- and there are several that look like bugs and are not:

  * a zero step becomes the full span, so a two-point array is still
    produced rather than an empty one;
  * a one-point "fly_phi" axis counts as zero positions, because phi flies
    at fixed intervals and one point spans none;
  * a "fly_snake" X line gets one extra phantom trigger, and its Y line
    count is rounded up to even, both because the hexapod's trajectory
    generator requires it.
"""

import numpy as np

#: Scan kinds this module knows how to count.
STEP = "step"
FLY_SNAKE = "fly_snake"
FLY_HEXAPOD_1D = "fly_hexapod_1d"
FLY_PHI = "fly_phi"

SCAN_KINDS = (STEP, FLY_SNAKE, FLY_HEXAPOD_1D, FLY_PHI)


def signed_step(start: float, end: float, step: float) -> float:
    """Correct `step`'s sign to match the start->end direction.

    A zero step becomes the full span, so a two-element array can still be
    produced. A zero-length span with a zero step becomes 1.0, which only
    matters in that it keeps the caller out of a division by zero.
    """
    if step == 0:
        step = (end - start) if (end != start) else 1.0
    return -abs(step) if start > end else abs(step)


def make_positions(p0: float, st: float, fe: float, step: float) -> np.ndarray:
    """Absolute scan positions as a 1-D array.

    p0+st is the scan start, p0+fe the end. The sign of `step` is corrected
    to match, so a caller cannot reverse a scan by passing a negative step
    by accident.
    """
    start = p0 + st
    end = p0 + fe
    step = signed_step(start, end, step)
    return np.arange(start, end + step / 2, step)


def snake_positions(x_positions, y_positions) -> np.ndarray:
    """Nx2 array of (x, y) in boustrophedon order.

    y is the slow axis. Even rows run x forwards, odd rows backwards, so
    the stage never flies back across the sample to start the next line.
    """
    coords = []
    for j, y in enumerate(y_positions):
        row = x_positions if j % 2 == 0 else x_positions[::-1]
        for x in row:
            coords.append((x, y))
    return np.asarray(coords)


def adjust_axis_length(n: int, axis_index: int, scan_kind: str) -> int:
    """The number of positions this axis really contributes.

    One place, so counts and arrays cannot disagree -- count_positions and
    extend_axis_positions both go through here.

    `axis_index` is the position in the ordered axis list: 0 is the fast
    axis, 1 the slow one.

    "fly_snake" adds a phantom trigger at the end of each X line and rounds
    the Y line count up to even. Both are hexapod hardware constraints: the
    trajectory generator fires one extra trigger per line to get the pixel
    timing right, and its table requires an even line count. Neither
    depends on anything the user typed.

    "fly_phi" counts intervals rather than points on the fast axis, because
    constant-velocity phi motion triggers on a clock rather than at rest
    points -- so a single-point phi fly collects nothing at all.
    """
    if scan_kind == FLY_SNAKE:
        if axis_index == 0:
            return n + 1
        if axis_index == 1:
            return n + (n % 2)
    elif scan_kind == FLY_PHI and axis_index == 0:
        return max(n - 1, 0)
    return n


def extend_axis_positions(coords: np.ndarray, step: float, axis_index: int,
                          scan_kind: str) -> np.ndarray:
    """Pad `coords` to the length adjust_axis_length predicts.

    Extra points are extrapolated by `step` from the last one, so an array
    built here has exactly the length the count says it will -- with one
    exception. This function only ever appends, and "fly_phi" is the only
    kind that *reduces* a count, so a fly_phi fast axis keeps n points
    while count_positions says n-1. That mismatch predates the move and is
    left alone deliberately; see
    tests/test_scanplan_golden.py::test_fly_phi_is_the_one_case_where_extend_and_count_disagree.
    """
    target_len = adjust_axis_length(len(coords), axis_index, scan_kind)
    extra = target_len - len(coords)
    if extra > 0:
        last = coords[-1] if len(coords) else 0.0
        coords = np.append(coords, last + step * np.arange(1, extra + 1))
    return coords


def count_positions(position_arrays, scan_kind: str = STEP) -> int:
    """Total collected positions for a scan over these axes."""
    n_pos = 1
    for i, pos in enumerate(position_arrays):
        n_pos *= adjust_axis_length(len(pos), i, scan_kind)
    return n_pos


def format_duration(seconds: float) -> str:
    """Seconds as a human-readable string, as the estimate label shows it.

    Switches to minutes above five minutes, not at it: 300 s reads as
    seconds and 300.1 s as minutes.
    """
    return "%.1f min" % (seconds / 60) if seconds > 300 else "%.1f s" % seconds
