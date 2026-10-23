"""Shared pytest configuration for the ptychoSAXS test suite.

No hardware or Qt event loop is started by any fixture here - tests import
plain Python classes (debug stubs, ini/JSON logic) directly, never a live
QApplication, so the suite runs headlessly with no display/X server/
QT_QPA_PLATFORM configuration required.

There is deliberately no sys.path manipulation. The repository is installed
with `pip install -e .`, so `gui`, `ptychosaxs`, `tools` and `debug` are real
packages and import the same way here as they do in the application. The old
path block also put ptychosaxs/ itself on sys.path, which made each device
module importable under two different names and forced every one of them to
carry a `try: from .x import ... except ImportError: from x import ...` shim.
"""
