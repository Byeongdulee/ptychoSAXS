import os

# PTYCHOSAXS_DEBUG_QDS is exported by gui/rungui.py from its DEBUG_QDS flag.
# When set, neither the QDS USB library nor softglueZynq is imported.
DEBUG_QDS = os.environ.get("PTYCHOSAXS_DEBUG_QDS") == "1"

# PTYCHOSAXS_QDS_USE_BRIDGE is exported by gui/rungui.py from its
# QDS_USE_BRIDGE flag. Only consulted when DEBUG_QDS is False: read QDS
# positions from qds_bridge/qds_publisher.py over the network instead of
# USB/softglueZynq directly. See ptychosaxs/qds_bridge_client.py.
QDS_USE_BRIDGE = os.environ.get("PTYCHOSAXS_QDS_USE_BRIDGE") == "1"

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
elif QDS_USE_BRIDGE:
    from ptychosaxs.qds_bridge_client import QDSBridgeClient

    def plot_position(*args, **kwargs):
        print("[qds-client] plot_position() is not available over the bridge.")

    qds = QDSBridgeClient()
    # Nothing in gui/rungui.py ever calls instruments.connect() (which would
    # call qds.connect()) - confirmed by grep across the repo. Without this,
    # the background thread never starts and get_position() raises "no
    # samples received yet" forever. Connect eagerly here instead of relying
    # on a call site that does not exist.
    qds.connect()
else:
    from qds.qds import ptycho_qudis, plot_position
    from ptychosaxs.tools.softglue import sgz_pty
    try:
        qds = ptycho_qudis()
        qds.get_position()
    except:
        print("QDS cannot be reached through USB. Instead softglueZynq is used.")
        qds = sgz_pty()
