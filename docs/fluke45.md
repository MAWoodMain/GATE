# Fluke 45 serial acquisition

The attached Fluke 45 (main firmware 1.7, display firmware D1.0) has been identified and read successfully at **9600 baud, no parity, 8 data bits, 1 stop bit**. Both command echo modes are supported; GATE preserves the meter's echo setting, measurement functions, ranges, trigger mode, and modifiers.

## Connect

In GATE's connection field enter:

```text
serial:///dev/ttyUSB0?baud=9600
```

Then click **Connect**. The serial adapter used during verification also has this path, which remains stable when tty numbering changes:

```text
serial:///dev/serial/by-id/usb-1a86_USB2.0-Ser_-if00-port0?baud=9600
```

Or use the running service:

```bash
gate command '{"action":"connect","resource":"serial:///dev/ttyUSB0?baud=9600"}'
```

The meter joins live acquisition, the top bar device list, and any recording on the same host timebase as the scope. It must be reconnected after restarting the service. **Scan USB** scans USBTMC equipment; it does not send meter commands to arbitrary serial devices. A USB adapter identifies itself, not the instrument on its RS-232 cable.

The account needs permission to open the serial port, typically through the `dialout` group. This was already configured on the development host. Supported baud URL values are 300, 600, 1200, 2400, 4800 and 9600; framing currently supports no parity only. Set the meter to matching settings using its front panel. A serial timeout suggests mismatched settings, a wrong cable pinout, another application using the port, or disconnected equipment.

## Readings and units

The driver reads `FUNC1?`, `MOD?`, and `VAL?`. If two numeric values are returned, it reads `FUNC2?` to determine the secondary display's function. It rechecks the primary function and modifiers after reading, invalidating values if they changed mid-cycle.

- Primary and secondary functions: DC/AC voltage and current, combined AC+DC, resistance, frequency, diode and continuity modes where the meter supports them.
- Disabled secondary display: a NULL reading with quality `disabled`.
- Overload: NULL with quality `overload`; documented high-resistance underload: NULL with quality `underload`.
- Format 1 numeric replies and Format 2 replies with units are accepted.
- Primary dB and power modifiers use dB and W. Modifier bits are included in the logged channel name so held, relative, minimum, and maximum readings can be distinguished from normal measurements. For example `primary.VDC.mod32` is relative DC voltage.

The Fluke protocol returns command echo and an explicit completion prompt. GATE waits for the complete prompt before starting another transaction, handles command/execution errors without losing framing, and closes a timed-out connection. The host serial port stays in raw mode when closed to avoid an echo loop with the meter; the meter's own configuration is unchanged. The serial transport is implemented with Python's standard library and does not need pyserial.

## Validation

On the attached meter, `*IDN?` returned a Fluke 45 identity; `FUNC1?` returned `VDC`, `MOD?` returned zero, and the primary reading was approximately 0.17 mV with the current inputs. The secondary display was inactive. Communication is verified; measurement accuracy has not been calibrated or compared against a known reference.

Pseudo-terminal tests exercise real termios/serial framing, echo on/off, fragmented replies and prompts, dual displays, unit handling, command errors, timeouts, front-panel function changes, overloads, and logging alongside other instruments. Dual-display and modifier variants have simulator coverage and still need validation on physical hardware.

Protocol reference: [Fluke 45 user manual](https://media.fluke.com/d295e628-9a14-4bb4-9784-b10800c1cf0b_original%20file.pdf), chapter 5, including function/modifier commands and measurement queries.
