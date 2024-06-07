from qds.qds import ptycho_qudis, plot_position
from tools.softglue import sgz_pty
try:
    qds = ptycho_qudis()
    qds.get_position()
except:
    print("QDS cannot be reached through USB. Instead softglueZynq is used.")
    qds = sgz_pty()