"""Acquisition-thread experiment runner and durable result artifacts."""
import copy
import csv
import html
import json
import math
import os
import sqlite3
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from gate.experiments import check_bindings
from gate.procedures import test_spec
from gate.test_spec import BASE_METRICS, CONTROLS, validate_test, parameter_values, expand_axes, number
from gate.expressions import Expression
from gate.secondary_capture import required_roles, waveform_columns, CapturePlan, Captures


def data_directory():
    return Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share'))) / 'gate/experiments'


def history(directory):
    results = []
    if Path(directory).exists():
        for path in sorted(Path(directory).glob('*/run.json'), key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
            try:
                value = json.loads(path.read_text())
                if not isinstance(value, dict) or not {'id', 'experiment', 'state', 'started_utc_ns', 'completed_points', 'points', 'summary', 'metrics', 'test', 'procedure', 'simulated'} <= set(value):
                    continue
                if value['state'] == 'running':
                    value.update(state='interrupted', error='Service stopped before the run finished')
                results.append(value)
            except (OSError, ValueError, KeyError):
                continue
    return results


def plot_svg(plot, rows):
    """Render the declarative line/scatter contract, including scales and markers."""
    esc = lambda value: html.escape(str(value), quote=True)
    groups = defaultdict(list)
    xlog, ylog = plot.get('x_scale') == 'log', plot.get('y_scale') == 'log'
    for row in rows:
        x, y = row.get(plot['x']), row.get(plot['y'])
        valid = all(isinstance(value, (int, float)) and math.isfinite(value) and abs(value) <= 1e300 for value in (x, y))
        valid = valid and (not xlog or x > 0) and (not ylog or y > 0)
        key = str(row.get(plot.get('series'), '')) if plot.get('series') else ''
        groups[key].append(((math.log10(x) if xlog else x), (math.log10(y) if ylog else y), row) if valid else None)
    points = [point for series in groups.values() for point in series if point is not None]
    if not points:
        return '<svg xmlns="http://www.w3.org/2000/svg" width="760" height="120"><title>'+esc(plot['title'])+'</title><text x="20" y="40">'+esc(plot['title'])+'</text><text x="20" y="80">No valid data for the configured axes/scales</text></svg>'
    xmin, xmax = min(p[0] for p in points), max(p[0] for p in points)
    ymin, ymax = min(p[1] for p in points), max(p[1] for p in points)
    if xmin == xmax:
        padding = .5 if xlog or xmin == 0 else max(abs(xmin)*.05, 1e-9)
        xmin, xmax = xmin-padding, xmax+padding
    if ymin == ymax:
        padding = .5 if ylog or ymin == 0 else max(1e-9, abs(ymin)*.05)
        ymin, ymax = ymin-padding, ymax+padding
    padding = (ymax-ymin)*.08
    ymin, ymax = ymin-padding, ymax+padding
    px = lambda value: 85+(value-xmin)/(xmax-xmin)*590
    py = lambda value: 340-(value-ymin)/(ymax-ymin)*270
    marker_spec = plot.get('markers', {})
    marker_styles = marker_spec.get('styles', {})
    legend_rows = math.ceil(len(groups)/4) if plot.get('series') else 0
    height = max(460, 430+legend_rows*20+(30 if marker_styles else 0))
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="760" height="{height}" viewBox="0 0 760 {height}" role="img">',
             f'<title>{esc(plot["title"])}</title><rect width="760" height="{height}" fill="white"/>',
             f'<text x="380" y="28" text-anchor="middle" font-family="sans-serif" font-size="17">{esc(plot["title"])}</text>']
    for index in range(6):
        x, y = 85+index*118, 340-index*54
        xv, yv = xmin+(xmax-xmin)*index/5, ymin+(ymax-ymin)*index/5
        parts += [f'<path d="M{x} 70V340 M85 {y}H675" stroke="#ddd"/>',
                  f'<text x="{x}" y="362" text-anchor="middle" font-size="12">{(10**xv if xlog else xv):.4g}</text>',
                  f'<text x="77" y="{y+4}" text-anchor="end" font-size="12">{(10**yv if ylog else yv):.4g}</text>']
    parts += ['<path d="M85 70V340H675" stroke="#333" fill="none"/>',
              f'<text x="380" y="392" text-anchor="middle" font-size="14">{esc(plot["x_label"])}</text>',
              f'<text transform="translate(19 205) rotate(-90)" text-anchor="middle" font-size="14">{esc(plot["y_label"])}</text>']
    colors = ['#3584e4', '#e66100', '#26a269', '#9141ac', '#c01c28', '#1b7588']
    for index, (name, series) in enumerate(groups.items()):
        color = colors[index % len(colors)]
        if plot.get('kind', 'line') == 'line':
            path, command = [], 'M'
            for point in series:
                if point is None:
                    command = 'M'
                    continue
                x, y, _ = point
                path.append(f'{command}{px(x):.2f},{py(y):.2f}')
                command = 'L'
            parts.append(f'<path d="{" ".join(path)}" stroke="{color}" fill="none" stroke-width="2"/>')
        for x, y, row in (point for point in series if point is not None):
            style = marker_styles.get(str(row.get(marker_spec.get('field'))), {})
            shape = style.get('shape', 'circle')
            title = '<title>'+esc(style['label'])+'</title>' if 'label' in style else ''
            if shape == 'triangle':
                parts.append(f'<path d="M{px(x):.2f} {py(y)-5:.2f}l-5 9h10Z" fill="{color}">{title}</path>')
            elif shape == 'square':
                parts.append(f'<rect x="{px(x)-4:.2f}" y="{py(y)-4:.2f}" width="8" height="8" fill="{color}">{title}</rect>')
            else:
                parts.append(f'<circle cx="{px(x):.2f}" cy="{py(y):.2f}" r="3" fill="{color}">{title}</circle>')
        if plot.get('series'):
            label = name + (' '+plot['series_unit'] if plot.get('series_unit') else '')
            parts.append(f'<text x="{85+(index%4)*160}" y="{420+(index//4)*20}" font-size="12" fill="{color}">{esc(label)}</text>')
    if marker_styles:
        labels = [dict(circle='●', triangle='▲', square='■')[style['shape']]+' '+style['label'] for style in marker_styles.values()]
        parts.append(f'<text x="85" y="{435+legend_rows*20}" font-size="12">{esc(" · ".join(labels))}</text>')
    parts.append('</svg>')
    return ''.join(parts)


