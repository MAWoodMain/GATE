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
from gate.procedures import control_roles, test_spec, parameters, sweep_values, METRICS, presentation


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
    """Small standalone vector plot with units, grouped curves and explicit axes."""
    esc = lambda text: html.escape(str(text), quote=True)
    groups = defaultdict(list)
    for row in rows:
        x, y = row.get(plot['x']), row.get(plot['y'])
        key = str(row.get(plot['series'], '')) if plot['series'] else ''
        groups[key].append((x, y) if x is not None and y is not None and math.isfinite(x) and math.isfinite(y) else None)
    points = [point for series in groups.values() for point in series if point is not None]
    if not points:
        return '<svg xmlns="http://www.w3.org/2000/svg" width="720" height="100"><text x="20" y="50">No valid data to plot</text></svg>'
    xmin, xmax = min(p[0] for p in points), max(p[0] for p in points)
    ymin, ymax = min(p[1] for p in points), max(p[1] for p in points)
    if xmin == xmax:
        xmin, xmax = xmin - .5, xmax + .5
    if ymin == ymax:
        ymin, ymax = ymin - max(.5, abs(ymin)*.05), ymax + max(.5, abs(ymax)*.05)
    padding = (ymax-ymin)*.08
    ymin, ymax = ymin-padding, ymax+padding
    px = lambda value: 85 + (value-xmin)/(xmax-xmin)*590
    py = lambda value: 340 - (value-ymin)/(ymax-ymin)*270
    height = max(460, 440 + 18 * math.ceil(len(groups)/6))
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="760" height="{height}" viewBox="0 0 760 {height}" role="img">',
             f'<title>{esc(plot["title"])}</title><rect width="760" height="{height}" fill="white"/>',
             f'<text x="380" y="28" text-anchor="middle" font-family="sans-serif" font-size="17">{esc(plot["title"])}</text>']
    for i in range(6):
        x, y = 85+i*118, 340-i*54
        parts += [f'<path d="M{x} 70V340 M85 {y}H675" stroke="#ddd"/>',
                  f'<text x="{x}" y="362" text-anchor="middle" font-size="12">{xmin+(xmax-xmin)*i/5:.4g}</text>',
                  f'<text x="77" y="{y+4}" text-anchor="end" font-size="12">{ymin+(ymax-ymin)*i/5:.4g}</text>']
    parts += ['<path d="M85 70V340H675" stroke="#333" fill="none"/>',
              f'<text x="380" y="392" text-anchor="middle" font-size="14">{esc(plot["x_label"])}</text>',
              f'<text transform="translate(19 205) rotate(-90)" text-anchor="middle" font-size="14">{esc(plot["y_label"])}</text>']
    colors = ['#3584e4', '#e66100', '#26a269', '#9141ac', '#c01c28', '#1b7588']
    for index, (name, series) in enumerate(groups.items()):
        color = colors[index % len(colors)]
        path = []
        command = 'M'
        for point in series:
            if point is None:
                command = 'M'
                continue
            x, y = point
            path.append(f'{command}{px(x):.2f},{py(y):.2f}')
            command = 'L'
        coordinates = ' '.join(path)
        parts.append(f'<path d="{coordinates}" stroke="{color}" fill="none" stroke-width="2"/>')
        lower_bounds = {(row.get(plot['x']), row.get(plot['y'])) for row in rows if row.get('boundary') == 'at_least_sweep_limit'}
        for x, y in (point for point in series if point is not None):
            if (x, y) in lower_bounds:
                parts.append(f'<path d="M{px(x):.2f} {py(y)-5:.2f}l-5 9h10Z" fill="{color}"><title>At least sweep limit</title></path>')
                continue
            parts.append(f'<circle cx="{px(x):.2f}" cy="{py(y):.2f}" r="3" fill="{color}"/>')
        if name:
            parts.append(f'<text x="{85+(index%6)*108}" y="{420+(index//6)*18}" font-size="12" fill="{color}">{esc(name)} V input</text>')
    if plot['y'] == 'maximum_current_a':
        parts.append('<text x="85" y="435" font-size="12">Triangle: at least sweep limit · Circle: threshold reached</text>')
    parts.append('</svg>')
    return ''.join(parts)


