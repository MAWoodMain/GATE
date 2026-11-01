"""Independent output panel. All instrument operations go through the owner."""
import json
import math
import uuid
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gio, GLib, Gtk
from gate.service import BUS, PATH


CSS = b"""
.gate-output .panel-content { padding: 22px; }
.gate-output .eyebrow { font-size: 10px; font-weight: 700; letter-spacing: 1.2px; }
.gate-output .instrument-name { font-size: 19px; font-weight: 700; }
.gate-output .subtle { opacity: 0.62; font-size: 11px; }
.gate-output .readout { background: #172128; color: #f2f7fa; border-radius: 16px; padding: 16px; }
.gate-output .readout .eyebrow { color: #a8b8c2; }
.gate-output .state-pill { border-radius: 20px; padding: 5px 10px; background: #2c3942; font-size: 11px; font-weight: 700; }
.gate-output .state-pill.energized { background: #235b43; color: #aaf0c5; }
.gate-output .metric-title { font-size: 11px; color: #a8b8c2; }
.gate-output .metric-value { font-family: monospace; font-size: 30px; font-weight: 600; }
.gate-output .metric-unit { font-size: 13px; padding-bottom: 5px; color: #a8b8c2; }
.gate-output .voltage-value { color: #80d9ee; }
.gate-output .current-value { color: #f4cf86; }
.gate-output .power-value { font-size: 23px; }
.gate-output .readout-note { font-size: 11px; color: #a8b8c2; }
.gate-output .control-card { border: 1px solid alpha(@theme_fg_color, 0.12); background: alpha(@theme_fg_color, 0.025); border-radius: 12px; padding: 12px; }
.gate-output .control-title { font-weight: 700; font-size: 12px; }
.gate-output .setpoint { font-family: monospace; font-size: 23px; min-height: 38px; padding: 3px 8px; }
.gate-output .apply-button { min-height: 28px; }
.gate-output .section-heading { font-size: 12px; font-weight: 700; }
.gate-output .output-button { min-height: 40px; font-weight: 700; font-size: 14px; border-radius: 10px; }
.gate-output .status-line { font-size: 11px; opacity: 0.75; }
.gate-output .protection-content { padding-top: 12px; }
"""


def valid_reading(card, key, unit=None):
    for reading in card.get('readings', []):
        value = reading.get('value')
        if (reading['channel'] == key and reading.get('quality') == 'ok' and
                isinstance(value, (int, float)) and not isinstance(value, bool) and
                math.isfinite(value) and (unit is None or reading.get('unit') == unit)):
            return value
    return None


