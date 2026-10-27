# GATE

**Gnome Automatic Test Equipment** — an agent-operated Ubuntu instrument service with a GNOME top bar for human monitoring and a small device-management/settings window.

This is the initial application foundation. The GTK4 desktop, D-Bus acquisition service, SQLite recorder, CSV export, extension, and simulated bench are implemented. Hardware support includes the **Fluke 45** over RS-232 and **Siglent SDS1202X-E**, reading C1/C2 peak-to-peak, RMS, and frequency using SCPI over Linux USBTMC or a manually supplied TCP endpoint. USB identity and all six scalar queries have been verified against an attached SDS1202X-E running firmware 1.3.26; comparison against calibrated signals and the front panel is still pending. Waveform capture is a future addition.

Agents: start with [the instrument control guide](AGENT_INSTRUMENT_GUIDE.md). The authenticated loopback HTTP API listens on port 8765 by default; its private token/URL are discovered from `$XDG_RUNTIME_DIR/gate/control.json`.

## Equipment support

| Category | Real hardware | Demo bench |
|---|---|---|
| Bench multimeters | Fluke 45: primary and secondary displays over RS-232, physically verified | DC voltage |
| Bench power supplies | Driver interface ready; model drivers pending | Voltage/current readback, static voltage, stepped sweeps, output enable |
| Oscilloscopes | SDS1202X-E scalar measurement driver, USB communication verified | Peak-to-peak and frequency |

Demo devices are explicitly labelled **SIMULATED** in the app and **DEMO** in the top bar. Their IDs start with `demo:` in recordings. GATE never infers supply command dialects from a generic SCPI ID; each real output driver needs model-specific limits and verification.

## Debian package and automatic releases

The GitHub Actions workflow builds an architecture-independent `.deb`, verifies an actual apt installation on Ubuntu 24.04, and uploads packages/checksums/guides as build artifacts. Once merged into `main`, successful builds publish versioned GitHub Releases automatically: 0.1.0 initially, then patch increments. Manual runs can choose minor or major bumps. Branch/PR builds use commit-qualified preview versions.

Build locally with Python 3.11+ and dpkg:

```bash
python3 scripts/build_deb.py --output dist
sudo apt install ./dist/gate-ate_*_all.deb
```

Follow [package installation and migration instructions](packaging/INSTALL.md) before replacing a development installation. The package installs `/usr/bin/gate`, a user service, desktop entry, GNOME extension, USB rule and agent documentation. Runtime and package versions are stamped consistently from the release version.

## Ubuntu installation

Target: Ubuntu with GNOME Shell 46–50 and Python 3.10+. These Shell versions are declared by the extension; live Shell testing is still required. Core and desktop smoke tests run in CI on Ubuntu 24.04.

```bash
sudo apt update
sudo apt install python3-venv python3-gi gir1.2-gtk-4.0
./install.sh
systemctl --user enable --now gate
~/.local/bin/gate gui
```

Keep the checkout in place: the installer creates an editable virtual environment inside it and a launcher in `~/.local/bin`. Add that directory to your PATH if `gate` isn't found. The service runs as your normal user. The window follows the desktop theme and groups equipment into **TMC**, **Serial**, and **Network** settings sections. Each section has its own **Add…** dialog; TMC also has **Scan** for accessible USBTMC nodes. Serial settings include the device path and baud rate (8N1); Network settings include host and TCP port. Device actions use consistent icon buttons: **Connect/Disconnect**, **Settings**, and a **bin**. The Settings dialog separates **Connection** (path/baud or host/port) from **Instrument** (friendly name and saved measurement function). Instrument profiles use the driver’s model/type and hardware serial number, so names and settings follow an identified instrument across node renames and communication methods. Only one connection per physical instrument is acquired at a time; connecting the same instrument elsewhere hands over to that connection. Settings can be copied from another saved instrument of the exact same type, without copying its name, identity or addresses. The bin asks whether to remove only that connection (keeping the profile), or the entire instrument with all its settings and connections. Profiles without connections remain under **Saved Instruments**. Existing profiles migrate automatically. Drivers without a usable serial number keep separate connection-local profiles. Edited connection settings remain saved even if the device is unavailable. Closing the window leaves the service running. **Background Service** shows service state and provides **Start**, **Stop**, and **Restart** through the current user’s systemd service, plus the acquisition interval. Stopping/restarting ends any active logging session; start logging again explicitly afterward. Controls report systemd errors in the window. Connecting never resets an instrument; an explicitly saved measurement function is restored on connection. Output enable states and sweeps are never replayed.

The initial service uses a 1-second polling interval. Change it in **Background Service** or through the agent API; instrument profiles and connection methods persist across service restarts. Deliberately disconnected instruments stay disconnected until explicitly connected. USBTMC equipment is rescanned at startup. Stop recording before changing interval. Slower equipment may determine the actual cycle rate.

### GNOME top bar

The installer copies the extension to your user extension directory. Log out and back in, then enable:

```bash
gnome-extensions enable gate@mawoodmain.github.io
```

