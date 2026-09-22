"""Shared pytest fixtures/path setup for the ptychoSAXS test suite.

No hardware or Qt event loop is started by any fixture here - tests import
plain Python classes (debug stubs, ini/JSON logic) directly, never a live
QApplication, so the suite runs headlessly with no display/X server/
QT_QPA_PLATFORM configuration required.
"""
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (
    _REPO_ROOT,
    os.path.join(_REPO_ROOT, "debug"),
    os.path.join(_REPO_ROOT, "gui"),
    os.path.join(_REPO_ROOT, "ptychosaxs"),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)
