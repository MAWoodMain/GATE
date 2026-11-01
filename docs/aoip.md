# AOIP SN-8310 and PJ-6301 over USB RS-232

Both drivers are implemented against the supplied manuals and protocol fixtures.
**Neither has been bench-verified.** Use a USB-to-RS232 adapter and the correct
instrument cable; a USB TTL UART is not an RS232 interface. GATE does not scan
serial ports. No reset, calibration, REM, function change or output command is
sent when connecting or polling.

## Connection

In **Devices & Settings → Serial → Add…**, choose **AOIP SN-8310 / PJ-6301**,
enter the adapter path, and match the instrument's baud rate and framing. Prefer
`/dev/serial/by-id/…` to a changing `/dev/ttyUSB0` number. For agents:

```python
api.command('connect', resource='aoip:///dev/serial/by-id/ADAPTER?baud=9600')
```

Replace `ADAPTER` with the actual adapter name. Defaults are 9600 baud, 8 data
bits, no parity, one stop bit, no flow control, and a 0.2-second reply quiet
interval. Supported options are `baud` (300/600/1200/2400/4800/9600/19200),
`bits=7|8`, `parity=none|even|odd`, `stop=1|2`,
`flow=none|xonxoff|rtscts`, and `idle=0.05…2` seconds. The instrument's documented
DTR/CTS wiring must be checked before using host RTS/CTS flow control; adapters
and cable pinouts differ. Prefer no flow control or XON/XOFF where configured on
both ends. Selecting a flow option does not reconfigure the instrument.

Commands end in LF. Replies may end in LF, CRLF, CR, or have **no terminator**.
The transport waits for the configured interval of silence after the last byte,
including for terminated replies, to absorb fragmented CRLF. It never completes
an empty reply or treats the overall timeout as a successful reply. Each
transaction has a 3-second absolute deadline and a 64 KiB response bound; failure
closes the connection so delayed data cannot become the next query's reply.
Increase `idle` if your firmware inserts longer gaps within a reply. Quiet-time
framing cannot distinguish a pause longer than this interval from message end;
malformed values/units are rejected by the driver. Each query adds approximately
`idle` seconds, so PJ-6301 polling can take several seconds; choose the acquisition
interval accordingly. GATE records actual acquisition times.

Discovery uses the manuals' `*IDN?`, not the abbreviated `IDN?` in the supplied
personal command notes. Matching requires manufacturer `AOIP` or `AOIP_MESURES`
and exact SN8310/PJ6301 model (spaces/hyphen accepted). The PJ manual's IDN table
shows `OMEGA,CL8500`; this apparent manual inconsistency is deliberately not used
to enable an unverified third model. If your unit reports another identity,
capture that response for a follow-up driver correction. Hardware serials retain
GATE's transport-independent profile behavior; unusable serials stay local to the
connection.

## Independent channels and recorded values

The PJ-6301 has **Source output** (`source`) and **Measurement input** (`measure`)
channels. They are independent. Source setpoints use keys such as
`source.voltage` and `source.current`; input measurements use `measure.voltage`,
`measure.current`, `measure.resistance`, `measure.thermocouple`, and `measure.rtd`.
`source.enabled` reports operation versus standby. `OUT?` reports the requested
setpoint, **not measured terminal voltage/current**, even in standby or saturation.
Inactive functions return null with `inactive` quality. Selecting a top-bar source
reading is passive and never configures an output.

The SN-8310 manual documents a source with OUTPUT/SENSE terminals but no
independent input measurement query. It therefore exposes only the source
channel. SENSE is a feedback connection, not a second acquisition channel. An
additional independent measurement protocol would be needed to extend this.

PJ input configuration reads use `CFG_IN?`, `MEAS?`, `PCR?`, and `ISR?` without
changing free-run, trigger, hold, relative, scaling or wiring settings. Voltage,
current, resistance, thermocouple and RTD functions are selectable. Existing
sensor type, wire count, temperature unit and reference-junction configuration
are preserved and confirmed. Current input selection requires the saved
`MA60,TXA` configuration. GATE refuses to select `TXP`, which can activate the
internal 24 V transmitter-loop supply. Set up the input wiring/function on the
front panel as needed. Voltage and resistance ranges offer their documented
manual choices and Auto; current has the fixed 60 mA range. Source ranges are
configured by the separate source command, not restored as measurement ranges.

Values use SI volts/amperes/ohms. Fahrenheit and kelvin temperature replies are
converted to Celsius. Overload (including the PJ sentinel 10,000,000), held,
stale, relative, scaled, mode-changing or incompatible readings remain null with
quality flags. Source faults also invalidate the setpoint reading. A source
setpoint must never be used as a measured feedback channel in an experiment.
PJ input capture can be used in measurement-only experiments. Automatic source
sweeps, PSU-style compliance/current limits, and experiment output control are
not advertised by these calibrator drivers.

