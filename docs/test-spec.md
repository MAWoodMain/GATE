# Declarative test specification (version 1)

A test is a JSON object, not a Python procedure name. The same interpreter expands its axes, applies verified typed controls, acquires channels, evaluates formulas, checks conditions, reduces groups and renders plots. Labels, units, parameter forms and result tables also come from the definition.

The structural [JSON Schema](../gate/templates/test-v1.schema.json) supports editor validation. GATE additionally checks role references, expression names and dependencies, dataset columns, expanded grid sizes, acquisition settings and actual driver limits. Unknown fields and unsupported versions are rejected. A definition is validated when saved, and its parameterised plan is validated again before execution. Every test in a sequence is preflighted before any output is enabled.

Complete bundled examples are [passive capture](../gate/templates/capture.json), [converter efficiency](../gate/templates/efficiency.json) and [maximum output current](../gate/templates/maximum_output_current.json). These files are starting templates; their names have no special meaning to the runner. Existing description/procedure definitions are converted to this format when loaded or saved.

## A complete single-axis example

This test assumes an experiment equipment role named `bias`, assigned to a verified DC supply with voltage/current readback. It sweeps the supply voltage and plots its readback and tracking error. The experiment still separately defines its DUT, role requirements and physical wiring.

```json
{
  "schema_version": 1,
  "label": "Source voltage tracking",
  "description": "Sweep the bias supply and compare its readback with the command.",
  "parameters": {
    "start_v": {"label": "Start", "unit": "V", "default": 1, "min": 0, "max": 30, "step": 0.1, "type": "number"},
    "stop_v": {"label": "Stop", "unit": "V", "default": 5, "min": 0, "max": 30, "step": 0.1, "type": "number"},
    "step_v": {"label": "Step", "unit": "V", "default": 1, "min": -30, "max": 30, "step": 0.1, "type": "number"},
    "limit_a": {"label": "Current limit", "unit": "A", "default": 1, "min": 0.01, "max": 5, "step": 0.1, "type": "number"}
  },
  "axes": [
    {"id": "command_v", "label": "Commanded voltage", "unit": "V", "role": "bias", "control": "voltage", "start": "start_v", "stop": "stop_v", "step": "step_v"}
  ],
  "setup": [{"role": "bias", "control": "current_limit", "value": "limit_a"}],
  "before_point": [],
  "outputs": ["bias"],
  "limits": {"bias": {"voltage": "max(start_v, stop_v)", "current": "limit_a"}},
  "acquisition": {"settle_s": 1, "samples": 2, "repeat": 1, "capture_all": false, "on_invalid": "fail"},
  "measurements": {
    "measured_v": {"label": "Readback voltage", "unit": "V", "role": "bias", "quantity": "voltage", "required": true}
  },
  "derived": {
    "error_v": {"label": "Tracking error", "unit": "V", "expression": "measured_v - command_v", "aggregate": "point"}
  },
  "checks": [{"stage": "sample", "expression": "abs(measured_v) <= 30", "message": "Readback exceeded the configured range"}],
  "plots": [
    {"kind": "line", "data": "points", "x": "command_v", "y": "measured_v", "title": "Voltage tracking"},
    {"kind": "scatter", "data": "points", "x": "command_v", "y": "error_v", "title": "Tracking error"}
  ],
  "table": {"data": "points", "columns": ["command_v", "measured_v", "error_v", "quality"]}
}
```

Use **Parameters → Edit Test…** to open the GUI test builder for an existing test, or **New Experiment → Tests** to build one from a template. **Testing** contains parameters, sweep axes, controls, operating limits, acquisition, measurements, formulas, checks and reductions. **Output visualization** separately contains plots, table columns and result notes. Use the sidebar to move through adjustable inputs, sweeps, equipment controls, measurements, calculations and stop conditions. Open a card to edit it; its heading summarizes the current settings. New entries have sensible starting values and unique formula names. Choose a sweep mode or measurement source from a single selector, and select equipment/results by name. Use Add/Remove and the arrow buttons to manage entries; Undo removal restores the last removed entry. Less common settings are under More options. Use Check test to validate the definition before saving. The chart layout preview shows axis labels; measurement data appears after a run. Formula names must be unique. Enter expressions directly, without JSON quotes, for example `start_v + 1`.

**Advanced JSON** supports existing specifications: paste JSON and click **Load JSON into builder** before saving. **Refresh JSON from builder** copies form edits into the JSON editor. Unapplied JSON blocks saving so edits cannot be silently discarded. Equipment role identifiers are shown in the creation form. The separate Description field in New Experiment supplies the narrative when non-empty.

