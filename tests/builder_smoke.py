"""Run with GTK/display available under a private dbus-run-session."""
import copy
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gate.test_builder import Gtk, TestBuilder
from gate.procedures import load_template, test_spec
from gate.experiments import BUCK_CONVERTER
from gate.test_spec import validate_test

Gtk.init()
for name in ('capture', 'efficiency', 'maximum_output_current'):
    spec = load_template(name)
    builder = TestBuilder(spec, BUCK_CONVERTER['roles'])
    assert builder.get_spec() == spec
    builder.set_spec(spec)
    assert builder.get_spec() == spec
# Include the secondary measurements, waveform triggers and marker styles.
for name in BUCK_CONVERTER['tests']:
    spec = test_spec(BUCK_CONVERTER, name)
    builder = TestBuilder(spec, BUCK_CONVERTER['roles'])
    assert builder.get_spec() == spec

builder = TestBuilder(load_template('capture'))
testing = builder.forms['Testing']
presentation = builder.forms['Output visualization']
assert not set(testing.children) & {'plots', 'table', 'notes'}
assert set(presentation.children) == {'plots', 'table', 'notes'}
parameter = testing.children['parameters'][1].rows[0][2]
parameter.children['default'][1].entry.set_text('20')
assert builder.get_spec()['parameters']['capture_points']['default'] == 20
plots = presentation.children['plots'][1]
plot = plots.add_row(dict(kind='line', data='points', x='elapsed_s', y='elapsed_s', title='Elapsed'))
validate_test(builder.get_spec(), {})
assert builder.get_spec()['plots'][0]['title'] == 'Elapsed'
plots.add_row(dict(kind='scatter', data='points', x='elapsed_s', y='elapsed_s', title='Second'))
plots.move(plots.rows[1], -1)
assert builder.get_spec()['plots'][0]['title'] == 'Second'
plots.remove_row(plots.rows[0])
assert builder.get_spec()['plots'][0]['title'] == 'Elapsed'
# Duplicated identifiers must never silently overwrite a metric.
params = testing.children['parameters'][1]
params.add_row(copy.deepcopy(builder.get_spec()['parameters']['capture_points']), 'capture_points')
try:
    builder.get_spec()
except ValueError:
    pass
else:
    raise AssertionError('Duplicate identifier was accepted')
params.remove_row(params.rows[-1])
# Invalid execution edits still pass through the authoritative validator.
parameter.children['default'][1].entry.set_text('0')
try:
    validate_test(builder.get_spec(), {})
except ValueError:
    pass
else:
    raise AssertionError('Invalid parameter range was accepted')
parameter.children['default'][1].entry.set_text('20')
previous = builder.get_spec()
builder.raw.get_buffer().set_text('{invalid')
builder.load_json()
assert builder.error.get_text() and builder.form_spec() == previous
try:
    builder.get_spec()
except ValueError:
    pass
else:
    raise AssertionError('Unapplied JSON was ignored')
builder.raw.get_buffer().set_text(json.dumps(load_template('capture')))
builder.load_json()
assert builder.get_spec() == load_template('capture')
builder.refresh_json()
assert builder.json_spec() == builder.get_spec()
print('PASS: test builder template round trips, split forms, edits, ordering, validation and JSON import')

# Workflow choices cannot leave mutually exclusive JSON fields enabled together.
builder = TestBuilder(load_template('efficiency'), BUCK_CONVERTER['roles'])
original = builder.get_spec()
sweeps = builder.forms['Testing'].children['axes'][1]
sweep = sweeps.rows[0][2]
sweep.mode_picker.set_active(1)
values = sweep.children['values'][1]
values.add_row(8)
values.add_row(12)
axis = builder.get_spec()['axes'][0]
assert axis['values'] == [8, 12]
assert not {'start', 'stop', 'step'} & set(axis)
sweep.mode_picker.set_active(0)
assert builder.get_spec() == original
sweep.drive_toggle.set_active(False)
assert not {'role', 'control'} & set(builder.get_spec()['axes'][0])
sweep.drive_toggle.set_active(True)
assert builder.get_spec() == original
# Opening and closing a card is presentation-only.
card = sweeps.rows[0][0]
expander = card.get_first_child().get_first_child()
expander.emit('activate')
assert sweep.get_parent().get_visible()
expander.emit('activate')
assert not sweep.get_parent().get_visible()
assert builder.get_spec() == original
# Removing an entry can be undone without losing its edits or position.
sweeps.remove_row(sweeps.rows[0])
sweeps.undo_remove()
assert builder.get_spec() == original
# Friendly labels in a picker still serialize the metric identifier.
chart = builder.forms['Output visualization'].children['plots'][1].rows[0][2]
y = chart.children['y'][1]
y.picker.set_active_id('output_voltage_v')
assert builder.get_spec()['plots'][0]['y'] == 'output_voltage_v'
assert y.entry.get_text() != 'output_voltage_v'
builder.check_test()
assert 'Definition valid' in builder.error.get_text(), builder.error.get_text()
# Useful defaults and unique formula names for a newly added parameter.
params = builder.forms['Testing'].children['parameters'][1]
params.add_row(params.new_item(), params.new_key())
assert params.rows[-1][1].get_text()
new_parameter = params.rows[-1][2].value()
assert new_parameter['min'] <= new_parameter['default'] <= new_parameter['max']
assert new_parameter['step'] > 0
print('PASS: workflow modes, card disclosure, undo, named result choices and definition check')
