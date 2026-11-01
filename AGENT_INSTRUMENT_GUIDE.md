# GATE instrument guide for agents

GATE (Gnome Automatic Test Equipment) is a background acquisition and control service for Ubuntu. Agents operate instruments through its authenticated local HTTP API. The GNOME top bar monitors readings and provides instrument → channel → function menus, display switches and range selection. The main window manages devices/settings, creates and edits experiments/tests, runs test sequences and displays results.

This guide is the entry point for every supported agent command. Detailed JSON
fields and driver limitations are defined in the linked [test specification](docs/test-spec.md),
[secondary observation contract](docs/secondary-measurements.md) and
[channel/range contract](docs/instrument-ranges.md). Inspect live capabilities;
an API feature does not imply every driver supports it.

## Discover the running service

The default listener is `http://127.0.0.1:8765`. Discover its actual URL and bearer token from:

- `$XDG_RUNTIME_DIR/gate/control.json`, normally `/run/user/1000/gate/control.json` on this machine.
- If `XDG_RUNTIME_DIR` is unset: `/tmp/gate-<uid>/gate/control.json`.

The manifest contains `api_version`, `url`, `token`, `pid`, and `allow_raw`. Its directory is private and the file is mode 0600. Do not print, commit, or include the token in reports. It rotates when the service restarts. The listener binds only to IPv4 loopback. Requests must use its localhost/127.0.0.1 host and a bearer token; browser Origin requests are rejected.

If the service is installed, start it with `systemctl --user start gate`. For development, from the repository run `python3 -m gate service --allow-raw`. Omit `--allow-raw` to restrict HTTP clients to typed driver operations. `--control-port PORT` changes the port; `--no-control-api` disables HTTP. `--ephemeral` disables saved device profiles/settings.

`service --demo` adds the original simulated instruments; `--interval SECONDS`
overrides the polling interval. `gate status` prints a snapshot and
`gate command '{"action":"scan","request":"agent-scan-001"}'` queues a command
through the session D-Bus service rather than HTTP. CLI command completion only
acknowledges queueing (`{"queued": true}`); supply your own unique `request` ID
and inspect that key in the snapshot's `commands` map. These local
D-Bus commands remain available when HTTP is disabled. `gate gui` opens the main
window; `gate --version` prints the installed version.

## Use the supplied Python client

The client needs only Python's standard library. Run from the repository or install the GATE package into your environment.

```python
from gate.client import GateClient

api = GateClient()  # reads the local manifest; does not print credentials
health = api.request('/v1/health')
state = api.request('/v1/state')
devices = api.request('/v1/instruments')['instruments']
for device in devices:
    print(device['id'], device['name'], device['category'])
    print(device['functions'])
    print(device.get('channels', []))
    print(device.get('range_controls', []))
    print(device['readings'])
```

`api.submit(action, **args)` returns immediately with an operation ID and status URL. `api.command(action, **args)` waits for completion and raises on failure. Its default wait is 30 seconds; a client timeout does **not** cancel an already queued operation.

Command completion and acquisition publication are separate: new readings and
some newly added instrument cards appear on the next polling cycle. Poll for the
expected instrument/function rather than assuming the next snapshot contains it.

## HTTP contract

Every request requires `Authorization: Bearer <token>`. POST bodies require `Content-Type: application/json` and a Content-Length of at most 1048576 bytes (1 MiB, to accommodate declarative experiment definitions). There is no CORS or anonymous API.

| Endpoint | Purpose |
|---|---|
| `GET /v1/health` | API/application versions, raw-command setting, supported actions |
| `GET /v1/state` | Complete cached acquisition state, command results, settings and recording status |
| `GET /v1/experiment-runs` | Current/latest run, progress, retained results and output paths |
| `GET /v1/experiments` | Saved model-independent equipment/DUT arrangements and test descriptions |
| `GET /v1/instruments` | Instrument identities, readings and driver capabilities |
| `GET /v1/instruments/<encoded-id>` | One instrument; percent-encode the entire ID including slashes |
| `POST /v1/commands` | Submit an action; returns HTTP 202 and `{id,state,status_url}` |
| `GET /v1/commands/<id>` | Poll queued/running/succeeded/failed operation state |