class ExperimentRun:
    def __init__(self, definition, test, bindings, supplied, instruments, resolve, interval, directory,
                 enable_outputs=False, wiring_confirmed=False):
        self.definition = copy.deepcopy(definition)
        self.kind = test_spec(definition, test)['procedure']
        self.parameters = parameters(self.kind, supplied, interval)
        checked = check_bindings(definition, bindings, instruments, resolve)
        if not checked['compatible']:
            raise ValueError('; '.join(checked['errors']))
        self.bindings = checked['bindings']
        self.devices = {role: instruments[key] for role, key in self.bindings.items()}
        self.roles = control_roles(definition) if self.kind != 'capture' else {}
        self.outputs = []
        self.points, self.summary, self.frames = [], [], []
        self.voltage_index = self.current_index = 0
        self.best_current = None
        self.best_voltage = None
        self.cleanup_errors = []
        self.due = 0.
        self.origin_ns = time.monotonic_ns()
        if self.roles:
            if enable_outputs is not True or wiring_confirmed is not True:
                raise ValueError('Confirm wiring and explicitly allow output enable before running a sweep')
            supply, load = self.devices[self.roles['supply']], self.devices[self.roles['load']]
            if len({bool(device.simulated) for device in self.devices.values()}) != 1:
                raise ValueError('Do not mix simulated and real equipment in an output sweep')
            self.voltages = sweep_values(self.parameters['input_start_v'], self.parameters['input_stop_v'], self.parameters['input_step_v'])
            self.currents = sweep_values(self.parameters['load_start_a'], self.parameters['load_stop_a'], self.parameters['load_step_a'])
            self.validate_controls(supply, load)
            self.outputs = [load, supply]
            metrics = dict(METRICS)
            metrics.pop('maximum_current_a')
        else:
            metrics = {'elapsed_s': METRICS['elapsed_s']}
        self.metadata = dict(id=uuid.uuid4().hex, experiment=definition['id'], name=definition['name'], test=test,
            procedure=self.kind, parameters=self.parameters, bindings=self.bindings, definition=self.definition,
            instruments={role: dict(id=device.id, name=device.name, simulated=device.simulated,
                limits=copy.deepcopy(getattr(device, 'experiment_limits', {})),
                identity=getattr(device, 'name', '')) for role, device in self.devices.items()},
            simulated=any(device.simulated for device in self.devices.values()), state='running', error='',
            started_utc_ns=time.time_ns(), finished_utc_ns=None, points=[], summary=[], metrics=metrics,
            presentation=presentation(self.kind, metrics), completed_points=0,
            total_points=len(self.voltages)*len(self.currents) if self.roles else self.parameters['capture_points'],
            interval_s=interval, timing='Host monotonic time; sequential instrument read windows, not simultaneous sampling')
        self.directory = Path(directory) / self.metadata['id']
        self.directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        self.metadata.update(path=str(self.directory/'run.sqlite'), report=str(self.directory/'report.html'),
                             csv=str(self.directory/'points.csv'), summary_csv=str(self.directory/'summary.csv'))
        self.db = sqlite3.connect(self.directory/'run.sqlite')
        self.db.executescript('''CREATE TABLE run (metadata TEXT);
            CREATE TABLE samples (point_index INTEGER, role TEXT, instrument TEXT, channel TEXT,
                value REAL, unit TEXT, quality TEXT, tick_ns INTEGER, read_start_ns INTEGER, read_end_ns INTEGER);
            CREATE TABLE points (point_index INTEGER, data TEXT);
            CREATE TABLE summary (data TEXT);''')
        self.db.execute('INSERT INTO run VALUES(?)', (json.dumps(self.metadata),))
        self.persist()
        try:
            if self.roles:
                # Disable both ends before configuring; do not inherit front-panel state.
                load.set_enabled(False)
                supply.set_enabled(False)
                supply.set_current_limit(self.parameters['current_limit_a'])
                self.set_point()
                supply.set_enabled(True)
                load.set_enabled(True)
        except Exception as exc:
            self.finish('failed', str(exc))

    @property
    def active(self):
        return self.metadata['state'] == 'running'

    def validate_controls(self, supply, load):
        p = self.parameters
        for device, methods in [(supply, ('set_enabled', 'set_voltage', 'set_current_limit')),
                                (load, ('set_enabled', 'set_current'))]:
            if not getattr(device, 'experiment_control_supported', False) or any(not callable(getattr(device, method, None)) for method in methods):
                raise ValueError(f'{device.name}: driver has no verified experiment output control')
            limits = getattr(device, 'experiment_limits', {})
            for quantity in ('voltage', 'current'):
                bounds = limits.get(quantity)
                if not isinstance(bounds, (list, tuple)) or len(bounds) != 2 or not all(isinstance(v, (float, int)) and math.isfinite(v) for v in bounds) or bounds[0] > bounds[1]:
                    raise ValueError(f'{device.name}: missing verified {quantity} limits')
            power = limits.get('power_w')
            if not isinstance(power, (float, int)) or not math.isfinite(power) or power <= 0:
                raise ValueError(f'{device.name}: missing verified power limit')
        sl, ll = supply.experiment_limits, load.experiment_limits
        if not sl['voltage'][0] <= p['input_start_v'] <= p['input_stop_v'] <= sl['voltage'][1]:
            raise ValueError('Input voltage sweep exceeds the supply limits')
        if not sl['current'][0] <= p['current_limit_a'] <= sl['current'][1]:
            raise ValueError('Supply current limit exceeds its driver limits')
        if p['input_stop_v'] * p['current_limit_a'] > sl['power_w']:
            raise ValueError('Input voltage and current limit exceed the supply power rating')
        maximum_voltage = p['nominal_output_v'] * (1+p['tolerance_pct']/100)
        if not ll['voltage'][0] <= p['nominal_output_v'] <= maximum_voltage <= ll['voltage'][1]:
            raise ValueError('Expected output voltage exceeds the load limits')
        if not ll['current'][0] <= p['load_start_a'] <= p['load_stop_a'] <= ll['current'][1] or maximum_voltage*p['load_stop_a'] > ll['power_w']:
            raise ValueError('Load sweep exceeds the load current or power limits')
        for device in (supply, load):
            channels = getattr(device, 'experiment_channels', None)
            if not isinstance(channels, dict) or not all(isinstance(channels.get(key), str) for key in ('voltage', 'current')):
                raise ValueError(f'{device.name}: driver must map voltage and current readback channels')

    def set_point(self):
        self.frames.clear()
        supply, load = self.devices[self.roles['supply']], self.devices[self.roles['load']]
        # Remove load before changing input voltage, then apply the next load setpoint.
        load.set_current(0.)
        supply.set_voltage(self.voltages[self.voltage_index])
        load.set_current(self.currents[self.current_index])
        self.due = time.monotonic() + self.parameters['settle_s']

    def advance(self, batches, cycle_ns):
        if not self.active:
            return
        try:
            for role, identity in self.bindings.items():
                if identity not in batches or batches[identity]['error']:
                    raise ValueError(f'{role}: instrument disconnected or acquisition failed')
            if cycle_ns / 1e9 < self.due:
                return
            frame = {}
            for role, identity in self.bindings.items():
                batch = batches[identity]
                for reading in batch['readings']:
                    value = reading.value
                    if value is not None and not math.isfinite(value):
                        value = None
                    quality = reading.quality if value is not None else (reading.quality if reading.quality != 'ok' else 'unavailable')
                    frame[(role, reading.channel)] = (value, reading.unit, quality)
                    self.db.execute('INSERT INTO samples VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (len(self.points), role, identity, reading.channel, value, reading.unit, quality,
                         cycle_ns-self.origin_ns, batch['start_ns']-self.origin_ns, batch['end_ns']-self.origin_ns))
            self.db.commit()
            self.frames.append(frame)
            sample_invalid = False
            if self.roles:
                instant = {}
                for side, role_type in [('input', 'supply'), ('output', 'load')]:
                    role = self.roles[role_type]
                    for quantity, unit in [('voltage', 'V'), ('current', 'A')]:
                        channel = self.devices[role].experiment_channels[quantity]
                        value, actual_unit, quality = frame.get((role, channel), (None, '', 'missing'))
                        if value is None or actual_unit != unit or quality != 'ok':
                            sample_invalid = True
                        else:
                            instant[side+'_'+quantity] = value
                            if value < 0:
                                raise ValueError('Unexpected negative DC readback; sweep stopped')
                maximum_voltage = self.parameters['nominal_output_v']*(1+self.parameters['tolerance_pct']/100)
                if instant.get('output_voltage', 0) > maximum_voltage:
                    raise ValueError('Output exceeded the configured voltage tolerance')
                if instant.get('input_current', 0) > self.parameters['current_limit_a']*1.05:
                    raise ValueError('Input current exceeded the configured limit')
            if not sample_invalid and len(self.frames) < self.parameters.get('samples_per_point', 1):
                return
            point = dict(elapsed_s=(cycle_ns-self.origin_ns)/1e9, quality='ok')
            if self.kind == 'capture':
                for (role, channel), (value, unit, quality) in frame.items():
                    if quality == 'disabled':
                        continue
                    key = role + '.' + channel
                    self.metadata['metrics'][key] = (self.definition['roles'][role]['name'] + ' · ' + channel, unit)
                    point[key] = value if quality == 'ok' else None
                    if quality not in ('ok', 'disabled'):
                        point['quality'] = 'unavailable'
                self.frames.clear()
                self.add_point(point)
                if len(self.points) >= self.parameters['capture_points']:
                    self.finish('completed')
                return
            point.update(input_voltage_set_v=self.voltages[self.voltage_index],
                         load_current_set_a=self.currents[self.current_index])
            invalid = False
            for side, role_type in [('input', 'supply'), ('output', 'load')]:
                role = self.roles[role_type]
                device = self.devices[role]
                for quantity, unit in [('voltage', 'V'), ('current', 'A')]:
                    channel = device.experiment_channels[quantity]
                    values = [sample.get((role, channel), (None, '', 'missing')) for sample in self.frames]
                    valid = all(value is not None and actual_unit == unit and quality == 'ok' for value, actual_unit, quality in values)
                    point[f'{side}_{quantity}_{unit.lower()}'] = sum(value for value, _, _ in values)/len(values) if valid else None
                    invalid |= not valid
            averaged_frames = list(self.frames)
            self.frames.clear()
            point.update(input_power_w=None, output_power_w=None, efficiency_pct=None)
            if not invalid:
                for side, role_type in [('input', 'supply'), ('output', 'load')]:
                    role = self.roles[role_type]
                    channels = self.devices[role].experiment_channels
                    point[side + '_power_w'] = sum(frame[(role, channels['voltage'])][0] * frame[(role, channels['current'])][0]
                                                         for frame in averaged_frames) / len(averaged_frames)
                if point['input_power_w'] > 0:
                    point['efficiency_pct'] = 100 * point['output_power_w']/point['input_power_w']
            point['quality'] = 'invalid' if invalid else 'zero_input_power' if point['input_power_w'] <= 0 else 'ok'
            nominal = self.parameters['nominal_output_v']
            point['in_regulation'] = not invalid and abs(point['output_voltage_v'] - nominal) <= nominal*self.parameters['tolerance_pct']/100
            self.add_point(point)
            if invalid:
                raise ValueError('Required voltage/current readback is unavailable; sweep stopped')
            if any(point[key] < 0 for key in ('input_voltage_v', 'input_current_a', 'output_voltage_v', 'output_current_a')):
                raise ValueError('Unexpected negative DC readback; sweep stopped')
            if point['output_voltage_v'] > nominal*(1+self.parameters['tolerance_pct']/100):
                raise ValueError('Output exceeded the configured voltage tolerance')
            if self.kind == 'efficiency' and not point['in_regulation']:
                raise ValueError('Output fell outside the configured regulation tolerance')
            if point['input_current_a'] > self.parameters['current_limit_a']*1.05:
                raise ValueError('Input current exceeded the configured limit')
            if self.kind == 'maximum_output_current' and point['in_regulation']:
                self.best_current, self.best_voltage = point['output_current_a'], point['output_voltage_v']
            self.current_index += 1
            boundary = self.current_index >= len(self.currents)
            threshold = self.kind == 'maximum_output_current' and not point['in_regulation']
            if boundary or threshold:
                if self.kind == 'maximum_output_current':
                    summary = dict(input_voltage_set_v=self.voltages[self.voltage_index],
                        maximum_current_a=self.best_current, output_voltage_v=self.best_voltage,
                        boundary='threshold_reached' if threshold else 'at_least_sweep_limit')
                    self.summary.append(summary)
                    self.db.execute('INSERT INTO summary VALUES(?)', (json.dumps(summary),))
                self.voltage_index += 1
                self.current_index = 0
                self.best_current = self.best_voltage = None
            if self.voltage_index >= len(self.voltages):
                self.finish('completed')
            else:
                self.set_point()
        except Exception as exc:
            self.finish('failed', str(exc))

    def add_point(self, point):
        self.points.append(point)
        self.db.execute('INSERT INTO points VALUES(?,?)', (len(self.points)-1, json.dumps(point, allow_nan=False)))
        self.persist()

    def persist(self):
        self.metadata.update(points=self.points, summary=self.summary, completed_points=len(self.points))
        self.metadata['presentation'] = presentation(self.kind, self.metadata['metrics'])
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
        for name, rows in [('points', self.points), ('summary', self.summary)]:
            with (self.directory/(name+'.csv')).open('w', newline='') as stream:
                columns = list(dict.fromkeys(key for row in rows for key in row))
                writer = csv.DictWriter(stream, fieldnames=columns)
                writer.writeheader()
                writer.writerows(rows)
        esc = lambda value: html.escape(str(value), quote=True)
        plot_rows = self.summary if self.kind == 'maximum_output_current' else self.points
        plots = []
        for index, spec in enumerate(self.metadata['presentation']):
            svg = plot_svg(spec, plot_rows)
            (self.directory/f'plot-{index}.svg').write_text(svg)
            plots.append(svg)
        self.metadata['plots'] = [str(self.directory/f'plot-{index}.svg') for index in range(len(plots))]
        rows = plot_rows
        columns = list(dict.fromkeys(key for row in rows for key in row))
        def column_label(key):
            label, unit = self.metadata['metrics'].get(key, METRICS.get(key, (key.replace('_', ' ').capitalize(), '')))
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
        if self.kind == 'maximum_output_current':
            report += '<p>Results labelled at_least_sweep_limit are lower bounds: the regulation threshold was not reached within the configured sweep.</p>'
        report += ''.join(plots)+table+'<h2>Run configuration</h2><pre>'+esc(json.dumps({key:self.metadata[key] for key in ('test', 'parameters', 'instruments', 'definition')}, indent=2))+'</pre></body></html>'
        (self.directory/'report.html').write_text(report)
        self.persist()