The split is in the editor: execution and visualization remain in one schema-version-1 JSON document for compatibility with existing API clients, templates and saved runs. Saving still uses the full reference/expression/dataset validator. No running test is modified: saving changes the experiment definition, and each started run retains its own specification snapshot.

For a complete example using the forms, see the [screenshotted voltage sweep walkthrough](voltage-sweep-guide.md).

## Fields and execution order

| Field | Meaning |
|---|---|
| `schema_version` | Integer `1` |
| `label`, `description` | Display name and purpose |
| `parameters` | Named numeric settings, each with `label`, `unit`, `default`, `min`, `max`, `step`, `type` (`number` or `integer`); these generate the Parameters form |
| `axes` | Ordered sweep dimensions; the first is outermost and the last is innermost |
| `setup` | Typed controls applied once, with outputs disabled |
| `before_point` | Typed controls applied before the axes at every point, for example removing the load before changing the input voltage |
| `outputs` | Roles explicitly enabled after setup and the first point's controls, in the stated order; they are disabled in reverse order at start and on termination |
| `limits` | Maximum planned voltage/current envelope for every output role, expressed from parameters |
| `acquisition` | Settling duration, samples per point, capture repetition, passive channel discovery, invalid-reading policy |
| `secondary_measurements` (optional) | Named optional scalar observations, with temperature/location metadata; continuous logging plus settled point averages |
| `waveforms` (optional) | Settled or armed transition captures, conditions, bounds and trigger definitions |
| `measurements` | Named channels with labels, units, equipment role, and `required` flag |
| `derived` | Calculated metrics and their averaging stage |
| `checks` | Boolean conditions that must hold at sample or point stage; failure ends the test and sequence |
| `stop_when` (optional) | Boolean condition that successfully ends the run or the current reduction group |
| `reduce` (optional) | Grouped summary metrics, filter condition and optional boundary status labels |
| `plots` | Zero or more chart definitions referencing the points or summary dataset |
| `table` | Default result/CSV dataset and optional ordered column list |
| `notes` (optional) | Explanatory result notes, for example distinguishing a sweep-limit lower bound from a measured threshold |

A sweep axis has `id`, `label`, `unit`, and either `start`/`stop`/`step` expressions or a non-empty `values` array of numeric expressions. Set `role` and `control` together to drive hardware; omit both for a virtual axis that only supplies values to calculations. Start/stop/step include the exact endpoint, clamping the last step. Descending sweeps use a negative step. Zero steps, repeated setpoints and steps pointing away from the endpoint are rejected. Up to four axes and 1000 total grid points are supported.

Every control is `{role, control, value}`. The operation registry contains `voltage`, `current`, and `current_limit`, mapped to verified driver methods. JSON cannot specify a Python method name, raw SCPI command, import, shell command or transport operation. Drivers must advertise experiment control support, enforce their model's constraints and provide verified output-enable/disable methods. Every driver-required control must be supplied. All expanded setpoints, zero-load actions and setup controls must fit both the declared operating envelope and driver ranges; the voltage/current envelope product must fit the driver power rating. Outputs still require explicit Run-tab confirmation. No controls or outputs means passive acquisition, including real meter/scope readings.

`acquisition` contains:

- `settle_s`: nonnegative duration expression, up to 3600 s; output tests require at least the current polling interval.
- `samples`: integer expression, 1–100 successive polling cycles per point.
- `repeat`: integer expression, 1–1000 points for an axis-free capture; it must be 1 when sweep axes are present.
- `capture_all`: when true, also records each assigned instrument's current channels using keys such as `bias.voltage`. Disabled channels remain in raw data but are not plotted automatically.
- `on_invalid`: `record` or `fail` for missing required measurements. Output tests require `fail`.

