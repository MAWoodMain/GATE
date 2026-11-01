# Riden RD6006 over USB

GATE supports the **RD6006** using Modbus RTU over its USB serial interface.
Other RD/RK/DPS models, including RD6006P, are rejected: scaling and controls
must be verified separately. No Python serial or Modbus dependency is required.

## Connect

In **Devices & Settings → USB → Add…**, enter:

```text
riden:///dev/serial/by-id/YOUR-ADAPTER?baud=115200&address=1
```

Or use the agent API:

```python
api.command('connect', resource='riden:///dev/ttyUSB1?baud=115200&address=1')
```

The baud rate and station address must match the instrument's communication
settings. Defaults are 115200 baud, 8N1, address 1. Supported baud rates are
9600, 19200, 38400, 57600 and 115200; addresses are 1–247.
Prefer a stable by-id path, ensuring it refers to the correct adapter.
The account needs serial-port access (normally the dialout group).
No broad CH340 USB permission rule is installed: the Fluke and RD6006 can use
the same adapter chipset. USB Scan deliberately does not probe arbitrary serial
adapters. Do not use a Fluke serial:// endpoint or send SCPI to the RD6006.

Identification reads registers 0–3 and never resets, unlocks, changes presets,
writes calibration or enables output. Model codes 60060–60064 select RD6006
scaling. The instrument's own 32-bit serial number, not the USB adapter's
identity, determines its stable profile. Zero/all-ones serials stay endpoint-local.

## Readback and controls

One logical output channel publishes voltage/current/power, voltage setpoint,
current limit, explicit output state, protection (0 none, 1 OVP, 2 OCP) and
regulation (0 CV, 1 CC). Resolution is 0.01 V, 0.001 A and 0.01 W.
Power uses both 16-bit words; missing/invalid transport data is an acquisition
error rather than a fabricated zero. Catalog inspection performs no I/O.

After identifying the exact instrument and checking wiring and DUT limits:

```python
# Obtain psu_id from /v1/instruments, never from this historical bench example.
api.command('static', instrument=psu_id, voltage=3.3, current_limit=0.05)
api.command('output', instrument=psu_id, enabled=True, wiring_confirmed=True)
api.command('sweep', instrument=psu_id, start=1, stop=2, step=0.5, dwell=2)
api.command('cancel_sweep', instrument=psu_id)
api.command('output', instrument=psu_id, enabled=False)
```

Limits are 0–60 V, 0.001–6 A current limit, 360 W. These are model limits,
not permission to apply them to a connected DUT. Setpoint writes and output
changes require an acknowledged Modbus write and matching register readback.
There is no extra slew limiting: static changes are immediate instrument
setpoints; sweeps use explicit steps and dwell at least the polling interval.
Setpoints/sweeps do not enable output. Enabling requires a positive in-range
current limit, in-range voltage and no active protection, plus explicit
wiring_confirmed=true through the standalone API. OVP/OCP configuration is
preserved. A multi-field static change is not transactional if later I/O fails.

Output changes and reads execute on the acquisition owner. Controls are blocked
during experiment runs. Experiments use the existing enable_outputs,
wiring_confirmed and operating-envelope validation, and must configure voltage
and current_limit before enabling. Voltage/current quantity mappings and
source.dc_voltage are advertised; no electronic-load capability is invented.

GATE remembers responsibility for an enable attempt even if acknowledgement
fails, and attempts a verified disable on normal close/disconnect/error cleanup
when it enabled the supply. Merely connecting to an already enabled instrument
does not claim or disable its output. Explicit disable always verifies state.
Output enable, setpoints and sweeps are never restored on reconnect. Loss of
USB or process termination can prevent cleanup; check the front panel.
Raw SCPI query/write actions are unavailable for this binary transport.

## Verification and references

Protocol fixtures exercise fragmented serial frames, CRC, exception replies,
timeouts, write echo/readback mismatch, scaling, model rejection, bounds,
enable interlocks and cleanup after a lost acknowledgement.
On 2026-11-01, the attached RD6006 identified as firmware 1.25, serial 4684;
passive readback showed output off. Live checks through GATE's acquisition owner
used a 50 mA current limit, with the output connected to a Fluke 45 in DC voltage:

| Check | PSU readback | Fluke 45 |
| --- | --- | --- |
| 1 V static | 1.00 V | 1.0045 V |
| 3.3 V static | 3.30 V | 3.309 V |
| 1–2 V sweep, 0.5 V steps, 2 s minimum dwell; endpoint | 2.00 V | 1.9993 V |

Current-limit writes, enable and disable all confirmed by register readback.
The original 12 V / 6 A setpoints were restored **with output off**. Current
regulation under load, full-range accuracy, protection trips and endurance were
not bench-tested. These are functional comparisons, not calibration.
Sweep completion was awaited explicitly: the shared polling cycle can extend
requested dwell times. Historical wiring/paths do not establish current bench state.

References:
[RD6006 product specifications](https://joy-it.net/en/products/JT-RD6006),
[RD6006 register map](https://github.com/Baldanos/rd6006/blob/master/registers.md),
[RD60xx Modbus protocol](https://joy-it.net/files/files/Produkte/JT-PS1440-C-Pro/MODBUS_Protocol_RD60xx_PSxx.pdf).

### Output panel verification

On 2026-11-01 the GTK output panel was exercised through the acquisition service
with the RD6006 output connected to a Fluke 45 already in DC voltage mode.
A 0.010 A limit was applied through the new `configure_output` action, followed
by 1 V and 3.3 V setpoints. The Fluke read 1.0048 V and 3.313 V respectively;
the panel displayed output state, supply voltage/current and calculated V × A
power (0 W at the supply's zero-current resolution with this meter load).
Panel enable/disable completed with confirmed readback. The original 12 V / 6 A
setpoints were restored with output off. A later Modbus polling timeout required
a passive reconnect; a valid post-reconnect sample confirmed 12 V / 6 A and
output off. This verifies the panel workflow, not load regulation, current-limit
accuracy or calibration. The real driver still preserves OVP/OCP thresholds and
does not advertise writable priority/protection controls.
