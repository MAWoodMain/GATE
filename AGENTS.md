# Working in GATE

GATE is an Ubuntu instrument acquisition/control service with a GTK4 application,
a GNOME Shell extension and a local agent API. Python requires 3.10+; the core
uses the standard library, while GTK/D-Bus use system PyGObject packages.

## Read the relevant contracts

- [README.md](README.md): setup, supported hardware and development commands.
- [AGENT_INSTRUMENT_GUIDE.md](AGENT_INSTRUMENT_GUIDE.md): operating the service
  through its agent API; this is separate from these contributor instructions.
- [Architecture](docs/architecture.md): acquisition ownership, identity and storage.
- [Channels and ranges](docs/instrument-ranges.md): driver catalogs and selection.
- [Experiments](docs/experiments.md), [test JSON](docs/test-spec.md) and
  [secondary observations](docs/secondary-measurements.md): execution contracts.
- Driver-specific documents under `docs/` and [package migration](packaging/INSTALL.md)
  when changing hardware support or installation.

## Code map

- `gate/engine.py` owns instrument I/O, command execution and cached state.
- `gate/control.py` and `gate/client.py` implement the authenticated loopback API.
- `gate/drivers/` contains model-specific drivers and simulations;
  `gate/transport.py`, `gate/serial.py` and `gate/hantek_usb.py` own protocols.
- `gate/model.py`, `gate/channels.py` and `gate/ranges.py` define capabilities.
- `gate/experiments.py`, `gate/test_spec.py`, `gate/experiment_runs.py` and
  `gate/secondary_capture.py` validate and execute experiments/observations.
- `gate/app.py`, `gate/experiment_editor.py`, `gate/test_builder.py` and
  `gate/experiment_view.py` implement the GTK interface.
- `extension/extension.js` implements GNOME top-bar menus and monitoring.
- `gate/templates/` contains JSON templates and the structural schema;
  `scripts/`, `packaging/` and `.github/workflows/tests.yml` handle distribution.

## Preserve these invariants

- Keep instrument I/O on the acquisition owner. UI/HTTP inspection reads cached
  state; capability catalog methods must not perform hardware I/O. Bound all
  transport operations and confirm configuration changes before reporting success.
- An instrument can have any number of physical/logical channels, each with
  different functions. `instrument_channel` is ownership; a function's `channel`
  is its reading key. Function/control IDs are unique within the instrument;
  exclusive groups are scoped to their owning channel. Do not assume two inputs,
  one global primary function, or identical function sets across channels.
- Driver-declared choices determine range controls. Preserve saved manual ranges;
  use Auto only where advertised. Scope input scales and measurement full scales
  are not output safety ratings. Never invent capabilities from an identity or
  apply one model's commands/limits to another without verified support.
- Keep physical instrument identity separate from connection addresses. Preserve
  profiles across transport changes; do not merge devices with unusable serials.
- Preserve units, nulls and quality flags. Unavailable, overloaded, relative or
  incompatible readings must not silently become valid absolute measurements.
- Do not reset, calibrate, change live wiring/functions or enable real outputs
  merely to discover or test equipment. Operate the live bench only within the
  user's authorized task. Prefer protocol fixtures and simulated instruments.
- Retain explicit output/wiring confirmation, typed driver controls, operating
  limits and verified disable attempts on normal cleanup. Never replay output
  enable state on reconnect. Configuration changes remain blocked during runs.
- Test JSON stays declarative and validated: no arbitrary Python, shell or raw
  transport commands. Keep existing schema-v1 definitions compatible or provide
  an explicit version/migration path. Distinguish implemented features from future
  channel-specific experiment bindings and function/range setup.
- Preserve top-bar actor identity, open ancestors and keyboard focus across live
  refreshes. Wait for command confirmation; failed changes retain saved choices.
- Never print, commit or document live bearer tokens. Keep personal profiles,
  recordings and generated build artifacts out of commits.

## Validation

Use focused tests during development and the relevant checks from CI before
finishing. For Python behavior changes, run:

```bash
/usr/bin/python3 -m unittest discover -s tests -v
/usr/bin/python3 -m compileall -q gate scripts
```

For extension/installer changes, also use the relevant syntax checks:

```bash
node --input-type=module --check < extension/extension.js
bash -n install.sh
```

For GTK changes, run affected smoke scripts (`desktop_smoke.py`,
`experiment_editor_smoke.py`, `builder_smoke.py`, `experiment_run_smoke.py`)
under a private session bus, using `xvfb-run -a` when headless. For example:

```bash
dbus-run-session -- xvfb-run -a /usr/bin/python3 tests/desktop_smoke.py
```

For top-bar changes, pack the extension into a temporary directory and run
`tests/extension_smoke.js` with `gnome-shell-test-tool --headless
--disable-animations --extension <zip>` under a private `dbus-run-session` when
the tool is available. Do not use the user's active Shell as a test harness.
Report unavailable checks and environment failures accurately.

Documentation-only edits need appropriate link, command/schema coverage and
example checks, not an unrelated full hardware/desktop test run. Exercise new
operational examples against an ephemeral simulated service when practical.

## Documentation and delivery

Update the agent guide when commands, arguments, state fields, capabilities or
limitations change. Every action in `gate.control.ACTIONS` should remain
discoverable there. Keep detailed contracts, templates, UI labels and verification
status consistent; distinguish protocol-fixture tests from bench verification.
Treat documented bench wiring/addresses as historical observations, not live state.

Editing a checkout does not install its copied GNOME extension. If the task calls
for a visible installed update, inspect the active extension/service paths, install
the authorized changes, and verify their contents. GNOME may require a new login
after copying changed JavaScript; do not log the user out automatically. If only
source was changed, say so rather than claiming the installed UI has updated.

Use a `codex/` branch by default; keep fixes separate from already merged feature
branches. Stage only intended files. Report what changed, the checks performed,
and whether changes were committed, pushed or installed. Use force-with-lease only
for an authorized history rewrite, never an unconditional force push.
