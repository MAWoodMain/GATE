"""Optional observation roles and an acquisition-thread waveform state machine."""
import copy
import csv
import json
import math
import time
from pathlib import Path
from gate.expressions import Expression
from gate.test_spec import number
from gate.waveforms import STATISTICS, validate_waveform, preview_rows, validate_screenshot


def required_roles(definition, spec):
    roles = {key for key, value in definition['roles'].items() if not value.get('optional', False)}
    roles.update(spec['outputs'])
    for field in ('measurements', 'secondary_measurements', 'waveforms'):
        roles.update(value['role'] for value in spec.get(field, {}).values() if value['required'])
    return roles


def waveform_columns(spec):
    return {key+'_'+statistic: (waveform['label']+' · '+statistic.replace('_', ' '), waveform['unit'])
            for key, waveform in spec.get('waveforms', {}).items() for statistic in STATISTICS}


class CapturePlan:
    def __init__(self, plan):
        self.specs = plan.spec.get('waveforms', {})
        self.requests = {}
        self.disabled = {}
        budget = 0
        for key, spec in self.specs.items():
            requests = []
            for step in plan.steps:
                values = dict(plan.parameters, **step)
                def evaluate(field):
                    return Expression(spec[field], values).evaluate(values) if spec[field] is not None else None
                points = evaluate('points')
                number(points, 'Waveform points')
                if int(points) != points or not 2 <= points <= 16384:
                    raise ValueError('Waveform points must be an integer between 2 and 16384')
                window, pretrigger, timeout = evaluate('window_s'), evaluate('pretrigger'), evaluate('timeout_s')
                for field, value, low, high in [('window_s', window, 1e-9, 60.), ('pretrigger', pretrigger, 0., .95), ('timeout_s', timeout, .1, 30.)]:
                    if value is None and field != 'timeout_s':
                        continue
                    number(value, 'Waveform '+field)
                    if not low <= value <= high:
                        raise ValueError('Waveform '+field+' is outside its supported range')
                trigger = None
                if spec['trigger']:
                    level = Expression(spec['trigger']['level'], values).evaluate(values)
                    number(level, 'Trigger level')
                    trigger = dict(level=level, slope=spec['trigger']['slope'])
                requests.append(dict(channel=spec['channel'], unit=spec['unit'], phase=spec['phase'], points=int(points),
                    window_s=window, pretrigger=pretrigger, trigger=trigger, timeout_s=timeout,
                    screenshot=spec.get('screenshot', spec['phase'] == 'transition')))
            self.requests[key] = requests
            budget += max(item['points'] for item in requests)*min(spec['max_captures'], len(plan.steps))
            device = plan.devices.get(spec['role'])
            capability = getattr(device, 'waveform_capabilities', {}).get(spec['channel'], {})
            reason = ''
            if device is None:
                reason = 'unassigned'
            elif not all(callable(getattr(device, method, None)) for method in ('begin_waveform', 'poll_waveform', 'cancel_waveform')):
                reason = 'Driver does not support waveform capture'
            elif spec['phase'] not in capability.get('phases', []) or capability.get('unit') != spec['unit']:
                reason = 'Channel does not support the requested capture phase or units'
            elif any(request['points'] > capability.get('max_points', 0) for request in requests):
                reason = 'Waveform point count exceeds driver limits'
            elif not capability.get('configurable_window', False) and any(request['window_s'] is not None or request['pretrigger'] is not None for request in requests):
                reason = 'Driver reads native scope timing; leave window/pretrigger unset'
            if reason:
                if spec['required']:
                    raise ValueError(key+': '+reason)
                self.disabled[key] = reason
        if budget > 1000000:
            raise ValueError('A test may request at most one million waveform samples in total')


