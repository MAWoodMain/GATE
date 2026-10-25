# GATE

**Gnome Automatic Test Equipment** — a native Ubuntu bench application with live GNOME top bar measurements, instrument discovery, output control, and a shared recording timeline.

This is the initial 0.1 foundation. The GTK4 desktop, D-Bus acquisition service, SQLite recorder, CSV export, extension, and simulated bench are implemented. The first hardware driver targets the **Siglent SDS1202X-E**, reading C1/C2 peak-to-peak, RMS, and frequency using SCPI over Linux USBTMC or a manually supplied TCP endpoint. Actual scope communication still needs hardware validation after USB permissions are configured. Waveform capture is a future addition.

## Equipment support

| Category | Real hardware | Demo bench |
|---|---|---|
| Bench multimeters | Driver interface ready; model drivers pending | DC voltage |
| Bench power supplies | Driver interface ready; model drivers pending | Voltage/current readback, static voltage, stepped sweeps, output enable |
| Oscilloscopes | SDS1202X-E scalar measurement driver, hardware validation pending | Peak-to-peak and frequency |

Demo devices are explicitly labelled **SIMULATED** in the app and **DEMO** in the top bar. Their IDs start with `demo:` in recordings. GATE never infers supply command dialects from a generic SCPI ID; each real output driver needs model-specific limits and verification.

## Ubuntu installation

Target: Ubuntu with GNOME Shell 46–50 and Python 3.10+. These Shell versions are declared by the extension; live Shell testing is still required. Core and desktop smoke tests run in CI on Ubuntu 24.04.

```bash
sudo apt update
sudo apt install python3-venv python3-gi gir1.2-gtk-4.0
./install.sh
systemctl --user enable --now gate
~/.local/bin/gate gui
```

Keep the checkout in place: the installer creates an editable virtual environment inside it and a launcher in `~/.local/bin`. Add that directory to your PATH if `gate` isn't found. The service runs as your normal user. It discovers accessible USBTMC instruments at startup; use **Scan USB** after connecting another device. A connection failure is displayed and logged as an unavailable reading; click Scan USB to reconnect. It does not reset the instrument or change acquisition settings.

The initial service uses a 1-second polling interval. To use another interval, stop the systemd service and start `gate service --interval 0.5` manually; minimum 0.1 seconds. Slower equipment may determine the actual cycle rate.

### GNOME top bar

The installer copies the extension to your user extension directory. Log out and back in, then enable:

```bash
gnome-extensions enable gate@mawoodmain.github.io
```

The top bar displays a live measurement and recording dot. Open its menu and choose **Display measurement** to select another channel. Selection lasts for the extension session. The menu lists current measurements and opens the app. When the service stops, the indicator says offline; old samples are marked stale after five seconds. The service continues logging when you close the window.

### Connect the SDS1202X-E over USB

Connect the rear USB device port. Linux should load the `usbtmc` module and create `/dev/usbtmcN`. On the development host the scope was visible in sysfs as `SDS1202X-E`, VID `f4ed`, PID `ee3a`, with a root-owned `/dev/usbtmc3` node. Node numbers may change.

Install the supplied local-seat access rule:

```bash
sudo install -m 0644 data/70-gate-siglent.rules /etc/udev/rules.d/70-gate-siglent.rules
sudo udevadm control --reload-rules
sudo modprobe usbtmc
```

Unplug/replug the scope, then click **Scan USB**. The rule grants access through `uaccess` to the active local seat and applies to the Siglent USB identifiers; it does not make all USB devices world-writable. Do not run the desktop or service as root. If discovery still fails, check `ls -l /dev/usbtmc*` and `getfacl /dev/usbtmcN`.

To connect over a verified raw SCPI TCP endpoint, enter `tcp://HOST:5025` (or its configured port) and click **Connect**. Automatic LAN discovery, VXI-11, HiSLIP, serial, and GPIB transports are not implemented yet. This raw TCP path is tested against a local simulator, not the attached scope's LAN interface.

## Try the bench without hardware

In the app click **Add demo bench**. Or start a demo service manually (stop an existing service first):