The top bar displays a live measurement and recording dot. Click **GATE** and expand a device dropdown under **Display measurement** to select a channel. Real instruments appear before demos; each instrument has its own dropdown with readable channel names and live values. Updates preserve open dropdowns and keyboard focus, including when a value becomes unavailable. Selecting a Fluke function switches its primary measurement mode and pins that reading only after hardware confirmation. All ten primary functions are listed even when inactive; secondary-display controls are omitted. Selection lasts for the extension session. A dot marks the selected channel, and **Open GATE** opens the app. When the service stops, the indicator says offline; old samples are marked stale after five seconds. The service continues logging when you close the window.

### Connect the SDS1202X-E over USB

Connect the rear USB device port. Linux should load the `usbtmc` module and create `/dev/usbtmcN`. On the development host the scope was visible in sysfs as `SDS1202X-E`, VID `f4ed`, PID `ee3a`, with a root-owned `/dev/usbtmc3` node. Node numbers may change.

Install the supplied local-seat access rule:

```bash
sudo install -m 0644 data/70-gate-siglent.rules /etc/udev/rules.d/70-gate-siglent.rules
sudo udevadm control --reload-rules
sudo modprobe usbtmc
```

Unplug/replug the scope, then click **Scan** in the TMC section. The rule grants access through `uaccess` to the active local seat and applies to the Siglent USB identifiers; it does not make all USB devices world-writable. Do not run the desktop or service as root. If discovery still fails, check `ls -l /dev/usbtmc*` and `getfacl /dev/usbtmcN`.

To connect over a verified raw SCPI TCP endpoint, enter the host and port (normally 5025) using **Add…** in the Network section. Agents can use `tcp://HOST:5025`. Automatic LAN discovery, VXI-11, HiSLIP and GPIB transports are not implemented yet. Serial support currently targets the Fluke 45 prompt protocol. This raw TCP path is tested against a local simulator, not the attached scope's LAN interface.

## Try the bench without hardware

In the device manager click **Add demo devices**. Or start a demo service manually (stop an existing service first):

```bash
systemctl --user stop gate
gate service --demo
# In another terminal:
gate gui
```

Use the agent API to set 0–30 V on the simulated supply (see the agent guide). Applying a voltage or running a sweep never enables the output; explicitly click **Enable output**. A sweep includes its starting value, steps toward the endpoint, clamps to the exact endpoint, and holds the final value. Dwell must be at least the polling interval; each step holds for at least the requested dwell and actual timing is recorded. Cancel holds the last setpoint. Disable output also cancels the sweep. Closing the window leaves the service running; stopping the service closes devices and disables demo outputs.

Real output control is deliberately not implemented until a specific bench PSU model can be validated. The demo establishes the workflow and recording contract.

## Logging and export

Agents start/stop recording through the HTTP API or CLI commands below. All connected devices, including PSU settings, output state, and readback, enter the same recording. Instruments connected during recording join subsequent cycles. Stopping recording commits the session; CSV export also works against committed live data.

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

**This is software alignment, not simultaneous hardware sampling.** Scope measurements describe the scope's own acquisition, and timing uncertainty includes transport and instrument latency. Output commands and sweep steps have timestamped events. Missed deadlines are skipped without bursts; overruns are exposed in service state. Hardware triggers/clock distribution and waveform timebase alignment are future work.

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

See [Fluke 45 setup and verification](docs/fluke45.md), [architecture and driver development](docs/architecture.md), and [roadmap](docs/roadmap.md).

The GNOME menu regression can be run on GNOME 50 with `gnome-shell-test-tool --headless --disable-animations --extension PACKAGE.zip tests/extension_smoke.js` under a private `dbus-run-session`; build the extension package using `gnome-extensions pack extension`. It checks actual Shell menu actors, open state, keyboard focus, unavailable readings, recovery, and disconnects.

## Protocol references

- [Fluke 45 official user manual](https://media.fluke.com/d295e628-9a14-4bb4-9784-b10800c1cf0b_original%20file.pdf): serial framing, display function queries, modifiers, and `VAL?`.
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

### Verified SDS1202X-E USB behavior

On the attached scope (firmware 1.3.26), USB identification and both channels' PKPK/RMS/FREQ queries succeed. With its current inputs, C1 measured about 80 mV peak-to-peak and 27 mV RMS; C2 about 8 mV peak-to-peak and 3 mV RMS. Frequency replies were `****`, which GATE correctly displays as unavailable. These are communication checks, not accuracy calibration.

The USB transport uses blocking kernel reads with a bounded USBTMC timeout and checks the USBTMC EOM flag. It requests 48-byte payload chunks because this scope advertises 64-byte bulk endpoints at high speed; larger requests caused truncated identity packets and subsequent tag mismatches on the development host. No scope settings are changed.

## Agent service and device manager

See [AGENT_INSTRUMENT_GUIDE.md](AGENT_INSTRUMENT_GUIDE.md) for authentication, inspection, command completion, primary-function selection, logging, sweeps, raw commands and the known bench connection. Run `gate service --allow-raw` to opt into authenticated model-specific commands in addition to typed controls. The service binds only to localhost. `gate gui` opens the device manager; all monitoring lives in the top bar.

On the development host, the user service is enabled for graphical login and runs with raw commands explicitly enabled. After restarting, reacquire the runtime manifest because its bearer token rotates.
