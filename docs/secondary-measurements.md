# Secondary measurements

Secondary observations enrich an experiment without defining its main sweep. Typical examples are DUT and ambient temperatures, ripple traces at each load, and an armed trace around a protection transition. They are described in the same JSON test specification and recorded with its data.

## Desktop workflow

Open an experiment's **Instruments** tab and choose **Add Secondary Measurements…**. Select DUT temperature, ambient temperature, ripple snapshots, and/or protection transitions. The dialog creates optional equipment slots and adds definitions to the selected test; **Apply to every test** applies the chosen observations to all tests. Protection predicates must use names available in each affected test, so apply them individually when tests have different axes.

Use **Instruments → Waveform Channels & Screenshots…** to choose a channel and enable **Save scope screenshot** for each capture in the currently selected test. Screenshots default off for ripple snapshots and on for single shots. Enabling them raises a short capture timeout to 10 seconds. The dialog also lets existing definitions switch to the scope’s native timing; it preserves selected equipment, test checkboxes and parameter edits. A live ripple screenshot shows the current front-panel display and may differ from the earlier scalar/waveform read; single-shot screens are retrieved while stopped.

Assign equipment to the new slots. Optional slots offer **Not assigned** and do not silently select real equipment. **Add Simulated Converter Bench** supplies a paired converter, DUT/ambient probes and a waveform recorder, and allows the demo observation slots to be selected automatically. All data produced by the demo remains labelled simulated.

The new slots do not alter the DUT wiring. Optional roles may have no electrical terminals, which is useful for ambient/contact probes. Use the JSON editor to specify probe location, actual driver channel names, units, capture conditions and whether an observation is required. Parameters and existing bindings survive adding observations.

**Results** contains temperature plots, optional waveform statistics in point data, and a **Secondary Waveforms** browser. Select a trace to see its chart, quality, sweep settings and statistics, then open its full-resolution CSV. **Export Secondary CSV…** exports every scalar observation cycle, including settling and pending-capture time. Main CSV export retains the existing points/summary behavior.

## Optional roles and scalar observations

An equipment role can declare `optional: true`; its `terminals` list may then be empty. Missing or disconnected optional instruments do not make role binding incompatible. A test still requires the role if it uses that role for output controls or a measurement/capture with `required: true`. Required observations are validated before output enabling; losing a required instrument stops the run. Distinct primary/output roles still require distinct instruments. Optional read-only roles may share an instrument with another role, with one serialized acquisition owner.

```json
{
  "thermal": {
    "name": "Temperature probes",
    "capabilities": ["measure.temperature"],
    "terminals": [],
    "optional": true
  }
}
```

A test's optional `secondary_measurements` map uses the same channel contract as its primary measurements, with optional `kind` and `location` metadata:

```json
{
  "secondary_measurements": {
    "dut_temperature_c": {
      "label": "DUT temperature", "unit": "°C", "role": "thermal",
      "quantity": "dut_temperature", "required": false,
      "kind": "temperature", "location": "DUT case"
    },
    "ambient_temperature_c": {
      "label": "Ambient temperature", "unit": "°C", "role": "thermal",
      "quantity": "ambient_temperature", "required": false,
      "kind": "temperature", "location": "Ambient"
    }
  }
}
```

Use `channel` instead of `quantity` for an exact driver reading channel. Logical quantities are resolved through `experiment_channels`. Temperature units may be Celsius (`°C` or `C`), Fahrenheit (`°F` or `F`) or Kelvin (`K`); the configured unit must match the driver's unit exactly. GATE does not guess probe types or convert units silently. Additional physical thermometer/logging models need drivers; the existing Fluke 45 driver does not become a thermometer merely by selecting the role.

These identifiers can be used in calculations. For example, `temperature_rise_c` with expression `dut_temperature_c - ambient_temperature_c`, aggregate `point`, gives the settled DUT temperature rise above ambient. Optional missing values propagate NULL, never zero.

Temperature readings are saved on every acquisition cycle, including settling. Point columns contain settled averages. To plot the full observation stream, use `data: "secondary"`, `x: "elapsed_s"`, and `y` equal to one secondary scalar identifier. Secondary-stream plots do not support series grouping. CSV/SQLite retain every sample; long-stream previews use bounded min/max buckets and mark incomplete buckets as gaps.

```json
{
  "kind": "line", "data": "secondary",
  "x": "elapsed_s", "y": "dut_temperature_c",
  "title": "DUT temperature versus time"
}
```

`point.quality` describes primary data; `point.secondary_quality` describes the optional observation data. Missing secondary data therefore does not falsely invalidate valid efficiency/current measurements. Each raw secondary sample separately records its value, units, quality, location and read window. `unassigned`, `unsupported`, `disconnected` and `unavailable` remain distinguishable in the raw data.

