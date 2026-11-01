# GATE instrument guide for agents

Instruments publish `channels`, channel-owned `functions`, `current_functions`
and driver-declared `range_controls`. Use `select_range` with catalog IDs for
manual range selection; `instrument_settings` accepts `channel_functions` and
`ranges` for persistent per-channel choices. See the [channel/range contract and
driver support](docs/instrument-ranges.md). Range controls are not output safety
ratings and do not enable outputs.

GATE (Gnome Automatic Test Equipment) is a background acquisition and control service for Ubuntu. Agents operate instruments through its authenticated local HTTP API. The human-facing GNOME top bar monitors readings and selects measurement functions; the main window only manages devices and GATE settings.

## Discover the running service

The default listener is `http://127.0.0.1:8765`. Discover its actual URL and bearer token from:

- `$XDG_RUNTIME_DIR/gate/control.json`, normally `/run/user/1000/gate/control.json` on this machine.
- If `XDG_RUNTIME_DIR` is unset: `/tmp/gate-<uid>/gate/control.json`.

The manifest contains `api_version`, `url`, `token`, `pid`, and `allow_raw`. Its directory is private and the file is mode 0600. Do not print, commit, or include the token in reports. It rotates when the service restarts. The listener binds only to IPv4 loopback. Requests must use its localhost/127.0.0.1 host and a bearer token; browser Origin requests are rejected.

If the service is installed, start it with `systemctl --user start gate`. For development, from the repository run `python3 -m gate service --allow-raw`. Omit `--allow-raw` to restrict HTTP clients to typed driver operations. `--control-port PORT` changes the port; `--no-control-api` disables HTTP. `--ephemeral` disables saved device profiles/settings.

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
    print(device['readings'])
```

`api.submit(action, **args)` returns immediately with an operation ID and status URL. `api.command(action, **args)` waits for completion and raises on failure. Its default wait is 30 seconds; a client timeout does **not** cancel an already queued operation.

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

## Inspect before acting

Read the current state, identify the exact instrument, and use its advertised `functions` rather than guessing a model's command dialect. Each function includes:

- `id`: driver function identifier used by `select_function`.
- `label`: human-readable name.
- `channel`: reading channel to observe after selection.
- `unit`: normal physical unit.
- `configurable`: true if selecting it changes the instrument's measurement function.

A driver advertises only implemented capabilities. The scope currently exposes C1/C2 peak-to-peak voltage, RMS voltage and frequency as read-only scalar channels. The Fluke 45 exposes ten configurable **primary-display** functions. Secondary-display control is intentionally outside this workflow; any existing secondary readings are passive.

Record the original function/settings/output state before making changes. Stay within the user's requested task and the equipment's limits. Never infer that an output is disabled from a zero reading. Calibration, resets and output enabling require a corresponding user request; avoid issuing them merely to discover a device.

### Current bench

Use the returned `id` for instrument commands and `resource` for connection commands. Identified hardware has a stable `instrument:…` ID derived from driver type/model and serial number; the resource may change across ports or transport methods. Endpoint aliases still resolve to their identified instrument. Drivers without usable serial numbers retain connection-local IDs.

| Instrument | Verified transport | Notes |
|---|---|---|
| Siglent SDS1202X-E, firmware 1.3.26 | Linux USBTMC, previously `/dev/usbtmc3` | Scalar readings on C1/C2; raw SCPI can configure other documented settings |
| Fluke 45, firmware 1.7 / display D1.0 | `serial:///dev/serial/by-id/usb-1a86_USB2.0-Ser_-if00-port0?baud=9600` | 9600 baud, 8N1, command echo and completion prompts handled by GATE |

The user has connected the Fluke input to the scope's nominal **1 kHz square-wave probe compensation output**. Voltage and frequency are suitable checks for that connection. Resistance, continuity and diode measurements require a suitable unpowered test object; current measurements require the correct input terminals and circuit arrangement. Do not automatically cycle all modes on the connected calibration signal.

Demo devices have `simulated: true` and IDs beginning `demo:`. Keep simulated and real data distinct in reports.

## Connect and manage instruments

