"""Every self-call in the relocated scan code resolves on the real object.

The bug this exists for: stepscan2d0 called self._scan_fields(...), a
helper that was supposed to be added to ScanHandler in the same change
and never was -- the script that would have written it exited on an
unrelated pattern mismatch first. Nothing caught it. The file compiles,
because attribute access is resolved at runtime, and ruff's undefined-name
check does not look at attributes. The first thing to notice was a scan
that had already created its master file and then died on the next line.

ScanHandler is composed of two classes now, and the whole premise of
moving code in tranches is that the pieces still add up. These tests
check that statically, which is the one kind of check that works here:
nothing exercises a real scan, so a green run of everything else proves
very little about whether the executors are wired up.
"""

import ast
import inspect
import pathlib

import pytest

from gui.handlers.scan_handler import ScanHandler
from ptychosaxs.api.scan_exec import ScanExecutorsMixin

EXEC_PATH = pathlib.Path(inspect.getfile(ScanExecutorsMixin))
HANDLER_PATH = pathlib.Path(inspect.getfile(ScanHandler))


def _self_calls(path, class_name=None):
    """{method: {names it calls on self}} for one class in a file."""
    tree = ast.parse(path.read_text())
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef)
               and (class_name is None or n.name == class_name))
    out = {}
    for fn in cls.body:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        called = set()
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "self"):
                called.add(node.func.attr)
        out[fn.name] = called
    return out


def test_every_method_the_moved_code_calls_exists():
    """The regression. A moved executor calling a helper that was never
    written compiles perfectly and fails only when that scan runs."""
    missing = {}
    for method, called in _self_calls(EXEC_PATH).items():
        for name in sorted(called):
            if not hasattr(ScanHandler, name):
                missing.setdefault(method, []).append(name)
    assert missing == {}, (
        "relocated methods call things that do not exist on ScanHandler:\n  "
        + "\n  ".join(f"{m} -> {sorted(n)}" for m, n in sorted(missing.items())))


def test_every_method_the_handler_calls_exists():
    """The same check the other way: the handler may call into the mixin,
    and a method moving out must not strand a caller."""
    missing = {}
    for method, called in _self_calls(HANDLER_PATH, "ScanHandler").items():
        for name in sorted(called):
            if not hasattr(ScanHandler, name):
                missing.setdefault(method, []).append(name)
    assert missing == {}, (
        "ScanHandler calls things that do not exist:\n  "
        + "\n  ".join(f"{m} -> {sorted(n)}" for m, n in sorted(missing.items())))


def test_no_method_was_lost_or_duplicated_in_the_move():
    """A name defined in both places means one silently shadows the other,
    and whichever the MRO picks is not necessarily the one that was
    edited."""
    in_mixin = {n for n, v in vars(ScanExecutorsMixin).items()
                if callable(v) and not n.startswith("__")}
    in_handler = {n for n, v in vars(ScanHandler).items()
                  if callable(v) and not n.startswith("__")}
    both = in_mixin & in_handler
    assert both == set(), f"defined in both the mixin and the handler: {sorted(both)}"


def test_the_eight_executors_are_reachable():
    for name in ("stepscan0", "stepscan2d0", "stepscan3d0", "fly3d0",
                 "fly2d0", "fly2d0_SNAKE", "fly0", "helix_fly0"):
        assert hasattr(ScanHandler, name), name


@pytest.mark.parametrize("name", [
    "_scan_fields", "_write_scan_summary_line", "_advance_scan_number_safe",
    "_advance_scan_number_thread_safe", "_get_phi_angle",
])
def test_the_qt_helpers_the_mixin_calls_back_into_are_present(name):
    """These stayed in the handler because they build widgets. The moved
    code calls them, so they have to be there."""
    assert hasattr(ScanHandler, name)
    assert name in vars(ScanHandler), f"{name} should live on the handler"
