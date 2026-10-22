# qds_bridge

A one-way ZMQ link that carries quDIS interferometer samples off the Windows
machine that holds the USB connection.

**This folder is walled off.** Nothing here imports from `ptychosaxs/`,
`gui/`, `tools/` or `handlers/`, and nothing in the rest of the repository
imports from here. Copy the folder to the Windows box on its own and it runs.

## What this round is for

Measuring message loss and stability at the real USB sample rate, nothing
more. It is deliberately *not* yet:

- a host-side history deque, or any gap-recovery / backfill mechanism
- integrated into `rungui.py`

Both are planned. The loss figures this produces are what should size the
deque, which is why they come first. `qds_publisher.py` carries a marked
extension point where the deque drops in; nothing around it has to change.

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
queue bounds, report interval. Each is a one-line edit.

## Reading the output

The subscriber prints a gap the moment it sees one:

```
[sub] MISSING 10432-10449 (18)
```

and a summary every few seconds:

```
[sub] received=48231 expected=48250 lost=19 (0.039%) rate=9,641/s lost-since-last=4
```

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

**Sequence numbers, not timestamps.** Every sample carries a `uint64` counter
that increments forever. Alignment is done by matching those numbers, so the
two machines never need synchronised clocks — which removes NTP skew from the
problem entirely rather than correcting for it.

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

**Wire format** is a 32-byte `struct` — `<Qddd` — defined once in
[`wire.py`](wire.py). Binary rather than JSON because encoding sits on the
acquisition hot path.

## Known wart

Importing `qds.qds` from `pyqudis` pulls in `matplotlib` and `IPython` at
module scope. That is a startup cost on the publisher, not a per-sample one,
and it is skipped entirely under `--simulate` because the import is done
inside `HardwareSource.open()`. If it ever becomes a problem, the ~40 lines of
ctypes signatures could be vendored into this folder — at the cost of a second
copy of the DLL bindings to keep in step.
