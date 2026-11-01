"""Independent output panel. All instrument operations go through the owner."""
import json
import math
import uuid
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gio, GLib, Gtk
from gate.service import BUS, PATH


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
        super().__init__(application=application, title='GATE · Output', default_width=470, default_height=580)
        self.instrument = instrument
        self.closed = self.polling = self.syncing = False
        self.pending = None
        self.available = False
        self.enabled_key = 'output_enabled'
        self.card = None
        self.signature = None
        self.rows = {}
        self.sources = {}
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                           margin_top=18, margin_bottom=18, margin_start=18, margin_end=18)
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_child(self.box)
        self.set_child(scroll)
        self.title_label = self.label('Connecting…')
        self.title_label.add_css_class('title-2')
        self.state_label = self.label('Output state unknown')
        self.state_label.add_css_class('title-1')
        self.readback = self.label('Readback unavailable')
        self.controls = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.box.append(self.controls)
        self.wiring = Gtk.CheckButton(label='I have checked wiring and DUT operating limits')
        self.box.append(self.wiring)
        self.wiring.connect('toggled', lambda *_: self.update_sensitivity())
        self.toggle = Gtk.Button(label='Enable output')
        self.toggle.connect('clicked', lambda *_: self.toggle_output())
        self.box.append(self.toggle)
        self.status = self.label('Waiting for acquisition service')
        self.connect('close-request', self.on_close)
        self.timer = GLib.timeout_add(500, self.refresh)
        self.update_sensitivity()
        self.refresh()

    def label(self, text, parent=None):
        label = Gtk.Label(label=text, xalign=0, wrap=True, selectable=True)
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
        elif enabled == 0 and (self.card.get('simulated') or self.wiring.get_active()):
            self.send('output', enabled=True, wiring_confirmed=self.wiring.get_active())
            self.wiring.set_active(False)

    def build_controls(self, config, sources):
        child = self.controls.get_first_child()
        while child:
            following = child.get_next_sibling()
            self.controls.remove(child)
            child = following
        self.rows, self.sources = {}, {}
        protections = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for key, spec in config.get('controls', {}).items():
            parent = protections if key in ('ovp', 'ocp') else self.controls
            group = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            parent.append(group)
            self.label(spec['label'], group)
            line = Gtk.Box(spacing=8)
            group.append(line)
            confirmed = self.label('Confirmed: unavailable', group)
            if 'choices' in spec:
                # Priority is an explicit voltage/current toggle when advertised.
                editor = Gtk.Switch(valign=Gtk.Align.CENTER)
                line.append(editor)
                self.label('Off: voltage priority · On: current priority', group)
                editor.connect('notify::active', lambda widget, _param, k=key: self.change_priority(k, widget))
                apply = None
            else:
                editor = Gtk.Entry(input_purpose=Gtk.InputPurpose.NUMBER, hexpand=True)
                line.append(editor)
                self.label(f"{spec['min']:g}–{spec['max']:g} {spec['unit']}", group)
                apply = Gtk.Button(label='Apply')
                line.append(apply)
                apply.connect('clicked', lambda *_args, k=key: self.apply_control(k))
            self.rows[key] = dict(editor=editor, apply=apply, confirmed=confirmed, spec=spec, initialized=False)
        if protections.get_first_child():
            expander = Gtk.Expander(label='Protection · OCP / OVP')
            expander.set_child(protections)
            self.controls.append(expander)
        else:
            self.label('OCP / OVP configuration is not supported by this driver.', self.controls)
        if 'priority' not in self.rows:
            self.label('Voltage/current priority selection is not supported by this driver.', self.controls)
        for quantity, ranges in sources.items():
            group = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            self.controls.append(group)
            self.label(f'{quantity.title()} source setpoint (not measured readback)', group)
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

    def apply_control(self, key):
        row = self.rows[key]
        try:
            value = float(row['editor'].get_text())
            spec = row['spec']
            if not math.isfinite(value) or not spec['min'] <= value <= spec['max']:
                raise ValueError('Enter a value within the displayed limits')
            self.send('configure_output', control=key, value=value)
        except ValueError as exc:
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
        self.available = bool(self.card and self.card.get('output_control_supported') and not stale and not running and not self.card.get('error'))
        if self.pending:
            result = state.get('commands', {}).get(self.pending, {})
            if result.get('state') in ('succeeded', 'failed'):
                self.pending = None
                self.status.set_text('Instrument confirmed the change' if result['state'] == 'succeeded' else result.get('error', 'Change failed'))
        if not self.card:
            self.wiring.set_active(False)
            self.state_label.set_text('Output state unknown · instrument offline')
            self.readback.set_text('Readback unavailable')
            self.update_sensitivity()
            return
        self.title_label.set_text(self.card.get('custom_name') or self.card['name'])
        config = self.card.get('output_configuration', {})
        sources = self.card.get('source_controls', {})
        signature = json.dumps([config, sources], sort_keys=True)
        if signature != self.signature:
            self.build_controls(config, sources)
            self.signature = signature
        self.enabled_key = config.get('enabled_reading', 'output_enabled')
        enabled = valid_reading(self.card, self.enabled_key)
        self.state_label.set_text('STALE · output state unknown' if stale else
                                  '● OUTPUT ON' if enabled == 1 else '○ Output OFF' if enabled == 0 else 'Output state unknown')
        self.state_label.remove_css_class('error')
        if enabled == 1 and not stale:
            self.state_label.add_css_class('error')
        voltage = valid_reading(self.card, config.get('voltage_reading'), 'V')
        current = valid_reading(self.card, config.get('current_reading'), 'A')
        def display(value, unit):
            return f'{value:.5g} {unit}' if value is not None else 'unavailable'
        self.readback.set_text(
            f"Voltage: {display(voltage, 'V')}\nCurrent: {display(current, 'A')}\nCalculated power: {display(voltage * current if voltage is not None and current is not None else None, 'W')}"
            if enabled == 1 and not stale else 'Readback shown when output is enabled and readings are fresh')
        self.syncing = True
        for key, row in self.rows.items():
            value = valid_reading(self.card, row['spec'].get('reading'), row['spec'].get('unit'))
            if value is None and not row['spec'].get('reading'):
                value = self.card.get('output_settings', {}).get(key)
            if 'choices' in row['spec']:
                # Enum status is published separately from scalar measurements.
                value = self.card.get('output_settings', {}).get('priority')
                row['editor'].set_active(value == 'current')
            elif value is not None and not row['initialized']:
                row['editor'].set_text(f'{value:g}')
                row['initialized'] = True
            row['confirmed'].set_text(f'Confirmed: {value if value is not None and not stale else "unavailable"} {row["spec"].get("unit", "")}')
        self.syncing = False
        if stale or running:
            self.status.set_text('Readings are stale' if stale else 'Stop the active experiment to configure outputs')
        self.update_sensitivity()

    def update_sensitivity(self):
        available = getattr(self, 'available', False) and not self.pending
        self.controls.set_sensitive(available)
        enabled = valid_reading(self.card or {}, getattr(self, 'enabled_key', 'output_enabled'))
        self.toggle.set_label('Disable output' if enabled == 1 else 'Enable output')
        if enabled == 1:
            self.toggle.add_css_class('destructive-action')
        else:
            self.toggle.remove_css_class('destructive-action')
        self.toggle.set_sensitive(available and enabled in (0, 1) and
                                  (enabled == 1 or self.card.get('simulated') or self.wiring.get_active()))
        self.wiring.set_sensitive(available and enabled == 0)

    def refresh(self):
        if self.closed or self.polling:
            return not self.closed
        self.polling = True
        def updated(state, error):
            self.polling = False
            if error:
                self.pending = None
                self.wiring.set_active(False)
                self.available = False
                self.card = None
                self.state_label.set_text('Output state unknown · service offline')
                self.readback.set_text('Readback unavailable')
                self.status.set_text(str(error))
                self.update_sensitivity()
            else:
                self.render(state)
        self.bus_call('Snapshot', None, updated)
        return True

    def on_close(self, *_args):
        self.closed = True
        GLib.source_remove(self.timer)
        return False


def launch(instrument):
    app = Gtk.Application(application_id=BUS+'.Output', flags=Gio.ApplicationFlags.NON_UNIQUE)
    app.connect('activate', lambda app: OutputWindow(app, instrument).present())
    app.run(None)