For example, submit `{"action":"select_function","instrument":"<exact ID from discovery>","function":"OHMS"}`. A 202 acknowledges queueing, **not hardware success**. Poll the returned status URL until `succeeded` or `failed`. Terminal results include `finished_utc_ns` and either `result` or `error`. Only the latest 100 command results are retained in memory; restart clears them.

Missing/wrong tokens return 401. Browser/foreign-host requests or disabled raw operations return 403. Malformed actions/JSON return 400; wrong content type returns 415; oversized bodies return 413. A well-formed action that fails on the device returns a **failed command result**, even though submission returned 202.

### Command index

The following covers every action in the current service. Use `/v1/health` to
check the actions available in the running version; raw actions are omitted
when disabled. Examples and constraints follow below or in the linked contracts.

| Actions | Principal arguments / purpose |
|---|---|
| `scan`, `connect`, `edit_device` | Discover supported USB devices; connect using `resource`; edit an existing connection using `instrument` and replacement `resource` |
| `disconnect`, `forget` | `instrument`; retain a connection with `forget=False`, or explicitly remove `scope='connection'` / `'instrument'` |
| `instrument_settings`, `copy_settings` | `instrument`, optional `name`/`settings`; copy from `source` of the same driver type |
| `select_function`, `display_measurements` | `instrument` and `function`, or a `functions` list; the latter accepts optional `ranges` preferences |
| `select_range` | `instrument`, range `control` ID and `range` choice ID |
| `settings` | Service-wide polling `interval` in seconds, 0.1–60 |
| `demo`, `demo_experiment` | Add the original demo instruments or the coupled converter/observation bench; do not enable outputs |
| `record`, `stop_recording` | Start ordinary all-device logging at `path`, or stop it |
| `source` | AOIP calibrators: `instrument`, `quantity` (`voltage`/`current`, PJ also `resistance`), SI `value`, explicit `range` from cached `source_controls`; requires standby, never enables output |
| `static`, `sweep`, `output`, `cancel_sweep` | Demo PSU/RD6006: `instrument`, `voltage`, optional `current_limit` on supported drivers; sweep `start`/`stop`/`step`/`dwell`; output `enabled` (also AOIP calibrators), real enable requires `wiring_confirmed=true` |
| `query`, `write` | `instrument` and one model-specific `command`; requires HTTP raw access enabled |
| `save_experiment`, `delete_experiment`, `check_experiment` | Save a definition object as `experiment`; delete/check by experiment ID, with `bindings` for checking |
| `start_experiment`, `start_experiment_sequence`, `cancel_experiment` | Start a `test` with `parameters`, or an ordered `tests` list; both use `experiment`/`bindings` and explicit output/wiring confirmations where required; cancel stops the active sequence as well as its test |

## Inspect before acting

Read the current state, identify the exact instrument, and use its advertised `functions` rather than guessing a model's command dialect. Each function includes:

- `id`: driver function identifier used by `select_function`.
- `label`: human-readable name.
- `channel`: reading channel to observe after selection.
- `unit`: normal physical unit.
- `configurable`: true if selecting it changes the instrument's measurement function.
- `instrument_channel`: owning physical/logical channel; distinct from the reading key in `channel`.
- `exclusive_group`: functions sharing this group **on the same instrument channel** cannot be selected together.

`channels` contains each instrument channel's ID, label and functions; the flat
`functions` catalog remains available. Function IDs are unique within an
instrument even when functions share a reading key. Different channels may
have different function sets. `current_functions` maps channel IDs to active
function IDs. `current_function` remains the compatibility field for existing
single-channel meters; it is not a complete multi-channel state description.

A driver advertises only implemented capabilities. The scope currently exposes C1/C2 peak-to-peak voltage, RMS voltage and frequency as read-only scalar channels. The Fluke 45 exposes ten configurable **primary-display** functions. Secondary-display control is intentionally outside this workflow; any existing secondary readings are passive.

Record the original function/settings/output state before making changes. Stay within the user's requested task and the equipment's limits. Never infer that an output is disabled from a zero reading. Calibration, resets and output enabling require a corresponding user request; avoid issuing them merely to discover a device.

### Previously verified bench

