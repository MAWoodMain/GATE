# Aim-TTi New PL-P power supplies

GATE supports the New PL-P family over raw LAN TCP and USB virtual COM/RS-232.
Support is protocol-fixture tested; physical hardware verification is pending.
The original PL-P family, nonprogrammable PL models, PLH, QL and other TTi
families are not inferred from similar names or command syntax. Optional `(G)`
model suffixes are recognized, but GPIB transport is not implemented.

| Model | Outputs | GATE control |
| --- | --- | --- |
| PL068-P | 6 V / 8 A | Voltage, current limit, enable/disable, sweeps and experiments |
| PL155-P | 15 V / 5 A | Same |
| PL303-P | 30 V / 3 A | Same |
| PL601-P | 60 V / 1.5 A | Same |
| PL303QMD-P | Two 30 V / 3 A outputs | Acquisition only |
| PL303QMT-P | Two 30 V / 3 A outputs plus 6 V / 8 A | Acquisition only |

Multi-output supplies have separate `output1`, `output2`, `output3` channels as
applicable. They do not advertise source capability, experiment output control
or an output panel: channel-targeted output control is not implemented. Do not
interpret whole-instrument commands as selecting output 1 on these models.

## Connect

For LAN, choose **Devices & Settings → Network → Add…**, supply the instrument's
actual host/IP and **9221** as the TCP port. This is raw TCP, not VXI-11 control.
For USB or RS-232, choose **Serial → Add…**, the **TTi PL-P** protocol and its
actual device path. USB uses a virtual COM port, not USBTMC; Scan does not probe
serial devices. The account needs serial-port access (normally `dialout`).
Prefer a stable `/dev/serial/by-id/` path when available.

```python
api.command('connect', resource='tcp://YOUR-PSU:9221')
# Or USB virtual COM / RS-232, fixed 9600 baud, 8N1, XON/XOFF:
api.command('connect', resource='plp:///dev/ttyACM0?baud=9600')
```

The dedicated `plp://` protocol sends LF commands and receives CRLF replies,
with bounded transactions and no Fluke device-clear or completion prompts.
Timeouts close the session so late replies cannot confirm later commands.
Discovery sends only `*IDN?`. Model plus the instrument's serial number gives a
transport-independent identity; unusable serials remain connection-local.

**Instrument behavior:** receiving remote traffic enters remote mode and locks
out front-panel controls, cancelling S-Lock and V-Span. GATE does not send reset,
trip reset, calibration, range-change or output commands during discovery.
Set single-output rear-panel CV/CC switches to Local before digital control;
GATE cannot verify these physical switch positions. Sense/wiring remains the
operator's responsibility. GATE does not send `LOCAL` on disconnect, because
that changes the instrument's remote protection behavior.

## Readings and controls

Each output publishes `voltage` (V), `current` (A), calculated `power` (V × A),
`set_voltage` (V), `current_limit` (A) and `output_enabled` (0/1).
Single-output models use those bare reading keys; multi-output models prefix
keys and function IDs with `outputN.`. Catalog methods perform no I/O.
`CONFIG?` is checked each cycle. In parallel mode, output 2 is unavailable:
its readings are null with `unavailable` quality and no invalid output-2 queries
are issued. Output 1 reports the combined supply readback; output 3 remains
independent. Tracking modes preserve the instrument's current configuration.
Malformed/nonfinite replies fail acquisition rather than becoming zero readings.

Single-output voltage is bounded by the table above, starting at 0 V, with
1 mV setting resolution. GATE conservatively requires at least 1 mA current
limit in either current range. Current setting steps are 1 mA for PL068-P and
0.1 mA for the other single-output models. The existing low/high current range
is queried before setting current or enabling: low range caps are 800 mA for
PL068-P and 500 mA otherwise. GATE never changes the current range automatically.
The passive panel catalog shows model maxima; a value beyond the selected
current range fails with an explicit error. OVP/OCP thresholds, averaging and
front-panel modes remain unchanged. Writable protection/priority and range
selection are not exposed. Bounds are equipment limits, not DUT ratings.

```python
# Use the actual returned single-output instrument ID after checking wiring.
api.command('static', instrument=psu_id, voltage=3.3, current_limit=0.05)
api.command('output', instrument=psu_id, enabled=True, wiring_confirmed=True)
api.command('sweep', instrument=psu_id, start=1, stop=2, step=0.5, dwell=2)
api.command('output', instrument=psu_id, enabled=False)
```

Setpoints require matching `V1?`/`I1?` readback and output changes require matching
`OP1?`. This confirms settings and switch state, not physical voltage settling
or calibration. Trips are never reset automatically. The standalone panel's
Enable click supplies operator confirmation; experiment wiring/output arming is
separate. Static/sweep/configure_output never enable the output. Changes run on
the acquisition owner and remain blocked during experiments. Multi-field changes
are not transactional. Single-output models expose voltage/current mappings and
model-specific experiment envelopes; no real electronic-load driver is added.

GATE attempts verified disable on normal cleanup for an output it enabled,
including a lost enable acknowledgement. An already enabled output merely
observed at connect is not claimed or disabled on close. Nothing replays enable,
setpoint or sweep state on reconnect. Communication loss or process termination
can prevent cleanup. Raw query/write is unavailable on this driver; use its
validated typed controls.

## Verification and reference

Fixtures cover exact model recognition/rejection and ratings, serial/TCP routing,
identity across transports/branding, passive catalogs/discovery, per-output
readings, parallel-mode nulls, malformed responses, selected-range limits,
confirmed setters, API enable confirmation, sweep bounds and uncertain-enable
cleanup. Pseudo-terminal tests cover fragmented CRLF, bounded replies/timeouts
and session closure. GTK smoke checks cover serial protocol selection and the
fixed baud rate. These checks do not establish bench verification.

Manufacturer reference: [PL & PL-P instruction manual, Issue 21](https://resources.aimtti.com/manuals/PL%2BPL-P_Series_Instruction_Manual_EN_48511-1140_21.pdf),
printed pages 4–6 (ratings), 23–27 (interfaces and port 9221), 34–39 (commands).
