# Architecture and driver development

```text
GTK4 application ─────┐
GNOME Shell extension ├─ session D-Bus ─ acquisition owner ─ instrument drivers ─ SCPI / serial transport
CLI ─────────────────┘                       │
                                             └─ SQLite WAL ─ CSV
```

The service owns instrument connections and serializes commands with reads on one acquisition thread. The main GLib loop only handles IPC and snapshots; the GTK and Shell clients fetch state asynchronously. The bounded command queue prevents unbounded control backlog. Transports have bounded response sizes and transaction timeouts. A timeout closes the connection to prevent a delayed response being mistaken for the next query.

## Plugin contract

Drivers can be installed in the same Python environment via the `gate.drivers` entry point group. No central model switch needs to grow as the catalog expands.

```toml
[project.entry-points."gate.drivers"]
my_meter = "my_package.driver:Meter"
```

The driver class supplies:

```python
class Meter:
    category = "multimeter"  # or power_supply / oscilloscope
    simulated = False

    @staticmethod
    def matches(identity: str) -> bool:
        # Match exact manufacturer/model combinations, not loose SCPI heuristics.
        ...

    def __init__(self, transport, identity):
        self.id = transport.resource
        self.name = identity
        self.transport = transport

    def read(self):
        # Return list[gate.model.Reading]; use None + quality for unavailable values.
        ...

    def close(self):
        self.transport.close()
```

Discovery probes Linux USBTMC resources or a user-supplied TCP/Fluke serial endpoint with `*IDN?`; it never sends resets. Third-party drivers are trusted Python code. They must bound I/O, validate units/values, preserve instrument settings during reads, and match only hardware they implement. Driver loading errors appear in connection status.

This first output API is a demo-only engine adapter. A future hardware output capability must specify units, channels, voltage/current limits, slew policy, interlocks, readback verification, and explicit output state before removing the demo guard. Don't implement a generic unverified `VOLT` write for arbitrary supplies.

Examples with a running demo service:

```bash
gate command '{"action":"static","instrument":"demo:power_supply","voltage":3.3}'
gate command '{"action":"output","instrument":"demo:power_supply","enabled":true}'
gate command '{"action":"sweep","instrument":"demo:power_supply","start":0,"stop":5,"step":0.5,"dwell":1}'
gate command '{"action":"cancel_sweep","instrument":"demo:power_supply"}'
gate command '{"action":"output","instrument":"demo:power_supply","enabled":false}'
```

## Storage

- `sessions`: UTC origin, monotonic origin, requested interval, timing contract.
- `samples`: session, cycle/tick, shared tick offset, instrument ID, channel, numeric value/unit/quality, read batch start/end offsets.
- `events`: session, monotonic offset, instrument ID, command or sweep setpoint change.

SQLite commits once per cycle and uses WAL so exports can read completed cycles while recording. Reopening a file creates another session. CSV exports samples from all sessions with their UTC tick times. Event data remains in SQLite; a separate event CSV export is pending.

## Scaling and limits

The registry is intended to support hundreds of models without dependencies between drivers. Simultaneously polling hundreds of instruments is a different performance problem: this initial scheduler reads devices sequentially, and must not be presented as guaranteeing a fixed rate at that scale. Future workers should serialize per instrument/transport, run different instruments concurrently, and retain acquisition windows on the shared host clock. Tests must cover disconnects and overload before that change.

Manually added serial/TCP endpoints and the sampling interval persist in the user configuration; the selected top-bar channel is session-only. No stored hardware commands are replayed on startup. Polling defaults to one second; disconnected devices require an explicit reconnect during a run, and there is no LAN subnet probing. Saved endpoints reconnect at service startup. A scope driver's scalar values are not waveform captures. SDS1202X-E USB scalar communication has been verified on firmware 1.3.26. Accuracy comparison, extension-in-Shell behavior, multi-hour endurance, and Ubuntu package installation still need validation.

Linux USBTMC protocol and ioctl behavior follow the [kernel driver](https://github.com/torvalds/linux/blob/master/drivers/usb/class/usbtmc.c) and the local `linux/usb/tmc.h` UAPI header. Regression tests cover identity responses split across USB messages with no newline and closure after USB timeout.

The Fluke 45 driver uses a separate prompt-aware serial transport and preserves front-panel configuration. See [its setup and validation notes](fluke45.md). Top bar dropdowns are stable actors keyed by device ID and channel; updates change labels and selection markers without rebuilding open menus. GNOME supports one expanded submenu in the top menu, so devices are sibling dropdowns instead of nested submenus.

## Agent control plane

The service hosts a bearer-authenticated IPv4 loopback HTTP API on port 8765. HTTP workers inspect cached state and submit commands to the acquisition owner; they never use instrument transports directly. Command IDs track queued/running/succeeded/failed state. Credentials rotate on startup and are written to a private runtime manifest. Model-specific raw query/write operations require an explicit startup flag. The device manager is limited to device connection management, service settings and access information; measurements and primary-function selection live in the GNOME top bar. See [the agent guide](../AGENT_INSTRUMENT_GUIDE.md).

Drivers may expose `functions()` returning `MeasurementFunction` values, and `select_function(id)` for configurable functions. Capability metadata is independent of current readings so inactive modes remain visible. A mode selection is acknowledged to clients only after the driver confirms it. Fluke primary-display capabilities are implemented; secondary-display control remains excluded.