Use the returned `id` for instrument commands and `resource` for connection commands. Identified hardware has a stable `instrument:…` ID derived from driver type/model and serial number; the resource may change across ports or transport methods. Endpoint aliases still resolve to their identified instrument. Drivers without usable serial numbers retain connection-local IDs.

| Instrument | Verified transport | Notes |
|---|---|---|
| Siglent SDS1202X-E, firmware 1.3.26 | Linux USBTMC, previously `/dev/usbtmc3` | Scalar readings on C1/C2; raw SCPI can configure other documented settings |
| Fluke 45, firmware 1.7 / display D1.0 | `serial:///dev/serial/by-id/usb-1a86_USB2.0-Ser_-if00-port0?baud=9600` | 9600 baud, 8N1, command echo and completion prompts handled by GATE |

An earlier bench check connected the Fluke input to the scope's nominal **1 kHz square-wave probe compensation output**. This is historical verification, not a live wiring assertion. Confirm the current arrangement before choosing a function. Voltage and frequency are suitable checks for that connection. Resistance, continuity and diode measurements require a suitable unpowered test object; current measurements require the correct input terminals and circuit arrangement. Do not automatically cycle all modes on the connected calibration signal.

Demo devices have `simulated: true` and IDs beginning `demo:`. Keep simulated and real data distinct in reports.

AOIP SN-8310/PJ-6301 USB RS-232 support is protocol-tested; hardware verification
is pending. Use `aoip:///dev/serial/by-id/…?baud=9600`, with framing/flow and
`idle` options matched to the unit. The default 0.2-second quiet interval completes
replies even without CR/LF, within a 3-second transaction deadline. See the
[AOIP setup, channel and source-control contract](docs/aoip.md).
PJ `measure.*` input readings are independent from `source.*` setpoints; the SN
exposes source only because its manual provides no independent measurement query.
`source_controls` maps quantity/range IDs to `[min, max, resolution]` in SI units.
Use `source` in standby, then separate `output(enabled=true,
wiring_confirmed=true)`. Enable requires a freshly configured, confirmed source
in this connection; reconnect never restores it. STBY is zero output, not
isolation. Generic PSU sweeps/compliance and experiment output controls are not
implemented for these calibrators. PJ TXP loop supply selection is excluded.

## Connect and manage instruments

```python
api.command('scan')  # accessible USBTMC nodes and recognized Hantek USB meters; no arbitrary serial probing
api.command('connect', resource='serial:///dev/ttyUSB0?baud=9600')
api.command('connect', resource='aoip:///dev/ttyUSB1?baud=9600')  # AOIP via USB RS-232 adapter
api.command('connect', resource='tcp://192.168.1.100:5025')
api.command('connect', resource='hantek://usb/3-3.1.3')  # use the actual discovered USB port
```

USB serial adapters identify the adapter, not the meter behind it, so add serial devices by explicit address. Schema version 2 in `$XDG_CONFIG_HOME/gate/devices.json` separates `connections` (resource, linked instrument, enabled flag) from `instrument_profiles` (type, hardware serial, friendly name, settings and supported functions). Legacy address-based profiles migrate automatically. `/v1/state` includes both collections, including disconnected instruments. TMC paths can change after replugging; Scan or add/edit the new path. After identification, an existing model/type + serial reuses its profile. For hardware without a usable serial, profiles stay connection-local rather than merging unrelated devices.

```python
state = api.request('/v1/state')
device = state['instruments'][0]
device_id, resource = device['id'], device['resource']
api.command('disconnect', instrument=device_id, forget=False)  # retain connection and instrument settings
api.command('connect', resource=resource)  # reuses settings after identification
api.command('connect', resource=resource, reconnect=True)  # force reopen even if connected
api.command('edit_device', instrument=resource,
            resource='serial:///dev/ttyUSB0?baud=4800')  # edit just this connection
api.command('instrument_settings', instrument=device_id, name='Bench reference',
            settings={'function': 'FREQ'})  # choose a function valid for the driver
api.command('display_measurements', instrument=device_id, functions=['FREQ'])  # checked top-bar readings
api.command('display_measurements', instrument=device_id, functions=[])  # hide every reading; no hardware switch
api.command('copy_settings', instrument=target_id, source=source_id)  # exact same type/model only
api.command('forget', instrument=device_id, resource=resource, scope='connection')
api.command('forget', instrument=device_id, scope='instrument')
api.command('settings', interval=1.0)  # service-wide polling interval; stop recording first
```