```python
api.command('scan')  # accessible Linux USBTMC nodes; does not probe arbitrary serial ports
api.command('connect', resource='serial:///dev/ttyUSB0?baud=9600')
api.command('connect', resource='tcp://192.168.1.100:5025')
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

`instrument_settings.settings` supports `function` and `display_functions`, using function IDs from the instrument catalog. `display_functions` is a list of zero or more checked top-bar measurements; an explicit empty list keeps all readings hidden. Prefer `display_measurements` for updating that list: it validates coexistence, confirms any required mode change, and saves only on success. Functions with the same non-null `exclusive_group` cannot be displayed together. Configurable functions without a group are treated as the primary exclusive group for compatibility; passive scope measurements can coexist. Unchecking/hiding leaves the current hardware function and logging unchanged. Choices persist across restarts and connection methods, and can be changed for a saved offline instrument. Existing profiles without this field initially show one reading per instrument. Omitting `settings` preserves existing settings; `{}` clears the stored preferences and returns to the initial display choice. Names can be blank (use model identity) or up to 80 printable characters. Copying copies measurement and top-bar display preferences only, never names, serials or connections. Source and target must have the same exact driver type/model. Saved functions apply immediately if connected and are restored on subsequent connections, including a different transport; output enable state, arbitrary raw commands and sweeps are never saved/replayed. Only explicit function selections are saved; passive front-panel reads do not overwrite preferences.

Connecting the same physical instrument through another method replaces its active transport, keeping its stable ID and shared logging series; GATE does not poll the same instrument twice. Previously used addresses remain saved but inactive. Deliberately disconnected instruments stay disconnected across service restarts. An inactive address can be removed without disrupting another active method. Removing the last connection keeps a saved instrument profile available for renaming/copying; forgetting the whole instrument deletes its profile and all addresses. Recordings are retained. The legacy `disconnect` default still removes just the selected connection; specify `forget=False` for a plain disconnect, and use `forget` with an explicit scope for removal.

A failed add or connection edit keeps the new endpoint for correction/retry and returns a failed command result. Invalid or duplicate connection edits leave the existing transport intact. If a different physical instrument is discovered at a reused address, it receives its own profile and never inherits settings from the previous serial number.

The interval is a requested host polling interval, not a guarantee of sample rate or simultaneous acquisition. Inspect actual timestamps. Hardware may take longer, especially immediately after a function change. An unresponsive device is removed from acquisition; reconnect explicitly after correcting access, cable or settings problems.

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

Use a numeric value only when `quality == 'ok'` and `value` is not null. Other qualities include `unavailable`, `disabled`, `overload`, `underload`, `mode_changed` and `error`. In particular, scope frequency may be unavailable without a usable signal, and open resistance inputs may report overload. Neither means zero.

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

Typed static/sweep/output control currently supports the demo PSU only; no real PSU driver is installed. Inspect capabilities and support before issuing an output request.

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

## References

- [Fluke 45 official user manual](https://media.fluke.com/d295e628-9a14-4bb4-9784-b10800c1cf0b_original%20file.pdf), chapter 5: function mnemonics, serial prompts, modifiers and measurement queries.
- [Siglent SDS1000X-E programming resources](https://www.siglent.com/int/products-overview/sds1000x-e/).
- [GATE architecture](docs/architecture.md) and [Fluke setup notes](docs/fluke45.md).

## Experiments

Use `save_experiment`, `delete_experiment`, and `check_experiment` to manage arrangements and check role bindings. Definitions include the DUT and its wiring, functional equipment requirements, and named test descriptions. Checks never configure hardware or energise outputs. `demo_experiment` adds a coupled simulated converter bench. `start_experiment` starts an asynchronous test with bindings and parameters; converter sweeps require explicit `enable_outputs=true` and `wiring_confirmed=true`. Poll `/v1/experiment-runs` for run completion, independently of command completion, and use `cancel_experiment` to stop. Each procedure defines its automatic metrics and presentation; results include SQLite, CSV, SVG and HTML artifacts. Output sweeps disable both ends at completion or cancellation; real PSU/load drivers are still pending. See [the experiment contract and buck converter example](docs/experiments.md).

Tests are schema-v1 JSON objects, not fixed Python procedure types. See [the generic test specification](docs/test-spec.md) for sweeps, expressions, reductions, checks and rendering. `experiment_spec_version: 1` in the snapshot advertises support. Legacy definitions migrate on load/save; each run retains its resolved test JSON. The HTTP request limit is 1 MiB to accommodate larger definitions.

Secondary measurements use optional roles (`optional: true`, empty terminals permitted), `secondary_measurements` for scalar probes, and `waveforms` for settled/transition captures. Snapshots advertise `secondary_measurements_version: 1` and driver `waveform_capabilities`. Missing optional probes preserve primary test validity. Mark observations required for preflight/fail-closed behavior. Waveform data/CSV/SVG references and scalar statistics appear in run metadata; raw samples live in the run database. See [secondary measurement semantics and hardware verification status](docs/secondary-measurements.md).

Scope channel/single-shot behaviour: scalar polling never measures off traces. Runs temporarily select the union of scalar and waveform channels and restore trace states afterward. SDS1202X-E transition captures use the current timebase/pre-trigger position (`window_s`/`pretrigger` null), program/verify the edge trigger, wait for readiness, and require a fresh acquisition with STOP before waveform and BMP screen transfer. Screens are saved by default for transition captures (`screenshot: false` opts out), displayed in Results and linked in reports. Trigger settings restore on capture cleanup. These additions are protocol-fixture tested and await hardware verification. See [secondary measurements](docs/secondary-measurements.md).

Hantek 365 direct USB: use USB Scan or `hantek://usb/PORT` (for example `hantek://usb/3-3.1.3`). It is a binary vendor interface, never SCPI or serial. The 365B thermocouple path and Celsius/Fahrenheit switching are hardware checked; other ranges/models are protocol-tested. Select TEMPC/TEMPF through instrument settings. Celsius logical quantities `temperature`/`dut_temperature` map to the single `temperature` channel; there is no fabricated ambient probe. USB serial `Y` is not unique, so identity remains port-local. See [Hantek 365](docs/hantek365.md) for permissions, settling and quality flags.

Keysight 344xxA bench DMMs: the 34401A/10A/11A/60A/61A/65A/70A driver supports USBTMC and raw TCP, model-specific primary functions and scalar acquisition. Inspect the advertised catalog. Polling requires immediate triggering, one sample and one trigger, with math/scaling/null disabled; incompatible settings return `unsupported_configuration` without reconfiguring hardware. `READ?` starts fresh acquisition and may clear meter reading memory/statistics. Model and serial provide stable identities across Keysight/Agilent/HP branding. Software protocol checks pass; physical hardware verification is pending. See [Keysight setup and acquisition semantics](docs/keysight344xxa.md).