class ExperimentPlan:
    """Validated, side-effect-free execution plan for any version-1 test."""
    def __init__(self, definition, test, bindings, supplied, instruments, resolve, interval,
                 enable_outputs=False, wiring_confirmed=False):
        self.definition = copy.deepcopy(definition)
        self.spec, self.derived_order = validate_test(test_spec(definition, test), definition['roles'])
        self.kind = 'declarative'
        self.parameters = parameter_values(self.spec, supplied)
        checked = check_bindings(definition, bindings, instruments, resolve)
        if not checked['compatible']:
            raise ValueError('; '.join(checked['errors']))
        self.needed_roles = required_roles(definition, self.spec)
        if self.needed_roles - set(checked['bindings']):
            raise ValueError('Required observation equipment is missing: '+', '.join(sorted(self.needed_roles-set(checked['bindings']))))
        self.requested_bindings = copy.deepcopy(bindings)
        self.bindings = checked['bindings']
        self.devices = {role: instruments[key] for role, key in self.bindings.items()}
        self.steps = expand_axes(self.spec, self.parameters)
        acquisition = self.spec['acquisition']
        def acquisition_value(key, low, high, integer=False):
            value = Expression(acquisition[key], self.parameters).evaluate(self.parameters)
            number(value, 'Acquisition '+key)
            if not low <= value <= high or integer and int(value) != value:
                raise ValueError('Invalid acquisition '+key)
            return int(value) if integer else value
        self.settle_s = acquisition_value('settle_s', 0., 3600.)
        self.sample_count = acquisition_value('samples', 1, 100, True)
        repeat = acquisition_value('repeat', 1, 1000, True)
        if self.spec['axes'] and repeat != 1:
            raise ValueError('Acquisition repeat is for axis-free captures; use samples for each sweep point')
        self.steps *= repeat
        self.output_roles = self.spec['outputs']
        self.outputs = [self.devices[role] for role in reversed(self.output_roles)]
        if self.outputs:
            if enable_outputs is not True or wiring_confirmed is not True:
                raise ValueError('Confirm wiring and explicitly allow output enable before running a sweep')
            if len({bool(self.devices[role].simulated) for role in self.needed_roles}) != 1:
                raise ValueError('Do not mix simulated and real equipment in an output test')
            if self.settle_s < interval:
                raise ValueError('Settling time must be at least the acquisition interval')
        self.metrics = dict(BASE_METRICS)
        for axis in self.spec['axes']:
            self.metrics[axis['id']] = (axis['label'], axis['unit'])
        self.channels = {}
        self.scalar_metrics = dict(self.spec['measurements'], **self.spec.get('secondary_measurements', {}))
        self.secondary_names = set(self.spec.get('secondary_measurements', {})) | set(waveform_columns(self.spec))
        for key, metric in self.scalar_metrics.items():
            device = self.devices.get(metric['role'])
            channel = (getattr(device, 'experiment_channels', {}).get(metric['quantity']) if 'quantity' in metric else metric['channel'])
            if (not isinstance(channel, str) or not channel) and (key not in self.secondary_names or metric['required']):
                raise ValueError(f'{metric["role"]}: driver does not map quantity {metric.get("quantity")}')
            self.channels[key] = (metric['role'], channel) if channel else None
            self.metrics[key] = (metric['label'], metric['unit'])
        self.metrics.update(waveform_columns(self.spec))
        names = set(self.parameters) | set(self.metrics) | set(self.spec['derived'])
        self.derived_expressions = {}
        for key, metric in self.spec['derived'].items():
            self.derived_expressions[key] = Expression(metric['expression'], names)
            self.metrics[key] = (metric['label'], metric['unit'])
        for key in self.derived_order:
            if self.derived_expressions[key].names & self.secondary_names:
                self.secondary_names.add(key)
        self.checks = [(item['stage'], Expression(item['expression'], names), item['message']) for item in self.spec['checks']]
        self.stop_expression = Expression(self.spec['stop_when']['expression'], names) if 'stop_when' in self.spec else None
        self.reduction = self.spec.get('reduce')
        self.summary_expressions = {}
        if self.reduction:
            self.where_expression = Expression(self.reduction['where'], names)
            for key, metric in self.reduction['metrics'].items():
                self.summary_expressions[key] = Expression(metric['expression'], names)
                self.metrics[key] = (metric['label'], metric['unit'])
        if len({self.bindings[role] for role in self.output_roles}) != len(self.output_roles):
            raise ValueError('Output roles require distinct instruments')
        self.validate_controls()
        self.capture_plan = CapturePlan(self)

    def control_value(self, operation, context):
        value = Expression(operation['value'], context).evaluate(context)
        number(value, 'Control value')
        return value

    def validate_controls(self):
        operations = list(self.spec['setup'])
        all_operations = [(item, self.parameters) for item in operations]
        for step in self.steps:
            context = dict(self.parameters, **step)
            all_operations += [(item, context) for item in self.spec['before_point']]
            all_operations += [(dict(role=axis['role'], control=axis['control'], value=step[axis['id']]), context)
                               for axis in self.spec['axes'] if 'control' in axis]
        for role in self.output_roles:
            device = self.devices[role]
            configured = {item['control'] for item, _ in all_operations if item['role'] == role}
            if not configured or not set(getattr(device, 'experiment_required_controls', ())) <= configured:
                raise ValueError(role + ': configure all driver-required controls before enabling output')
            methods = {'set_enabled'} | {CONTROLS[control][0] for control in configured}
            if not getattr(device, 'experiment_control_supported', False) or any(not callable(getattr(device, method, None)) for method in methods):
                raise ValueError(f'{device.name}: driver has no verified experiment output control')
            limits = getattr(device, 'experiment_limits', {})
            envelope = self.spec['limits'][role]
            maxima = {}
            for quantity in ('voltage', 'current'):
                bounds = limits.get(quantity)
                if not isinstance(bounds, (list, tuple)) or len(bounds) != 2:
                    raise ValueError(role + ': missing verified '+quantity+' limits')
                for bound in bounds:
                    number(bound, 'Driver limit')
                value = Expression(envelope[quantity], self.parameters).evaluate(self.parameters)
                number(value, role+' operating limit')
                if not bounds[0] <= value <= bounds[1]:
                    raise ValueError(role+': operating '+quantity+' envelope exceeds driver limits')
                maxima[quantity] = value
            rating = limits.get('power_w')
            number(rating, role+' driver power limit')
            if rating <= 0 or maxima['voltage']*maxima['current'] > rating:
                raise ValueError(role+': operating envelope exceeds driver power limit')
            for operation, context in all_operations:
                if operation['role'] != role:
                    continue
                quantity = CONTROLS[operation['control']][1]
                value = self.control_value(operation, context)
                if not limits[quantity][0] <= value <= maxima[quantity] or operation['control'] == 'current_limit' and value <= 0:
                    raise ValueError(role+': '+operation['control']+' setpoint exceeds operating or driver limits')

    def plots(self):
        plots = copy.deepcopy(self.spec['plots'])
        if not plots and self.spec['acquisition']['capture_all']:
            plots = [dict(kind='line', data='secondary' if key in self.spec.get('secondary_measurements', {}) else 'points', x='elapsed_s', y=key, title=label+' versus time')
                     for key, (label, _) in self.metrics.items() if key != 'elapsed_s' and (not self.reduction or key not in self.reduction['metrics'] or key in self.spec['measurements'] or key in self.spec['derived'])]
        for plot in plots:
            plot.setdefault('series', None)
            for axis in ('x', 'y'):
                key = plot[axis]
                label, unit = self.metrics.get(key, (key, ''))
                plot.setdefault(axis+'_label', label + (f' ({unit})' if unit else ''))
                plot.setdefault(axis+'_scale', 'linear')
            series = plot.get('series')
            if series:
                label, unit = self.metrics.get(series, (series, ''))
                plot['series_label'] = label
                plot['series_unit'] = unit
        return plots