`instrument_settings.settings` accepts these keys:

| Key | Meaning |
|---|---|
| `function` | Legacy single-function preference; value is a driver function ID |
| `channel_functions` | Map of instrument channel IDs to their chosen function IDs |
| `display_functions` | List of zero or more function IDs checked in the top bar |
| `ranges` | Map of driver range-control IDs to driver range-choice IDs |

`settings` replaces the saved settings object; merge with the current settings
when editing only one preference. Omitting `settings` preserves it; `{}` clears
saved preferences without resetting hardware. An explicit empty
`display_functions` list keeps all readings hidden. Legacy `function` and
`channel_functions` must not conflict on the same channel.

Prefer `display_measurements` for changing visibility: it validates coexistence,
confirms required mode changes and saves only on success. Functions with the same
non-null `exclusive_group` **and the same `instrument_channel`** exclude one
another. Configurable functions without a group default to that channel's primary
group; passive scope measurements can coexist. Unchecking/hiding leaves hardware
functions, ranges and logging unchanged. The action accepts an optional `ranges`
map, validated and applied after function changes. The top bar uses this to select
Auto when a function without a saved range is newly shown. Plain agent
`display_measurements` calls do not implicitly request Auto; include the range
preference explicitly if needed.

Preferences persist across restarts and connection methods and can be edited for
saved offline instruments using their last known catalogs. Older profiles without
display preferences fall back to saved/current functions or an available reading;
multi-channel instruments can show their known active functions together.
Names may be blank (use model identity) or up to 80 printable characters. Copying
copies function, range and display preferences, never names, serials or connections;
source and target must have the same driver type/model. Connected instruments
apply saved functions before their applicable ranges; reconnect restores them.
Output enable state, arbitrary raw commands and sweeps are never replayed.
Passive front-panel reads do not overwrite preferences. Failed configuration
preserves saved preferences, but earlier hardware writes in a multi-step change
are not rolled back transactionally.

Connecting the same physical instrument through another method replaces its active transport, keeping its stable ID and shared logging series; GATE does not poll the same instrument twice. Previously used addresses remain saved but inactive. Deliberately disconnected instruments stay disconnected across service restarts. An inactive address can be removed without disrupting another active method. Removing the last connection keeps a saved instrument profile available for renaming/copying; forgetting the whole instrument deletes its profile and all addresses. Recordings are retained. The legacy `disconnect` default still removes just the selected connection; specify `forget=False` for a plain disconnect, and use `forget` with an explicit scope for removal.

A failed add or connection edit keeps the new endpoint for correction/retry and returns a failed command result. Invalid or duplicate connection edits leave the existing transport intact. If a different physical instrument is discovered at a reused address, it receives its own profile and never inherits settings from the previous serial number.

The interval is a requested host polling interval, not a guarantee of sample rate or simultaneous acquisition. Inspect actual timestamps. Hardware may take longer, especially immediately after a function change. An unresponsive device is removed from acquisition; reconnect explicitly after correcting access, cable or settings problems.

## Channel functions and ranges

A driver can expose any number of channels with different function catalogs.
Use `select_function` with the instrument-wide function ID; its declared
`instrument_channel` determines the affected channel. To save several channel
choices together, merge a `channel_functions` map into `instrument_settings`.
This does not imply that an SMU hardware driver is currently installed.

Each `range_controls` entry has `id`, `instrument_channel`, an optional `function`,
`label`, `unit` and `options`. Each option has an `id`, `label`, optional nominal
numeric `value` and an `automatic` flag. Treat IDs as opaque: a numeric-looking
ID is not a command or a physical limit. Range values are nominal full scales
or scales such as V/div, not equipment safety ratings. Some ranges are
rate-dependent and have no single numeric value.

