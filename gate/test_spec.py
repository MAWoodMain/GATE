"""Version 1 declarative tests: schema validation and bounded plan expansion."""
import copy
import itertools
import math
import re
from gate.expressions import Expression, RESERVED
from gate.waveforms import STATISTICS

CONTROLS = {'voltage': ('set_voltage', 'voltage'), 'current': ('set_current', 'current'),
            'current_limit': ('set_current_limit', 'current')}
BASE_METRICS = {'elapsed_s': ('Elapsed time', 's')}


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch('[a-z][a-z0-9_]{0,63}', value) or value in RESERVED:
        raise ValueError('Use identifiers of up to 64 lowercase letters, digits and underscores, starting with a letter')


def text(value, field, maximum=2000, empty=False):
    if not isinstance(value, str) or len(value) > maximum or not empty and not value.strip():
        raise ValueError(field + ' must be text' + ('' if empty else ' and cannot be empty'))


def fields(value, required, optional=()):
    if not isinstance(value, dict) or set(required) - set(value) or set(value) - set(required) - set(optional):
        raise ValueError('Expected fields: ' + ', '.join(required) + ('; optional: '+', '.join(optional) if optional else ''))


def number(value, field):
    try:
        valid = not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(field + ' must be finite numeric data')


def collection(value, field, maximum):
    if not isinstance(value, (dict, list)) or len(value) > maximum:
        raise ValueError(f'{field} must contain at most {maximum} items')


def _metadata(value):
    text(value['label'], 'Label', 160)
    text(value['unit'], 'Unit', 32, empty=True)