class ExperimentRun(ExperimentPlan):
    def __init__(self, definition, test, bindings, supplied, instruments, resolve, interval, directory,
                 enable_outputs=False, wiring_confirmed=False, sequence_id=None, sequence_index=None):
        super().__init__(definition, test, bindings, supplied, instruments, resolve, interval,
                         enable_outputs, wiring_confirmed)
        self.points, self.summary, self.frames, self.group_points = [], [], [], []
        self.step_index = 0
        self.pending_point = None
        self.pending_required_invalid = False
        self.cleanup_errors = []
        self.due = 0.
        self.origin_ns = time.monotonic_ns()
        self.metadata = dict(id=uuid.uuid4().hex, experiment=definition['id'], name=definition['name'], test=test,
            sequence_id=sequence_id, sequence_index=sequence_index, procedure='declarative', test_spec=self.spec,
            parameters=self.parameters, bindings=self.bindings, requested_bindings=self.requested_bindings, definition=self.definition,
            secondary_measurements=copy.deepcopy(self.spec.get('secondary_measurements', {})),
            instruments={role: dict(id=device.id, name=device.name, simulated=device.simulated,
                limits=copy.deepcopy(getattr(device, 'experiment_limits', {})), identity=device.name)
                for role, device in self.devices.items()},
            simulated=any(device.simulated for device in self.devices.values()), state='running', error='',
            started_utc_ns=time.time_ns(), finished_utc_ns=None, points=[], summary=[], metrics=self.metrics,
            presentation=self.plots(), table=self.spec['table'], notes=self.spec.get('notes', []), completed_points=0,
            total_points=len(self.steps), interval_s=interval,
            timing='Host monotonic time; sequential instrument read windows, not simultaneous sampling')
        self.directory = Path(directory)/self.metadata['id']
        self.directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        self.metadata.update(path=str(self.directory/'run.sqlite'), report=str(self.directory/'report.html'),
                             csv=str(self.directory/'points.csv'), summary_csv=str(self.directory/'summary.csv'), secondary_csv=str(self.directory/'secondary.csv'))
        self.db = sqlite3.connect(self.directory/'run.sqlite')
        self.db.executescript('''CREATE TABLE run (metadata TEXT);
            CREATE TABLE samples (point_index INTEGER, role TEXT, instrument TEXT, channel TEXT,
                value REAL, unit TEXT, quality TEXT, tick_ns INTEGER, read_start_ns INTEGER, read_end_ns INTEGER);
            CREATE TABLE points (point_index INTEGER, data TEXT);
            CREATE TABLE summary (data TEXT);
            CREATE TABLE secondary_samples (point_index INTEGER, metric TEXT, role TEXT, instrument TEXT, value REAL, unit TEXT, quality TEXT, location TEXT, tick_ns INTEGER, read_start_ns INTEGER, read_end_ns INTEGER);
            CREATE TABLE waveforms (id TEXT, metadata TEXT);
            CREATE TABLE waveform_samples (waveform_id TEXT, sample_index INTEGER, time_s REAL, value REAL);''')
        self.captures = Captures(self)
        self.channel_devices = []
        self.metadata['waveforms'] = self.captures.records
        self.db.execute('INSERT INTO run VALUES(?)', (json.dumps(self.metadata),))
        self.persist()
        try:
            for device in self.outputs:
                device.set_enabled(False)
            used_channels = defaultdict(set)
            for role_channel in self.channels.values():
                if role_channel and role_channel[0] in self.devices:
                    role, channel = role_channel
                    used_channels[self.bindings[role]].add(channel)
            for key, spec in self.spec.get('waveforms', {}).items():
                if key not in self.capture_plan.disabled:
                    used_channels[self.bindings[spec['role']]].add(spec['channel'])
            for identity, channels in used_channels.items():
                device = instruments[identity]
                if callable(getattr(device, 'begin_experiment_channels', None)):
                    self.channel_devices.append(device)
                    device.begin_experiment_channels(channels)
            for operation in self.spec['setup']:
                self.apply_control(operation, self.parameters)
            self.set_point()
            for role in self.output_roles:
                self.devices[role].set_enabled(True)
        except Exception as exc:
            self.finish('failed', str(exc))

    @property
    def active(self):
        return self.metadata['state'] == 'running'

    def apply_control(self, operation, context):
        value = self.control_value(operation, context)
        method = CONTROLS[operation['control']][0]
        getattr(self.devices[operation['role']], method)(value)

    def set_point(self):
        self.frames.clear()
        context = dict(self.parameters, **self.steps[self.step_index])
        for operation in self.spec['before_point']:
            self.apply_control(operation, context)
        self.armed_statistics = {}
        self.captures.begin_point()
        self.captures.start('transition', context)
        if self.captures.failure:
            raise ValueError(self.captures.failure)
        for axis in self.spec['axes']:
            if 'control' in axis:
                self.apply_control(dict(role=axis['role'], control=axis['control'], value=context[axis['id']]), context)
        self.due = time.monotonic()+self.settle_s

    def calculate(self, values, aggregate):
        for key in self.derived_order:
            if self.spec['derived'][key]['aggregate'] == aggregate:
                values[key] = self.derived_expressions[key].evaluate(values)

    def check(self, stage, values):
        for check_stage, expression, message in self.checks:
            if check_stage == stage and expression.evaluate(values) is not True:
                raise ValueError(message)

    def group_key(self, step):
        return tuple(step[key] for key in self.reduction['group_by']) if self.reduction else ()

    def reduce_group(self, stopped):
        reduction = self.reduction
        if not reduction:
            return
        accepted = [point for point in self.group_points if self.where_expression.evaluate(dict(self.parameters, **point)) is True]
        row = {key:self.group_points[0][key] for key in reduction['group_by']}
        for key, metadata in reduction['metrics'].items():
            values = [self.summary_expressions[key].evaluate(dict(self.parameters, **point)) for point in accepted]
            values = [value for value in values if value is not None]
            reducer = metadata['reducer']
            row[key] = (len(values) if reducer == 'count' else None if not values else
                        values[-1] if reducer == 'last' else values[0] if reducer == 'first' else
                        min(values) if reducer == 'min' else max(values) if reducer == 'max' else sum(values)/len(values))
        if 'status' in reduction:
            status = reduction['status']
            row[status['field']] = status['stopped'] if stopped else status['exhausted']
        self.summary.append(row)
        self.db.execute('INSERT INTO summary VALUES(?)', (json.dumps(row, allow_nan=False),))
        self.group_points.clear()

    def advance(self, batches, cycle_ns):
        if not self.active:
            return
        try:
            self.record_secondary(batches, cycle_ns)
            for role in self.needed_roles:
                identity = self.bindings[role]
                if identity not in batches or batches[identity]['error']:
                    raise ValueError(f'{role}: instrument disconnected or acquisition failed')
            if any(stage == 'continuous' for stage, _, _ in self.checks):
                values = dict(self.parameters, **self.steps[self.step_index], elapsed_s=(cycle_ns-self.origin_ns)/1e9)
                for key, metric in self.scalar_metrics.items():
                    identity = self.bindings.get(metric['role'])
                    channel = self.channels[key]
                    reading = next((item for item in batches.get(identity, {}).get('readings', []) if channel and item.channel == channel[1]), None)
                    valid = reading is not None and reading.value is not None and math.isfinite(reading.value) and reading.unit == metric['unit'] and reading.quality == 'ok'
                    values[key] = reading.value if valid else None
                self.calculate(values, 'sample_mean')
                self.check('continuous', values)
            if self.pending_point is None:
                self.captures.poll(self.armed_statistics)
                if self.captures.failure:
                    raise ValueError(self.captures.failure)
            if cycle_ns/1e9 < self.due:
                return
            frame = {}
            for role, identity in self.bindings.items():
                batch = batches.get(identity, dict(readings=[], start_ns=cycle_ns, end_ns=cycle_ns))
                for reading in batch['readings']:
                    value = reading.value
                    if value is not None and (not math.isfinite(value) or abs(value) > 1e300):
                        value = None
                    quality = reading.quality if value is not None else (reading.quality if reading.quality != 'ok' else 'unavailable')
                    frame[(role, reading.channel)] = (value, reading.unit, quality)
                    self.db.execute('INSERT INTO samples VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (len(self.points), role, identity, reading.channel, value, reading.unit, quality,
                         cycle_ns-self.origin_ns, batch['start_ns']-self.origin_ns, batch['end_ns']-self.origin_ns))
            self.db.commit()
            context = dict(self.parameters, **self.steps[self.step_index], elapsed_s=(cycle_ns-self.origin_ns)/1e9)
            required_invalid = False
            for key, metric in self.scalar_metrics.items():
                value, unit, quality = frame.get(self.channels[key], (None, '', 'missing'))
                valid = value is not None and unit == metric['unit'] and quality == 'ok'
                context[key] = value if valid else None
                required_invalid |= metric['required'] and not valid
            if self.spec['acquisition']['capture_all']:
                for (role, channel), (value, unit, quality) in frame.items():
                    if quality == 'disabled':
                        continue
                    key = role+'.'+channel
                    self.metrics[key] = (self.definition['roles'][role]['name']+' · '+channel, unit)
                    context[key] = value if quality == 'ok' else None
            self.calculate(context, 'sample_mean')
            if self.pending_point is not None:
                if required_invalid:
                    raise ValueError('Required readback became unavailable while awaiting a waveform')
                self.check('sample', context)
                if self.captures.poll(self.pending_point):
                    self.complete_point()
                return
            self.frames.append(context)
            # Invalid required measurements are recorded and fail immediately, not after averaging.
            if not required_invalid:
                self.check('sample', context)
            fail_now = required_invalid and self.spec['acquisition']['on_invalid'] == 'fail'
            if not fail_now and len(self.frames) < self.sample_count:
                return
            required_invalid = any(sample.get(key) is None for sample in self.frames for key, metric in self.scalar_metrics.items() if metric['required'])
            point = dict(self.steps[self.step_index], elapsed_s=context['elapsed_s'], quality='ok')
            sample_keys = set(self.scalar_metrics) | {key for key, metric in self.spec['derived'].items() if metric['aggregate'] == 'sample_mean'}
            if self.spec['acquisition']['capture_all']:
                sample_keys |= {key for sample in self.frames for key in sample if '.' in key}
            for key in sorted(sample_keys):
                values = [sample.get(key) for sample in self.frames]
                point[key] = sum(values)/len(values) if all(value is not None for value in values) else None
            point.update({key:None for key in waveform_columns(self.spec)})
            point.update(self.armed_statistics)
            point_context = dict(self.parameters, **point)
            self.calculate(point_context, 'point')
            point.update({key:point_context.get(key) for key, metric in self.spec['derived'].items() if metric['aggregate'] == 'point'})
            self.frames.clear()
            self.pending_point = point
            self.pending_required_invalid = required_invalid
            if required_invalid and self.spec['acquisition']['on_invalid'] == 'fail':
                self.complete_point()
                return
            self.captures.start('settled', dict(self.parameters, **point))
            if self.captures.poll(point):
                self.complete_point()
        except Exception as exc:
            self.finish('failed', str(exc))

    def record_secondary(self, batches, cycle_ns):
        for key, metric in self.spec.get('secondary_measurements', {}).items():
            identity = self.bindings.get(metric['role'])
            batch = batches.get(identity, {})
            channel = self.channels[key]
            reading = next((item for item in batch.get('readings', []) if channel and item.channel == channel[1]), None)
            valid = reading is not None and reading.value is not None and math.isfinite(reading.value) and abs(reading.value) <= 1e300 and reading.quality == 'ok' and reading.unit == metric['unit']
            quality = 'ok' if valid else 'unassigned' if not identity else 'unsupported' if channel is None else 'disconnected' if not batch or batch.get('error') else 'unavailable'
            self.db.execute('INSERT INTO secondary_samples VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (len(self.points), key, metric['role'], identity, reading.value if valid else None, metric['unit'], quality, metric.get('location', ''),
                 cycle_ns-self.origin_ns, batch.get('start_ns', cycle_ns)-self.origin_ns, batch.get('end_ns', cycle_ns)-self.origin_ns))
        if self.spec.get('secondary_measurements'):
            self.db.commit()

    def secondary_plot_rows(self, metric):
        count = self.db.execute('SELECT COUNT(*) FROM secondary_samples WHERE metric=?', (metric,)).fetchone()[0]
        stride = max(1, math.ceil(count/700))
        rows, bucket = [], []
        def flush():
            valid = [(tick/1e9, value) for tick,value,quality in bucket if quality == 'ok' and value is not None]
            if len(valid) < len(bucket):
                rows.append(dict(elapsed_s=bucket[-1][0]/1e9, **{metric:None}))
                return
            extremes = {min(valid, key=lambda pair:pair[1]), max(valid, key=lambda pair:pair[1])}
            rows.extend(dict(elapsed_s=tick, **{metric:value}) for tick,value in sorted(extremes))
        for row in self.db.execute('SELECT tick_ns,value,quality FROM secondary_samples WHERE metric=? ORDER BY tick_ns', (metric,)):
            bucket.append(row)
            if len(bucket) >= stride:
                flush()
                bucket.clear()
        if bucket:
            flush()
        return rows

    def complete_point(self):
        point = self.pending_point
        self.pending_point = None
        context = dict(self.parameters, **point)
        self.calculate(context, 'point')
        point.update({key:context.get(key) for key, metric in self.spec['derived'].items() if metric['aggregate'] == 'point'})
        point['quality'] = 'invalid' if self.pending_required_invalid or self.captures.failure else 'unavailable' if any(value is None for key,value in point.items() if key not in self.secondary_names) else 'ok'
        if self.spec.get('secondary_measurements') or self.spec.get('waveforms'):
            unavailable = any(point.get(key) is None for key in self.spec.get('secondary_measurements', {}))
            unavailable |= any(status not in ('ok', 'not_requested', 'limit_reached') for status in self.captures.status.values())
            point['secondary_quality'] = 'unavailable' if unavailable else 'ok'
            point.update({key+'_status':status for key,status in self.captures.status.items()})
        self.group_points.append(point)
        self.add_point(point)
        if self.pending_required_invalid and self.spec['acquisition']['on_invalid'] == 'fail':
            raise ValueError('Required readback is unavailable; test stopped')
        if self.captures.failure:
            raise ValueError(self.captures.failure)
        self.check('point', dict(self.parameters, **point))
        stop_value = self.stop_expression.evaluate(dict(self.parameters, **point)) if self.stop_expression else False
        if self.stop_expression and stop_value is None:
            raise ValueError('Stop condition is unavailable; test stopped')
        stopped = stop_value is True
        previous = self.steps[self.step_index]
        self.step_index += 1
        if stopped:
            if self.spec['stop_when']['scope'] == 'run':
                self.step_index = len(self.steps)
            else:
                while self.step_index < len(self.steps) and self.group_key(self.steps[self.step_index]) == self.group_key(previous):
                    self.step_index += 1
        boundary = self.step_index >= len(self.steps) or self.group_key(previous) != self.group_key(self.steps[self.step_index])
        if boundary:
            self.reduce_group(stopped)
        if self.step_index >= len(self.steps):
            self.finish('completed')
        else:
            self.set_point()

    def add_point(self, point):
        self.points.append(point)
        self.db.execute('INSERT INTO points VALUES(?,?)', (len(self.points)-1, json.dumps(point, allow_nan=False)))
        self.persist()

    def persist(self):
        self.metadata.update(points=self.points, summary=self.summary, completed_points=len(self.points))
        self.metadata['presentation'] = self.plots()
        self.db.execute('UPDATE run SET metadata=?', (json.dumps(self.metadata, allow_nan=False),))
        self.db.commit()
        temporary = self.directory/'run.tmp'
        temporary.write_text(json.dumps(self.metadata, allow_nan=False))
        temporary.replace(self.directory/'run.json')

    def finish(self, state, error=''):
        if not self.active:
            return
        # Always try both outputs, including after one driver's disable fails.
        for device in self.outputs:
            try:
                device.set_enabled(False)
            except Exception as exc:
                self.cleanup_errors.append(f'{device.name}: {exc}')
        try:
            capture_errors = self.captures.cancel()
        except Exception as exc:
            capture_errors = [str(exc)]
        if capture_errors:
            state = 'failed'
            error = '; '.join(filter(None, [error, 'Waveform cleanup failed: '+ '; '.join(capture_errors)]))
        for device in self.channel_devices:
            try:
                device.end_experiment_channels()
            except Exception as exc:
                state = 'failed'
                error = '; '.join(filter(None, [error, 'Scope channel restoration failed: '+str(exc)]))
        self.metadata['capture_cleanup_errors'] = capture_errors
        if self.cleanup_errors:
            state = 'failed'
            error = '; '.join(filter(None, [error, 'Output disable failed: ' + '; '.join(self.cleanup_errors)]))
        self.metadata.update(state=state, error=error, finished_utc_ns=time.time_ns(),
                             output_disable_confirmed=not self.cleanup_errors if self.outputs else None)
        try:
            self.persist()
            self.export()
        except Exception as exc:
            self.metadata.update(state='failed', error='; '.join(filter(None, [error, 'Saving results failed: '+str(exc)])))
            try:
                self.persist()
            except Exception:
                pass
        finally:
            self.db.close()

    def export(self):
        with Path(self.metadata['secondary_csv']).open('w', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(['point_index','metric','role','instrument','value','unit','quality','location','tick_ns','read_start_ns','read_end_ns'])
            writer.writerows(self.db.execute('SELECT * FROM secondary_samples ORDER BY tick_ns,metric'))
        for name, rows in [('points', self.points), ('summary', self.summary)]:
            with (self.directory/(name+'.csv')).open('w', newline='') as stream:
                columns = list(dict.fromkeys(key for row in rows for key in row))
                writer = csv.DictWriter(stream, fieldnames=columns)
                writer.writeheader()
                writer.writerows(rows)
        esc = lambda value: html.escape(str(value), quote=True)
        table_rows = self.summary if self.spec['table']['data'] == 'summary' else self.points
        plots = []
        for index, spec in enumerate(self.metadata['presentation']):
            rows = self.secondary_plot_rows(spec['y']) if spec['data'] == 'secondary' else self.summary if spec['data'] == 'summary' else self.points
            svg = plot_svg(spec, rows)
            (self.directory/f'plot-{index}.svg').write_text(svg)
            plots.append(svg)
        self.metadata['plots'] = [str(self.directory/f'plot-{index}.svg') for index in range(len(plots))]
        rows = table_rows
        columns = self.spec['table'].get('columns', list(dict.fromkeys(key for row in rows for key in row)))
        def column_label(key):
            label, unit = self.metadata['metrics'].get(key, (key.replace('_', ' ').capitalize(), ''))
            return label + (f' ({unit})' if unit else '')
        table = '<table><thead><tr>' + ''.join('<th>'+esc(column_label(key))+'</th>' for key in columns) + '</tr></thead><tbody>'
        for row in rows:
            table += '<tr>' + ''.join('<td>'+esc('Unavailable' if row.get(key) is None else row[key])+'</td>' for key in columns) + '</tr>'
        table += '</tbody></table>'
        report = '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>'+esc(self.metadata['name'])+'</title><style>body{font:16px system-ui;max-width:1100px;margin:40px auto;padding:0 24px}svg{max-width:100%;height:auto}table{border-collapse:collapse;font-size:13px}td,th{padding:8px;border:1px solid #ccc}pre{white-space:pre-wrap}</style></head><body>'
        report += '<h1>'+esc(self.metadata['name'])+'</h1><p>'+('SIMULATED · ' if self.metadata['simulated'] else '')+esc(self.metadata['state'])+'</p>'
        started = datetime.fromtimestamp(self.metadata['started_utc_ns']/1e9, timezone.utc).isoformat()
        report += '<p>Started '+esc(started)+' · '+esc(self.metadata['test'].replace('_', ' '))+'</p>'
        report += '<p>'+esc(self.metadata['error'])+'</p><p>'+esc(self.metadata['timing'])+'</p>'
        for note in self.metadata.get('notes', []):
            report += '<p>'+esc(note)+'</p>'
        if self.spec.get('secondary_measurements'):
            report += '<p><a href="secondary.csv">Continuous secondary measurements CSV</a></p>'
        wave_html = '<h2>Secondary waveform captures</h2>' if self.captures.records else ''
        for capture in self.captures.records:
            wave_html += '<h3>'+esc(capture['label'])+' · point '+str(capture['point_index']+1)+'</h3><p>'+esc(capture['quality'])+' '+esc(capture['error'])+'</p>'
            if capture.get('plot'):
                wave_html += Path(capture['plot']).read_text()+'<p><a href="'+esc(Path(capture['csv']).name)+'">Full-resolution waveform CSV</a></p>'
            if capture.get('screenshot'):
                filename = esc(Path(capture['screenshot']).name)
                wave_html += '<p>Scope screenshot from this capture</p><a href="'+filename+'"><img style="max-width:100%" alt="Scope screen" src="'+filename+'"></a>'
            if capture.get('screenshot_error'):
                wave_html += '<p>Scope screenshot unavailable: '+esc(capture['screenshot_error'])+'</p>'
        report += ''.join(plots)+table+wave_html+'<h2>Run configuration</h2><pre>'+esc(json.dumps({key:self.metadata[key] for key in ('test', 'parameters', 'instruments', 'definition')}, indent=2))+'</pre></body></html>'
        (self.directory/'report.html').write_text(report)
        self.persist()