`select_range` requires a connected instrument. For a function-specific control,
select the matching function on that channel first and wait for confirmation.
It changes neither current terminals nor output enable state. Independent
controls, such as the scope's C1/C2 input scales, belong to their channel and
affect all functions using that input. The scope declares input scale before
probe attenuation; its driver accounts for the existing probe factor. Do not
confuse these controls with scalar frequency ranges.

This complete example uses only the original **simulated** multimeter:

```python
import time

api.command('demo')
deadline = time.monotonic() + 5
meter = None
while meter is None and time.monotonic() < deadline:
    meter = next((d for d in api.request('/v1/instruments')['instruments']
                  if d['id'] == 'demo:multimeter'), None)
    if meter is None:
        time.sleep(.1)
if meter is None:
    raise TimeoutError('Demo meter has not appeared in acquisition state')
control = next(c for c in meter['range_controls'] if c['id'] == 'voltage_dc')
automatic = next(o for o in control['options'] if o['automatic'])
fixed = next(o for o in control['options'] if o['value'] == 10)
api.command('select_range', instrument=meter['id'], control=control['id'], range=fixed['id'])
api.command('select_range', instrument=meter['id'], control=control['id'], range=automatic['id'])
```

Successful selection saves `settings.ranges`. Reconnect restores independent
controls and the ranges belonging to each channel's active function; inactive
function preferences wait until that function is selected through GATE.
Opening/refreshing the top bar sends no configuration commands. **Auto (default)**
there is a preference, not a queried hardware-range readback. A saved manual
choice takes precedence. **Keep instrument range** in Device Settings removes a
saved override without writing a replacement range. Range changes are recording
events and are blocked during active experiments or sequences.

Keysight meters expose model-specific DC/AC V/A, resistance and capacitance
ranges; 10 A choices require the meter's 10 A terminal already selected. Fluke
range codes show slow versus medium/fast full scales without changing reading
rate. Hantek manual ranging is explicitly unavailable pending USB protocol
verification. An empty catalog is not an Auto capability. See the
[driver range table and verification status](docs/instrument-ranges.md).

## Select a Fluke primary measurement function

```python
meter = next(d for d in api.request('/v1/instruments')['instruments']
             if d['name'].startswith('FLUKE') and not d['simulated'])
meter_id = meter['id']

# Suitable for the currently connected nominal 1 kHz source:
api.command('select_function', instrument=meter_id, function='FREQ')
```

| Function ID | Measurement | Unit |
|---|---|---|
| `VDC` | DC voltage | V |
| `VAC` | AC voltage | V |
| `VACDC` | AC + DC RMS voltage | V |
| `ADC` | DC current | A |
| `AAC` | AC current | A |
| `AACDC` | AC + DC RMS current | A |
| `OHMS` | Resistance | Ω |
| `FREQ` | Frequency | Hz |
| `DIODE` | Diode test | V |
| `CONT` | Continuity test | V |

Selecting sends the driver's fixed mnemonic and checks `FUNC1?` before reporting success. The function command does not set trigger mode, range or calibration, or expose secondary-display controls. After function confirmation, GATE reapplies an explicitly saved range for that function, if present. The meter's own function-change behavior may clear modifiers or its secondary display. A new conversion can take longer after a mode change; the first read gets a bounded longer timeout.

After success, poll instrument state until `current_function` and the reading channel match the target. Do not relabel an old voltage sample as a new resistance reading. After the requested task, restore the original function if appropriate.

## Reading data and quality

A reading is `{channel,value,unit,quality}`. Trust the reported unit; do not assume that a numeric exponent or displayed prefix implies a different unit. Modifiers may change the unit to dB or W and are represented in the channel name.

Use a numeric value only when `quality == 'ok'` and `value` is not null. Other qualities include `unavailable`, `disabled`, `overload`, `underload`, `mode_changed`, `error`, `not_ready`, `out_of_range`, `relative` and `unsupported_configuration`. This is not an exhaustive enum: retain the driver's quality flag. In particular, scope frequency may be unavailable without a usable signal, and open resistance inputs may report overload. Neither means zero. A Hantek relative reading can retain a numeric value; it must not be consumed as an absolute measurement. Waveform quality is separate and can report clipping.