For a thermal limit during settling, add a `continuous` check. This stage evaluates every polling cycle from current scalar readings, unlike `sample`/`point` checks tied to settled acquisition. Conditions fail closed if unavailable. An optional probe can be explicitly tolerated with a condition such as `not is_valid(dut_temperature_c) or dut_temperature_c < maximum_temperature_c`; a required safety probe should be required and use a condition that does not tolerate missing values. Hardware interlocks and the driver's own protections remain necessary for hard real-time protection.

## Waveform requests

A test's optional `waveforms` map describes named captures. A settled snapshot is read after the point's scalar acquisition. A transition capture is armed after `before_point` actions and before axis controls, so the recorder can retain pre-trigger data around the setpoint change.

```json
{
  "ripple": {
    "label": "Ripple under load", "unit": "V", "role": "scope", "channel": "C1",
    "required": false, "phase": "settled", "when": "output_current_a > 0",
    "points": 2048, "window_s": null, "pretrigger": null,
    "timeout_s": 3, "max_captures": 8, "trigger": null
  },
  "overcurrent": {
    "label": "Overcurrent protection behavior", "unit": "V", "role": "scope", "channel": "C1",
    "required": false, "phase": "transition", "when": "load_current_set_a >= 3.5",
    "points": 4096, "window_s": null, "pretrigger": null,
    "screenshot": true,
    "timeout_s": 10, "max_captures": 3,
    "trigger": {"level": "nominal_output_v * (1 - tolerance_pct / 100)", "slope": "falling"}
  }
}
```

Declare an equipment role such as `scope` with capability `capture.waveform`. Request fields:

- `phase`: `settled` or `transition`. Transition mode requires an explicit trigger with numeric level expression and rising/falling slope.
- `when`: boolean expression. Transition conditions may reference parameters/axes, since the point has not been measured yet. Settled conditions may additionally reference measured scalar identifiers and `elapsed_s`. Neither condition can depend on derived values or future waveform statistics.
- `points`: integer expression, 2–16384; `max_captures`: integer, 1–64. Total requested waveform samples across a test are limited to one million.
- `window_s`: duration expression, 1 ns–60 s, or null to use native recorder settings.
- `pretrigger`: fraction, 0–0.95, or null for native settings.
- `screenshot`: optional boolean; defaults to true for transition captures and false for settled snapshots. A supported driver returns the physical scope screen from the stopped acquisition; it is stored as `waveform-N-screen.bmp`, shown in Results and included in the HTML report. Missing/failed screen transfers are reported separately without discarding valid waveform samples. The simulator has no physical screen and reports this explicitly.
- `timeout_s`: expression, 0.1–30 s. Armed captures are polled while the point is settling, and after its scalar acquisition when necessary. Trigger polling is asynchronous after arming; a driver may perform a bounded readiness handshake before returning its token.
- `required`: required failures/timeouts stop the run and sequence; optional failures are recorded without discarding primary measurements. An unsupported required phase/channel is rejected in preflight. Explicit false conditions and capture limits skip that point's capture.

Settled and armed capture quality/status is retained per record, and each point includes `<id>_status`. Supported statuses include `ok`, `not_requested`, `limit_reached`, `unassigned`, `unsupported`, `condition_unavailable`, `timeout`, `error`, `clipped` and `cancelled`.

A successful trace also supplies scalar columns `<id>_peak_to_peak`, `_rms`, `_ac_rms`, `_mean`, `_min`, and `_max`. RMS includes DC; AC RMS subtracts the mean. These metrics are calculated from full-resolution samples and can be plotted against load, voltage or time, used in point calculations/checks, and reduced into summaries. They are NULL when a capture is missing or invalid. Sample-mean calculations cannot depend on waveforms, which complete later; ordinary point calculations are reevaluated after capture completion.

The scope controls its own acquisition clock and trigger. Host timestamps describe arming/read windows and retrieval. `trigger_utc_ns` is null when the driver cannot attest an absolute trigger time. Time-domain points carry calibrated relative time and units; they must not be presented as simultaneous sampling with software-polled instrument readings.

## Stored data

Every run additionally contains:

- SQLite `secondary_samples`: continuous scalar samples with metric, role, instrument, location, unit, quality and host read timing.
- SQLite `waveforms`: capture metadata/status, point ordinal, setpoint index, sweep settings, channel, calibrated sample spacing/origin, available trigger timing, driver metadata and statistics.
- SQLite `waveform_samples`: full-resolution relative times and values keyed by capture ID.
- `secondary.csv`: all secondary scalar cycles.
- `waveform-N.csv` and `waveform-N.svg`: full-resolution trace data and an extrema-preserving preview. Preview rendering is bounded; no full waveform arrays are copied into service snapshots.