## Configure and enable a source

Use the cached `source_controls` in `/v1/instruments`. Each quantity maps range
IDs to `[minimum, maximum, resolution]` in V, A or Ω. Source configuration accepts
`quantity`, `value`, and an explicit `range`. This selects the source function and
range; it requires the instrument already in standby and never enables output.
Values are rounded to the selected range's resolution and read back with units.
Commands enter remote mode with `REM`, wait for `*OPC?`, and confirm configuration.
The SN uses `RANGE`; the PJ uses `RANGE_OUT`. Both use `OUT value` with an explicit
unit suffix, avoiding the manuals' default milliampere units.

| Instrument | Source quantity | Ranges and allowed SI values |
| --- | --- | --- |
| SN-8310 | Voltage | `MV100`: −0.011…0.11 V; `V1`: −0.11…1.1 V; `V10`: −1.1…11 V; `V100`: −5…110 V |
| SN-8310 | Current | `MA1`: −0.00011…0.0011 A; `MA10`: −0.0011…0.011 A; `MA100`: −0.011…0.11 A |
| PJ-6301 | Voltage | `MV600`: −0.1…0.6 V; `V6`: −1…6 V; `V60`: −10…60 V |
| PJ-6301 | Current | `MA60`: 0…0.06 A |
| PJ-6301 | Resistance simulation | `OHM600`: 0…600 Ω; `OHM6000`: 0…6000 Ω |

These are source setpoint bounds, not a rectangular SMU operating envelope.
Observe the manuals' load, compliance, grounding, floating voltage and polarity
requirements. SN polarity is preserved; inverted polarity blocks enabling.
SN wire configuration is preserved; `MV100` requires the front-panel 2-wire
configuration and uses the SENSE terminals. GATE never changes live wiring,
terminal board, 25 V limit or programmed limits to make a command succeed.
Temperature source setpoints can be monitored on the PJ, but typed sensor
simulation setup is not implemented.

After inspecting identity, present state and wiring, an example sequence is:

```python
# device_id is the exact returned instrument ID; wait for each command to finish.
api.command('output', instrument=device_id, enabled=False)
api.command('source', instrument=device_id,
            quantity='voltage', value=1.25, range='V6')  # PJ-6301; use V10 for SN
api.command('output', instrument=device_id, enabled=True, wiring_confirmed=True)
# ... acquire input measurements or use an external meter ...
api.command('output', instrument=device_id, enabled=False)

# A 4 mA PJ current source, still requiring a separate explicit enable:
api.command('source', instrument=device_id,
            quantity='current', value=0.004, range='MA60')  # SN: MA10
```

Enable requires a successfully configured setpoint in the current connection and
unchanged confirmed range/setpoint. Front-panel changes can cause refusal.
Scaling, saturation and out-of-limit output states block PJ enabling. Real enable
retains the API's explicit `wiring_confirmed=true` requirement. `OPER`/`STBY`
are confirmed through status readback; **standby means zero output, not electrical
isolation**. A connection that attempted to enable output owns a verified STBY
attempt on normal cleanup, including after a lost enable acknowledgement. A
broken cable/transport can prevent verification and must be checked locally.
Reconnect never restores setpoints or enables outputs. Existing live output
state is inspected, not automatically overridden during discovery.

## Verification and later bench check

`tests/test_aoip.py` uses manual-derived stateful fixtures and Linux
pseudo-terminals. It covers identity routing, passive catalogs, independent
channels/ranges, units, quality flags, limits, readback failures, output guards,
cleanup after uncertain enable, fragmented/unterminated replies, silent and
continuous-stream timeouts, oversize replies, and failed-session closure.
The desktop smoke test checks AOIP serial option round-tripping.

On the later bench test, first connect and inspect without enabling. Record the
actual `*IDN?`, adapter framing, firmware and observed reply gaps. Confirm PJ
input versus a known signal with its existing front-panel configuration. Then,
with appropriate wiring/load and explicit authorization, test a low source
setpoint and compare it with an external meter; verify STBY locally. Verify
saturation/overload, range changes, negative setpoints and cleanup separately.
No accuracy, firmware timing, cable flow control or live output behavior is
claimed from protocol fixtures alone.

Sources: user-supplied **SN-8310_EN.pdf**, sections 2.3 and 5.12;
**PJ 6301_NT45983-100C_EN.pdf**, sections 3.2, 7.2, 8.2, 8.3 and technical
specifications 3.3; **AOIP command set.docx** (personal command examples).
Manual commands are protocol reference data, not instructions to operate a live
instrument. The source bounds and syntax follow the manuals where the personal
notes disagree (for example SN current uses `RANGE MA100`, not `RANGE_OUT MA100`).
