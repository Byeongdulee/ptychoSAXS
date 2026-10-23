"""ptychosaxs-server --status / --stop.

An escape hatch the operator can reach for. This matters more here than it
would elsewhere: the daemon holds the C-887's single TCP connection, so a
wedged server locks every panel out of the hexapod -- a failure mode that
did not exist when each GUI opened its own connection. "Restart it" has to
be something you can actually do, and see the result of.
"""

import json
import sys

from ptychosaxs.link import config, errors
from ptychosaxs.server import lifecycle


def _call(method, params=None, timeout=5.0):
    from ptychosaxs.client.connection import BeamlineClient
    client = BeamlineClient(kind="cli", label="admin")
    client.connect(start_server=False, timeout_s=timeout)
    try:
        return client.call(method, params or {}, timeout_s=timeout)
    finally:
        client.close()


def status(as_json=False) -> int:
    if not lifecycle.server_is_running():
        info = lifecycle.read_lock_info()
        print("no ptychoSAXS server is running")
        if info:
            print(f"  (last recorded pid {info.get('pid')}, "
                  f"socket {config.socket_path()})")
        return 1
    try:
        st = _call("system.status")
    except Exception as exc:
        print(f"server is listening but did not answer: {exc}")
        return 2

    if as_json:
        print(json.dumps(st, indent=2, sort_keys=True))
        return 0

    print(f"ptychoSAXS server  pid {st['server_pid']}  "
          f"up {st['uptime_s']}s")
    print(f"  socket    {st['socket']}")
    print(f"  state     {'ready' if st['ready'] else st['phase']}")
    print(f"  hardware  {'SIMULATED' if st['simulated'] else 'real'}")
    groups = [g for g in st.get("groups", []) if g["open"]]
    open_groups = ", ".join(f"{g['name']}({g['refs']})" for g in groups)
    print(f"  devices   {open_groups or 'none open'}")
    clients = st.get("clients", [])
    print(f"  clients   {len(clients)}")
    for c in clients:
        print(f"    {c['label']:<18} {c['kind']:<9} pid {c['pid']}  "
              f"up {c['uptime_s']}s")
    jobs = st.get("jobs", [])
    print(f"  jobs      {len(jobs)}")
    for j in jobs:
        print(f"    {j['job_id']}  {j['kind']}  {j['state']}  "
              f"{j['elapsed_s']}s"
              f"{'' if j['cancellable'] else '  (cannot be interrupted)'}")
    return 0


def stop(force=False) -> int:
    if not lifecycle.server_is_running():
        print("no ptychoSAXS server is running")
        return 0
    try:
        _call("system.shutdown")
    except errors.ServerError as exc:
        if exc.code == errors.ERR_FLY_IN_PROGRESS and not force:
            print(f"refusing to stop: {exc.message}")
            print("  a fly scan runs in the C-887's wave generator and has "
                  "no host-side stop.")
            print("  re-run with --force to kill the server anyway "
                  "(the stage keeps moving).")
            return 3
        if exc.code != errors.ERR_FLY_IN_PROGRESS:
            print(f"server refused: {exc}")
            return 2
        info = lifecycle.read_lock_info() or {}
        pid = info.get("pid")
        if pid:
            import os
            import signal
            print(f"forcing: SIGTERM to pid {pid}")
            os.kill(pid, signal.SIGTERM)
        return 0
    except Exception as exc:
        print(f"could not reach the server: {exc}")
        return 2
    print("shutdown requested")
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--json" in argv:
        return status(as_json=True)
    if "--stop" in argv:
        return stop(force="--force" in argv)
    return status()
