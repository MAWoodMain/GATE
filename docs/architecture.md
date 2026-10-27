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

Seen TMC endpoints, manually added serial/TCP endpoints, last-known instrument names/categories, and the sampling interval persist in the user configuration; the top-bar function list is saved per instrument, including an empty selection. Explicitly saved measurement functions are restored after hardware identification. Output states, setpoints, sweeps and arbitrary raw commands are never replayed. Polling defaults to one second; disconnected devices require an explicit reconnect during a run, and there is no LAN subnet probing. Enabled endpoints reconnect at service startup; deliberate disconnects remain disabled. A scope driver's scalar values are not waveform captures. SDS1202X-E USB scalar communication has been verified on firmware 1.3.26. Accuracy comparison, extension-in-Shell behavior, multi-hour endurance, and Ubuntu package installation still need validation.

Linux USBTMC protocol and ioctl behavior follow the [kernel driver](https://github.com/torvalds/linux/blob/master/drivers/usb/class/usbtmc.c) and the local `linux/usb/tmc.h` UAPI header. Regression tests cover identity responses split across USB messages with no newline and closure after USB timeout.

The Fluke 45 driver uses a separate prompt-aware serial transport and preserves front-panel configuration. See [its setup and validation notes](fluke45.md). Top bar dropdowns are stable actors keyed by device ID and channel; updates change labels and selection markers without rebuilding open menus. GNOME supports one expanded submenu in the top menu, so devices are sibling dropdowns instead of nested submenus.

## Agent control plane

The service hosts a bearer-authenticated IPv4 loopback HTTP API on port 8765. HTTP workers inspect cached state and submit commands to the acquisition owner; they never use instrument transports directly. Command IDs track queued/running/succeeded/failed state. Credentials rotate on startup and are written to a private runtime manifest. Model-specific raw query/write operations require an explicit startup flag. The device manager is limited to device connection management, service settings and access information; measurements and primary-function selection live in the GNOME top bar. See [the agent guide](../AGENT_INSTRUMENT_GUIDE.md).

Drivers may expose `functions()` returning `MeasurementFunction` values, and `select_function(id)` for configurable functions. Capability metadata is independent of current readings so inactive modes remain visible. A mode selection is acknowledged to clients only after the driver confirms it. Fluke primary-display capabilities are implemented; secondary-display control remains excluded.


The GTK settings window groups equipment by transport (TMC, Serial, Network). Discovery is explicit per transport: TMC scan probes accessible USBTMC nodes; Serial and Network use structured connection dialogs without probing serial ports or LAN subnets. `edit_device` validates replacements before closing a live connection, saves the new endpoint, and attempts to reconnect; a failed attempt leaves the endpoint available for correction. `connect` with `reconnect=true` closes and replaces an existing transport. Service Start/Stop/Restart uses asynchronous `systemctl --user` calls and reports errors; D-Bus snapshot calls disable activation so stopping the service persists while the window polls.


Instrument identity and connection settings use separate schema-v2 stores. Supported drivers expose a transport-independent model/type; that and the IDN hardware serial derive a stable instrument ID. Names and measurement preferences are keyed by this ID. Resources map to that ID with separate enabled state and framing/address options. Driver functions validate both restored and copied settings. Copy requires an exact type match and deep-copies settings only. A new address identifying an already active instrument hands over acquisition to one transport while preserving its ID in logs. Endpoint aliases remain accepted by instrument commands. Profiles without usable serials stay endpoint-local. Legacy schema migration keeps endpoints and identifies profiles from saved IDN strings without relying on USB-adapter serial numbers.

The Settings dialog has Connection and Instrument tabs, and the three row actions use symbolic icons. Connect/Disconnect reflects the state of that specific connection. Forget requires an explicit connection/instrument choice; removing an inactive connection does not close another method, and removing an entire instrument also deletes all its addresses. Historical recordings are retained. Orphan profiles remain visible in Saved Instruments. No user name or measurement preference is stored on a port/path.


Top-bar selection uses `display_measurements` with an instrument ID and a `functions` list. The service validates all IDs and exclusive groups before changing hardware, persists `settings.display_functions`, and publishes completion through the normal command-result map. The Shell uses confirmed preferences for panel text and pending check states in the menu. Failed changes preserve the last successful display list. Passive functions can coexist; `MeasurementFunction.exclusive_group` identifies modes sharing a hardware measurement slot. Configurable functions without an explicit group default to the primary slot. Hiding never switches or disables hardware. Explicit function changes by agents keep an already visible exclusive group aligned, while a hidden group stays hidden. Settings-copy and transport-independent profile storage include the display list.

Each instrument has independent switch rows that suppress menu-closing activation. A single panel label concatenates the selected readings without a configured count limit; hiding all retains the GATE service/recording indicator. Known offline instruments with catalogs remain configurable in the menu. Missing/invalid readings are shown as unavailable, rather than reused under another function's units.