`updated_utc_ns` describes the cached state's freshness. Every logging cycle has a shared tick; each device has its own acquisition duration. During a slow instrument operation the cached state may temporarily become old. HTTP inspection reads cached state; it does not race direct I/O against acquisition. Commands and reads run on one acquisition owner. Queued commands wake that owner independently of the sampling interval, although an in-progress instrument transaction must finish first.

## Record measurements on one timebase

```python
from pathlib import Path
recording = str(Path.home() / '.local/share/gate/recordings/agent-session.sqlite')
api.command('record', path=recording)
# Perform the authorized experiment and inspect progress.
api.command('stop_recording')
```

All connected instruments join the same recording, including output-device settings, enable state and readback when supported. Typed function changes, raw operations and sweep steps are timestamped events. Failed function changes are reported to the caller and do not count as successful changes.

SQLite stores `sessions`, `samples`, and `events`. Each session maps a monotonic origin to UTC; samples contain a shared `tick_ns` plus per-instrument batch `read_start_ns` and `read_end_ns`. Channels in a batch are queried sequentially. This is host time alignment, **not hardware-synchronized sampling**. Changing acquisition interval while recording is rejected so the session's declared interval remains valid.

Export locally after stopping:

```bash
gate export /absolute/path/agent-session.sqlite /absolute/path/agent-session.csv
```

CSV includes samples and derived UTC tick times. Event details remain in SQLite. Do not compare a simulated PSU value to a real meter reading as if it were the same physical circuit.

## Output controls

Typed static/sweep/output control supports the demo PSU and [Riden RD6006 over USB](docs/riden.md). Inspect `output_control_supported`, `experiment_limits` and current readings before issuing an output request. RD6006 USB addresses use `riden:///dev/ttyUSB1?baud=115200&address=1`; add manually under USB, since Scan does not probe serial adapters.

For RD6006, `static` accepts `voltage` and optional `current_limit` (amperes). Real output enable requires `enabled=true, wiring_confirmed=true`; disable needs only `enabled=false`. Driver bounds are 0–60 V and 0.001–6 A current limit, with confirmed register writes. Keep DUT limits separate. The RD6006 supports typed experiment voltage/current-limit controls; other Riden models and raw SCPI are unsupported. No enable state is replayed, and GATE attempts a verified disable on close for outputs it enabled.

```python
api.command('demo')
api.command('static', instrument='demo:power_supply', voltage=3.3)
api.command('output', instrument='demo:power_supply', enabled=True)
api.command('sweep', instrument='demo:power_supply', start=0, stop=5, step=0.5, dwell=1)
api.command('cancel_sweep', instrument='demo:power_supply')
api.command('output', instrument='demo:power_supply', enabled=False)
```

A static setpoint or sweep never enables an output by itself. Demo limits are 0–30 V. Step must point toward the endpoint; dwell must be at least the polling interval. Cancelling holds the current setpoint; disabling also cancels a sweep. No startup output commands are replayed.

## Advanced instrument commands

Check `GET /v1/health` for `allow_raw`. If enabled at startup with `--allow-raw`, agents can send a single documented command through an attached instrument's own transport:

```python
result = api.command('query', instrument=meter_id, command='*IDN?')
print(result['result']['response'])

# Scope example: inspection only. Get the scope's current ID from discovery.
result = api.command('query', instrument=scope_id, command='C1:VDIV?')
print(result['result']['response'])
```

`write` accepts a documented non-query command, e.g. `{"action":"write","instrument":"<scope-id>","command":"STOP"}` to stop acquisition **when requested by the user**. A write success means the transport accepted it (or the Fluke emitted a success prompt); use a documented query to verify the intended hardware state. Prefer typed operations where available because they validate supported functions and confirm the result.

Commands are limited to one line, 256 characters, no semicolon batches. `query` must contain `?`; `write` must not. Fluke writes still consume their completion prompt. SCPI writes do not attempt to read a nonexistent reply. Responses and failures are returned through command status. Consult the exact model's programming manual; do not guess commands from another manufacturer's dialect.

## Experiments