def validate_test(value, roles):
    required = ('schema_version', 'label', 'description', 'parameters', 'axes', 'outputs', 'limits',
                'setup', 'before_point', 'acquisition', 'measurements', 'derived', 'checks', 'plots', 'table')
    fields(value, required, ('stop_when', 'reduce', 'notes', 'secondary_measurements', 'waveforms'))
    spec = copy.deepcopy(value)
    if type(spec['schema_version']) is not int or spec['schema_version'] != 1:
        raise ValueError('Unsupported test schema_version; expected 1')
    text(spec['label'], 'Test label', 160)
    text(spec['description'], 'Test description')
    if not isinstance(spec.get('notes', []), list) or len(spec.get('notes', [])) > 8:
        raise ValueError('Provide at most eight result notes')
    for note in spec.get('notes', []):
        text(note, 'Result note')
    for key, maximum, kind in [('parameters', 32, dict), ('axes', 4, list), ('outputs', 32, list),
            ('limits', 32, dict), ('setup', 32, list), ('before_point', 32, list), ('measurements', 64, dict),
            ('derived', 64, dict), ('checks', 32, list), ('plots', 32, list)]:
        if not isinstance(spec[key], kind):
            raise ValueError(key + ' has the wrong type')
        collection(spec[key], key, maximum)
    names = set(BASE_METRICS) | {'quality', 'secondary_quality'}
    for key, parameter in spec['parameters'].items():
        identifier(key)
        if key in names:
            raise ValueError('Duplicate name: '+key)
        names.add(key)
        fields(parameter, ('label', 'unit', 'default', 'min', 'max', 'step', 'type'))
        _metadata(parameter)
        if parameter['type'] not in ('number', 'integer'):
            raise ValueError('Parameter type must be number or integer')
        for field in ('default', 'min', 'max', 'step'):
            number(parameter[field], field)
        if not parameter['min'] <= parameter['default'] <= parameter['max'] or parameter['step'] <= 0:
            raise ValueError('Parameter default/range/step is invalid: '+key)
        if parameter['type'] == 'integer' and any(int(parameter[field]) != parameter[field] for field in ('default', 'min', 'max', 'step')):
            raise ValueError('Integer parameters require integer bounds, default and step')
    param_names = set(spec['parameters'])
    def role(key):
        if not isinstance(key, str) or key not in roles:
            raise ValueError('Unknown equipment role: '+str(key))
    def control(item, available):
        role(item['role'])
        if not isinstance(item['control'], str) or item['control'] not in CONTROLS or item['role'] not in spec['outputs']:
            raise ValueError('Controls require an allowed operation and an explicitly enabled output role')
        return Expression(item['value'], available)
    if any(not isinstance(key, str) for key in spec['outputs']) or len(set(spec['outputs'])) != len(spec['outputs']):
        raise ValueError('Output roles must be unique strings')
    for key in spec['outputs']:
        role(key)
    if set(spec['limits']) != set(spec['outputs']):
        raise ValueError('Each output role requires voltage and current operating limits')
    for bounds in spec['limits'].values():
        fields(bounds, ('voltage', 'current'))
        for bound in bounds.values():
            Expression(bound, param_names)
    for axis in spec['axes']:
        fields(axis, ('id', 'label', 'unit'), ('role', 'control', 'start', 'stop', 'step', 'values'))
        identifier(axis['id'])
        _metadata(axis)
        if axis['id'] in names:
            raise ValueError('Duplicate name: '+axis['id'])
        names.add(axis['id'])
        if ('role' in axis) != ('control' in axis):
            raise ValueError('An axis control requires both role and control')
        if 'values' in axis:
            if any(key in axis for key in ('start', 'stop', 'step')) or not isinstance(axis['values'], list) or not 1 <= len(axis['values']) <= 1000:
                raise ValueError('Use either axis values or start/stop/step')
            for item in axis['values']:
                Expression(item, param_names)
        else:
            if not {'start', 'stop', 'step'} <= set(axis):
                raise ValueError('Sweep axis requires start, stop and step')
            for key in ('start', 'stop', 'step'):
                Expression(axis[key], param_names)
        if 'control' in axis:
            control(dict(axis, value=0), param_names)
    axis_names = {axis['id'] for axis in spec['axes']}
    context = param_names | axis_names | set(BASE_METRICS)
    for item in spec['setup']:
        fields(item, ('role', 'control', 'value'))
        control(item, param_names)
    for item in spec['before_point']:
        fields(item, ('role', 'control', 'value'))
        control(item, param_names | axis_names)
    acquisition = spec['acquisition']
    fields(acquisition, ('settle_s', 'samples', 'repeat', 'capture_all', 'on_invalid'))
    for key in ('settle_s', 'samples', 'repeat'):
        Expression(acquisition[key], param_names)
    if type(acquisition['capture_all']) is not bool or acquisition['on_invalid'] not in ('record', 'fail'):
        raise ValueError('Acquisition requires capture_all boolean and on_invalid record/fail')
    if spec['outputs'] and acquisition['on_invalid'] != 'fail':
        raise ValueError('Output tests must fail on invalid required readbacks')
    secondary = spec.get('secondary_measurements', {})
    waveforms = spec.get('waveforms', {})
    if not isinstance(secondary, dict) or len(secondary) > 32 or not isinstance(waveforms, dict) or len(waveforms) > 8:
        raise ValueError('Provide at most 32 secondary scalar measurements and eight waveform definitions')
    scalar_metrics = dict(spec['measurements'])
    for key, metric in secondary.items():
        if key in scalar_metrics:
            raise ValueError('Duplicate measurement name: '+key)
        scalar_metrics[key] = metric
    for key, metric in scalar_metrics.items():
        identifier(key)
        if key in names:
            raise ValueError('Duplicate name: '+key)
        names.add(key)
        fields(metric, ('label', 'unit', 'role', 'required'), ('quantity', 'channel', 'kind', 'location') if key in secondary else ('quantity', 'channel'))
        _metadata(metric)
        role(metric['role'])
        if type(metric['required']) is not bool or ('quantity' in metric) == ('channel' in metric):
            raise ValueError('A measurement requires exactly one channel or logical quantity and a required boolean')
        text(metric.get('quantity', metric.get('channel')), 'Measurement channel', 160)
        if key in secondary:
            if metric.get('kind', 'scalar') not in ('scalar', 'temperature'):
                raise ValueError('Secondary kind must be scalar or temperature')
            if 'location' in metric:
                text(metric['location'], 'Measurement location', 160)
            if metric.get('kind') == 'temperature' and metric['unit'] not in ('°C', 'C', '°F', 'F', 'K'):
                raise ValueError('Temperature measurements require Celsius, Fahrenheit or Kelvin units')
    measured = set(scalar_metrics)
    waveform_metrics = set()
    for key, waveform in waveforms.items():
        identifier(key)
        if key in names:
            raise ValueError('Duplicate measurement name: '+key)
        names.add(key)
        fields(waveform, ('label', 'unit', 'role', 'channel', 'required', 'phase', 'when', 'points', 'window_s', 'pretrigger', 'timeout_s', 'max_captures', 'trigger'), ('screenshot',))
        if 'screenshot' in waveform and type(waveform['screenshot']) is not bool:
            raise ValueError('Waveform screenshot must be a boolean')
        _metadata(waveform)
        role(waveform['role'])
        text(waveform['channel'], 'Waveform channel', 80)
        if type(waveform['required']) is not bool or waveform['phase'] not in ('settled', 'transition'):
            raise ValueError('Waveform requires a required boolean and settled/transition phase')
        if type(waveform['max_captures']) is not int or not 1 <= waveform['max_captures'] <= 64:
            raise ValueError('Waveforms allow at most 64 captures per definition')
        Expression(waveform['when'], param_names | axis_names if waveform['phase'] == 'transition' else context | measured)
        for field in ('points', 'window_s', 'pretrigger', 'timeout_s'):
            if waveform[field] is not None:
                Expression(waveform[field], param_names | axis_names)
        if waveform['trigger'] is not None:
            fields(waveform['trigger'], ('level', 'slope'))
            if waveform['trigger']['slope'] not in ('rising', 'falling'):
                raise ValueError('Trigger slope must be rising or falling')
            Expression(waveform['trigger']['level'], param_names | axis_names)
        if waveform['phase'] == 'transition' and waveform['trigger'] is None:
            raise ValueError('Transition captures require an explicit hardware trigger condition')
        if key+'_status' in names:
            raise ValueError('Waveform status name conflicts with a metric')
        names.add(key+'_status')
        for statistic in STATISTICS:
            column = key+'_'+statistic
            if column in names:
                raise ValueError('Duplicate waveform statistic name: '+column)
            names.add(column)
            waveform_metrics.add(column)
    for key, metric in spec['derived'].items():
        identifier(key)
        if key in names:
            raise ValueError('Duplicate name: '+key)
        names.add(key)
        fields(metric, ('label', 'unit', 'expression', 'aggregate'))
        _metadata(metric)
        if metric['aggregate'] not in ('point', 'sample_mean'):
            raise ValueError('Derived aggregate must be point or sample_mean')
    derived_names = set(spec['derived'])
    expressions = {key: Expression(metric['expression'], context | measured | derived_names | waveform_metrics) for key, metric in spec['derived'].items()}
    order = []
    remaining = set(expressions)
    while remaining:
        ready = [key for key in spec['derived'] if key in remaining and not expressions[key].names.intersection(remaining)]
        if not ready:
            raise ValueError('Derived metrics contain a dependency cycle')
        order += ready
        remaining -= set(ready)
    point_metrics = {key for key, metric in spec['derived'].items() if metric['aggregate'] == 'point'}
    for key, metric in spec['derived'].items():
        if metric['aggregate'] == 'sample_mean' and expressions[key].names & (point_metrics | waveform_metrics):
            raise ValueError('Sample-mean calculations cannot depend on point calculations')
    known = context | measured | derived_names | waveform_metrics
    for check in spec['checks']:
        fields(check, ('stage', 'expression', 'message'))
        if check['stage'] not in ('sample', 'point', 'continuous'):
            raise ValueError('Check stage must be sample, point or continuous')
        text(check['message'], 'Check message')
        Expression(check['expression'], known - point_metrics - waveform_metrics if check['stage'] in ('sample', 'continuous') else known)
    reduction = spec.get('reduce')
    if reduction is not None:
        fields(reduction, ('group_by', 'where', 'metrics'), ('status',))
        axes = [axis['id'] for axis in spec['axes']]
        if not isinstance(reduction['group_by'], list) or reduction['group_by'] != axes[:len(reduction['group_by'])]:
            raise ValueError('Reduction group_by must be an ordered prefix of the sweep axes')
        Expression(reduction['where'], known)
        if not isinstance(reduction['metrics'], dict) or not reduction['metrics'] or len(reduction['metrics']) > 64:
            raise ValueError('Provide between 1 and 64 summary metrics')
        for key, metric in reduction['metrics'].items():
            identifier(key)
            if key in param_names | axis_names | set(BASE_METRICS) | {'quality'}:
                raise ValueError('Summary name conflicts with an axis/parameter: '+key)
            fields(metric, ('label', 'unit', 'expression', 'reducer'))
            _metadata(metric)
            source = scalar_metrics.get(key, spec['derived'].get(key))
            if source and (source['unit'] != metric['unit'] or source['label'] != metric['label']):
                raise ValueError('A reused summary name must keep the same label and units as its point metric')
            if metric['reducer'] not in ('last', 'first', 'min', 'max', 'mean', 'count'):
                raise ValueError('Unsupported summary reducer')
            Expression(metric['expression'], known)
        if 'status' in reduction:
            fields(reduction['status'], ('field', 'stopped', 'exhausted'))
            identifier(reduction['status']['field'])
            if reduction['status']['field'] in known | set(reduction['metrics']) | {'quality'}:
                raise ValueError('Summary status name conflicts with a metric')
            text(reduction['status']['stopped'], 'Stopped status', 80)
            text(reduction['status']['exhausted'], 'Exhausted status', 80)
    if 'stop_when' in spec:
        fields(spec['stop_when'], ('expression', 'scope'))
        if spec['stop_when']['scope'] not in ('group', 'run') or spec['stop_when']['scope'] == 'group' and reduction is None:
            raise ValueError('Group stopping requires a reduction; scope must be group or run')
        Expression(spec['stop_when']['expression'], known)
    waveform_statuses = {key+'_status' for key in waveforms}
    point_columns = axis_names | measured | derived_names | waveform_metrics | waveform_statuses | set(BASE_METRICS) | {'quality', 'secondary_quality'}
    summary_columns = set(reduction['group_by']) | set(reduction['metrics']) if reduction else set()
    if reduction and 'status' in reduction:
        summary_columns.add(reduction['status']['field'])
    def column(key, data):
        if not isinstance(key, str) or key not in (summary_columns if data == 'summary' else point_columns):
            if data == 'points' and acquisition['capture_all'] and isinstance(key, str) and any(key.startswith(role+'.') for role in roles):
                return
            raise ValueError('Unknown '+data+' column: '+str(key))
    for plot in spec['plots']:
        fields(plot, ('kind', 'data', 'x', 'y', 'title'), ('series', 'x_label', 'y_label', 'x_scale', 'y_scale', 'markers'))
        if plot['kind'] not in ('line', 'scatter') or plot['data'] not in ('points', 'summary', 'secondary') or plot['data'] == 'summary' and not reduction:
            raise ValueError('Plot requires line/scatter and a valid points/summary dataset')
        if plot['data'] == 'secondary':
            if plot['x'] != 'elapsed_s' or plot['y'] not in secondary or plot.get('series') is not None:
                raise ValueError('Secondary plots use elapsed_s and one secondary scalar metric, without series grouping')
        for field in ('x', 'y'):
            if plot['data'] != 'secondary':
                column(plot[field], plot['data'])
            status_field = reduction.get('status', {}).get('field') if reduction else None
            if plot[field] in {'quality', 'secondary_quality'} | waveform_statuses or plot[field] == status_field:
                raise ValueError('Plot axes require numeric columns')
        if plot.get('series') is not None:
            column(plot['series'], plot['data'])
        for field in ('x_scale', 'y_scale'):
            if plot.get(field, 'linear') not in ('linear', 'log'):
                raise ValueError('Plot scale must be linear or log')
        text(plot['title'], 'Plot title', 160)
        for field in ('x_label', 'y_label'):
            if field in plot:
                text(plot[field], 'Axis label', 160)
        if 'markers' in plot:
            fields(plot['markers'], ('field', 'styles'))
            column(plot['markers']['field'], plot['data'])
            styles = plot['markers']['styles']
            if not isinstance(styles, dict) or len(styles) > 16:
                raise ValueError('Provide at most 16 marker styles')
            for style in styles.values():
                fields(style, ('shape', 'label'))
                if style['shape'] not in ('circle', 'triangle', 'square'):
                    raise ValueError('Marker shape must be circle, triangle or square')
                text(style['label'], 'Marker label', 80)
    fields(spec['table'], ('data',), ('columns',))
    if spec['table']['data'] not in ('points', 'summary') or spec['table']['data'] == 'summary' and not reduction:
        raise ValueError('Table dataset must be available points/summary')
    if 'columns' in spec['table']:
        if not isinstance(spec['table']['columns'], list) or not 1 <= len(spec['table']['columns']) <= 128:
            raise ValueError('Table requires 1–128 columns')
        for key in spec['table']['columns']:
            column(key, spec['table']['data'])
    return spec, order


