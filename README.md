# GATE

**Gnome Automatic Test Equipment** — an agent-operated Ubuntu instrument service with a GNOME top bar for human monitoring and a small device-management/settings window.

This is the initial application foundation. The GTK4 desktop, D-Bus acquisition service, SQLite recorder, CSV export, extension, and simulated bench are implemented. Hardware support includes **Keysight 344xxA bench DMMs** (protocol-tested; bench verification pending), **Hantek 365-series USB meters**, the **Fluke 45** over RS-232 and **Siglent SDS1202X-E**, reading C1/C2 peak-to-peak, RMS, and frequency using SCPI over Linux USBTMC or a manually supplied TCP endpoint. USB identity and all six scalar queries have been verified against an attached SDS1202X-E running firmware 1.3.26; comparison against calibrated signals and the front panel is still pending. Experiment waveform capture now supports calibrated traces, with a protocol-tested SDS1202X-E native-snapshot, single-shot and screenshot paths; USB screenshots have been verified on the attached scope; waveform accuracy and single-shot triggering await bench verification. The demo supports settled and armed transition captures.

Agents: start with [the instrument control guide](AGENT_INSTRUMENT_GUIDE.md). The authenticated loopback HTTP API listens on port 8765 by default; its private token/URL are discovered from `$XDG_RUNTIME_DIR/gate/control.json`.

## Equipment support

| Category | Real hardware | Demo bench |
|---|---|---|
| Multimeters | Keysight 34401A/10A/11A/60A/61A/65A/70A over USBTMC or raw TCP; Fluke 45 over RS-232; Hantek 365 USB, with 365B temperature acquisition verified | DC voltage |
| Bench power supplies | Driver interface ready; model drivers pending | Voltage/current readback, static voltage, stepped sweeps, output enable |
| Oscilloscopes | SDS1202X-E scalar acquisition verified; snapshots, single shots and screenshots protocol-tested, bench verification pending | Scalar readings plus converter ripple/protection traces |
| Temperature probes | Hantek 365B thermocouple: Celsius/Fahrenheit acquisition and recording verified | DUT and ambient temperatures |

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

Keep the checkout in place: the installer creates an editable virtual environment inside it and a launcher in `~/.local/bin`. Add that directory to your PATH if `gate` isn't found. The service runs as your normal user. The window follows the desktop theme, with a left sidebar for **Devices & Settings** and each saved experiment. Experiment pages provide equipment selection, test parameters, execution, progress and saved charts/tables alongside the DUT, wiring and test descriptions. **Devices & Settings** groups equipment into **TMC**, **USB**, **Serial**, and **Network** settings sections. Each section has its own **Add…** dialog; TMC and USB also have **Scan** for accessible USBTMC nodes and known Hantek meters. Serial settings include the device path and baud rate (8N1); Network settings include host and TCP port. Device actions use consistent icon buttons: **Connect/Disconnect**, **Settings**, and a **bin**. The Settings dialog separates **Connection** (path/baud or host/port) from **Instrument** (friendly name and saved measurement function). Instrument profiles use the driver’s model/type and hardware serial number, so names and settings follow an identified instrument across node renames and communication methods. Only one connection per physical instrument is acquired at a time; connecting the same instrument elsewhere hands over to that connection. Settings can be copied from another saved instrument of the exact same type, without copying its name, identity or addresses. The bin asks whether to remove only that connection (keeping the profile), or the entire instrument with all its settings and connections. Profiles without connections remain under **Saved Instruments**. Existing profiles migrate automatically. Drivers without a usable serial number keep separate connection-local profiles. Edited connection settings remain saved even if the device is unavailable. Closing the window leaves the service running. **Background Service** shows service state and provides **Start**, **Stop**, and **Restart** through the current user’s systemd service, plus the acquisition interval. Stopping/restarting ends any active logging session; start logging again explicitly afterward. Controls report systemd errors in the window. Connecting never resets an instrument; an explicitly saved measurement function is restored on connection. Output enable states and sweeps are never replayed.

The initial service uses a 1-second polling interval. Change it in **Background Service** or through the agent API; instrument profiles and connection methods persist across service restarts. Deliberately disconnected instruments stay disconnected until explicitly connected. USBTMC equipment is rescanned at startup. Stop recording before changing interval. Slower equipment may determine the actual cycle rate.

### GNOME top bar

The installer copies the extension to your user extension directory. Log out and back in, then enable:

```bash
gnome-extensions enable gate@mawoodmain.github.io
```

