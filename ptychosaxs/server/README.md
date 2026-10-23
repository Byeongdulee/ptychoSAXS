# The shared beamline server

One process owns every hardware connection. GUIs are clients.

## Why

Before this, `rungui`, `optics_motors` and `CRL_3dprint` each opened their own
connections at module import. The PI C-887 accepts exactly one TCP client, so
running two panels was a genuine conflict rather than a tidiness problem, and
which one got the hexapod was a race.

## Design notes

**Transport is a Unix domain socket, `SOCK_STREAM`.** The protocol this
replaces was UDP: unreliable, unordered, one datagram per message, and with no
reply path at all — `gui/network/server.py` captured its transport in
`connection_made` and never sent anything back, so a client could not learn
whether its command had arrived. `AF_UNIX`/`SOCK_STREAM` gives TCP's delivery
guarantees with no port to allocate, nothing reachable from the network, file
permissions for access control, and `SO_PEERCRED` so the server knows which
process it is talking to rather than being told.

**The wire lives in `ptychosaxs/link/`, not here.** The client and the MCP
process both need it; having them import from `server` would drag the daemon
into their address space. It is not called `transport` because
`ptychosaxs/transport.py` already means driver selection (EPICS / direct /
simulated), and two meanings of one word in one package is a lasting trap.

**Startup order is the correctness argument.** Take the flock before importing
anything; unlink a stale socket and bind inside the lock; build hardware only
after. Several panels starting together each spawn a daemon, and the losers
must exit having touched nothing — otherwise they race for the C-887. The
lockfile is never unlinked: removing a file another process holds an flock on
lets the next starter create a fresh inode and "hold the lock" too.

**Bind before building.** The socket answers `system.hello` with
`ready:false` and a phase while stages are still coming up, so a panel shows
"building hexapod" instead of timing out against a socket that is not there.

**Locks are command-scoped.** A move sends `MOV` and releases; completion is
established by polling position, each poll taking the lock for a millisecond.
Holding the lock for a 30-second move would stall the position publisher and
therefore every connected panel's readout.

**Reference counting happens twice.** The server lives while any client is
connected; a device group lives while any connected client declared it needs
one. An optics panel never causes the hexapod to be opened.

**Shutdown respects the hardware.** A step scan is driven from here, so it is
asked to stop and finishes its current step, closes the shutter and writes its
data. A fly scan's trajectory runs in the C-887's wave generator with no
host-side stop, so the server waits it out and a GUI trying to close is told
to wait.

**Loss is counted, never silent.** A client that stops draining gets its
position events conflated rather than queued; if even that overflows the
connection is closed with a logged count. Same rule as `qds_bridge`: lose
data visibly, never lose time.

**The device layer prints.** `build_controller` announces every driver
fallback with `print()`, and those lines are the first thing anyone wants when
a stage comes up simulated. `logging_setup.capture_prints()` routes them into
`server.log` rather than `/dev/null`.

## Operating it

    python -m ptychosaxs.server --simulate --foreground
    python -m ptychosaxs.server --status
    python -m ptychosaxs.server --stop [--force]

`--status` and `--stop` matter more than they look: the daemon holds the
C-887's only connection, so a wedged server locks every panel out of the
hexapod — a failure mode that did not exist before. Recovery has to be
something an operator can do and see the result of.

Socket, lockfile and log live in `$XDG_RUNTIME_DIR/ptychosaxs/`;
`PTYCHOSAXS_RUNTIME_DIR` overrides it.