def parameter_specs(spec):
    return {key: (value['label'] + (f" ({value['unit']})" if value['unit'] else ''), value['default'],
                  value['min'], value['max'], value['step'], value['type'] == 'integer') for key, value in spec['parameters'].items()}


def parameter_values(spec, supplied):
    if not isinstance(supplied, dict) or set(supplied) - set(spec['parameters']):
        raise ValueError('Unknown experiment parameter')
    result = {}
    for key, metadata in spec['parameters'].items():
        value = supplied.get(key, metadata['default'])
        number(value, metadata['label'])
        if not metadata['min'] <= value <= metadata['max'] or metadata['type'] == 'integer' and int(value) != value:
            raise ValueError(f"{metadata['label']} must be {metadata['type']} between {metadata['min']} and {metadata['max']}")
        result[key] = int(value) if metadata['type'] == 'integer' else float(value)
    return result


def sweep_values(start, stop, step):
    for item in (start, stop, step):
        number(item, 'Sweep value')
    if step == 0 or stop > start and step < 0 or stop < start and step > 0:
        raise ValueError('Sweep step must move toward the stop value')
    distance = abs((stop-start)/step)
    if not math.isfinite(distance) or distance > 999:
        raise ValueError('A sweep can contain at most 1000 setpoints')
    nearest = round(distance)
    if nearest > 0 and math.isclose(distance, nearest, rel_tol=1e-12):
        distance = nearest
    count = math.ceil(distance)
    if count > 999:
        raise ValueError('A sweep can contain at most 1000 setpoints')
    return [start + index*step for index in range(count)] + [stop]


def expand_axes(spec, values):
    axes = []
    for axis in spec['axes']:
        evaluate = lambda expression: Expression(expression, values).evaluate(values)
        points = ([evaluate(expression) for expression in axis['values']] if 'values' in axis else
                  sweep_values(*(evaluate(axis[key]) for key in ('start', 'stop', 'step'))))
        for value in points:
            number(value, 'Axis value')
        if len(set(points)) != len(points):
            raise ValueError('An axis cannot repeat setpoints')
        axes.append(points)
    count = math.prod(len(axis) for axis in axes)
    if count > 1000:
        raise ValueError('An experiment can contain at most 1000 sweep points')
    return [dict(zip([axis['id'] for axis in spec['axes']], point)) for point in itertools.product(*axes)] if axes else [{}]