The HTML report includes secondary charts, capture statuses and trace CSV links. Definitions and bindings are retained with the run. Cancelling/stopping the service disables outputs first and cancels every armed token; no capture or output is replayed on restart. Temporary UI closure leaves the service-owned sequence running.

## Drivers and verification

Scalar temperature drivers provide normal `Reading` values and advertise `measure.temperature`; they may map logical names such as `dut_temperature` and `ambient_temperature`. They are otherwise ordinary acquisition drivers.

Waveform drivers advertise `capture.waveform`, a `waveform_capabilities` map keyed by channel (`unit`, `phases`, `max_points`, `configurable_window`, `verification`), and three bounded methods:

- `begin_waveform(request)` returns a token. Transition requests must arm the recorder before returning, without fabricating a post-event trace.
- `poll_waveform(token)` returns a calibrated `gate.waveforms.Waveform` or None while acquisition is pending. Drivers must bound I/O; the service remains a sequential acquisition owner, so a slow transaction can delay commands/polling.
- `cancel_waveform(token)` releases/cancels pending acquisition. Multiple captures on one recorder must be coordinated by the driver.

The coupled demo implements temperatures, settled ripple and triggered protection traces. Its waveforms are synthetic and clearly labelled.

The **SDS1202X-E** driver supports native-timing snapshots and edge-triggered single shots over USBTMC/raw TCP. Normal scalar polling checks `TRA?` and never requests PAVA measurements on off channels. An experiment temporarily enables only its scalar/waveform channels and disables unused channels, restoring previous trace states at completion, cancellation or failure. During an armed shot, scalar scope polling pauses so it cannot disturb acquisition.

For single shots the driver saves trigger mode, selection, level, slope and trigger coupling; selects the requested channel/edge/level with DC trigger coupling; verifies readback; clears stale acquisition events; and enters SINGLE mode. It waits up to one second for trigger-ready before the experiment changes its setpoint. A premature trigger fails the capture. Polling requires a new acquisition event and STOP mode before data transfer. Waveform data and the `SCDP` BMP screenshot are retrieved while stopped, before restoring the trigger settings/mode. Only one capture can own the physical scope at a time. A missing trigger produces a timeout, with no fabricated late snapshot. Absolute trigger UTC remains unknown.

Configure vertical scales, probe attenuation, channel coupling, timebase and pre-trigger position on the front panel. Keep `window_s` and `pretrigger` null for this driver; existing definitions with explicit values must be edited to use native timing. Settled snapshots also require `trigger: null`. New protection examples use native timing automatically. Single-shot transfers decimate across the acquired record to retain the event within the bounded sample count; `transfer_stride` and `acquired_points` record this, and CSV spacing reflects the stride. Settled ripple transfers retain native sample spacing. Transfer framing is restored after transfer. Scope screenshots retain the display trace and settings independently of the reconstructed preview.

Binary waveform blocks and raw BMP dumps are length-framed and bounded, preserving embedded CR/LF bytes. BMP files are capped at 1 MiB. Protection examples allow 10 seconds for trigger and transfer; increase the timeout within its supported bound for slow USB links. Scale/unit checks reject inconsistent captures. Command syntax and conversion follow the [Siglent programming guide](https://siglentna.com/wp-content/uploads/dlm_uploads/2017/10/Programming-Guide_forSDS1000X-E.pdf), with the BMP screen-dump path based on [Siglent’s screen capture example](https://siglentna.com/application-note/programming-example-sds-oscilloscope-screen-capture-python/). Signed samples use ordinary 8-bit two's-complement conversion.

The USB screenshot path has been verified on the attached SDS1202X-E, firmware 1.3.26: a complete 800×480 BMP was saved by a passive experiment, included in its report, and followed by a successful ordinary scope query. The large-image path temporarily claims the same physical interface with libusb, receives the complete USBTMC message despite undersized packets, and restores the kernel driver and normal connection. `libusb-1.0-0` and access to the corresponding `/dev/bus/usb` device are required; the existing text and small-waveform path continues to use Linux USBTMC. Tests cover packet tags, bounds, zero-length packets during image preparation and cleanup after failures. **Real waveform accuracy and single-shot trigger readiness still need bench verification**. The Instruments/Results views expose verification limitations. Settled snapshots retrieve an existing trace without a freshness guarantee. A service crash or lost connection can prevent restoring scope settings; inspect the scope before resuming manual use.

Complete JSON examples are [efficiency with observations](../gate/templates/efficiency-with-observations.json) and [maximum current with observations](../gate/templates/maximum_output_current-with-observations.json).

Temperature defaults use °C for observations, temperature rise, simulated probes and generated plots/reports. Hantek readings default to Celsius even when the meter is initially reporting Fahrenheit; an explicit Fahrenheit function remains available. Saved run data retains its recorded units.