Use `save_experiment`, `delete_experiment` and `check_experiment` to manage
definitions and check role bindings. Definitions contain `id`, `name`,
`description`, `dut`, `roles`, `connections` and `tests`. Tests define numeric
parameters, sweep axes, typed setup/per-point controls, acquisition, scalar
measurements, formulas, checks, early stopping, grouped reductions and output
presentation. Saving/checking definitions does not configure or energise hardware.
`check_experiment` checks role capabilities; starting a run additionally validates
the complete parameterised plan and applicable driver limits.

`start_experiment` takes an experiment ID, `test`, `bindings` and `parameters`.
Output tests also require `enable_outputs=True` and `wiring_confirmed=True`.
Command success acknowledges the **start**, not completion of the test. Poll
`/v1/experiment-runs`: `active` is the current/latest run, `runs` holds the latest
20 finished runs and `sequence` is the current/latest sequence. The equivalent
snapshot keys are `experiment_run`, `experiment_runs` and `experiment_sequence`.
Use `cancel_experiment` to stop a run or sequence.

### Ordered test sequences

`start_experiment_sequence` takes the same experiment, bindings and output
confirmations plus an ordered `tests` list. Each entry is exactly
`{'test': '<test-id>', 'parameters': {...}}`. Choose 1–20 distinct tests from that
experiment. Every plan is preflighted before output changes. A test failure,
cancellation, connection change or graceful service shutdown stops the sequence;
pending tests are skipped. Outputs are disabled between tests. Sequences continue
when the window closes, but do not resume after service restart.

This example exercises the **coupled simulated** converter, not real outputs:

```python
import time

api.command('demo_experiment')
parameters = {
    'input_start_v': 12., 'input_stop_v': 12.,
    'load_start_a': .1, 'load_stop_a': .1,
    'settle_s': api.request('/v1/state')['interval_s'],
    'samples_per_point': 1,
}
started = api.command('start_experiment_sequence', experiment='buck_converter',
    bindings={'supply': 'demo:converter_supply', 'load': 'demo:electronic_load'},
    tests=[{'test': 'efficiency', 'parameters': parameters},
           {'test': 'maximum_output_current', 'parameters': parameters}],
    enable_outputs=True, wiring_confirmed=True)
sequence_id = started['result']['id']
while True:
    sequence = api.request('/v1/experiment-runs')['sequence']
    if sequence['id'] != sequence_id:
        raise RuntimeError('The active sequence changed; inspect service state')
    if sequence['state'] != 'running':
        break
    time.sleep(.2)
print(sequence['state'], sequence['completed_tests'], sequence['tests'])
```

Inspect `sequence.state`, `tests`, zero-based `index` and `completed_tests`.
Each test entry includes its state, run ID and any error. Executed tests retain
separate results with a shared sequence ID and position. Terminal sequence states
include `completed`, `failed` and `cancelled`; pending entries become `skipped`
after an unsuccessful stop. Startup reports unfinished run artifacts as
`interrupted`; it does not automatically recover physical output state.

### Results and configuration boundaries

Run artifacts live under `$XDG_DATA_HOME/gate/experiments/<run-id>` (normally
`~/.local/share/gate/experiments`). Metadata provides their actual paths:
`run.json`, `run.sqlite`, `points.csv`, `summary.csv`, `report.html` and configured
`plot-*.svg` files. The JSON/SQLite retain the definition, resolved test JSON,
parameters, equipment identities and results. The UI previews the first 20 table
rows; reports/CSV retain all rows. Historical artifacts remain on disk beyond the
20-run list. Ordinary `record` logging can run alongside an experiment.

Configuration/function/range/raw/output-control changes are rejected during a
run or sequence. Disconnect/forget/connection editing first cancels the active
execution. Changing saved definitions does not mutate a running snapshot. Output
tests attempt verified output disable on completion, failure, cancellation and
graceful shutdown; communications loss, forced termination or power failure can
prevent it. RD6006 typed PSU controls are protocol-tested and low-voltage bench-verified; real electronic-load drivers remain unavailable. See [RD6006 verification status](docs/riden.md).

