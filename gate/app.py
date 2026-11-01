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
from gate.experiments import BUCK_CONVERTER

# Keep the desktop's light/dark theme and Ubuntu accent colours.
CSS = b'''
.settings-title { font-size: 26px; font-weight: bold; }
.section-title { font-size: 16px; font-weight: bold; }
.settings-list { border-radius: 12px; border: 1px solid alpha(@theme_fg_color, 0.12); }
.settings-list > row { padding: 16px; }
.settings-list > row:first-child { border-top-left-radius: 12px; border-top-right-radius: 12px; }
.settings-list > row:last-child { border-bottom-left-radius: 12px; border-bottom-right-radius: 12px; }
.device-title { font-weight: bold; }
.navigation-sidebar { padding: 8px; background: transparent; }
.navigation-sidebar > row { border-radius: 8px; padding: 12px; }
.sidebar-heading { padding: 18px 20px 6px; font-weight: bold; }
'''


class Window(Gtk.ApplicationWindow):
    def __init__(self, application):
        super().__init__(application=application, title='GATE · Devices & Settings',
                         default_width=1150, default_height=820)
        self.pending = self.closed = self.syncing = self.interval_dirty = False
        self.online = False
        self.experiments_supported = False
        self.ids = None
        self.row_kinds = {}
        self.labels, self.titles, self.device_actions = {}, {}, {}
        self.requests = set()
        self.request_callbacks = {}
        self.view_state = {}
        self.connect_buttons = {}
        self.connected = set()
        self.service_busy = self.service_check_pending = False
        self.service_state = 'Checking…'
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(self.get_display(), provider, 600)
        self.page_title = Gtk.Label(label='Devices & Settings')
        self.page_title.add_css_class('device-title')
        header = Gtk.HeaderBar()
        header.set_title_widget(self.page_title)
        self.set_titlebar(header)
        layout = Gtk.Box()
        self.set_child(layout)
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, width_request=250)
        sidebar.add_css_class('sidebar')
        sidebar.append(self.label('GATE', 'sidebar-heading'))
        self.new_experiment_button = self.button('New Experiment…', self.new_experiment)
        self.new_experiment_button.set_margin_start(12)
        self.new_experiment_button.set_margin_end(12)
        sidebar.append(self.new_experiment_button)
        navigation_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        sidebar.append(navigation_scroll)
        self.navigation = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.navigation.add_css_class('navigation-sidebar')
        self.navigation.connect('row-selected', self.select_page)
        self.navigation.set_header_func(self.navigation_header)
        navigation_scroll.set_child(self.navigation)
        layout.append(sidebar)
        layout.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        self.pages = Gtk.Stack(hexpand=True, vexpand=True)
        layout.append(self.pages)
        self.experiment_definitions = {}
        self.experiment_pages = {}
        self.experiment_rows = {}
        self.experiment_views = {}
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.pages.add_named(scroll, 'devices')
        self.devices_navigation = self.navigation_row('devices', 'Devices & Settings', 'preferences-system-symbolic')
        self.navigation.select_row(self.devices_navigation)
        self.render_experiments({'buck_converter': BUCK_CONVERTER})
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
                ('USB', 'Hantek 365 USB meters'),
                ('Serial', 'Serial instruments and USB serial adapters'),
                ('Network', 'Instruments with a raw SCPI TCP connection')):
            section, rows = self.section(kind, subtitle, outer)
            controls = Gtk.Box(spacing=6)
            if kind in ('TMC', 'USB'):
                scan = self.button('Scan', lambda: self.command('scan'))
                controls.append(scan)
                self.section_controls.append(scan)
            add = self.button('Add…', lambda kind=kind: self.edit_connection(kind))
            controls.append(add)
            self.section_controls.append(add)
            section.append(controls)
            self.sections[kind] = rows
        self.saved_section, self.sections['Saved'] = self.section('Saved Instruments', 'Instrument profiles without a saved connection', outer)
        self.saved_section.get_parent().set_visible(False)
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
            self.render_experiments(saved.get('experiments', {'buck_converter': BUCK_CONVERTER}))
            self.render_devices(dict(instruments=[], configured_resources=saved.get('resources', []),
                                     saved_devices=saved.get('devices', {}), connections=saved.get('connections', {}),
                                     instrument_profiles=saved.get('instrument_profiles', {})))
        except (OSError, ValueError, TypeError):
            pass
        self.timer = GLib.timeout_add(1000, self.refresh)
        self.refresh()

    def new_experiment(self):
        from gate.experiment_editor import ExperimentEditor
        dialog = ExperimentEditor(self)
        dialog.present()
        return dialog

    def navigation_header(self, row, previous):
        row.set_header(self.label('Experiments', 'sidebar-heading')
                       if row.page != 'devices' and previous and previous.page == 'devices' else None)

    def navigation_row(self, page, title, icon):
        row = Gtk.ListBoxRow()
        row.page = page
        row.page_title = title
        content = Gtk.Box(spacing=12)
        content.append(Gtk.Image.new_from_icon_name(icon))
        label = self.label(title)
        label.set_hexpand(True)
        content.append(label)
        row.set_child(content)
        self.navigation.append(row)
        return row

    def select_page(self, navigation, row):
        if row is None:
            return
        self.pages.set_visible_child_name(row.page)
        self.page_title.set_text(row.page_title)
        self.set_title('GATE · ' + row.page_title)

    def experiment_page(self, definition):
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=28,
                          margin_top=28, margin_bottom=28, margin_start=32, margin_end=32,
                          halign=Gtk.Align.CENTER, hexpand=True)
        scroll.set_child(content)
        content.append(self.label(definition['name'], 'settings-title'))
        content.append(self.label(definition['description'], 'dim-label'))
        from gate.experiment_view import ExperimentView
        view = ExperimentView(self, definition)
        content.append(view)
        self.experiment_views[definition['id']] = view
        return scroll

    def render_experiments(self, definitions):
        # Preserve navigation selection and scroll positions on routine polling.
        selected_row = self.navigation.get_selected_row()
        selected_page = selected_row.page if selected_row else 'devices'
        for identity in list(self.experiment_pages):
            if identity not in definitions or definitions[identity] != self.experiment_definitions[identity]:
                row = self.experiment_rows.pop(identity)
                selected = self.navigation.get_selected_row() == row
                self.navigation.remove(row)
                self.pages.remove(self.experiment_pages.pop(identity))
                self.experiment_views.pop(identity, None)
                if selected:
                    self.navigation.select_row(self.devices_navigation)
        for identity, definition in definitions.items():
            if identity in self.experiment_pages:
                continue
            page_name = 'experiment:' + identity
            page = self.experiment_page(definition)
            self.pages.add_named(page, page_name)
            row = self.navigation_row(page_name, definition['name'], 'applications-science-symbolic')
            self.experiment_pages[identity] = page
            self.experiment_rows[identity] = row
        for row in self.experiment_rows.values():
            if row.page == selected_page:
                self.navigation.select_row(row)
        self.experiment_definitions = json.loads(json.dumps(definitions))

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

    def command(self, action, on_complete=None, **args):
        request = str(uuid.uuid4())
        self.requests.add(request)
        if on_complete:
            self.request_callbacks[request] = on_complete
        def queued(value, error):
            if self.closed:
                return
            if error:
                self.requests.discard(request)
                self.notify(str(error))
                callback = self.request_callbacks.pop(request, None)
                if callback:
                    callback(dict(state='failed', error=str(error)))
            else:
                if action == 'settings':
                    self.interval_dirty = False
                self.notify('Change queued…')
        self.bus_call('Command', dict(action=action, request=request, **args), queued)

    def icon_button(self, icon, tooltip, callback):
        button = Gtk.Button.new_from_icon_name(icon)
        button.set_valign(Gtk.Align.CENTER)
        button.set_tooltip_text(tooltip)
        button.update_property([Gtk.AccessibleProperty.LABEL], [tooltip])
        button.connect('clicked', lambda *_: callback())
        return button

    def make_dialog(self, title):
        dialog = Gtk.Dialog(title=title, transient_for=self, modal=True, default_width=560)
        content = dialog.get_content_area()
        content.set_spacing(12)
        for setter in (content.set_margin_top, content.set_margin_bottom, content.set_margin_start, content.set_margin_end):
            setter(24)
        return dialog, content

    def connection_fields(self, kind, identity, content):
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
            rates = ['300', '600', '1200', '2400', '4800', '9600', '19200']
            baud = Gtk.DropDown.new_from_strings(rates)
            current = parse_qs(url.query).get('baud', ['9600'])[0]
            baud.set_selected(rates.index(current) if current in rates else 5)
            content.append(baud)
            content.append(self.label('Protocol', 'device-title'))
            protocol = Gtk.DropDown.new_from_strings(['Fluke 45', 'AOIP SN-8310 / PJ-6301'])
            protocol.set_selected(1 if url.scheme == 'aoip' else 0)
            content.append(protocol)
            aoip_options = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            content.append(aoip_options)
            serial_options = {}
            options = parse_qs(url.query)
            for key, title, choices, default in (
                    ('bits', 'Data bits', ['8', '7'], '8'),
                    ('parity', 'Parity', ['none', 'even', 'odd'], 'none'),
                    ('stop', 'Stop bits', ['1', '2'], '1'),
                    ('flow', 'Flow control', ['none', 'xonxoff', 'rtscts'], 'none')):
                aoip_options.append(self.label(title, 'device-title'))
                widget = Gtk.DropDown.new_from_strings(choices)
                value = options.get(key, [default])[0]
                widget.set_selected(choices.index(value) if value in choices else 0)
                aoip_options.append(widget)
                serial_options[key] = (widget, choices)
            aoip_options.append(self.label('Reply quiet interval (seconds)', 'device-title'))
            idle = Gtk.SpinButton.new_with_range(.05, 2., .05)
            idle.set_digits(2)
            idle.set_value(float(options.get('idle', ['0.2'])[0]))
            aoip_options.append(idle)
            aoip_options.set_visible(protocol.get_selected() == 1)
            protocol.connect('notify::selected', lambda *_: aoip_options.set_visible(protocol.get_selected() == 1))
            content.append(self.label('Match the instrument settings. Fluke uses 8N1. Prefer a stable /dev/serial/by-id/ path.', 'dim-label'))
        elif kind == 'Network':
            fields['host'] = entry('Host or IP address', url.hostname or '', '192.168.1.100')
            fields['port'] = entry('TCP port', str(url.port or 5025), '5025')
            content.append(self.label('Raw SCPI TCP connections. Automatic LAN discovery is not available.', 'dim-label'))
        else:
            fields['path'] = entry('USB address' if kind == 'USB' else 'TMC device path', identity or '', 'riden:///dev/ttyUSB1 or hantek://usb/3-1' if kind == 'USB' else '/dev/usbtmc0')
            content.append(self.label('Scan finds USBTMC/Hantek devices. For RD6006 enter riden:///dev/serial/by-id/…?baud=115200&address=1. Serial adapters are not automatically probed.', 'dim-label'))
        def read():
            if kind == 'Serial':
                scheme = 'aoip' if protocol.get_selected() == 1 else 'serial'
                resource = f"{scheme}://{fields['path'].get_text().strip()}?baud={rates[baud.get_selected()]}"
                if scheme == 'aoip':
                    resource += ''.join(f'&{key}={choices[widget.get_selected()]}' for key, (widget, choices) in serial_options.items())
                    resource += f'&idle={idle.get_value():g}'
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
            return resource
        return read

    def edit_connection(self, kind, identity=None):
        dialog, content = self.make_dialog('Connection Settings' if identity else f'Add {kind} Instrument')
        dialog.add_button('Cancel', Gtk.ResponseType.CANCEL)
        dialog.add_button('Save & Connect' if identity else 'Add Instrument', Gtk.ResponseType.ACCEPT).add_css_class('suggested-action')
        read = self.connection_fields(kind, identity, content)
        error = self.label('', 'error')
        content.append(error)
        def response(dialog, response):
            if response == Gtk.ResponseType.ACCEPT:
                try:
                    resource = read()
                except ValueError as exc:
                    error.set_text(str(exc))
                    return
                self.command('edit_device', instrument=identity, resource=resource) if identity else self.command('connect', resource=resource)
            dialog.destroy()
        dialog.connect('response', response)
        dialog.set_default_response(Gtk.ResponseType.ACCEPT)
        dialog.present()
        return dialog

    def connection_toggle(self, resource):
        if resource in self.connected:
            self.command('disconnect', instrument=resource, forget=False)
        elif resource.startswith('demo:'):
            self.command('demo')
        else:
            self.command('connect', resource=resource)

    def profile_for(self, resource):
        connection = self.view_state.get('connections', {}).get(resource, {})
        key = connection.get('instrument') or resource
        profile = self.view_state.get('instrument_profiles', {}).get(key)
        return key, profile

    def device_settings(self, resource):
        key, profile = self.profile_for(resource)
        dialog, content = self.make_dialog('Device Settings')
        dialog.add_button('Close', Gtk.ResponseType.CLOSE)
        dialog.connect('response', lambda dialog, _: dialog.destroy())
        tabs = Gtk.Notebook()
        content.append(tabs)
        connection_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=16)
        tabs.append_page(connection_page, Gtk.Label(label='Connection'))
        if resource in self.view_state.get('configured_resources', []) and not resource.startswith('demo:'):
            read = self.connection_fields(communication_type(resource), resource, connection_page)
            error = self.label('', 'error')
            connection_page.append(error)
            def save_connection():
                try:
                    replacement = read()
                except ValueError as exc:
                    error.set_text(str(exc))
                    return
                self.command('edit_device', instrument=resource, resource=replacement)
                dialog.destroy()
            connection_page.append(self.button('Save Connection & Connect', save_connection))
            connection_page.append(self.label('Address and communication options apply only to this connection. Instrument settings are matched after identification.', 'dim-label'))
        else:
            connection_page.append(self.label('No physical connection settings. Add a connection in TMC, Serial or Network to identify an instrument.', 'dim-label'))
        instrument_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=16)
        tabs.append_page(instrument_page, Gtk.Label(label='Instrument'))
        if profile:
            instrument_page.append(self.label(f"{profile['type']} · Serial: {profile.get('serial') or 'Unavailable (connection-local identity)'}", 'dim-label'))
            instrument_page.append(self.label('Instrument name', 'device-title'))
            name = Gtk.Entry(text=profile.get('name', ''), placeholder_text='Use model name', max_length=80)
            instrument_page.append(name)
            functions = profile.get('functions', [])
            channel_labels = {item['id']: item['label'] for item in profile.get('channels', [])}
            function_selectors = {}
            saved = profile.get('settings', {})
            for channel in dict.fromkeys(item.get('instrument_channel', 'primary') for item in functions):
                channel_functions = [item for item in functions if item.get('instrument_channel', 'primary') == channel]
                instrument_page.append(self.label(channel_labels.get(channel, channel.title())+' function', 'device-title'))
                selected = saved.get('channel_functions', {}).get(channel, saved.get('function'))
                selector = Gtk.DropDown.new_from_strings(['Keep current channel function'] + [item['label'] for item in channel_functions])
                selector.set_selected(next((index+1 for index, item in enumerate(channel_functions) if item['id'] == selected), 0))
                instrument_page.append(selector)
                function_selectors[channel] = (channel_functions, selector)
            range_selectors = {}
            range_rows = []
            controls = profile.get('range_controls', [])
            saved_ranges = profile.get('settings', {}).get('ranges', {})
            for control in controls:
                row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
                row.append(self.label(control['label']+' range', 'device-title'))
                selector = Gtk.DropDown.new_from_strings(['Keep instrument range'] + [option['label'] for option in control['options']])
                selector.set_selected(next((index+1 for index, option in enumerate(control['options'])
                                            if option['id'] == saved_ranges.get(control['id'])), 0))
                row.append(selector)
                instrument_page.append(row)
                range_selectors[control['id']] = (control, selector)
                range_rows.append((control, row))
            if controls:
                hint = self.label('Saved ranges apply when their measurement function is selected. Keep instrument range removes the saved override.', 'dim-label')
                instrument_page.append(hint)
                def show_ranges(*_):
                    card = next((item for item in self.view_state.get('instruments', []) if item['id'] == key), {})
                    active = dict(card.get('current_functions', {'primary': card.get('current_function')}))
                    for channel, (channel_functions, selector) in function_selectors.items():
                        index = selector.get_selected()
                        if index:
                            active[channel] = channel_functions[index-1]['id']
                    for control, row in range_rows:
                        row.set_visible(control.get('function') is None or control['function'] == active.get(control.get('instrument_channel', 'primary')))
                for _, selector in function_selectors.values():
                    selector.connect('notify::selected', show_ranges)
                show_ranges()
            else:
                instrument_page.append(self.label(profile.get('range_unavailable_reason') or 'This driver has no selectable measurement ranges.', 'dim-label'))
            instrument_page.append(self.label('Names, saved functions and ranges follow this instrument across all connections and are reapplied when it connects.', 'dim-label'))
            def save_instrument():
                settings = dict(profile.get('settings', {}))
                settings.pop('function', None)
                settings.pop('channel_functions', None)
                ranges = dict(settings.get('ranges', {}))
                for identity, (control, selector) in range_selectors.items():
                    range_index = selector.get_selected()
                    if range_index:
                        ranges[identity] = control['options'][range_index-1]['id']
                    else:
                        ranges.pop(identity, None)
                if ranges:
                    settings['ranges'] = ranges
                else:
                    settings.pop('ranges', None)
                def group(item):
                    exclusive = item.get('exclusive_group') or ('primary' if item.get('configurable') else None)
                    return (item.get('instrument_channel', 'primary'), exclusive) if exclusive else None
                catalog = {item['id']:item for item in functions}
                for channel, (channel_functions, selector) in function_selectors.items():
                    index = selector.get_selected()
                    if index:
                        chosen = channel_functions[index-1]
                        if len(function_selectors) == 1 and channel == 'primary':
                            settings['function'] = chosen['id']
                        else:
                            settings.setdefault('channel_functions', {})[channel] = chosen['id']
                        chosen_group = group(chosen)
                        shown = settings.get('display_functions')
                        if shown is not None and chosen_group and any(group(catalog.get(identity, {})) == chosen_group for identity in shown):
                            settings['display_functions'] = [identity for identity in shown
                                if group(catalog.get(identity, {})) != chosen_group] + [chosen['id']]
                self.command('instrument_settings', instrument=key, name=name.get_text(), settings=settings)
                dialog.destroy()
            instrument_page.append(self.button('Save Instrument Settings', save_instrument))
            sources = [(identity, item) for identity, item in self.view_state.get('instrument_profiles', {}).items()
                       if identity != key and item['type'] == profile['type']]
            instrument_page.append(self.label('Copy settings from the same instrument type', 'device-title'))
            if sources:
                source = Gtk.DropDown.new_from_strings([f"{item.get('name') or item['model']} · {item.get('serial') or identity}" for identity, item in sources])
                instrument_page.append(source)
                def copy_settings():
                    self.command('copy_settings', instrument=key, source=sources[source.get_selected()][0])
                    dialog.destroy()
                instrument_page.append(self.button('Copy Instrument Settings', copy_settings))
                instrument_page.append(self.label('Copies measurement settings only. Names, serial numbers and connections stay with their instruments.', 'dim-label'))
            else:
                instrument_page.append(self.label('No other saved instrument of this type.', 'dim-label'))
        else:
            instrument_page.append(self.label('Connect first to identify the model and serial number. Instrument settings are kept separately once identified.', 'dim-label'))
        if resource not in self.view_state.get('configured_resources', []):
            tabs.set_current_page(1)
        dialog.present()
        return dialog

    def forget_device(self, resource):
        key, profile = self.profile_for(resource)
        dialog, content = self.make_dialog('Forget Device')
        content.append(self.label('What would you like to forget?', 'section-title'))
        content.append(self.label((profile.get('name') or profile['identity']) if profile else resource, 'device-title'))
        if profile and resource != key:
            content.append(self.label(resource, 'dim-label'))
        content.append(self.label('Remove Connection removes only this address and its communication settings. The instrument name and measurement settings are kept.', 'dim-label'))
        content.append(self.label('Forget Instrument removes its name, measurement settings and all saved connection methods. Recorded data is kept.', 'dim-label'))
        dialog.add_button('Cancel', Gtk.ResponseType.CANCEL)
        if resource in self.view_state.get('configured_resources', []):
            dialog.add_button('Remove Connection', 1)
        if profile:
            dialog.add_button('Forget Instrument', 2).add_css_class('destructive-action')
        dialog.set_default_response(Gtk.ResponseType.CANCEL)
        def response(dialog, response):
            if response == 1:
                self.command('forget', instrument=key, resource=resource, scope='connection')
            elif response == 2:
                self.command('forget', instrument=key, scope='instrument')
            dialog.destroy()
        dialog.connect('response', response)
        dialog.present()
        return dialog

    def render_devices(self, state):
        self.view_state = state
        profiles = state.get('instrument_profiles', {})
        connections = state.get('connections', {})
        live = {device.get('resource', device['id']): device for device in state['instruments']}
        self.connected = {resource for resource, device in live.items() if not device['error'] and self.online}
        saved = state.get('saved_devices', {})
        resources = set(live) | set(state.get('configured_resources', []))
        linked = {connection.get('instrument') for connection in connections.values()}
        orphans = set(profiles) - linked - {device['id'] for device in live.values()}
        resources |= orphans
        def kind_for(key):
            return 'Saved' if key in orphans and not key.startswith('demo:') else communication_type(key)
        ids = sorted(resources, key=lambda key: (kind_for(key), key))
        if ids != self.ids or self.row_kinds != {key: kind_for(key) for key in ids}:
            for rows in self.sections.values():
                while rows.get_first_child():
                    rows.remove(rows.get_first_child())
            self.labels, self.titles, self.device_actions, self.connect_buttons = {}, {}, {}, {}
            for resource in ids:
                kind = kind_for(resource)
                details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
                if resource not in orphans:
                    details.append(self.label(resource, 'dim-label'))
                self.labels[resource] = self.label('', 'dim-label')
                details.append(self.labels[resource])
                actions = Gtk.Box(spacing=6)
                toggle = self.icon_button('media-playback-start-symbolic', 'Connect', lambda resource=resource: self.connection_toggle(resource))
                connection_action = Gtk.Box(spacing=6)
                connection_action.append(Gtk.Image.new_from_icon_name('media-playback-start-symbolic'))
                connection_action.append(Gtk.Label(label='Connect'))
                toggle.set_child(connection_action)
                toggle.set_visible(resource not in orphans)
                actions.append(toggle)
                self.connect_buttons[resource] = toggle
                actions.append(self.icon_button('emblem-system-symbolic', 'Device settings', lambda resource=resource: self.device_settings(resource)))
                actions.append(self.icon_button('user-trash-symbolic', 'Forget…', lambda resource=resource: self.forget_device(resource)))
                self.device_actions[resource] = actions
                row = self.setting_row('', details, actions)
                self.titles[resource] = row.get_first_child().get_first_child()
                self.sections[kind].append(row)
            for kind, rows in self.sections.items():
                if rows.get_first_child() is None:
                    rows.append(self.label(f'No {kind.lower()} instruments configured', 'dim-label'))
            self.demo_section.get_parent().set_visible(any(kind_for(key) == 'Demo' for key in ids))
            self.saved_section.get_parent().set_visible(any(kind_for(key) == 'Saved' for key in ids))
            self.ids = ids
            self.row_kinds = {key: kind_for(key) for key in ids}
        for resource in ids:
            device = live.get(resource)
            key, profile = self.profile_for(resource)
            name = (profile.get('name') or profile['identity']) if profile else (device or saved.get(resource, {})).get('name', 'Saved instrument')
            self.titles[resource].set_text(name)
            button = self.connect_buttons[resource]
            connected = resource in self.connected
            title = 'Disconnect' if connected else 'Connect'
            action = button.get_child()
            action.get_first_child().set_from_icon_name('media-playback-stop-symbolic' if connected else 'media-playback-start-symbolic')
            action.get_last_child().set_text(title)
            button.set_tooltip_text(title)
            button.update_property([Gtk.AccessibleProperty.LABEL], [title])
            if not self.online:
                text = 'Service offline'
            elif resource in orphans:
                text = f"Saved profile · {profile['type']} · Serial: {profile.get('serial') or 'Unavailable'}"
            elif not device:
                text = 'Disconnected · Not currently available'
            elif device['error']:
                text = 'Disconnected · ' + device['error']
            else:
                text = f"{'Demo' if device['simulated'] else 'Connected'} · {device['category'].replace('_', ' ')}"
            self.labels[resource].set_text(text)

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
            self.experiments_supported = error is None and 'experiments' in state
            if error:
                self.status.set_text('Offline · Start the background service to manage devices')
                self.api.set_text('Offline')
                for request in list(self.request_callbacks):
                    callback = self.request_callbacks.pop(request)
                    self.requests.discard(request)
                    callback(dict(state='failed', error='The background service is unavailable. Check its status in Devices & Settings and try again.'))
                self.render_devices(self.view_state or dict(instruments=[]))
                for view in self.experiment_views.values():
                    view.offline()
            else:
                self.status.set_text(state['status'] + (' · Recording active' if state['recording'] else ''))
                if not self.interval_dirty:
                    self.syncing = True
                    self.interval.set_value(state.get('interval_s', 1))
                    self.syncing = False
                control = state.get('control_api')
                self.api.set_text(f"{control['url']}\nPrivate local token · Advanced commands {'enabled' if control['allow_raw'] else 'disabled'}" if control else 'Disabled')
                self.render_devices(state)
                self.render_experiments(state.get('experiments', {}))
                for view in self.experiment_views.values():
                    view.update(state)
                for request in list(self.requests):
                    result = state.get('commands', {}).get(request, {})
                    if result.get('state') in ('succeeded', 'failed'):
                        self.requests.discard(request)
                        self.notify(result.get('error', 'Changes applied.'))
                        callback = self.request_callbacks.pop(request, None)
                        if callback:
                            callback(result)
            self.update_sensitivity()
        self.bus_call('Snapshot', None, updated)
        return True


def launch():
    app = Gtk.Application(application_id=BUS+'.App')
    app.connect('activate', lambda app: Window(app).present())
    app.run(None)