class OutputWindow(Gtk.ApplicationWindow):
    def __init__(self, application, instrument):
        super().__init__(application=application, title='GATE · Output control', default_width=560, default_height=760)
        self.add_css_class('gate-output')
        self.css = Gtk.CssProvider()
        self.css.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(self.get_display(), self.css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        header = Gtk.HeaderBar()
        header.set_title_widget(Gtk.Label(label='Output control', css_classes=['heading']))
        self.set_titlebar(header)
        self.instrument = instrument
        self.closed = self.polling = self.syncing = False
        self.pending = None
        self.available = False
        self.enabled_key = 'output_enabled'
        self.card = None
        self.signature = None
        self.rows = {}
        self.sources = {}
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, css_classes=['panel-content'])
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_child(self.box)
        layout = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        scroll.set_vexpand(True)
        layout.append(scroll)
        self.set_child(layout)
        heading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        self.box.append(heading)
        self.label('GATE  /  INSTRUMENT OUTPUT', heading, 'eyebrow subtle')
        self.title_label = self.label('Connecting…', heading, 'instrument-name')
        self.identity_label = self.label('Acquisition service', heading, 'subtle')
        self.dashboard = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, css_classes=['readout'])
        self.box.append(self.dashboard)
        topline = Gtk.Box(spacing=12)
        self.dashboard.append(topline)
        caption = self.label('OUTPUT READBACK', topline, 'eyebrow')
        caption.set_hexpand(True)
        self.state_label = self.label('UNKNOWN', topline, 'state-pill')
        metrics = Gtk.Box(spacing=24, homogeneous=True)
        self.dashboard.append(metrics)
        self.metric_values = {}
        for key, title, unit in [('voltage', 'Voltage', 'V'), ('current', 'Current', 'A')]:
            column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            metrics.append(column)
            self.label(title, column, 'metric-title')
            value_line = Gtk.Box(spacing=6)
            column.append(value_line)
            value = self.label('—', value_line, f'metric-value {key}-value')
            unit_label = self.label(unit, value_line, 'metric-unit')
            unit_label.set_valign(Gtk.Align.END)
            self.metric_values[key] = value
        powerline = Gtk.Box(spacing=8)
        self.dashboard.append(powerline)
        self.label('Calculated power', powerline, 'metric-title').set_hexpand(True)
        self.metric_values['power'] = self.label('—', powerline, 'metric-value power-value')
        self.label('W', powerline, 'metric-unit').set_valign(Gtk.Align.END)
        self.readback = self.label('Output is off · readback paused', self.dashboard, 'readout-note')
        self.controls = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.box.append(self.controls)
        output = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        layout.append(Gtk.Separator())
        output.set_margin_start(22)
        output.set_margin_end(22)
        output.set_margin_top(14)
        output.set_margin_bottom(18)
        layout.append(output)
        self.toggle = Gtk.Button(label='Enable output', css_classes=['output-button', 'suggested-action'])
        self.toggle.connect('clicked', lambda *_: self.toggle_output())
        output.append(self.toggle)
        footer = Gtk.Box(spacing=8)
        output.append(footer)
        self.spinner = Gtk.Spinner()
        footer.append(self.spinner)
        self.status = self.label('Waiting for acquisition service', footer, 'status-line')
        self.connect('close-request', self.on_close)
        self.timer = GLib.timeout_add(500, self.refresh)
        self.update_sensitivity()
        self.refresh()

    def label(self, text, parent=None, style=None):
        label = Gtk.Label(label=text, xalign=0, wrap=True, selectable=False)
        if style:
            for name in style.split():
                label.add_css_class(name)
        (parent or self.box).append(label)
        return label

    def bus_call(self, method, payload, callback):
        try:
            connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            params = GLib.Variant('(s)', (json.dumps(payload),)) if payload is not None else None
            def finished(conn, result):
                if self.closed:
                    return
                try:
                    value = json.loads(conn.call_finish(result).unpack()[0])
                except Exception as exc:
                    callback(None, exc)
                    return
                callback(value, None)
            connection.call(BUS, PATH, BUS, method, params, GLib.VariantType.new('(s)'),
                            Gio.DBusCallFlags.NO_AUTO_START, 2000, None, finished)
        except Exception as exc:
            callback(None, exc)

    def send(self, action, **args):
        if self.pending or not self.card or not self.available:
            return
        self.pending = str(uuid.uuid4())
        self.update_sensitivity()
        self.status.set_text('Waiting for instrument confirmation…')
        def queued(_value, error):
            if error:
                self.pending = None
                self.status.set_text(str(error))
                self.update_sensitivity()
            else:
                self.refresh()
        self.bus_call('Command', dict(action=action, instrument=self.instrument, request=self.pending, **args), queued)

    def toggle_output(self):
        enabled = valid_reading(self.card or {}, self.enabled_key)
        if enabled == 1:
            self.send('output', enabled=False)
        elif enabled == 0:
            self.send('output', enabled=True, operator_confirmed=True)

    def build_controls(self, config, sources):
        child = self.controls.get_first_child()
        while child:
            following = child.get_next_sibling()
            self.controls.remove(child)
            child = following
        self.rows, self.sources = {}, {}
        self.label('SETPOINTS', self.controls, 'eyebrow subtle')
        setpoints = Gtk.Box(spacing=12, homogeneous=True)
        self.controls.append(setpoints)
        protections = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, css_classes=['protection-content'])
        other = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for key, spec in config.get('controls', {}).items():
            parent = protections if key in ('ovp', 'ocp') else other if 'choices' in spec else setpoints
            group = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7, hexpand=True, css_classes=['control-card'])
            parent.append(group)
            self.label(spec['label'], group, 'control-title')
            if 'choices' in spec:
                line = Gtk.Box(spacing=12)
                group.append(line)
                self.label('Voltage', line).set_hexpand(True)
                editor = Gtk.Switch(valign=Gtk.Align.CENTER)
                editor.set_tooltip_text('Voltage priority when left; current priority when right')
                line.append(editor)
                self.label('Current', line)
                editor.connect('notify::active', lambda widget, _param, k=key: self.change_priority(k, widget))
                apply = None
                confirmed = self.label('Waiting for confirmation', group, 'subtle')
                confirmed.set_visible(False)
            else:
                line = Gtk.Box(spacing=6)
                group.append(line)
                editor = Gtk.Entry(input_purpose=Gtk.InputPurpose.NUMBER, hexpand=True,
                                   width_chars=6, max_width_chars=9, css_classes=['setpoint'])
                editor.set_tooltip_text(f"{spec['label']}: {spec['min']:g}–{spec['max']:g} {spec['unit']}")
                editor.update_property([Gtk.AccessibleProperty.LABEL], [spec['label']])
                line.append(editor)
                self.label(spec['unit'], line, 'subtle')
                self.label(f"{spec['min']:g}–{spec['max']:g} {spec['unit']}", group, 'subtle')
                confirmed = self.label('Instrument: —', group, 'subtle')
                apply = Gtk.Button(label='Apply', css_classes=['apply-button'])
                group.append(apply)
                apply.connect('clicked', lambda *_args, k=key: self.apply_control(k))
                editor.connect('activate', lambda *_args, k=key: self.apply_control(k))
            self.rows[key] = dict(editor=editor, apply=apply, confirmed=confirmed, spec=spec, initialized=False, dirty=False)
            if apply:
                editor.connect('changed', lambda *_args, k=key: self.edit_control(k))
        setpoints.set_visible(bool(setpoints.get_first_child()))
        self.controls.append(other)
        if protections.get_first_child():
            expander = Gtk.Expander(label='Protection settings · OVP / OCP')
            expander.set_child(protections)
            self.controls.append(expander)
        limitations = []
        if 'priority' not in self.rows:
            limitations.append('Priority selection is unavailable for this instrument driver.')
        if not protections.get_first_child():
            limitations.append('OVP and OCP thresholds are managed on the instrument front panel.')
        if limitations:
            details = Gtk.Expander(label='Instrument capabilities')
            note = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, css_classes=['protection-content'])
            for text in limitations:
                self.label(text, note, 'subtle')
            details.set_child(note)
            self.controls.append(details)
        for quantity, ranges in sources.items():
            group = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            self.controls.append(group)
            group.add_css_class('control-card')
            self.label(f'{quantity.title()} source setpoint', group, 'control-title')
            options = list(ranges)
            selector = Gtk.DropDown.new_from_strings(options)
            group.append(selector)
            entry = Gtk.Entry(input_purpose=Gtk.InputPurpose.NUMBER)
            group.append(entry)
            hint = self.label('', group)
            def changed(widget, _param, values=ranges, ids=options, label=hint):
                low, high, step = values[ids[widget.get_selected()]]
                label.set_text(f'SI units: {low:g}–{high:g}; resolution {step:g}. Requires output standby.')
            selector.connect('notify::selected', changed)
            changed(selector, None)
            apply = Gtk.Button(label='Apply source setpoint')
            group.append(apply)
            apply.connect('clicked', lambda *_args, q=quantity: self.apply_source(q))
            self.sources[quantity] = dict(selector=selector, entry=entry, apply=apply, options=options)

    def change_priority(self, key, widget):
        if self.syncing:
            return
        value = 'current' if widget.get_active() else 'voltage'
        self.send('configure_output', control=key, value=value)
        # Keep the switch at its confirmed choice while the command is pending.
        self.syncing = True
        widget.set_active(self.card.get('output_settings', {}).get(key) == 'current')
        self.syncing = False

    def edit_control(self, key):
        if not self.syncing:
            row = self.rows[key]
            row['dirty'] = True
            row['editor'].remove_css_class('error')
            row['apply'].add_css_class('suggested-action')

    def apply_control(self, key):
        row = self.rows[key]
        try:
            value = float(row['editor'].get_text())
            spec = row['spec']
            if not math.isfinite(value) or not spec['min'] <= value <= spec['max']:
                raise ValueError('Enter a value within the displayed limits')
            row['editor'].remove_css_class('error')
            self.send('configure_output', control=key, value=value)
        except ValueError as exc:
            row['editor'].add_css_class('error')
            self.status.set_text(str(exc))

    def apply_source(self, quantity):
        row = self.sources[quantity]
        try:
            value = float(row['entry'].get_text())
            range_id = row['options'][row['selector'].get_selected()]
            low, high, _step = self.card['source_controls'][quantity][range_id]
            if not math.isfinite(value) or not low <= value <= high:
                raise ValueError('Enter a finite value within the selected range')
            self.send('source', quantity=quantity, value=value, range=range_id)
        except ValueError as exc:
            self.status.set_text(str(exc))

    def render(self, state):
        self.card = next((card for card in state['instruments'] if card['id'] == self.instrument), None)
        stale = GLib.get_real_time() / 1e6 - state['updated_utc_ns'] / 1e9 > max(5, state.get('interval_s', 1) * 2)
        run = state.get('experiment_run') or {}
        sequence = state.get('experiment_sequence') or {}
        running = run.get('state') in ('running', 'preparing') or sequence.get('state') == 'running'
        previously_available = self.available
        self.available = bool(self.card and self.card.get('output_control_supported') and not stale and not running and not self.card.get('error'))
        if self.available and not previously_available and not self.pending:
            self.status.set_text('Ready · changes require instrument confirmation')
        if self.pending:
            result = state.get('commands', {}).get(self.pending, {})
            if result.get('state') in ('succeeded', 'failed'):
                self.pending = None
                self.status.set_text('Instrument confirmed the change' if result['state'] == 'succeeded' else result.get('error', 'Change failed'))
        if not self.card:
            self.state_label.set_text('OFFLINE')
            self.status.set_text('Instrument disconnected · output state unknown')
            self.clear_readback()
            self.update_sensitivity()
            return
        self.title_label.set_text(self.card.get('custom_name') or self.card['name'].replace(',', ' · '))
        self.identity_label.set_text('Simulated instrument' if self.card.get('simulated') else
                                     f"Connected instrument{' · ' + self.card['serial'] if self.card.get('serial') else ''}")
        config = self.card.get('output_configuration', {})
        sources = self.card.get('source_controls', {})
        signature = json.dumps([config, sources], sort_keys=True)
        if signature != self.signature:
            self.build_controls(config, sources)
            self.signature = signature
        self.enabled_key = config.get('enabled_reading', 'output_enabled')
        enabled = valid_reading(self.card, self.enabled_key)
        self.state_label.set_text('STALE' if stale else '● OUTPUT ON' if enabled == 1 else '○ Output OFF' if enabled == 0 else 'UNKNOWN')
        self.state_label.remove_css_class('energized')
        if enabled == 1 and not stale:
            self.state_label.add_css_class('energized')
        voltage = valid_reading(self.card, config.get('voltage_reading'), 'V')
        current = valid_reading(self.card, config.get('current_reading'), 'A')
        power = voltage * current if voltage is not None and current is not None else None
        visible = enabled == 1 and not stale and not self.card.get('error')
        for key, value in [('voltage', voltage), ('current', current), ('power', power)]:
            self.metric_values[key].set_text(f'{value:.5g}' if visible and value is not None and math.isfinite(value) else '—')
        regulation = valid_reading(self.card, 'regulation')
        mode = {0: 'Constant voltage', 1: 'Current limiting'}.get(regulation, 'Live output')
        self.readback.set_text('Readings are stale · controls paused' if stale else
                               f'{mode} · power calculated from V × A' if visible else
                               'Output is off · readback paused' if enabled == 0 else 'Output readback unavailable')
        self.syncing = True
        for key, row in self.rows.items():
            value = valid_reading(self.card, row['spec'].get('reading'), row['spec'].get('unit'))
            if value is None and not row['spec'].get('reading'):
                value = self.card.get('output_settings', {}).get(key)
            if 'choices' in row['spec']:
                # Enum status is published separately from scalar measurements.
                value = self.card.get('output_settings', {}).get('priority')
                row['editor'].set_active(value == 'current')
            elif value is not None and (not row['initialized'] or not row['dirty']):
                row['editor'].set_text(f'{value:g}')
                row['initialized'] = True
            actual = f'{value:g}' if isinstance(value, (int, float)) else value
            if row['apply']:
                row['confirmed'].set_visible(row['dirty'])
            row['confirmed'].set_text(f'Instrument: {actual if actual is not None and not stale else "—"} {row["spec"].get("unit", "")}')
            if row['apply'] and row['dirty'] and value is not None:
                try:
                    matches = math.isclose(float(row['editor'].get_text()), value, abs_tol=row['spec'].get('step', 0) / 2)
                except ValueError:
                    matches = False
                if matches and not self.pending:
                    row['dirty'] = False
                    row['apply'].remove_css_class('suggested-action')
        self.syncing = False
        if stale or running:
            self.status.set_text('Readings are stale' if stale else 'Stop the active experiment to configure outputs')
        self.update_sensitivity()

    def update_sensitivity(self):
        available = getattr(self, 'available', False) and not self.pending
        self.controls.set_sensitive(available)
        self.spinner.set_spinning(bool(self.pending))
        self.spinner.set_visible(bool(self.pending))
        enabled = valid_reading(self.card or {}, getattr(self, 'enabled_key', 'output_enabled'))
        self.toggle.set_label('Disable output' if enabled == 1 else 'Enable output')
        if enabled == 1:
            self.toggle.add_css_class('destructive-action')
            self.toggle.remove_css_class('suggested-action')
        else:
            self.toggle.remove_css_class('destructive-action')
            self.toggle.add_css_class('suggested-action')
        self.toggle.set_sensitive(available and enabled in (0, 1))

    def refresh(self):
        if self.closed or self.polling:
            return not self.closed
        self.polling = True
        def updated(state, error):
            self.polling = False
            if error:
                self.pending = None
                self.available = False
                self.card = None
                self.state_label.set_text('OFFLINE')
                self.clear_readback()
                self.status.set_text(str(error))
                self.update_sensitivity()
            else:
                self.render(state)
        self.bus_call('Snapshot', None, updated)
        return True

    def clear_readback(self):
        for label in self.metric_values.values():
            label.set_text('—')
        self.readback.set_text('Output readback unavailable')
        self.state_label.remove_css_class('energized')

    def on_close(self, *_args):
        self.closed = True
        GLib.source_remove(self.timer)
        Gtk.StyleContext.remove_provider_for_display(self.get_display(), self.css)
        return False


def launch(instrument):
    app = Gtk.Application(application_id=BUS+'.Output', flags=Gio.ApplicationFlags.NON_UNIQUE)
    app.connect('activate', lambda app: OutputWindow(app, instrument).present())
    app.run(None)