```bash
systemctl --user stop gate
gate service --demo
# In another terminal:
gate gui
```

Use the output workbench to set 0–30 V on the simulated supply. Applying a voltage or running a sweep never enables the output; explicitly click **Enable output**. A sweep includes its starting value, steps toward the endpoint, clamps to the exact endpoint, and holds the final value. Dwell must be at least the polling interval; each step holds for at least the requested dwell and actual timing is recorded. Cancel holds the last setpoint. Disable output also cancels the sweep. Closing the window leaves the service running; stopping the service closes devices and disables demo outputs.

Real output control is deliberately not implemented until a specific bench PSU model can be validated. The demo establishes the workflow and recording contract.

## Logging and export

Choose a recording filename and click **Start recording**. All connected devices, including PSU settings, output state, and readback, enter the same recording. Instruments connected during recording join subsequent cycles. **Stop recording** commits the session; CSV export also works against committed live data.

```bash
gate export bench.sqlite bench.csv
gate status
gate command '{"action":"scan"}'
gate command '{"action":"connect","resource":"/dev/usbtmc3"}'
gate command '{"action":"record","path":"/absolute/path/bench.sqlite"}'
gate command '{"action":"stop_recording"}'
```

D-Bus commands acknowledge queueing, not completion; inspect `gate status` or the app for results. Output commands are `static`, `output`, `sweep`, and `cancel_sweep`; examples are in [the architecture guide](docs/architecture.md).

### What “same timebase” means

Every recording session maps a host monotonic clock to a UTC origin. Every cycle has a shared `tick` and `tick_ns`. Each instrument has its own `read_start_ns` and `read_end_ns`, relative to the same session origin, enclosing its batch of channel queries. Individual channels within that batch are read sequentially and do not have separate acquisition timestamps. CSV includes `tick_utc_ns` derived from the session origin.

**This is software alignment, not simultaneous hardware sampling.** Scope measurements describe the scope's own acquisition, and timing uncertainty includes transport and instrument latency. Output commands and sweep steps have timestamped events. Missed deadlines are skipped without bursts; overruns are shown in service status. Hardware triggers/clock distribution and waveform timebase alignment are future work.

Readings with no valid value use SQL NULL and a quality flag, not a fabricated zero. A failed instrument produces an error record and is disconnected so other devices continue. Re-scan to retry.

## Development

The acquisition core uses only the standard library. GTK and D-Bus use Ubuntu's PyGObject packages.

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q gate
node --input-type=module --check < extension/extension.js
# With GTK/display available; isolated session bus:
dbus-run-session -- python3 tests/desktop_smoke.py
# For headless CI, install xvfb and use:
dbus-run-session -- xvfb-run -a python3 tests/desktop_smoke.py
```

See [architecture and driver development](docs/architecture.md) and [roadmap](docs/roadmap.md).

## Protocol references

- [Siglent SDS1000X-E product and programming resources](https://www.siglent.com/int/products-overview/sds1000x-e/)
- [Siglent programming guide](https://int.siglent.com/u_file/download/25_11_25/SDS1000%20Series%26SDS2000X%26SDS2000X-E_ProgrammingGuide_EN02E.pdf): `*IDN?`, `C1:PAVA? PKPK`, `C1:PAVA? RMS`, `C1:PAVA? FREQ` and C2 equivalents.
- [GNOME extension guide](https://gjs.guide/extensions/): ES module Shell extension and panel menu APIs.

## Uninstall

```bash
systemctl --user disable --now gate
gnome-extensions disable gate@mawoodmain.github.io
rm -f ~/.local/bin/gate ~/.config/systemd/user/gate.service
rm -f ~/.local/share/applications/io.github.MAWoodMain.GATE.desktop
rm -rf ~/.local/share/gnome-shell/extensions/gate@mawoodmain.github.io
systemctl --user daemon-reload
# If installed:
sudo rm -f /etc/udev/rules.d/70-gate-siglent.rules
sudo udevadm control --reload-rules
```

Recordings remain under `~/.local/share/gate/recordings` (or `$XDG_DATA_HOME/gate/recordings`). Keep or back them up as needed.
