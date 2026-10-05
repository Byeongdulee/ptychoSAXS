import os

# PTYCHOSAXS_DEBUG_QDS is exported by gui/rungui.py from its DEBUG_QDS flag.
# When set, neither the QDS USB library nor softglueZynq is imported.
DEBUG_QDS = os.environ.get("PTYCHOSAXS_DEBUG_QDS") == "1"

if DEBUG_QDS:

    class _QDSDebug:
        """Simulated interferometer: reports the origin, connects to nothing."""

        def get_position(self):
            return [[0.0, 0.0, 0.0]], None

        def connect(self):
            pass

        def disconnect(self):
            pass

    def plot_position(*args, **kwargs):
        print("[DEBUG] plot_position() skipped - QDS is in debug mode.")

    qds = _QDSDebug()
else:
    from qds.qds import ptycho_qudis, plot_position
    from tools.softglue import sgz_pty
    try:
        qds = ptycho_qudis()
        qds.get_position()
    except:
        print("QDS cannot be reached through USB. Instead softglueZynq is used.")
        qds = sgz_pty()
