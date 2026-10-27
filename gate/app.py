"""Ubuntu-style device and service settings; monitoring lives in GNOME Shell."""
import json
import os
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gio, GLib, Gtk
from gate.connections import communication_type, validate_resource
from gate.service import BUS, PATH
from gate.resources import documentation

# Keep the desktop's light/dark theme and Ubuntu accent colours.
CSS = b'''
.settings-title { font-size: 26px; font-weight: bold; }
.section-title { font-size: 16px; font-weight: bold; }
.settings-list { border-radius: 12px; border: 1px solid alpha(@theme_fg_color, 0.12); }
.settings-list > row { padding: 16px; }
.settings-list > row:first-child { border-top-left-radius: 12px; border-top-right-radius: 12px; }
.settings-list > row:last-child { border-bottom-left-radius: 12px; border-bottom-right-radius: 12px; }
.device-title { font-weight: bold; }
'''


class Window(Gtk.ApplicationWindow):
    def __init__(self, application):
        super().__init__(application=application, title='GATE · Devices & Settings',
                         default_width=900, default_height=820)
        self.pending = self.closed = self.syncing = self.interval_dirty = False
        self.online = False
        self.ids = None
        self.labels, self.titles, self.device_actions = {}, {}, {}
        self.requests = set()
        self.service_busy = self.service_check_pending = False
        self.service_state = 'Checking…'
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(self.get_display(), provider, 600)
        self.set_titlebar(Gtk.HeaderBar())
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.set_child(scroll)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=28,
                        margin_top=28, margin_bottom=28, margin_start=32, margin_end=32)
        outer.set_halign(Gtk.Align.CENTER)
        outer.set_hexpand(True)
        scroll.set_child(outer)
        outer.append(self.label('Devices & Settings', 'settings-title'))
        intro = self.label('Manage your equipment and background service. Follow measurements in the top bar.', 'dim-label')
        outer.append(intro)
        self.message = self.label('', 'dim-label')
        self.message.set_visible(False)
        outer.append(self.message)
        self.sections = {}
        self.section_controls = []
        for kind, subtitle in (
                ('TMC', 'USB instruments using the Test & Measurement Class'),
                ('Serial', 'Serial instruments and USB serial adapters'),
                ('Network', 'Instruments with a raw SCPI TCP connection')):
            section, rows = self.section(kind, subtitle, outer)
            controls = Gtk.Box(spacing=6)
            if kind == 'TMC':
                scan = self.button('Scan', lambda: self.command('scan'))
                controls.append(scan)
                self.section_controls.append(scan)
            add = self.button('Add…', lambda kind=kind: self.edit_connection(kind))
            controls.append(add)
            self.section_controls.append(add)
            section.append(controls)
            self.sections[kind] = rows
        self.demo_section, self.sections['Demo'] = self.section('Demo instruments', 'Simulated equipment for development', outer)
        self.demo_section.get_parent().set_visible(False)
        _, service = self.section('Background Service', 'Acquisition and agent access continue when this window is closed.', outer)
        self.service_status = self.label('Checking…', 'dim-label')
        actions = Gtk.Box(spacing=6)
        self.service_buttons = {}
        for action in ('start', 'stop', 'restart'):
            button = self.button(action.title(), lambda action=action: self.service_action(action))
            actions.append(button)
            self.service_buttons[action] = button
        service.append(self.setting_row('GATE service', self.service_status, actions))
        self.status = self.label('Connecting…', 'dim-label')
        service.append(self.setting_row('Acquisition', self.status))
        row = Gtk.Box(spacing=8)
        self.interval = Gtk.SpinButton.new_with_range(.1, 60, .1)
        self.interval.set_digits(1)
        self.interval.set_value(1)
        self.interval.connect('value-changed', self.interval_changed)
        row.append(self.interval)
        self.apply_interval = self.button('Apply', lambda: self.command('settings', interval=self.interval.get_value()))
        row.append(self.apply_interval)
        service.append(self.setting_row('Acquisition interval', self.label('Seconds between host polling cycles', 'dim-label'), row))
        _, agent = self.section('Agent Access', 'Agents control instruments and coordinate logging through the local API.', outer)
        self.api = self.label('Waiting for service…', 'dim-label')
        agent.append(self.setting_row('Control API', self.api))
        guide = documentation('AGENT_INSTRUMENT_GUIDE.md')
        agent.append(self.setting_row('Instrument guide', self.label('Connection, measurement and logging commands', 'dim-label'),
                     self.button('Open', lambda: Gio.AppInfo.launch_default_for_uri(Path(guide).as_uri(), None))))
        self.demo_button = self.button('Add demo instruments', lambda: self.command('demo'))
        self.demo_button.set_halign(Gtk.Align.START)
        outer.append(self.demo_button)
        self.connect('close-request', self.on_close)
        # Show remembered devices even if the service was stopped before opening settings.
        profile = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home()/'.config'))) / 'gate/devices.json'
        try:
            saved = json.loads(profile.read_text())
            self.render_devices(dict(instruments=[], configured_resources=saved.get('resources', []),
                                     saved_devices=saved.get('devices', {})))
        except (OSError, ValueError, TypeError):
            pass
        self.timer = GLib.timeout_add(1000, self.refresh)
        self.refresh()

    def label(self, text, css=None):
        label = Gtk.Label(label=text, xalign=0, wrap=True)
        label.set_max_width_chars(66)
        if css:
            label.add_css_class(css)
        return label

    def button(self, text, callback):
        button = Gtk.Button(label=text, valign=Gtk.Align.CENTER)
        button.connect('clicked', lambda *_: callback())
        return button

    def section(self, title, subtitle, outer):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        heading = Gtk.Box(spacing=12)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        text.append(self.label(title, 'section-title'))
        text.append(self.label(subtitle, 'dim-label'))
        heading.append(text)
        box.append(heading)
        rows = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, show_separators=True)
        rows.add_css_class('settings-list')
        box.append(rows)
        outer.append(box)
        return heading, rows

    def setting_row(self, title, subtitle, actions=None):
        row = Gtk.Box(spacing=16)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5, hexpand=True)
        title_label = self.label(title, 'device-title')
        text.append(title_label)
        text.append(subtitle)
        row.append(text)
        if actions:
            row.append(actions)
        return row

    def notify(self, text):
        self.message.set_text(text)
        self.message.set_visible(bool(text))

    def interval_changed(self, *args):
        if not self.syncing:
            self.interval_dirty = True

    def run_systemctl(self, arguments, callback):
        try:
            process = Gio.Subprocess.new(['systemctl', '--user', *arguments],
                         Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE)
            def finished(process, result):
                try:
                    _, stdout, stderr = process.communicate_utf8_finish(result)
                    callback(process.get_successful(), stdout.strip(), stderr.strip())
                except Exception as exc:
                    callback(False, '', str(exc))
            process.communicate_utf8_async(None, None, finished)
        except Exception as exc:
            callback(False, '', str(exc))

    def service_action(self, action):
        self.service_busy = True
        self.update_sensitivity()
        self.notify(dict(start='Starting', stop='Stopping', restart='Restarting')[action] + ' background service…')
        def finished(ok, output, error):
            self.service_busy = False
            if self.closed:
                return
            self.notify(f'Background service {dict(start="started", stop="stopped", restart="restarted")[action]}.' if ok
                        else f'Unable to {action} service: {error or output}')
            self.refresh()
        self.run_systemctl([action, 'gate.service'], finished)

    def update_sensitivity(self):
        for action, button in self.service_buttons.items():
            button.set_sensitive(not self.service_busy and (action == 'restart' or
                                 (not self.online if action == 'start' else self.online or self.service_state in ('active', 'activating'))))
        for button in self.section_controls + [self.apply_interval, self.interval, self.demo_button]:
            button.set_sensitive(self.online and not self.service_busy)
        for actions in self.device_actions.values():
            actions.set_sensitive(self.online and not self.service_busy)

    def on_close(self, *args):
        self.closed = True
        GLib.source_remove(self.timer)
        return False

    def bus_call(self, method, payload, callback):
        try:
            connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            params = GLib.Variant('(s)', (json.dumps(payload),)) if payload is not None else None
            def finished(conn, result):
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

    def command(self, action, **args):
        request = str(uuid.uuid4())
        self.requests.add(request)
        def queued(value, error):
            if self.closed:
                return
            if error:
                self.requests.discard(request)
                self.notify(str(error))
            else:
                if action == 'settings':
                    self.interval_dirty = False
                self.notify('Change queued…')
        self.bus_call('Command', dict(action=action, request=request, **args), queued)

    def edit_connection(self, kind, identity=None):
        dialog = Gtk.Dialog(title='Connection Settings' if identity else f'Add {kind} Instrument',
                            transient_for=self, modal=True, default_width=520)
        dialog.add_button('Cancel', Gtk.ResponseType.CANCEL)
        save = dialog.add_button('Save & Reconnect' if identity else 'Add Instrument', Gtk.ResponseType.ACCEPT)
        save.add_css_class('suggested-action')
        content = dialog.get_content_area()
        content.set_spacing(12)
        for setter in (content.set_margin_top, content.set_margin_bottom, content.set_margin_start, content.set_margin_end):
            setter(24)
        fields = {}
        url = urlsplit(identity or '')
        def entry(title, value, hint):
            content.append(self.label(title, 'device-title'))
            widget = Gtk.Entry(text=value, placeholder_text=hint, activates_default=True)
            content.append(widget)
            return widget
        if kind == 'Serial':
            fields['path'] = entry('Device path', url.path if identity else '', '/dev/serial/by-id/… or /dev/ttyUSB0')
            content.append(self.label('Baud rate', 'device-title'))
            rates = ['300', '600', '1200', '2400', '4800', '9600']
            baud = Gtk.DropDown.new_from_strings(rates)
            current = parse_qs(url.query).get('baud', ['9600'])[0]
            baud.set_selected(rates.index(current) if current in rates else 5)
            content.append(baud)
            content.append(self.label('8 data bits · No parity · 1 stop bit. Prefer a stable /dev/serial/by-id/ path.', 'dim-label'))
        elif kind == 'Network':
            fields['host'] = entry('Host or IP address', url.hostname or '', '192.168.1.100')
            fields['port'] = entry('TCP port', str(url.port or 5025), '5025')
            content.append(self.label('Raw SCPI TCP connections. Automatic LAN discovery is not available.', 'dim-label'))
        else:
            fields['path'] = entry('TMC device path', identity or '', '/dev/usbtmc0')
            content.append(self.label('Use Scan in the TMC section to find accessible USB instruments.', 'dim-label'))
        error_label = self.label('')
        error_label.add_css_class('error')
        content.append(error_label)
        dialog.set_default_response(Gtk.ResponseType.ACCEPT)
        def response(dialog, response):
            if response != Gtk.ResponseType.ACCEPT:
                dialog.destroy()
                return
            try:
                if kind == 'Serial':
                    resource = f"serial://{fields['path'].get_text().strip()}?baud={rates[baud.get_selected()]}"
                elif kind == 'Network':
                    host = fields['host'].get_text().strip()
                    if ':' in host and not host.startswith('['):
                        host = f'[{host}]'
                    port = int(fields['port'].get_text())
                    if not 1 <= port <= 65535:
                        raise ValueError('TCP port must be 1–65535')
                    resource = f'tcp://{host}:{port}'
                else:
                    resource = fields['path'].get_text().strip()
                resource = validate_resource(resource)
                if communication_type(resource) != kind:
                    raise ValueError(f'Enter a {kind} connection address')
            except ValueError as exc:
                error_label.set_text(str(exc))
                return
            if identity:
                self.command('edit_device', instrument=identity, resource=resource)
            else:
                self.command('connect', resource=resource)
            dialog.destroy()
        dialog.connect('response', response)
        dialog.present()
        return dialog

    def render_devices(self, state):
        live = {device['id']: device for device in state['instruments']}
        saved = state.get('saved_devices', {})
        ids = sorted(set(live) | set(state.get('configured_resources', [])),
                     key=lambda key: (communication_type(key), saved.get(key, {}).get('name', key), key))
        if ids != self.ids:
            for rows in self.sections.values():
                while rows.get_first_child():
                    rows.remove(rows.get_first_child())
            self.labels, self.titles, self.device_actions = {}, {}, {}
            for identity in ids:
                kind = communication_type(identity)
                details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
                details.append(self.label(identity, 'dim-label'))
                self.labels[identity] = self.label('', 'dim-label')
                details.append(self.labels[identity])
                actions = Gtk.Box(spacing=6)
                if kind != 'Demo':
                    actions.append(self.button('Reconnect', lambda resource=identity: self.command('connect', resource=resource, reconnect=True)))
                    edit = Gtk.Button.new_from_icon_name('emblem-system-symbolic')
                    edit.set_valign(Gtk.Align.CENTER)
                    edit.set_tooltip_text('Edit connection settings')
                    edit.update_property([Gtk.AccessibleProperty.LABEL], ['Edit connection settings'])
                    edit.connect('clicked', lambda _, kind=kind, resource=identity: self.edit_connection(kind, resource))
                    actions.append(edit)
                forget = self.button('Forget', lambda resource=identity: self.command('disconnect', instrument=resource))
                actions.append(forget)
                self.device_actions[identity] = actions
                row = self.setting_row('', details, actions)
                self.titles[identity] = row.get_first_child().get_first_child()
                self.sections[kind].append(row)
            for kind, rows in self.sections.items():
                if rows.get_first_child() is None:
                    rows.append(self.label(f'No {kind.lower()} instruments configured', 'dim-label'))
            self.demo_section.get_parent().set_visible(any(key.startswith('demo:') for key in ids))
            self.ids = ids
        for identity in ids:
            device = live.get(identity)
            self.titles[identity].set_text((device or saved.get(identity, {})).get('name', 'Saved instrument'))
            if not self.online:
                text = 'Service offline'
            elif not device:
                text = 'Disconnected · Not currently available'
            elif device['error']:
                text = 'Disconnected · ' + device['error']
            else:
                text = f"{'Demo' if device['simulated'] else 'Connected'} · {device['category'].replace('_', ' ')}"
            self.labels[identity].set_text(text)

    def refresh(self):
        if self.closed:
            return False
        if not self.service_check_pending:
            self.service_check_pending = True
            def checked(ok, output, error):
                self.service_check_pending = False
                if self.closed:
                    return
                self.service_state = output if ok else 'unavailable'
                self.service_status.set_text(f'{self.service_state.title()} · ' + ('Agent service available' if self.online else 'Agent service offline'))
                self.update_sensitivity()
            self.run_systemctl(['show', 'gate.service', '--property=ActiveState', '--value'], checked)
        if self.pending:
            return True
        self.pending = True
        def updated(state, error):
            self.pending = False
            if self.closed:
                return
            self.online = error is None
            if error:
                self.status.set_text('Offline · Start the background service to manage devices')
                self.api.set_text('Offline')
                for label in self.labels.values():
                    label.set_text('Service offline')
            else:
                self.status.set_text(state['status'] + (' · Recording active' if state['recording'] else ''))
                if not self.interval_dirty:
                    self.syncing = True
                    self.interval.set_value(state.get('interval_s', 1))
                    self.syncing = False
                control = state.get('control_api')
                self.api.set_text(f"{control['url']}\nPrivate local token · Advanced commands {'enabled' if control['allow_raw'] else 'disabled'}" if control else 'Disabled')
                for request in list(self.requests):
                    result = state.get('commands', {}).get(request, {})
                    if result.get('state') in ('succeeded', 'failed'):
                        self.requests.discard(request)
                        self.notify(result.get('error', 'Changes applied.'))
                self.render_devices(state)
            self.update_sensitivity()
        self.bus_call('Snapshot', None, updated)
        return True


def launch():
    app = Gtk.Application(application_id=BUS+'.App')
    app.connect('activate', lambda app: Window(app).present())
    app.run(None)