Measurement entries use exactly one of `channel` (a driver reading channel such as `C1.RMS` or `primary.VDC`) and `quantity` (a logical key mapped by the driver's `experiment_channels`, such as `voltage`). Units must match the driver reading for a value to be valid. Selecting a channel does not switch the instrument's front-panel function. A required unavailable readback fails an output test immediately, even during a multi-sample average. Raw samples retain units, quality and read windows. Optional or record-only missing data produces NULL, with unavailable/invalid point quality.

## Expressions and averaging

An expression is a JSON number/boolean/null or a string using declared parameter, axis and metric identifiers. `elapsed_s` is supplied by the runner. Examples:

```json
{
  "input_power_w": {"label": "Input power", "unit": "W", "expression": "input_voltage_v * input_current_a", "aggregate": "sample_mean"},
  "output_power_w": {"label": "Output power", "unit": "W", "expression": "output_voltage_v * output_current_a", "aggregate": "sample_mean"},
  "efficiency_pct": {"label": "Efficiency", "unit": "%", "expression": "100 * output_power_w / input_power_w", "aggregate": "point"}
}
```

`sample_mean` evaluates on each acquired sample and then averages the calculated values. `point` evaluates after measured channels and sample-mean calculations have been averaged. Thus mean instantaneous power and the product of mean voltage/current remain distinct. Dependencies are sorted automatically; unknown references, cycles and a sample calculation depending on a point calculation are rejected.

Allowed syntax: arithmetic `+ - * / % **`, comparisons, boolean `and/or/not`, conditional `a if condition else b`, and `abs`, `min`, `max`, `sqrt`, `log`, `log10`, `is_valid`, `coalesce`. Powers are bounded to exponents between -16 and 16. Functions accept at most eight arguments; expressions are limited to 256 characters and 128 AST nodes. Attributes, indexing, comprehensions, arbitrary calls, containers and string literals are rejected. The implementation walks a validated syntax tree and does not call Python `eval`.

Missing input, division by zero, domain errors and non-finite calculations propagate NULL rather than a fabricated zero. `coalesce(value, fallback)` deliberately substitutes a fallback; `is_valid(value)` tests availability. Boolean checks must evaluate to true; an unavailable check fails closed. Missing stop conditions also fail closed. Units are declared metadata, not a dimensional-analysis proof. Expressions over auto-discovered channel names containing dots are not supported; declare an explicit measurement identifier for calculations.

## Stopping and grouped summaries

Checks (`{stage, expression, message}`) assert conditions at `sample` or `point` stage. They fail the test. `continuous` evaluates every polling cycle, including settling, using current scalar values. A successful early stop uses `stop_when: {expression, scope}`, where scope is `run` or `group`.

A group stop requires `reduce`. Its `group_by` is an ordered prefix of the axis IDs: for an input-voltage/output-load grid, grouping by input voltage creates one summary per voltage. `where` filters accepted points. Each summary metric has `label`, `unit`, `expression`, and a `reducer` of `first`, `last`, `min`, `max`, `mean`, or `count`. Null values are excluded; empty reductions are NULL, except count is zero. Optional `status: {field, stopped, exhausted}` writes caller-defined boundary labels. Reusing a point metric name in a summary requires the same label and units.

The maximum-current example expresses regulation as a derived boolean, stops the current group on `not in_regulation`, filters summary inputs with `where: in_regulation`, and reduces the last acceptable measured current. Its JSON status and marker definitions distinguish a detected threshold from a sweep ending while still in regulation. The interpreter contains no maximum-current-specific branch.

## Rendering contract

Each plot supplies `kind` (`line` or `scatter`), `data` (`points` or `summary`), `x`, `y`, and `title`. Optional fields:

- `series`: a grouping column; for example one efficiency curve per input voltage.
- `x_label`, `y_label`: explicit labels; otherwise inferred from metric labels and units.
- `x_scale`, `y_scale`: `linear` (default) or `log`. Nonpositive values on log axes are excluded.
- `markers`: `{field, styles}`, mapping category strings to `{shape, label}`. Shapes are circle, triangle or square, with a generated legend.

Null data makes a gap in a line. If a plot has no valid points, it says so. Scalar captures with `capture_all: true` and no explicit plots default to one time-series chart per recorded metric; otherwise an empty plot list means no charts. The same definitions drive SVG files, the desktop Results tab and the standalone HTML report.

`table.data` selects the default displayed/exported dataset, and `table.columns` optionally selects/arranges columns. Both full `points.csv` and `summary.csv` remain available regardless of that presentation choice. Every run stores the complete test JSON, resolved parameters, metrics, presentation and definition snapshot in `run.json` and SQLite. The UI previews the first 20 table rows; reports and exports retain the full chosen dataset.

## Secondary observation extension

Schema version 1 additionally supports optional role bindings, `secondary_measurements` and `waveforms`. Full details and examples are in [secondary measurements](secondary-measurements.md). Secondary-stream graphs use `data: "secondary"`, `x: "elapsed_s"` and a single secondary scalar as `y`; these graphs do not support series grouping. Waveform statistics become ordinary point metrics after capture, with raw calibrated traces stored separately. Optional observation failures do not invalidate primary data; secondary quality is reported separately.
