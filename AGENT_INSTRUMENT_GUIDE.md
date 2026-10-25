# GATE instrument guide for agents

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

Every request requires `Authorization: Bearer <token>`. POST bodies require `Content-Type: application/json` and a Content-Length of at most 16384 bytes. There is no CORS or anonymous API.

| Endpoint | Purpose |
|---|---|
| `GET /v1/health` | API/application versions, raw-command setting, supported actions |
| `GET /v1/state` | Complete cached acquisition state, command results, settings and recording status |
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

Discover IDs again after a reconnect; do not hard-code node numbers.

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

USB serial adapters identify the adapter, not the meter behind it, so add serial devices by explicit address. Serial/TCP connections and the acquisition interval are saved in `$XDG_CONFIG_HOME/gate/devices.json` (normally `~/.config/gate/devices.json`) and reconnected at service startup. USBTMC devices are rescanned.

```python
api.command('disconnect', instrument=device_id)  # disconnect and forget saved endpoint
api.command('disconnect', instrument=device_id, forget=False)  # disconnect for this run
api.command('settings', interval=1.0)  # 0.1–60 seconds; stop recording first
```

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

Selecting sends the driver's fixed mnemonic and checks `FUNC1?` before reporting success. It does not explicitly set trigger mode or range, alter calibration, or expose secondary-display controls. The meter's own function-change behavior may clear modifiers or its secondary display. A new conversion can take longer after a mode change; the first read gets a bounded longer timeout.

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
