# qds_bridge

A one-way ZMQ link that carries quDIS interferometer samples off the Windows
machine that holds the USB connection.

**This folder is walled off, in one direction.** Nothing here imports from
`ptychosaxs/`, `gui/`, `tools/` or `handlers/` — copy the folder to the
Windows box on its own and it still runs. The reverse is no longer true:
`ptychosaxs/qds_bridge_client.py` imports `wire.py` and `config.py` from here,
on purpose, so the wire format and host/port can't drift between the two
copies. That is the one sanctioned cross-import; nothing else in the
repository should import from `qds_bridge/`, and this folder still imports
nothing back.

## What this round is for

Measuring message loss and stability at the real USB sample rate was the
first round. This one adds the piece that was deliberately deferred then: a
quiet client meant to run inside the GUI process rather than a human's
terminal (`ptychosaxs/qds_bridge_client.py`), wired into
`ptychosaxs/interferometers.py` behind `rungui.py`'s `QDS_USE_BRIDGE` flag.
See that module's docstring for its watchdog behaviour.

Still not done:

- a host-side history deque, or any gap-recovery / backfill mechanism

`qds_publisher.py` carries a marked extension point where the deque drops in;
nothing around it has to change. The loss figures this tool produces are what
should size it.

## Running it

Publisher, on the machine with the USB device:

```
python qds_publisher.py
```

Subscriber, on the GUI machine:

```
python qds_subscriber.py --quiet
```

`--quiet` suppresses the per-sample line. Use it for anything above a trickle;
printing every sample will itself become the bottleneck and you will end up
measuring your terminal.

### Without hardware

The publisher can generate synthetic samples, so both ends can be exercised
on Linux before touching the Windows box:

```
python qds_publisher.py --simulate --rate 5000
python qds_publisher.py --simulate --rate 0      # free-running
```

### Other options

| Flag | Both | Meaning |
|---|---|---|
| `--port` | yes | override the TCP port |
| `--report-interval` | yes | seconds between summary lines |
| `--host` | sub | publisher hostname |
| `--device` | pub | quDIS device index (default 0) |
| `--quiet` | sub | gaps and summaries only |

Defaults live in [`config.py`](config.py) — host `sec12mic`, port `5561`,
queue bounds, report interval, and `HARDWARE_RATE_HZ` (100 Hz), the rate the
real quDIS hardware is polled at. Each is a one-line edit.

`HARDWARE_RATE_HZ` only applies to real hardware (`python qds_publisher.py`,
no `--simulate`) — it is unrelated to `--simulate --rate`, which stays
uncapped so it can keep stress-testing the transport above any rate the real
device would ever produce.

## Reading the output

The subscriber prints a gap the moment it sees one:

```
[sub] MISSING 10432-10449 (18)
```

and a summary every few seconds:

```
[sub] received=48231 expected=48250 lost=19 (0.039%) rate=9,641/s lost-since-last=4 lag=+1.83ms
```

`lag` is this machine's clock minus the publisher's sample timestamp,
averaged over the summary window: transit time plus any offset between the
two clocks. It is the starting value for a consumer that matches samples
against timestamps taken on the Linux side (the object accumulator's
"Detector-position timestamp offset").

The publisher prints its own view:

```
[pub] seq=48250 rate=9,650/s dropped=0 (total 0)
```

**Compare the two.** `lost` on the subscriber is end-to-end. `dropped` on the
publisher counts only what its own send queue refused. If the publisher
reports drops, the subscriber (or the link) could not keep up and `SND_HWM`
was reached. If the publisher reports zero drops but the subscriber still
reports loss, the samples went missing in the network or in the subscriber's
own receive queue.

Loss at startup is expected and is not counted: the first message after
connecting sets the baseline, because the publisher runs whether or not
anyone is listening.

## Design notes

**Sequence numbers for loss, timestamps for matching.** Every sample carries
a `uint64` counter that increments forever. Gap detection uses only those
numbers, so it never depends on the two machines agreeing on the time.

Every sample also carries `t`, the publisher's `time.time()` taken right after
the device read, in Unix-epoch seconds. That is the same epoch as an EPICS PVA
`timeStamp` (`secondsPastEpoch + nanoseconds * 1e-9`), so a sample can be
matched to a detector frame directly. The match is only as good as the two
clocks' NTP sync; consumers correct any residual offset themselves. Before
Python 3.13, `time.time()` on Windows advances only every ~15.6 ms, so at
100 Hz consecutive samples can share a timestamp. Consumers must allow for
that; the object accumulator picks the earliest of a tied group.

**Acquisition is independent of the client.** The publisher *binds*; the
subscriber *connects*. Sends are `NOBLOCK`, and a PUB socket at its send HWM
drops rather than blocking. With no subscriber, or a stalled one, the poll
loop keeps running at full rate and samples are lost instead of time. Killing
and restarting the subscriber mid-run is a supported thing to do: it will
reconnect on its own and report one gap covering the downtime.

**Units are nanometres**, exactly as the DLL reports them. The GUI divides by
1000 for micrometres (`gui/handlers/status_handler.py`, `get_qds_pos`). The
publisher ships what it measured and leaves scaling to the consumer.

**Channels** are the three *relative* positions of one device, matching what
the GUI actually consumes.

**Wire format** is a 40-byte `struct` — `<Qdddd`, i.e. `seq, ch0, ch1, ch2,
t` — defined once in [`wire.py`](wire.py). Binary rather than JSON because
encoding sits on the acquisition hot path.

Readers still accept the original 32-byte `<Qddd` form (with `t` reported as
`None`), so a new subscriber works against an old publisher. An *old*
subscriber rejects every 40-byte frame as malformed, so **update the Linux
side first, then the publisher on sec12mic**.

## Known wart

Importing `qds.qds` from `pyqudis` pulls in `matplotlib` and `IPython` at
module scope. That is a startup cost on the publisher, not a per-sample one,
and it is skipped entirely under `--simulate` because the import is done
inside `HardwareSource.open()`. If it ever becomes a problem, the ~40 lines of
ctypes signatures could be vendored into this folder — at the cost of a second
copy of the DLL bindings to keep in step.