class Captures:
    def __init__(self, run):
        self.run = run
        self.plan = run.capture_plan
        self.pending = []
        self.counts = {key:0 for key in self.plan.specs}
        self.records = []
        self.disabled_reported = set()
        self.status = {}
        self.failure = None

    def begin_point(self):
        self.status = {key:'not_requested' for key in self.plan.specs}
        self.failure = None

    def start(self, phase, context):
        for key, spec in self.plan.specs.items():
            if spec['phase'] != phase:
                continue
            if self.counts[key] >= spec['max_captures']:
                self.status[key] = 'limit_reached'
                continue
            condition = Expression(spec['when'], context).evaluate(context)
            if condition is False:
                continue
            if condition is not True:
                self.counts[key] += 1
                self.record(key, 'condition_unavailable', 'Capture condition is unavailable')
                if spec['required']:
                    self.failure = key+': capture condition is unavailable'
                continue
            if key in self.plan.disabled:
                reason = self.plan.disabled[key]
                self.status[key] = 'unassigned' if reason == 'unassigned' else 'unsupported'
                if key not in self.disabled_reported:
                    self.record(key, self.status[key], reason)
                    self.disabled_reported.add(key)
                continue
            request = self.plan.requests[key][self.run.step_index]
            self.counts[key] += 1
            device = self.run.devices[spec['role']]
            try:
                started = time.monotonic_ns()
                token = device.begin_waveform(copy.deepcopy(request))
                self.pending.append(dict(key=key, device=device, token=token, request=request,
                                         start_ns=started, deadline=time.monotonic()+request['timeout_s']))
                self.status[key] = 'pending'
            except Exception as exc:
                self.record(key, 'error', str(exc))
                if spec['required']:
                    self.failure = key+': '+str(exc)

    def record(self, key, quality, error='', data=None):
        spec = self.plan.specs[key]
        record = dict(id=str(len(self.records)), measurement=key, label=spec['label'], role=spec['role'],
            instrument=self.run.bindings.get(spec['role']), channel=spec['channel'], unit=spec['unit'], phase=spec['phase'],
            screenshot_requested=spec.get('screenshot', spec['phase'] == 'transition'),
            point_index=len(self.run.points), setpoint_index=self.run.step_index, setpoints=copy.deepcopy(self.run.steps[self.run.step_index]),
            quality=quality, error=error, captured_utc_ns=time.time_ns())
        record.update(data or {})
        self.records.append(record)
        self.status[key] = quality
        self.run.db.execute('INSERT INTO waveforms VALUES(?,?)', (record['id'], json.dumps(record, allow_nan=False)))
        self.run.db.commit()
        self.run.persist()
        return record

    def poll(self, point):
        from gate.experiment_runs import plot_svg
        remaining = []
        for item in self.pending:
            key, device, token = item['key'], item['device'], item['token']
            spec = self.plan.specs[key]
            try:
                if time.monotonic() >= item['deadline']:
                    raise TimeoutError('No waveform trigger/capture before the deadline')
                waveform = device.poll_waveform(token)
                if waveform is None:
                    remaining.append(item)
                    continue
                stats = validate_waveform(waveform, item['request'])
                elapsed_start = item['start_ns']-self.run.origin_ns
                elapsed_end = time.monotonic_ns()-self.run.origin_ns
                ordinal = len(self.records)
                name = f'waveform-{ordinal}'
                csv_path, plot_path = self.run.directory/(name+'.csv'), self.run.directory/(name+'.svg')
                with csv_path.open('w', newline='') as stream:
                    writer = csv.writer(stream)
                    writer.writerow(['time_s', 'value', 'unit'])
                    writer.writerows((waveform.start_time_s+index*waveform.sample_interval_s, value, waveform.unit) for index,value in enumerate(waveform.values))
                self.run.db.executemany('INSERT INTO waveform_samples VALUES(?,?,?,?)',
                    ((str(ordinal), index, waveform.start_time_s+index*waveform.sample_interval_s, value) for index,value in enumerate(waveform.values)))
                plot = dict(kind='line', data='points', x='time_s', y='value', series=None, title=spec['label']+' · point '+str(self.run.step_index+1),
                            x_label='Relative waveform time (s)', y_label=spec['label']+' ('+spec['unit']+')')
                plot_path.write_text(plot_svg(plot, preview_rows(waveform)))
                screenshot = None
                screenshot_error = waveform.screenshot_error
                if item['request'].get('screenshot'):
                    if waveform.screenshot is not None:
                        try:
                            validate_screenshot(waveform.screenshot)
                            screenshot = self.run.directory/(name+'-screen.bmp')
                            screenshot.write_bytes(waveform.screenshot)
                        except (ValueError, OSError) as exc:
                            screenshot_error = str(exc)
                    elif not screenshot_error:
                        screenshot_error = 'This driver did not provide a scope screen image'
                record = self.record(key, waveform.quality, data=dict(points=len(waveform.values), sample_interval_s=waveform.sample_interval_s,
                    start_time_s=waveform.start_time_s, trigger_utc_ns=waveform.trigger_utc_ns, driver_metadata=waveform.metadata,
                    read_start_ns=elapsed_start, read_end_ns=elapsed_end, statistics=stats, csv=str(csv_path), plot=str(plot_path),
                    screenshot=str(screenshot) if screenshot else None, screenshot_error=screenshot_error))
                if waveform.quality == 'ok':
                    point.update({key+'_'+statistic:value for statistic,value in stats.items()})
                elif spec['required']:
                    self.failure = key+': waveform quality '+waveform.quality
            except Exception as exc:
                self.record(key, 'timeout' if isinstance(exc, TimeoutError) else 'error', str(exc))
                if spec['required']:
                    self.failure = key+': '+str(exc)
            finally:
                if item not in remaining:
                    try:
                        device.cancel_waveform(token)
                    except Exception as exc:
                        self.failure = key+': capture cleanup failed: '+str(exc)
        self.pending = remaining
        return not remaining

    def cancel(self):
        errors = []
        for item in self.pending:
            try:
                item['device'].cancel_waveform(item['token'])
            except Exception as exc:
                errors.append(str(exc))
            self.record(item['key'], 'cancelled', 'Run stopped before capture completed')
        self.pending.clear()
        return errors
