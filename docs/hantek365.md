# Hantek 365-series USB meters

GATE supports the direct USB interface used by the Hantek 365 family. The attached **365B** has been checked with its supplied thermocouple: packet acquisition, Celsius and Fahrenheit selection, returning to Celsius, and USB interface release/reopen. Room readings were approximately 24–25 °C. A passive three-point experiment and simultaneous SQLite/CSV recording captured 24.2 °C in all three samples, with report and temperature plot generation checked. This is a functional check, not a calibration against a temperature reference. Other models and electrical ranges have protocol fixtures but have not been bench-verified here. Bluetooth/radio dongles are not implemented.

## Connect

In **Devices & Settings → USB**, click **Scan**. The meter appears as `Hantek,365 series,,USB`; give it a friendly name such as **Hantek 365B** in its settings. Its USB descriptors do not identify the A–F variant, so GATE does not infer a model from them.

Celsius (°C) is the default throughout GATE: temperature readings, built-in experiment definitions, plots, reports and CSV recordings. A passively discovered Hantek reporting Fahrenheit is converted to °C without changing its hardware mode. **Instrument Settings → Temperature (Fahrenheit)** remains an explicit opt-in; choosing Temperature (Celsius) restores the default. Hardware mode confirmation uses the original packet units before any conversion. The supplied thermocouple must use **red: V/Ω/C; black: mA** for temperature, as specified in the [Hantek manual, Temperature Measurement](https://www.hantek.com/product/ht365/ht365_manual.pdf). COM is used for other measurements; it is not the thermocouple return on this model. An out-of-range temperature is stored as NULL with `out_of_range` quality, not as a valid experiment value.

Direct USB connections use a stable physical-port address, for example:

```text
hantek://usb/3-3.1.3
```

The bus/device numbers under `/dev/bus/usb` are resolved on each connection, so unplug/replug into the same USB port preserves the address. Moving to another port creates a different connection. The attached device advertises serial `Y`, which is not treated as a unique hardware identity: two such meters on different ports remain separate profiles. No SCPI query, factory reset or measurement-mode change is sent during discovery. An explicitly saved function is restored by the service on reconnect.

## USB permissions

The Debian package includes `70-gate-hantek.rules`. For a source checkout:

```sh
sudo install -m 0644 data/70-gate-hantek.rules /etc/udev/rules.d/70-gate-hantek.rules
sudo udevadm control --reload-rules
```

Unplug/replug the USB cable. The rule grants the active local user access only when vendor/product `0483:5722`, manufacturer `Hantek`, and product `DMM USB` all match. That VID/PID is also used by unrelated ST demo devices; GATE additionally verifies the vendor interface and expected bulk endpoints before polling. It never detaches another kernel driver to claim a meter.

## Measurements and recording

Available functions are DC/AC voltage, DC/AC current using the mA or A terminals, resistance, diode, continuity, capacitance, and Celsius/Fahrenheit temperature. Functions use one exclusive measurement group: the meter measures one function at a time. Changes are acknowledged and checked against actual packet units. The first two conversions after a mode change are discarded after a settling delay; this prevents the previous display's digits from being recorded with new units. Functions may take a few seconds to confirm.

Values are converted to base SI units (V, A, Ω, F), with temperatures in °C by default. An explicitly selected Fahrenheit function records °F. Relative temperature differences use scale-only conversion, without an absolute-temperature offset. Relative readings retain their value and `relative` quality, so they are not silently consumed as absolute measurements by experiment calculations. Overload and out-of-range readings are NULL. A no-data reply produces `not_ready`, with no fabricated zero or recycled value. Normal unplug errors disconnect the device; three consecutive response timeouts also fail the connection. Prefer a polling interval of about one second; faster polling can legitimately return `not_ready` between meter conversions. Reads, replies and mode-change waits are bounded.

The current value can be shown in the GNOME top bar, recorded in SQLite and exported as CSV using the existing device controls.

## Experiments

The driver advertises `measure.scalar`, `measure.temperature`, `measure.dc_voltage` and `measure.dc_current`. Logical quantities map as follows:

| Quantity | Channel |
| --- | --- |
| `temperature` | `temperature` |
| `dut_temperature` | `temperature` |
| `dc_voltage` | `voltage_dc` |
| `dc_current` | `current_dc` |

Selecting the Hantek for the converter's temperature role records **DUT temperature** in Celsius mode. It has only one thermocouple input: the driver deliberately does not map that same sensor to `ambient_temperature`. The separate ambient observation remains unavailable unless another sensor is configured. For an ambient-only measurement, define an observation explicitly using `quantity: "temperature"` and `location: "Ambient"`, rather than treating one sensor as two independent temperatures:

```json
{
  "ambient_temperature_c": {
    "label": "Ambient temperature",
    "unit": "°C",
    "role": "ambient_probe",
    "quantity": "temperature",
    "required": false,
    "kind": "temperature",
    "location": "Ambient"
  }
}
```

The role must require `measure.temperature` and bind to the meter. Required metric units must match the reported units: default Celsius readings match °C specifications, while an explicit Fahrenheit choice needs °F specifications. Experiments do not change the meter's function automatically; select it before starting.

## Protocol reference

The USB interface is vendor-specific with bulk OUT `0x02` and IN `0x81`, 64-byte packets. GATE uses Linux usbdevfs, with no extra Python packages. A request `01 0f` yields either `aa` (no new measurement) or a 15-byte frame beginning `a0` and ending CR/LF. Mode commands are `03 mode` and acknowledge `dd`. Frame parsing validates digits, signs, decimal placement, SI multipliers, units, and incompatible AC/DC flags. Fragmented frames and interleaved status/acknowledgement bytes are buffered with explicit limits.

Protocol field and mode information was checked against [the protocol author's Hantek 365 implementation](https://github.com/ashevchuk/hantek-365) and actual 365B captures. GATE's transport and parser are implemented independently. Family capabilities and probe connections are documented in the [Hantek 365 manual](https://www.hantek.com/product/ht365/ht365_manual.pdf) and [365B product page](https://www.hantek.com/products/detail/130).
