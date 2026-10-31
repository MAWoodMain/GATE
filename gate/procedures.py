"""Bundled JSON examples and compatibility with pre-schema experiment definitions."""
import copy
import json
from pathlib import Path
from gate.test_spec import parameter_specs as _parameter_specs, parameter_values, expand_axes, sweep_values
from gate.expressions import Expression

# These are editor starting points, not runner procedure implementations.
PROCEDURES = {'capture': 'Measurement capture', 'efficiency': 'Converter efficiency example',
              'maximum_output_current': 'Maximum output current example'}
METRICS = {'elapsed_s': ('Elapsed time', 's')}


def control_roles(definition):
    result = {}
    for name, capability in [('supply', 'source.dc_voltage'), ('load', 'sink.dc_current')]:
        matches = [key for key, role in definition['roles'].items() if capability in role['capabilities']]
        if len(matches) != 1:
            raise ValueError('The converter example needs exactly one source role and one load role')
        result[name] = matches[0]
    if len(set(result.values())) != 2:
        raise ValueError('The converter example needs separate source and load roles')
    return result


def load_template(name, roles=None):
    if name not in PROCEDURES:
        raise ValueError('Unknown example template')
    spec = json.loads((Path(__file__).with_name('templates')/(name+'.json')).read_text())
    if name == 'capture' or roles is None:
        return spec
    mapping = control_roles({'roles':roles})
    for field in ('axes', 'setup', 'before_point'):
        for item in spec[field]:
            if 'role' in item:
                item['role'] = mapping.get(item['role'], item['role'])
    for item in spec['measurements'].values():
        item['role'] = mapping.get(item['role'], item['role'])
    spec['outputs'] = [mapping.get(role, role) for role in spec['outputs']]
    spec['limits'] = {mapping.get(role, role): bounds for role, bounds in spec['limits'].items()}
    return spec


def test_spec(definition, identity):
    value = definition['tests'][identity]
    if isinstance(value, dict) and 'schema_version' in value:
        return copy.deepcopy(value)
    if isinstance(value, dict) and set(value) != {'description', 'procedure'}:
        raise ValueError('A test requires schema_version: 1 or the legacy description/procedure fields')
    kind = value.get('procedure', 'capture') if isinstance(value, dict) else identity if identity in PROCEDURES else 'capture'
    spec = load_template(kind, definition['roles'])
    spec['description'] = value['description'] if isinstance(value, dict) else value
    return spec


def parameter_specs(spec):
    return _parameter_specs(load_template(spec) if isinstance(spec, str) else spec)


def parameters(spec, supplied, interval):
    """Compatibility helper; the runner uses parameter_values and its validated plan."""
    spec = load_template(spec) if isinstance(spec, str) else spec
    values = parameter_values(spec, supplied)
    expand_axes(spec, values)
    if spec['outputs'] and Expression(spec['acquisition']['settle_s'], values).evaluate(values) < interval:
        raise ValueError('Settling time must be at least the acquisition interval')
    return values
