"""Executable test contracts shared by the service, editor and run page."""
import math

PROCEDURES = {'capture': 'Measurement capture', 'efficiency': 'Converter efficiency sweep',
              'maximum_output_current': 'Maximum output current sweep'}
# key: label, default, minimum, maximum, increment, integer
COMMON = {
    'input_start_v': ('Input start (V)', 8., .01, 1000., .5, False),
    'input_stop_v': ('Input stop (V)', 12., .01, 1000., .5, False),
    'input_step_v': ('Input step (V)', 2., .001, 1000., .1, False),
    'load_start_a': ('Load start (A)', .1, 0., 1000., .1, False),
    'load_stop_a': ('Load stop (A)', 1., 0., 1000., .1, False),
    'load_step_a': ('Load step (A)', .3, .001, 1000., .1, False),
    'settle_s': ('Settling time (s)', 1., .1, 3600., .1, False),
    'samples_per_point': ('Samples per point', 1, 1, 100, 1, True),
    'current_limit_a': ('Supply current limit (A)', 3., .001, 1000., .1, False),
    'nominal_output_v': ('Expected output (V)', 5., .001, 1000., .1, False),
    'tolerance_pct': ('Output regulation tolerance (%)', 5., .1, 50., .5, False),
}


def parameter_specs(kind):
    if kind == 'capture':
        return {'capture_points': ('Number of polling cycles', 10, 1, 1000, 1, True)}
    specs = dict(COMMON)
    if kind == 'maximum_output_current':
        specs['load_stop_a'] = ('Load stop (A)', 4., 0., 1000., .1, False)
        specs['load_step_a'] = ('Load step (A)', .5, .001, 1000., .1, False)
    return specs


def test_spec(definition, identity):
    value = definition['tests'][identity]
    if isinstance(value, dict):
        return dict(value)
    # Compatibility for descriptions created before executable procedures existed.
    kind = identity if identity in ('efficiency', 'maximum_output_current') else 'capture'
    return dict(description=value, procedure=kind)


def control_roles(definition):
    roles = {}
    for role, requirement in definition['roles'].items():
        for key, capability in [('supply', 'source.dc_voltage'), ('load', 'sink.dc_current')]:
            if capability in requirement['capabilities']:
                if key in roles:
                    raise ValueError('Converter procedures require exactly one DC supply role and one load role')
                roles[key] = role
    if set(roles) != {'supply', 'load'} or roles['supply'] == roles['load']:
        raise ValueError('Converter procedures require separate DC supply and electronic load roles')
    return roles


def sweep_values(start, stop, step):
    if stop < start:
        raise ValueError('Sweep stop must be at least its start')
    if (stop - start) / step > 999:
        raise ValueError('A sweep can contain at most 1000 setpoints')
    count = int(math.ceil((stop-start) / step))
    return [min(stop, start + index * step) for index in range(count)] + [stop]


def parameters(kind, supplied, interval):
    if kind not in PROCEDURES or not isinstance(supplied, dict):
        raise ValueError('Choose a supported procedure and parameter object')
    specs = parameter_specs(kind)
    if set(supplied) - set(specs):
        raise ValueError('Unknown experiment parameter')
    values = {}
    for key, (label, default, low, high, _, integer) in specs.items():
        value = supplied.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f'{label} must be between {low} and {high}')
        if integer and int(value) != value:
            raise ValueError(f'{label} must be a whole number')
        values[key] = int(value) if integer else float(value)
    if kind != 'capture':
        if values['settle_s'] < interval:
            raise ValueError('Settling time must be at least the acquisition interval')
        voltages = sweep_values(values['input_start_v'], values['input_stop_v'], values['input_step_v'])
        currents = sweep_values(values['load_start_a'], values['load_stop_a'], values['load_step_a'])
        if len(voltages) * len(currents) > 1000:
            raise ValueError('An experiment can contain at most 1000 sweep points')
    return values


METRICS = {
    'elapsed_s': ('Elapsed time', 's'), 'input_voltage_set_v': ('Input voltage setpoint', 'V'),
    'load_current_set_a': ('Load current setpoint', 'A'),
    'input_voltage_v': ('Input voltage', 'V'), 'input_current_a': ('Input current', 'A'),
    'output_voltage_v': ('Output voltage', 'V'), 'output_current_a': ('Output current', 'A'),
    'input_power_w': ('Input power', 'W'), 'output_power_w': ('Output power', 'W'),
    'efficiency_pct': ('Efficiency', '%'), 'maximum_current_a': ('Maximum acceptable output current', 'A'),
}


def presentation(kind, metrics):
    if kind == 'efficiency':
        return [dict(x='output_current_a', y='efficiency_pct', series='input_voltage_set_v',
                     title='Efficiency versus output current', x_label='Output current (A)', y_label='Efficiency (%)')]
    if kind == 'maximum_output_current':
        return [dict(x='input_voltage_set_v', y='maximum_current_a', series=None,
                     title='Maximum output current versus input voltage', x_label='Input voltage (V)', y_label='Output current (A)')]
    return [dict(x='elapsed_s', y=key, series=None, title=label + ' versus time',
                 x_label='Elapsed time (s)', y_label=label + (f' ({unit})' if unit else ''))
            for key, (label, unit) in metrics.items() if key != 'elapsed_s']