The top bar can show any number of concurrent measurements from each instrument, or none. Click **GATE**, expand an instrument under **Measurements in top bar**, and toggle the readings you want. Toggles stay open while you make several choices; live updates preserve the menu and keyboard focus. Scope channels can be shown together, as can readings from different instruments. Mutually exclusive functions (the Fluke 45 primary display) replace one another and switch hardware only when checked, after confirmation. Unchecking a reading hides it without changing the instrument function or stopping logging. The Fluke secondary display remains outside the function picker. With everything unchecked, only **GATE** and the recording dot remain. Choices are saved with the instrument profile, including an explicit empty selection, and follow it across connection methods and service/extension restarts. Missing instruments remain in the picker so their offline readings can also be hidden. Existing profiles without display preferences initially show one available reading per instrument. **Open GATE** opens device management. When the service stops the indicator says offline; stale values are marked after five seconds.

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

## Experiments

An **experiment** defines equipment roles and wiring around a device under test, independently of instrument make or model. For example, a DC supply feeds a buck converter's input while an electronic load draws current from its output. The same arrangement supports efficiency sweeps across input voltages and loads, or maximum-output-current tests at each input voltage.

GATE now stores experiment definitions, named test descriptions and DUT terminal connections. Choose **New Experiment…** in the sidebar to create one using the Overview, Equipment, Wiring and Tests tabs, or start with the buck converter example. Agents can assign connected instruments to functional roles and check declared capabilities through the API. The built-in buck converter example requires DC sourcing, current sinking, and input/output voltage/current measurements. Experiments now run measurement captures, efficiency sweeps and maximum-output-current sweeps. Tests use a generic, versioned JSON specification for parameters, sweep axes, channel measurements, derived formulas, checks, grouped summaries and chart/table rendering. The built-in converter tests are JSON examples of that interpreter. Build or edit a test through **Parameters → Edit Test…**, with task-based navigation, editable summary cards, named equipment/result pickers, and separate **Testing** and **Output visualization** settings. Less common options and an **Advanced JSON** editor remain available. See [the JSON test contract](docs/test-spec.md) and [structural schema](gate/templates/test-v1.schema.json). Use **Add Simulated Converter Bench** to try the converter workflows; real output sweeps require verified supply/load drivers, which are still pending. **Run Selected Tests & Enable Outputs** explicitly arms the selected sweeps. The Run tab has per-test checkboxes for sequential execution, using each test’s own parameters and retaining separate results. Stop or a failure skips the remaining tests. Results include a raw SQLite recording, CSV, SVG charts and a standalone report, with recent runs retained across service restarts. See [experiments and the agent interface](docs/experiments.md).

### Secondary observations

Use **Instruments → Add Secondary Measurements…** to add DUT/ambient temperatures, ripple snapshots and armed protection captures to an existing experiment. Observation equipment can be optional. Temperatures record through settling; waveforms retain full calibrated samples, scalar statistics, sweep settings and timing. Results include waveform browsing, physical scope screenshots for single shots when supported, and secondary CSV export. Scope polling leaves off channels alone; experiments temporarily select only their configured channels and restore the previous trace states afterward. Required observations fail closed; missing optional observations remain unavailable rather than becoming zero. See [secondary measurements and driver support](docs/secondary-measurements.md).

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
dbus-run-session -- python3 tests/experiment_editor_smoke.py
dbus-run-session -- python3 tests/builder_smoke.py
dbus-run-session -- python3 tests/experiment_run_smoke.py
# For headless CI, install xvfb and use:
dbus-run-session -- xvfb-run -a python3 tests/desktop_smoke.py
```

See [Keysight 344xxA setup and verification](docs/keysight344xxa.md), [Hantek 365 setup and verification](docs/hantek365.md), [Fluke 45 setup and verification](docs/fluke45.md), [architecture and driver development](docs/architecture.md), and [roadmap](docs/roadmap.md).

The GNOME menu regression can be run on GNOME 50 with `gnome-shell-test-tool --headless --disable-animations --extension PACKAGE.zip tests/extension_smoke.js` under a private `dbus-run-session`; build the extension package using `gnome-extensions pack extension`. It checks actual Shell toggles, multiple/zero readings across instruments, open state, keyboard focus, unavailable readings, recovery, disconnected-device hiding, exclusive function switching, failure rollback and stable identity across transport changes.

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

### Hantek 365 USB meters

Hantek 365-series direct USB support is available under **Devices & Settings → USB → Scan**, including temperature, electrical measurement modes, top-bar readings, recording and secondary experiment measurements. Celsius/Fahrenheit acquisition has been checked with the attached 365B and supplied thermocouple. See [Hantek setup, probe connections and verification limits](docs/hantek365.md).

Temperatures default to **°C** across device readings, new experiments, plots, reports and recordings. Fahrenheit remains an explicit measurement choice.

### Keysight 344xxA bench DMMs

Connect supported Keysight, Agilent and HP meters through TMC Scan or a raw TCP endpoint. Primary function selection, scalar readings, logging and DC voltage/current experiment channels are implemented. See [supported models, trigger settings, USB access and verification limits](docs/keysight344xxa.md).