Existing experiment JSON needs no migration for the channel/range interface:
its scalar `channel` still identifies a **reading key**, not an instrument channel
ID, and `quantity` still resolves through the driver's `experiment_channels`.
However, schema-v1 tests do **not** declare/apply meter functions or measurement
ranges. Configure them before starting; top-bar Auto preferences are not an
experiment configuration policy. Run metadata does not yet capture a complete
applied-function/range snapshot. Mandatory roles require distinct instrument IDs,
so an SMU's source/measurement channels cannot currently fill two mandatory roles
of one experiment. Optional observation roles can reuse an instrument. Channel
targeting for output controls is also future work; current controls address an
instrument role. See [experiment execution and limits](docs/experiments.md).

Tests are schema-v1 JSON objects, not fixed Python procedure types. See [the generic test specification](docs/test-spec.md) for sweeps, expressions, reductions, checks and rendering. `experiment_spec_version: 1` in the snapshot advertises support. Legacy definitions migrate on load/save; each run retains its resolved test JSON. The HTTP request limit is 1 MiB to accommodate larger definitions.

Secondary measurements use optional roles (`optional: true`, empty terminals permitted), `secondary_measurements` for scalar probes, and `waveforms` for settled/transition captures. Snapshots advertise `secondary_measurements_version: 1` and driver `waveform_capabilities`. Missing optional probes preserve primary test validity. Mark observations required for preflight/fail-closed behavior. Waveform data/CSV/SVG references and scalar statistics appear in run metadata; raw samples live in the run database. See [secondary measurement semantics and hardware verification status](docs/secondary-measurements.md).

Scope channel/single-shot behaviour: scalar polling never measures off traces. Runs temporarily select the union of scalar and waveform channels and restore trace states afterward. SDS1202X-E transition captures use the current timebase/pre-trigger position (`window_s`/`pretrigger` null), program/verify the edge trigger, wait for readiness, and require a fresh acquisition with STOP before waveform and BMP screen transfer. Screens are saved by default for transition captures (`screenshot: false` opts out), displayed in Results and linked in reports. Trigger settings restore on capture cleanup. These additions are protocol-fixture tested and await hardware verification. See [secondary measurements](docs/secondary-measurements.md).

Hantek 365 direct USB: use USB Scan or `hantek://usb/PORT` (for example `hantek://usb/3-3.1.3`). It is a binary vendor interface, never SCPI or serial. The 365B thermocouple path and Celsius/Fahrenheit switching are hardware checked; other ranges/models are protocol-tested. Select TEMPC/TEMPF through instrument settings. Celsius logical quantities `temperature`/`dut_temperature` map to the single `temperature` channel; there is no fabricated ambient probe. USB serial `Y` is not unique, so identity remains port-local. See [Hantek 365](docs/hantek365.md) for permissions, settling and quality flags.

Keysight 344xxA bench DMMs: the 34401A/10A/11A/60A/61A/65A/70A driver supports USBTMC and raw TCP, model-specific primary functions and scalar acquisition. Inspect the advertised catalog. Polling requires immediate triggering, one sample and one trigger, with math/scaling/null disabled; incompatible settings return `unsupported_configuration` without reconfiguring hardware. `READ?` starts fresh acquisition and may clear meter reading memory/statistics. Model and serial provide stable identities across Keysight/Agilent/HP branding. Software protocol checks pass; physical hardware verification is pending. See [Keysight setup and acquisition semantics](docs/keysight344xxa.md).

## References and detailed contracts

- [Channel ownership, range choices and driver support](docs/instrument-ranges.md).
- [Experiment definitions, execution, sequences and results](docs/experiments.md).
- [Declarative JSON fields, expressions, reductions, checks and rendering](docs/test-spec.md), with the [structural JSON Schema](gate/templates/test-v1.schema.json).
- [Secondary scalars, temperatures, waveforms and screenshots](docs/secondary-measurements.md).
- [Keysight setup](docs/keysight344xxa.md), [Fluke serial/quality notes](docs/fluke45.md) and [Hantek USB/probe notes](docs/hantek365.md).
- [Screenshotted voltage-sweep walkthrough](docs/voltage-sweep-guide.md).
- [Architecture](docs/architecture.md) and [installation/migration](packaging/INSTALL.md).
- [Fluke 45 official manual](https://media.fluke.com/d295e628-9a14-4bb4-9784-b10800c1cf0b_original%20file.pdf), chapter 5, and [Siglent programming resources](https://www.siglent.com/int/products-overview/sds1000x-e/).
