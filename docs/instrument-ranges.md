# Channels, functions and ranges

An instrument may have any number of physical or logical channels, each with any
number of functions. Channels can have entirely different function sets: an SMU
can declare `source` and `measure` channels. No channel-count limit is imposed.

`MeasurementFunction.instrument_channel` identifies the owning instrument channel.
The older `channel` field remains the **reading key**, which may be shared by
mutually exclusive functions. Function IDs must be unique within an instrument,
for example `source.voltage` and `measure.voltage`. Exclusive groups are scoped
to the owning channel: two channels can use `mode` independently.

Drivers can implement `channels()` returning `InstrumentChannel(id, label)` values.
Otherwise GATE derives channels from the function catalog. API snapshots publish
`channels` (each containing its functions), the flat `functions` catalog for
compatibility, and `current_functions`, a channel-ID-to-function-ID map. Drivers
with independently configurable channels maintain a cached `current_functions`
dictionary. Existing single-channel drivers can retain `current_function` and
use the compatibility channel `primary`.

## Driver range contract

`range_controls()` returns `RangeControl` values with an instrument-wide unique
ID, owning `instrument_channel`, label, unit, optional function ID, and
`RangeOption` choices. Choice IDs and labels are driver-owned; numeric `value`
means nominal full scale or scale in the control's units, such as V/div. It is
not a safety rating. `automatic=True` identifies autorange; null numeric values
can also describe rate-dependent range codes.

Catalog methods must not perform I/O. `select_range(control_id, option_id)`
validates against the catalog, performs bounded I/O and confirms actual state.
Function-specific controls reject selection unless that function is active on
the owning channel. Selection must not change another channel, switch current
terminals, enable an output or accept arbitrary command text.

`range_functions()` confirms the active functions and returns a mapping for all
relevant channels. GATE restores independent controls and the range for **each
channel's** active function. Saved ranges for inactive functions remain dormant
until those functions are selected through GATE. Front-panel changes do not
cause settings to be continuously rewritten during polling.

Drivers without controllable ranges return an empty list and provide
`range_unavailable_reason`. Older plugins without range methods also work with
an empty catalog.

For example, a source and measurement driver could declare:

```python
def channels(self):
    return [InstrumentChannel('source', 'Source'),
            InstrumentChannel('measure', 'Measurement')]

def functions(self):
    return [MeasurementFunction('source.voltage', 'Source voltage', 'source.level',
                                'V', True, 'mode', 'source'),
            MeasurementFunction('measure.resistance', 'Resistance', 'measure.value',
                                'Ω', True, 'mode', 'measure')]

def range_controls(self):
    return [numeric_control('source.voltage.range', 'Source voltage', 'V',
                            [1., 10.], 'source.voltage', instrument_channel='source'),
            numeric_control('measure.resistance.range', 'Resistance', 'Ω',
                            [100., 1000.], 'measure.resistance', instrument_channel='measure')]
```

These values illustrate the interface, not an actual SMU hardware driver. The
regression fixture covers different function sets per channel, independent
exclusive groups, reading-key reuse, range restoration and reconnection.

## Selection and saved settings

The top-bar menu follows **instrument → channel → function**. A function's
submenu has **Show in top bar** and its range selector. Independent channel
controls, such as scope input scale, live directly under that channel. Selecting
a range keeps the menus open and reports success only after confirmation.
Offline or inactive-function range choices are disabled; activate the function
first. Instruments without range control show an unavailable notice.

**Auto** is the default preference wherever the driver declares autorange. When
a function is newly shown and has no saved range override, the top bar requests
Auto after the function change. An existing manual preference is preserved.
Opening or refreshing a menu performs no hardware configuration. Controls without
autorange retain the instrument setting until a manual choice is made.

The top bar can include an optional `ranges` map in `display_measurements` to
apply these defaults after function selection, with preferences saved only after
all requested changes confirm. Hiding a function sends no range change.

**Device Settings → Instrument** shows function selectors per channel and range
selectors for their selected functions. Independent controls such as scope
input scales are always shown. **Keep instrument range** removes the saved
override without writing a replacement; **Autorange** explicitly selects and
saves automatic ranging.

Inspect `range_controls` in `/v1/instruments` for valid IDs, then submit:

```json
{"action":"select_range","instrument":"instrument:…","control":"VDC","range":"10"}
```

The instrument must be connected, with the matching function already active.
The normal asynchronous command result reports success or failure; choices are
saved only after confirmation. Changes are recording events and are blocked
during experiments and test sequences.

`instrument_settings` also accepts per-channel functions and range preferences:

```json
{
  "action": "instrument_settings",
  "instrument": "instrument:…",
  "settings": {
    "channel_functions": {"source": "source.voltage", "measure": "measure.resistance"},
    "ranges": {"source.voltage.range": "10", "measure.resistance.range": "1000"}
  }
}
```

Functions are applied before ranges. Preferences follow physical instrument
identity, restore on reconnect, and copy between compatible instrument types.
Offline edits use the last known catalogs. Legacy single-channel `function`
settings remain supported. Failed confirmation preserves saved preferences;
hardware changes accepted before a later failure are not transactional.

## Current drivers

| Driver | Exposed controls |
| --- | --- |
| Keysight 344xxA | Model-specific DC/AC voltage/current, 2-/4-wire resistance and capacitance; fixed ranges and autorange. The 10 A choice exists only on 34461A/65A/70A and requires the 10 A terminal already selected. Frequency/period input ranging and diode/continuity/temperature range controls are not exposed. |
| Fluke 45 | Primary DC/AC/combined voltage/current, resistance and frequency. Labels give slow versus medium/fast full scales; rate is preserved. No diode/continuity or secondary-display range control. |
| Siglent SDS1202X-E | Independent C1/C2 input scales, 500 µV/div–10 V/div in 1–2–5 steps, before probe attenuation. Existing attenuation is accounted for and preserved. Capture ownership blocks range changes. |
| Hantek 365 | Explicitly unavailable: independent manual range selection is not verified for the USB protocol. Existing function and mA/A-terminal selection remain available. |
| Simulated multimeter | Autorange and 0.1/1/10/100 V; insufficient fixed range produces overload. |
| AOIP PJ-6301 | Input voltage and resistance manual/Auto ranges and fixed current range, preserving stored wiring/sensor settings. Source ranges use the separate standby-only `source` action. SN-8310 exposes source only. See [AOIP](aoip.md). |
| Other simulations | Empty catalog with fixed-range explanation. Output operating envelopes remain separate. |

The new controls are protocol-fixture tested, not bench-verified. Hardware
references: [Fluke 45, tables 5-13A/B](https://media.fluke.com/d295e628-9a14-4bb4-9784-b10800c1cf0b_original%20file.pdf),
[Keysight 34410A/11A guide](https://www.keysight.com/qa/en/assets/9018-05586/user-manuals/9018-05586.pdf),
[Truevolt specifications](https://www.keysight.com/content/dam/keysight/en/doc/ungate/data-sheets/5991-1983.pdf),
[Siglent guide, ATTN/VDIV](https://siglentna.com/wp-content/uploads/dlm_uploads/2025/11/SDS1000-SeriesSDS2000XSDS2000X-E_ProgrammingGuide_EN02E.pdf).
