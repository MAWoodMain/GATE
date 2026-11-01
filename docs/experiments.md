# Experiments

An experiment defines functional equipment roles and wiring around a device under test (DUT), independently of instrument make or model. Several executable tests can share the same arrangement. A run binds actual instruments, validates its parameters, acquires readings, calculates metrics and saves its results.

The built-in buck converter arrangement connects the DC supply to the converter input and the electronic load to the converter output. Input and output return terminals are distinct; GATE does not assume they are common.

## Run an experiment

Open its sidebar page. **Summary** contains the DUT, functional requirements, wiring, tests and recorded metrics. Configure the run in the following tabs:

1. In **Instruments**, choose a compatible connected instrument for each role. Selections survive normal refreshes; a disconnected selection is marked unavailable rather than silently replaced. Primary/output roles require separate instruments; optional observation roles may reuse an instrument.
2. In **Parameters**, choose a test and set its parameters. Converter sweeps expose input-voltage and output-load start/stop/step values, settling time, samples per point, a supply current limit, expected output voltage and regulation tolerance. Capture exposes its number of polling cycles. The current acquisition interval determines the capture rate and minimum settling time.
3. In **Run**, tick the tests you want to execute. Checked tests run from top to bottom, with the individual settings you entered for each test in **Parameters**. For output sweeps, check the wiring/limits confirmation and select **Run Selected Tests & Enable Outputs**. This explicitly permits output enabling for that run. **Run Selected Tests** for a capture-only selection acquires current readings without changing hardware configuration or outputs.
4. **Stop Tests** cancels the current test and skips all remaining tests. Successful sequences open the **Results** tab, with a chart, table, full report and CSV export. The run selector retains recent results.

**Add Simulated Converter Bench** adds a coupled supply, 5 V converter and electronic load simulation, separate from the original demo instruments. The simulated DUT has a 3 A regulation knee and a load-dependent efficiency. It allows both converter tests to run end to end without real equipment. Runs containing simulation are labelled. Adding the simulator does not enable its outputs.

GATE still has no verified real PSU or electronic-load driver. Converter sweeps require drivers with verified typed controls and limits, so existing meters and scopes cannot fill those output roles. To record those instruments, create a role with **Capture current instrument readings** and choose **Measurement capture** as the test procedure. This reads the instrument's existing functions and leaves front-panel settings unchanged.

A sequence can contain up to 20 distinct tests. The first test is initially checked; changing the parameter-editor test does not change the Run-tab checkboxes. Values for each test are retained while switching between parameter pages. All selected tests and equipment limits are validated before any output changes. Outputs are disabled between tests. A failure, cancellation, connection change or service shutdown ends the sequence and marks pending tests skipped. Each executed test keeps a separate result, with a shared sequence ID and its position recorded in the run metadata.

Sequencing belongs to the background service and continues if the window closes. Queues never resume after service restart. Per-test status and overall sequence progress appear on the Run tab; finished results are selectable individually in Results.

## Secondary observations

Use **Instruments → Add Secondary Measurements…** for optional DUT/ambient temperature channels, ripple snapshots or pre-armed protection waveforms. Temperatures are recorded continuously, including settling, while waveform captures retain full samples, statistics and point context. Required observations can be configured in JSON. See [the observation contract and driver support](secondary-measurements.md).

## JSON tests, metrics and presentation

Tests use [declarative schema version 1](test-spec.md). The table below describes bundled JSON starting examples, not hard-coded runner procedures. Use **Parameters → Edit Test…** for the guided test builder; **Testing** edits execution and **Output visualization** edits charts/tables. **Advanced JSON** remains available for direct specification editing. The same builder appears under **New Experiment → Tests**. Custom numeric parameters automatically become form fields.

### Bundled examples

| Procedure | Parameters | Automatic metrics | Default output |
|---|---|---|---|
| Measurement capture | Number of polling cycles | All current channels on the assigned instruments, their units and quality | One time-series chart per metric, table and CSV |
| Converter efficiency sweep | Input voltage grid, load current grid, settling, averaging, current limit, expected output and tolerance | Input/output V and A, mean input/output power, efficiency, regulation status, setpoints and elapsed time | Efficiency versus measured output current, one curve per input-voltage setpoint |
| Maximum output current sweep | Same sweep and regulation parameters | Same per-point metrics, plus last acceptable measured current for each input voltage | Maximum acceptable output current versus input-voltage setpoint |

