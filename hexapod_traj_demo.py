"""Minimal real-hardware trajectory test for the PI C-887 hexapod.

Connects directly over GCS/TCP (not EPICS), builds a small XZ snake
trajectory, and runs it. The direct socket needs sole use of the
controller - stop the 12idbHXP IOC and PI's vendor software first, or the
connection will be refused.

Run manually: python hexapod_traj_demo.py
"""
import time

from ptychosaxs import hexapod as hexapod_mod
from ptychosaxs import hexapod_traj

AXES = "XZ"                # set_traj_SNAKE2 always drives X and Z.
STEP_TIME_S = 0.1          # time between triggers
X_DISTANCE_MM = 0.02       # X sweeps [Xi, Xi + X_DISTANCE_MM]
X_STEP_MM = 0.01
Y_DISTANCE_MM = 0.02       # Z (the snake's "Y") sweeps [Yi, Yi + Y_DISTANCE_MM]
Y_STEP_MM = 0.01


def safe_disconnect(traj) -> None:
    """Work around two bugs in pihexapod's direct-socket transport.

    1. gcs.Hexapod.disconnect() -> pigcs.Hexapod.close() ->
       GCSDevice.CloseConnection() -> self.dll.CloseConnection(), but the
       socket gateway (PISocket) only implements close()/unload(), not
       CloseConnection(). That AttributeError leaves the socket open, so
       fall back to closing the gateway directly.
    2. GCSDevice.__del__ closes the gateway again at interpreter exit
       (PISocket.close() has no guard against a double close, and raises
       "Bad file descriptor" shutting down an already-closed socket). Once
       we've closed it ourselves, make the gateway's close/unload a no-op
       so that second close is harmless.
    """
    try:
        traj.disconnect()
        return
    except AttributeError as exc:
        print(f"[DEBUG] traj.disconnect() hit the PISocket/CloseConnection "
              f"bug ({exc}); closing the socket gateway directly")
    gateway = traj._dev.pidev.pidev.dll
    gateway.close()
    gateway.close = lambda: None
    gateway.unload = lambda: None


print(f"[DEBUG] connecting to hexapod at {hexapod_mod.HEXAPOD_IP} (direct GCS)")
traj = hexapod_traj.make_trajectory_backend(
    hexapod_mod.HEXAPOD_IP, prefer="direct", log=print)

print(f"[DEBUG] backend state={traj.state} available={traj.available}")
if not traj.available:
    raise SystemExit(f"no transport: {traj.reason}")

pos = traj._dev.get_pos()
Xi, Yi = float(pos["X"]), float(pos["Z"])
Xf, Yf = Xi + X_DISTANCE_MM, Yi + Y_DISTANCE_MM
print(f"[DEBUG] current position: X={Xi:.5f} mm, Z={Yi:.5f} mm")
print(f"[DEBUG] plan: X [{Xi:.5f} -> {Xf:.5f}] step {X_STEP_MM:g} mm, "
      f"Z [{Yi:.5f} -> {Yf:.5f}] step {Y_STEP_MM:g} mm, "
      f"triggers every {STEP_TIME_S:g} s")

print("[DEBUG] set_traj_SNAKE2(): writing waveform + trigger table")
traj.set_traj_SNAKE2(STEP_TIME_S, Xi, X_DISTANCE_MM, X_STEP_MM,
                      Yi, Yf, Y_STEP_MM)
print(f"[DEBUG] pulse_number={traj.pulse_number} pulse_step={traj.pulse_step}")

print("[DEBUG] goto_start_pos(): moving to the trajectory's start position")
traj.goto_start_pos(AXES)

print("[DEBUG] run_traj(): firing the wave generator")
# wait=True would use the device's self.scantime, which only set_traj() sets
# - set_traj_SNAKE2() never does, so that path raises AttributeError here.
# Wait out the known duration ourselves instead.
scantime = traj.pulse_number * traj.pulse_step
t0 = time.time()
traj.run_traj(AXES, wait=False)
time.sleep(scantime)
print(f"[DEBUG] waited {time.time() - t0:.2f} s "
      f"(expected scantime {scantime:.2f} s)")

traj.stop_traj()
pos = traj._dev.get_pos()
print(f"[DEBUG] final position: X={float(pos['X']):.5f} mm, "
      f"Z={float(pos['Z']):.5f} mm")

safe_disconnect(traj)
print("[DEBUG] disconnected")
