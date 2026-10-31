# Keysight 344xxA bench multimeters

GATE supports the **34401A, 34410A, 34411A, 34460A, 34461A, 34465A and
34470A** using their reported Keysight, Agilent or Hewlett-Packard identities.
Matching uses an explicit model list; 34405A, 34420A, 34450A, DM344xx and other
models are not claimed. Use the instrument's native identity/personality;
emulation exposes only the capabilities of the reported model.

## Connection

Use **Devices & Settings → TMC → Scan** for Linux `/dev/usbtmcN` devices,
or **Network → Add…** for a raw SCPI TCP endpoint (normally port 5025).
VXI-11, HiSLIP, GPIB and Keysight RS-232 are not implemented. A 34401A needs a
compatible raw TCP gateway; its native GPIB/RS-232 ports cannot connect directly.
Serial connection handling in GATE is specific to the Fluke 45.

Packaged installations include an active-seat USBTMC access rule for Keysight
and Agilent vendor IDs. For a development installation:

```bash
sudo install -m 0644 data/70-gate-keysight.rules /etc/udev/rules.d/70-gate-keysight.rules
sudo udevadm control --reload-rules
# Unplug/reconnect the meter, then scan.
```

The driver does not write configuration during identification. Model and serial
number give stable identities across USB/TCP and HP/Agilent/Keysight branding.
Different models retain separate profiles. Explicit saved function choices are
restored by GATE's normal profile handling.

## Functions

| ID | Measurement | Unit | Models |
|---|---|---|---|
| VDC / VAC | DC / AC voltage | V | All supported |
| ADC / AAC | DC / AC current | A | All supported |
| OHMS / OHMS4 | 2-wire / 4-wire resistance | Ω | All supported |
| FREQ / PER | Frequency / period | Hz / s | All supported |
| DIODE / CONT | Diode / continuity | V / Ω | All supported |
| CAP | Capacitance | F | All except 34401A |
| TEMPC / TEMPF | Temperature | °C / °F | All except 34401A |

These share one exclusive primary measurement slot. Selection writes
`SENS:FUNC` and confirms `SENS:FUNC?`; temperature also sets and confirms
`UNIT:TEMP`. Range, integration, trigger, sensor type and sensor calibration
remain instrument settings. Configure the appropriate RTD/thermistor/thermocouple
on the meter before temperature acquisition. Kelvin is not exposed.
Function changes can clear instrument math/secondary state according to the
meter's own behavior. Secondary displays, digitizing, ratio and math readouts
are outside this driver's catalog.

## Acquisition and quality

Polling uses `READ?` for a fresh scalar conversion; it starts acquisition and can
clear internal reading memory/statistics. Do not use GATE polling alongside an
independent triggered acquisition. It preserves range, NPLC, autozero, and
trigger configuration; it does not send `MEAS?`, `CONF`, `*RST` or `ABOR`.

Configure immediate triggering, one trigger and one sample on the meter before
polling. Other configurations return a null value with
`unsupported_configuration`, without starting acquisition. Enabled calculation
math/scaling or per-function null also returns that quality, preserving the
settings and avoiding transformed values labelled as ordinary SI readings.
Unsupported native functions and Kelvin temperature return the same quality.

Conversions have a bounded 15-second timeout (or the transport timeout if
longer); slow NPLC, trigger delays or settling can still exceed it. Transport
timeouts close the session; reconnect after correcting the settings.
Overload sentinels (magnitude at least `9.9e37`) become null `overload` readings.
The SCPI `9.91e37` NaN sentinel becomes null `unavailable`. Malformed or multiple values fail acquisition. The driver checks function,
temperature unit and modifiers again after acquisition; a detected change
returns null `mode_changed`. Very rapid front-panel changes between queries
cannot be made atomic with this protocol.

Electrical channels include `voltage_dc`, `current_dc`, `voltage_ac`,
`current_ac`, `resistance`, `resistance_4wire`, `frequency`, `period`, `diode`,
`continuity`, `capacitance` and `temperature`. The driver advertises scalar,
DC voltage and DC current experiment capabilities. Experiment channel mappings
do not switch hardware automatically; select the matching function first.
Temperature is available by explicit channel with the selected unit.

## Verification

Protocol fixtures cover supported model catalogs, selection confirmation,
front-panel changes, overloads, invalid data, trigger/math guards, timeout
restoration, registration, profile identity, engine integration and fragmented
responses over a real loopback TCP connection. These are software checks;
physical USB/LAN operation, firmware differences and measurement accuracy
await bench verification.

Programming references:

- [Keysight Truevolt operating and service guide](https://www.keysight.com/us/en/assets/9018-03876/service-manuals/9018-03876.pdf)
- [Keysight 34410A/11A user's guide](https://www.keysight.com/us/en/assets/9018-05586/user-manuals/9018-05586.pdf)
- [Keysight 34401A user's guide](https://www.keysight.com/us/en/assets/9018-01063/user-manuals/9018-01063.pdf)