Sweeps include their endpoints, clamping the final step if necessary. Descending sweeps use a negative step; explicit value lists are also supported. A run is limited to 1000 grid points, with up to 100 samples per point. Samples are taken on successive acquisition cycles after settling. Input/output power is averaged from each voltage/current pair; efficiency is the ratio of the two mean powers. Zero input power gives unavailable efficiency, never a fabricated zero. Missing required readbacks fail an output sweep; capture preserves unavailable readings and quality flags.

Maximum-current tests stop increasing current at the first point outside the configured voltage tolerance, then continue at the next input voltage. If the first point fails, the maximum is unavailable. If every point passes, the result is labelled **at least sweep limit**, a lower bound rather than a measured failure threshold. This identifies a voltage-regulation boundary; it does not diagnose which protection mechanism caused it. Efficiency sweeps stop if the output leaves regulation. Negative DC readbacks, overvoltage and current-limit violations stop a sweep.

## Recording and lifecycle

Each run gets a private directory under `$XDG_DATA_HOME/gate/experiments/<run-id>` (default `~/.local/share/gate/experiments`). It contains:

- `run.sqlite`: raw samples, quality, per-instrument read windows, calculated points, summaries and run metadata.
- `run.json`: definition snapshot, equipment identities, simulation flags, parameters, units, chart specifications, status and paths.
- `points.csv`: all calculated/captured points. `summary.csv` contains the maximum-current summary when applicable.
- `report.html` and `plot-*.svg`: a standalone report with charts, every result row and run configuration.

The UI previews the first 20 result rows; the report and CSV include them all. **Export CSV…** exports summary rows for maximum-current tests and point rows for the other procedures. **Open Results Folder** exposes both CSVs and the raw database. Ordinary all-device logging can run alongside an experiment.

One experiment runs at a time on the acquisition owner. It remains active if the window closes. It disables the load and supply on completion, cancellation, acquisition failure and graceful service shutdown. Both output disables are attempted even if one fails; failures are surfaced in the run status. It does not restore previous enabled states. Driver output-disable methods must verify their result. Software cannot guarantee output disable after power loss, process termination or loss of communications. Interrupted runs are shown as interrupted after restart and never resume automatically.

Acquisition continues during settling, without blocking sleeps on the acquisition owner. A run records settled samples, not the intermediate settling readings. Read windows use one host monotonic timebase; instrument readings remain sequential, not hardware-synchronised. Configuration/function/output changes are rejected during a run; disconnecting or changing connections first cancels it. Saved experiment definitions can change without changing a running definition snapshot.

## Create a definition

Choose **New Experiment…** in the sidebar:

1. **Overview**: name, purpose, DUT and comma-separated terminal names, or load the buck converter example.
2. **Equipment**: add named roles, their terminals, and functional requirements.
3. **Wiring**: use terminal pickers to connect declared endpoints. Every terminal must participate. Removed endpoints must be chosen again.
4. **Tests**: give each test a name and description, then use the test builder or load a starting template. **Advanced JSON** supports direct specification edits; load that JSON into the builder before saving. Equipment role identifiers shown in the form are the identifiers used by controls and measurements.

**Create** validates and saves through the service. Errors retain the draft. Success opens the new page; **Cancel** discards the draft. Display names may repeat, but each new definition receives a unique ID.

## Agent API

`GET /v1/experiments` returns the definition map, also present in `/v1/state`. `GET /v1/experiment-runs` returns `sequence` (the current/latest test sequence), `active` (the current or most recently finished run) and `runs` (the latest 20 finished runs across experiments). `/v1/state` also exposes `experiment_run`, `experiment_runs`, `experiment_sequence`, `experiment_sequence_version: 1`, `experiment_spec_version: 1`, and `experiment_execution_version: 1`. Historical artifacts remain on disk beyond the 20-run UI/API list.

Using authenticated `GateClient`:

```python
api.command('demo_experiment')
operation = api.command('start_experiment',
    experiment='buck_converter', test='efficiency',
    bindings={'supply': 'demo:converter_supply', 'load': 'demo:electronic_load'},
    parameters={
        'input_start_v': 8., 'input_stop_v': 12., 'input_step_v': 2.,
        'load_start_a': .1, 'load_stop_a': 1., 'load_step_a': .3,
        'settle_s': 1., 'samples_per_point': 2,
        'current_limit_a': 3., 'nominal_output_v': 5., 'tolerance_pct': 5.,
    }, enable_outputs=True, wiring_confirmed=True)
# Command completion starts the asynchronous run; inspect run.state separately.
state = api.request('/v1/experiment-runs')
api.command('cancel_experiment')
```

To queue multiple tests, use `start_experiment_sequence` instead of `start_experiment`, with the same `experiment`, `bindings`, `enable_outputs` and `wiring_confirmed` arguments. Pass `tests=[{'test': 'efficiency', 'parameters': {...}}, {'test': 'maximum_output_current', 'parameters': {...}}]` in execution order. Each test has its own parameter object; defaults apply to omitted values. Command completion acknowledges the start of the sequence. Inspect `sequence.state`, `sequence.tests`, `sequence.index` and `sequence.completed_tests` for progress. `cancel_experiment` stops the sequence as well as its active test.

`save_experiment`, `delete_experiment` and `check_experiment` manage definitions and check role bindings. `check_experiment` checks functional capability declarations only; starting an output sweep additionally validates verified controls and operating limits. Neither saving nor checking changes outputs.

Definitions contain `id`, `name`, `description`, `dut`, `roles`, `connections` and `tests`. Equipment roles contain `name`, `capabilities` and `terminals`. Connections join declared `component.terminal` endpoints, reserving `dut` for the DUT. Test values use the [versioned JSON test contract](test-spec.md), including their own parameter, sweep, measurement, calculation and rendering definitions. Legacy `{description, procedure}` objects and string descriptions are converted to schema version 1 on load/save. Unknown fields, references and expression syntax are rejected; descriptions are never evaluated as code. The structural JSON Schema is bundled at `gate/templates/test-v1.schema.json`.

## Driver contract

### Channel and range boundary

The [instrument channel/range interface](instrument-ranges.md) does not change
existing experiment JSON. Scalar `channel` fields are reading keys, while
`quantity` uses the driver's `experiment_channels` mapping. Neither configures a
meter function or range. Configure those before starting a run; schema-v1 tests
have no channel/function/range setup block, and run metadata does not yet retain
a complete applied-function/range snapshot. Auto defaults in the top bar are
display-selection preferences, not a per-experiment default.

Mandatory equipment roles require distinct instrument IDs; optional observation
roles can reuse one. Channel-specific role bindings and output controls are not
implemented, so separate source/measurement channels of one SMU cannot yet fill
two mandatory roles. The generic channel catalog supports such devices, but this
does not imply an SMU hardware driver or channel-aware experiment execution.

### Verified output controls

`experiment_capabilities` is a collection of stable functional identifiers. `measure.scalar` means the driver's current `read()` channels can be captured; it does not promise every selectable function at once. Existing Fluke and Siglent drivers expose this passive capability. DC converter roles use `source.dc_voltage`, `sink.dc_current`, `measure.dc_voltage` and `measure.dc_current`.

An output driver must additionally opt into `experiment_control_supported = True`, provide finite `experiment_limits = {'voltage': [min, max], 'current': [min, max], 'power_w': rating}` and `experiment_channels = {'voltage': channel_id, 'current': channel_id}`. Both readbacks must return V/A readings with quality. A supply implements verified `set_voltage`, `set_current_limit`, and `set_enabled`; a load implements verified `set_current` (including safe zero) and `set_enabled`. These methods must bound I/O, enforce model-specific limits and confirm actual state, especially output enable/disable. Limit checks use the planned input range/current limit and the expected output voltage, tolerance and load range. They do not replace DUT-specific ratings, physical wiring checks, interlocks or driver protections.

GATE never infers output controls from instrument identity, category or arbitrary SCPI access. Output sweeps reject mixed real/simulated bindings. JSON-defined sweeps, numeric expressions, grouped reductions, stop conditions, tables and charts are supported without adding procedure code. Multi-channel instrument assignment, arbitrary scripts and adaptive searches remain future work. Drivers may declare `experiment_required_controls` so a generic output test cannot enable an instrument without configuring its required controls.
