"""GNOME settings-style setup, progress and results for an experiment."""
import shutil
import copy
import json
from pathlib import Path
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gio, GLib, Gtk
from gate.procedures import parameter_specs, test_spec, METRICS
from gate.test_spec import validate_test
from gate.secondary_capture import required_roles


class ExperimentView(Gtk.Box):
    def __init__(self, owner, definition):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=28)
        self.owner, self.definition = owner, definition
        self.state = {}
        self.device_signature = None
        self.selected_test = None
        self.parameter_drafts = {}
        self.parameters = {}
        self.last_result = None
        self.saving = False
        self.pickers, self.role_status = {}, {}
        self.run_id = None
        self.sequence_id = None
        self.run_error = ''
        self.prefer_demo_observations = False
        self.tabs = Gtk.Notebook()
        self.tab_pages = {}
        for name in ('Summary', 'Instruments', 'Parameters', 'Run', 'Results'):
            page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=28, margin_top=20)
            self.tabs.append_page(page, Gtk.Label(label=name))
            self.tab_pages[name] = page
        self.summary_page = self.tab_pages['Summary']
        self.instrument_page = self.tab_pages['Instruments']
        self.parameter_page = self.tab_pages['Parameters']
        self.run_page = self.tab_pages['Run']
        self.run_page.set_spacing(16)
        self.result_page = self.tab_pages['Results']
        self.append(self.tabs)
        self.shown_results = None
        self.build_summary()
        _, roles = owner.section('Choose Equipment', 'Assign a connected instrument to each functional role.', self.instrument_page)
        for role, requirement in definition['roles'].items():
            picker = Gtk.ComboBoxText(hexpand=True)
            picker.update_property([Gtk.AccessibleProperty.LABEL], [requirement['name']])
            picker.connect('changed', lambda *_: self.update_ready())
            details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            status = owner.label('Waiting for equipment…', 'dim-label')
            details.append(status)
            details.append(picker)
            roles.append(owner.setting_row(requirement['name']+(' · Optional' if requirement.get('optional', False) else ''), details))
            self.pickers[role], self.role_status[role] = picker, status
        self.demo = owner.button('Add Simulated Converter Bench', self.add_demo)
        self.demo.set_halign(Gtk.Align.START)
        self.instrument_page.append(self.demo)
        self.add_secondary_button = owner.button('Add Secondary Measurements…', self.add_secondary_measurements)
        self.add_secondary_button.set_halign(Gtk.Align.START)
        self.instrument_page.append(self.add_secondary_button)
        self.capture_settings_button = owner.button('Waveform Channels & Screenshots…', self.configure_waveforms)
        self.capture_settings_button.set_halign(Gtk.Align.START)
        self.instrument_page.append(self.capture_settings_button)
        _, settings = owner.section('Test & Parameters', 'Choose a test and configure its operating parameters.', self.parameter_page)
        self.test_picker = Gtk.ComboBoxText(hexpand=True)
        for identity in definition['tests']:
            spec = test_spec(definition, identity)
            self.test_picker.append(identity, identity.replace('_', ' ').capitalize() + ' · ' + spec['label'])
        self.test_picker.set_active(0)
        settings.append(owner.setting_row('Test', self.test_picker))
        self.edit_spec_button = owner.button('Edit Test JSON…', self.edit_specification)
        self.edit_spec_button.set_halign(Gtk.Align.START)
        self.parameter_page.append(self.edit_spec_button)
        self.parameter_rows = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, show_separators=True)
        self.parameter_rows.add_css_class('settings-list')
        self.parameter_page.append(self.parameter_rows)
        self.metrics = owner.label('', 'dim-label')
        _, metric_rows = owner.section('Measurements & Output', 'Recorded metrics and the default presentation for the selected test.', self.summary_page)
        metric_rows.append(self.metrics)
        self.run_selection = owner.label('', 'device-title')
        self.run_page.append(self.run_selection)
        _, test_rows = owner.section('Tests to Run', 'Checked tests run from top to bottom, using their settings from Parameters. A failure or Stop ends the sequence.', self.run_page)
        self.test_checks, self.test_states = {}, {}
        for index, identity in enumerate(definition['tests']):
            check = Gtk.CheckButton(label=identity.replace('_', ' ').capitalize(), active=index == 0)
            check.connect('toggled', self.selection_changed)
            state = owner.label('Selected' if index == 0 else 'Not selected', 'dim-label')
            details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5, hexpand=True)
            details.append(check)
            details.append(owner.label(test_spec(definition, identity)['label'], 'dim-label'))
            row = Gtk.Box(spacing=12)
            row.append(details)
            row.append(state)
            test_rows.append(row)
            self.test_checks[identity], self.test_states[identity] = check, state
        self.run_description = owner.label('', 'dim-label')
        self.run_page.append(self.run_description)
        self.wiring = Gtk.CheckButton(label='I have checked the DUT wiring and the configured voltage, current and power limits.')
        self.wiring.connect('toggled', lambda *_: self.update_ready())
        self.run_page.append(self.wiring)
        actions = Gtk.Box(spacing=8)
        self.start = owner.button('Run', self.start_run)
        self.start.add_css_class('suggested-action')
        self.cancel = owner.button('Stop Tests', lambda: owner.command('cancel_experiment'))
        self.cancel.add_css_class('destructive-action')
        actions.append(self.start)
        actions.append(self.cancel)
        self.run_page.append(actions)
        self.status = owner.label('Waiting for service…', 'dim-label')
        self.run_page.append(self.status)
        self.progress = Gtk.ProgressBar(show_text=True)
        self.run_page.append(self.progress)
        _, results = owner.section('Results', 'Each run retains its equipment, parameters, raw readings, calculated metrics and charts.', self.result_page)
        self.history_picker = Gtk.ComboBoxText(hexpand=True)
        self.history_picker.connect('changed', lambda *_: self.show_result())
        results.append(self.history_picker)
        self.results = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.result_page.append(self.results)
        self.test_picker.connect('changed', self.choose_test)
        self.choose_test()
        self.offline()

    def build_summary(self):
        definition = self.definition
        _, dut_rows = self.owner.section('Device Under Test', 'The equipment is arranged around this device.', self.summary_page)
        dut_rows.append(self.owner.setting_row(definition['dut']['name'],
            self.owner.label('Terminals: ' + ', '.join(terminal.replace('_', ' ') for terminal in definition['dut']['terminals']), 'dim-label')))
        _, roles = self.owner.section('Equipment', 'Required functions, independent of instrument make or model.', self.summary_page)
        capability_names = {
            'measure.temperature': 'Temperature readings',
            'capture.waveform': 'Calibrated waveform captures',
            'source.dc_voltage': 'Variable DC voltage source',
            'sink.dc_current': 'DC current load',
            'measure.dc_voltage': 'DC voltage measurement',
            'measure.dc_current': 'DC current measurement',
        }
        for requirement in definition['roles'].values():
            roles.append(self.owner.setting_row(requirement['name']+(' · Optional' if requirement.get('optional', False) else ''), self.owner.label(
                '\n'.join(capability_names.get(item, item.replace('.', ' · ').replace('_', ' '))
                          for item in requirement['capabilities']), 'dim-label')))
        _, wiring = self.owner.section('Wiring', 'Connections between equipment and the device under test.', self.summary_page)
        names = {'dut': definition['dut']['name'], **{key: role['name'] for key, role in definition['roles'].items()}}
        def endpoint(value):
            component, terminal = value.split('.', 1)
            return names[component] + ' · ' + terminal.replace('_', ' ')
        for source, destination in definition['connections']:
            wiring.append(self.owner.setting_row(endpoint(source), self.owner.label('→ ' + endpoint(destination), 'dim-label')))
        _, tests = self.owner.section('Tests', 'Tests that share this equipment arrangement.', self.summary_page)
        for identity in definition['tests']:
            tests.append(self.owner.setting_row(identity.replace('_', ' ').capitalize(), self.owner.label(test_spec(definition, identity)['description'], 'dim-label')))

    def selected_tests(self):
        return [identity for identity, check in self.test_checks.items() if check.get_active()]

    def needs_outputs(self):
        return bool(self.controlled_roles())

    def controlled_roles(self):
        return {role for identity in self.selected_tests() for role in test_spec(self.definition, identity)['outputs']}

    def selection_changed(self, *_):
        if not hasattr(self, 'wiring') or getattr(self, 'syncing_selection', False):
            return
        self.wiring.set_active(False)
        self.device_signature = None
        if self.state:
            self.update(self.state)
        else:
            self.update_ready()

    def parameters_for_test(self, identity):
        kind = test_spec(self.definition, identity)
        values = ({key: spin.get_value() for key, spin in self.parameters.items()} if identity == self.selected_test
                  else self.parameter_drafts.get(identity, {}))
        result = {}
        for key, (_, default, _, _, _, integer) in parameter_specs(kind).items():
            if key == kind['acquisition']['settle_s'] and kind['outputs']:
                default = max(default, self.state.get('interval_s', 1.))
            value = values.get(key, default)
            result[key] = int(value) if integer else value
        return result

    def current_spec(self):
        return test_spec(self.definition, self.test_picker.get_active_id())

    def choose_test(self, *_):
        self.run_error = ''
        if self.selected_test:
            self.parameter_drafts[self.selected_test] = {key: spin.get_value() for key, spin in self.parameters.items()}
        self.selected_test = self.test_picker.get_active_id()
        self.parameters.clear()
        child = self.parameter_rows.get_first_child()
        while child:
            self.parameter_rows.remove(child)
            child = self.parameter_rows.get_first_child()
        for key, (label, default, low, high, step, integer) in parameter_specs(self.current_spec()).items():
            spin = Gtk.SpinButton.new_with_range(low, high, step)
            spin.set_digits(0 if integer else 3)
            if key == self.current_spec()['acquisition']['settle_s'] and self.current_spec()['outputs']:
                default = max(default, self.state.get('interval_s', 1.))
            spin.set_value(self.parameter_drafts.get(self.selected_test, {}).get(key, default))
            spin.update_property([Gtk.AccessibleProperty.LABEL], [label])
            self.parameter_rows.append(self.owner.setting_row(label, self.owner.label('', 'dim-label'), spin))
            self.parameters[key] = spin
        self.wiring.set_active(False)
        self.device_signature = None
        if self.state:
            self.update(self.state)
        self.update_metrics()
        self.update_ready()

    def update_metrics(self):
        spec = self.current_spec()
        names = [metric['label']+(f" ({metric['unit']})" if metric['unit'] else '') for field in ('measurements', 'secondary_measurements') for metric in spec.get(field, {}).values()]
        if spec['acquisition']['capture_all']:
            for role, picker in self.pickers.items():
                device = next((item for item in self.state.get('instruments', []) if item['id'] == picker.get_active_id()), {})
                names += [self.definition['roles'][role]['name']+' · '+reading['channel'] for reading in device.get('readings', []) if reading.get('quality') != 'disabled']
        derived = [metric['label']+(f" ({metric['unit']})" if metric['unit'] else '') for metric in spec['derived'].values()]
        names += [wave['label']+' (waveform, '+wave['phase']+')' for wave in spec.get('waveforms', {}).values()]
        charts = [plot['title'] for plot in spec['plots']]
        if not charts and spec['acquisition']['capture_all']:
            charts = ['Time series for each recorded metric']
        self.metrics.set_text('Records: '+(', '.join(names) or 'choose equipment to inspect its current readings')+
                             ('. Derived: '+', '.join(derived) if derived else '')+
                             '. Charts: '+('; '.join(charts) or 'none specified')+'. Table and CSV use the configured dataset.')

    def edit_specification(self):
        identity = self.test_picker.get_active_id()
        dialog = Gtk.Dialog(title='Edit Test JSON', transient_for=self.owner, modal=True,
                            default_width=820, default_height=720, use_header_bar=True)
        dialog.add_button('Cancel', Gtk.ResponseType.CANCEL)
        save = dialog.add_button('Save', Gtk.ResponseType.ACCEPT)
        save.add_css_class('suggested-action')
        content = dialog.get_content_area()
        content.set_spacing(12)
        content.append(self.owner.label('Define parameters, sweeps, metrics, calculations and plots using test schema version 1.', 'dim-label'))
        editor = Gtk.TextView(monospace=True, wrap_mode=Gtk.WrapMode.NONE, left_margin=12, right_margin=12, top_margin=12)
        editor.get_buffer().set_text(json.dumps(self.current_spec(), indent=2))
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_child(editor)
        content.append(scroll)
        error = self.owner.label('', 'error')
        content.append(error)
        busy = False
        def respond(_, response):
            nonlocal busy
            if busy:
                return
            if response != Gtk.ResponseType.ACCEPT:
                dialog.destroy()
                return
            try:
                buffer = editor.get_buffer()
                spec, _ = validate_test(json.loads(buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)), self.definition['roles'])
                definition = copy.deepcopy(self.definition)
                definition['tests'][identity] = spec
            except (ValueError, TypeError) as exc:
                error.set_text(str(exc))
                return
            busy = True
            save.set_sensitive(False)
            error.set_text('Saving…')
            def completed(result):
                nonlocal busy
                busy = False
                if result['state'] == 'failed':
                    error.set_text(result.get('error', 'Unable to save specification'))
                    save.set_sensitive(True)
                else:
                    view = self.owner.experiment_views.get(definition['id'])
                    if view:
                        view.test_picker.set_active_id(identity)
                        view.tabs.set_current_page(view.tabs.page_num(view.parameter_page))
                    dialog.destroy()
            self.owner.command('save_experiment', experiment=definition, on_complete=completed)
        dialog.connect('response', respond)
        dialog.connect('close-request', lambda *_: busy)
        dialog.present()
        return dialog

    def update(self, state):
        self.state = state
        sequence = state.get('experiment_sequence')
        if sequence and sequence['state'] == 'running' and sequence['experiment'] == self.definition['id']:
            self.syncing_selection = True
            try:
                queued = {item['test'] for item in sequence['tests']}
                for identity, check in self.test_checks.items():
                    check.set_active(identity in queued)
            finally:
                self.syncing_selection = False
        signature = [(item['id'], item['name'], item.get('error', ''), tuple(item.get('experiment_capabilities', [])), item.get('experiment_control_supported', False))
                     for item in state.get('instruments', [])]
        if signature != self.device_signature:
            self.device_signature = signature
            for role, picker in self.pickers.items():
                chosen = picker.get_active_id()
                required = set(self.definition['roles'][role]['capabilities'])
                candidates = [item for item in state.get('instruments', []) if not item.get('error') and
                              required <= set(item.get('experiment_capabilities', [])) and
                              (role not in self.controlled_roles() or item.get('experiment_control_supported', False))]
                picker.remove_all()
                optional = self.definition['roles'][role].get('optional', False)
                if optional:
                    picker.append('', 'Not assigned (optional)')
                for device in candidates:
                    picker.append(device['id'], device['name'])
                if chosen and chosen not in {item['id'] for item in candidates}:
                    picker.append(chosen, 'Unavailable · ' + chosen)
                if chosen:
                    picker.set_active_id(chosen)
                elif len(candidates) == 1 and (not optional or self.prefer_demo_observations and candidates[0].get('simulated')):
                    picker.set_active_id(candidates[0]['id'])
                elif optional:
                    picker.set_active_id('')
                self.role_status[role].set_text('Choose equipment' if candidates else 'No connected equipment provides the required functions and controls')
        self.update_metrics()
        current = state.get('experiment_run')
        sequence = state.get('experiment_sequence')
        if sequence and sequence['experiment'] == self.definition['id'] and current and current.get('sequence_id') == sequence['id']:
            if sequence['id'] == self.sequence_id or sequence['state'] == 'running':
                self.run_id = current['id']
        runs = [item for item in state.get('experiment_runs', []) if item['experiment'] == self.definition['id']]
        if current and current['experiment'] == self.definition['id']:
            runs = [current] + [item for item in runs if item['id'] != current['id']]
        previous = self.history_picker.get_active_id()
        signature = [(item['id'], item['state']) for item in runs]
        if getattr(self, 'history_signature', None) != signature:
            self.history_signature = signature
            self.history_picker.remove_all()
            for item in runs:
                from datetime import datetime
                timestamp = datetime.fromtimestamp(item['started_utc_ns']/1e9).strftime('%d %b %H:%M:%S')
                self.history_picker.append(item['id'], f"{timestamp} · {item['test'].replace('_', ' ')} · {item['state']}" + (' · SIMULATED' if item['simulated'] else ''))
            self.history_picker.set_active_id(self.run_id or previous or (runs[0]['id'] if runs else ''))
        self.show_result()
        sequence_done = sequence and sequence['id'] == self.sequence_id and sequence['state'] == 'completed'
        single_done = current and not current.get('sequence_id') and current['id'] == self.run_id and current['state'] != 'running'
        completion = sequence['id'] if sequence_done else current['id'] if single_done else None
        if completion and self.shown_results != completion:
            self.shown_results = completion
            self.tabs.set_current_page(self.tabs.page_num(self.result_page))
            scroll = self.get_ancestor(Gtk.ScrolledWindow)
            if scroll:
                GLib.idle_add(lambda: (scroll.get_vadjustment().set_value(0), False)[1])
        self.update_ready()

    def selected_result(self):
        identity = self.history_picker.get_active_id()
        current = self.state.get('experiment_run')
        if current and current['id'] == identity:
            return current
        return next((item for item in self.state.get('experiment_runs', []) if item['id'] == identity), None)

    def show_result(self):
        result = self.selected_result()
        if not result:
            return
        signature = (result['id'], result['state'], result['completed_points'], result.get('error'))
        if signature == self.last_result:
            return
        self.last_result = signature
        child = self.results.get_first_child()
        while child:
            self.results.remove(child)
            child = self.results.get_first_child()
        self.results.append(self.owner.label(('SIMULATED · ' if result['simulated'] else '') + result['state'].capitalize() +
            f" · {result['completed_points']} points" + (' · '+result['error'] if result['error'] else ''), 'device-title'))
        if result['state'] == 'running':
            return
        for path in result.get('plots', []):
            if Path(path).is_file():
                picture = Gtk.Picture.new_for_filename(path)
                picture.set_can_shrink(True)
                picture.set_size_request(-1, 400)
                self.results.append(picture)
        for note in result.get('notes', []):
            self.results.append(self.owner.label(note, 'dim-label'))
        table = result.get('table', {'data':'summary' if result.get('summary') else 'points'})
        rows = result.get(table['data'], [])
        if rows:
            columns = table.get('columns', list(dict.fromkeys(key for row in rows for key in row)))
            grid = Gtk.Grid(column_spacing=18, row_spacing=8, margin_top=8, margin_bottom=8)
            for col, key in enumerate(columns):
                label, unit = result.get('metrics', {}).get(key, METRICS.get(key, (key.replace('_', ' ').capitalize(), '')))
                grid.attach(self.owner.label(label + (f' ({unit})' if unit else ''), 'device-title'), col, 0, 1, 1)
            for index, row in enumerate(rows[:20], 1):
                for col, key in enumerate(columns):
                    value = row.get(key)
                    text = 'Unavailable' if value is None else f'{value:.6g}' if isinstance(value, float) else str(value).replace('_', ' ')
                    grid.attach(self.owner.label(text), col, index, 1, 1)
            scroll = Gtk.ScrolledWindow(min_content_height=160, max_content_height=300, propagate_natural_height=True)
            scroll.set_child(grid)
            self.results.append(scroll)
            if len(rows) > 20:
                self.results.append(self.owner.label('Showing the first 20 rows. The report and CSV include every result.', 'dim-label'))
        self.waveform_results(result)
        actions = Gtk.Box(spacing=8)
        if Path(result.get('report', '')).is_file():
            actions.append(self.owner.button('Open Full Report', lambda: self.open_report(result['report'])))
            actions.append(self.owner.button('Export CSV…', lambda: self.export_csv(result)))
            if result.get('secondary_measurements') and result.get('secondary_csv'):
                actions.append(self.owner.button('Export Secondary CSV…', lambda: self.export_csv(result, secondary=True)))
        if result.get('path'):
            actions.append(self.owner.button('Open Results Folder', lambda: self.open_file(str(Path(result['path']).parent))))
        self.results.append(actions)

    def add_demo(self):
        self.prefer_demo_observations = True
        self.device_signature = None
        self.owner.command('demo_experiment')

    def configure_waveforms(self):
        identity = self.test_picker.get_active_id()
        spec = copy.deepcopy(self.current_spec())
        dialog = Gtk.Dialog(title='Waveform Channels & Screenshots', transient_for=self.owner, modal=True,
                            default_width=580, use_header_bar=True)
        dialog.add_button('Cancel', Gtk.ResponseType.CANCEL)
        save = dialog.add_button('Save', Gtk.ResponseType.ACCEPT)
        save.add_css_class('suggested-action')
        content = dialog.get_content_area()
        content.set_spacing(16)
        for setter in (content.set_margin_top, content.set_margin_bottom, content.set_margin_start, content.set_margin_end):
            setter(20)
        content.append(self.owner.label(spec['label'], 'device-title'))
        content.append(self.owner.label('Settings apply to this test. Single shots save the stopped scope screen; ripple snapshots save the current live display.', 'dim-label'))
        controls = {}
        available = {item['id']:item for item in self.state.get('instruments', [])}
        for key, wave in spec.get('waveforms', {}).items():
            _, rows = self.owner.section(wave['label'], 'Single shot' if wave['phase'] == 'transition' else 'Settled snapshot', content)
            device = available.get(self.pickers[wave['role']].get_active_id(), {})
            channels = sorted(set(device.get('waveform_capabilities', {})) | {wave['channel']})
            picker = Gtk.ComboBoxText()
            for channel in channels:
                picker.append(channel, channel)
            picker.set_active_id(wave['channel'])
            rows.append(self.owner.setting_row('Channel', picker))
            screenshot = Gtk.CheckButton(label='Save scope screenshot', active=wave.get('screenshot', wave['phase'] == 'transition'))
            rows.append(screenshot)
            timeout = Gtk.SpinButton.new_with_range(.1, 30., .5)
            timeout.set_digits(1)
            # Expressions remain intact unless the user changes the displayed value.
            configured_timeout = wave['timeout_s']
            from gate.expressions import Expression
            from gate.test_spec import expand_axes
            values = dict(self.parameters_for_test(identity))
            values.update(expand_axes(spec, values)[0])
            initial_timeout = Expression(configured_timeout, values).evaluate(values)
            timeout.set_value(initial_timeout)
            rows.append(self.owner.setting_row('Capture timeout (seconds)', timeout))
            screenshot.connect('toggled', lambda check, spin=timeout: spin.set_value(max(10., spin.get_value())) if check.get_active() else None)
            native = Gtk.CheckButton(label='Use scope timebase and pre-trigger position', active=wave['window_s'] is None and wave['pretrigger'] is None)
            native.set_sensitive(not native.get_active())
            rows.append(native)
            controls[key] = dict(channel=picker, screenshot=screenshot, timeout=timeout, native=native,
                                 initial_timeout=initial_timeout)
        error = self.owner.label('', 'error')
        content.append(error)
        busy = False
        def respond(_, response):
            nonlocal busy
            if busy:
                return
            if response != Gtk.ResponseType.ACCEPT:
                dialog.destroy()
                return
            try:
                for key, widgets in controls.items():
                    wave = spec['waveforms'][key]
                    wave.update(channel=widgets['channel'].get_active_id(), screenshot=widgets['screenshot'].get_active())
                    if widgets['timeout'].get_value() != widgets['initial_timeout']:
                        wave['timeout_s'] = widgets['timeout'].get_value()
                    if widgets['native'].get_active():
                        wave.update(window_s=None, pretrigger=None)
                spec_validated, _ = validate_test(spec, self.definition['roles'])
            except (ValueError, TypeError) as exc:
                error.set_text(str(exc))
                return
            definition = copy.deepcopy(self.definition)
            definition['tests'][identity] = spec_validated
            drafts = dict(self.parameter_drafts)
            drafts[identity] = self.parameters_for_test(identity)
            bindings = {role:picker.get_active_id() for role,picker in self.pickers.items()}
            checked = self.selected_tests()
            busy = True
            save.set_sensitive(False)
            def completed(result):
                nonlocal busy
                busy = False
                if result['state'] == 'failed':
                    error.set_text(result.get('error', 'Unable to save waveform settings'))
                    save.set_sensitive(True)
                    return
                view = self.owner.experiment_views.get(definition['id'])
                if view:
                    view.parameter_drafts = drafts
                    view.selected_test = None
                    if view.test_picker.get_active_id() != identity:
                        view.test_picker.set_active_id(identity)
                    else:
                        view.choose_test()
                    for role, picker in view.pickers.items():
                        if bindings.get(role):
                            picker.set_active_id(bindings[role])
                    for test, check in view.test_checks.items():
                        check.set_active(test in checked)
                    view.tabs.set_current_page(view.tabs.page_num(view.instrument_page))
                dialog.destroy()
            self.owner.command('save_experiment', experiment=definition, on_complete=completed)
        dialog.connect('response', respond)
        dialog.connect('close-request', lambda *_: busy)
        dialog.capture_controls = controls
        dialog.present()
        return dialog

    def add_secondary_measurements(self):
        from gate.secondary import add_observations
        from gate.experiments import validate_definition
        dialog = Gtk.Dialog(title='Add Secondary Measurements', transient_for=self.owner, modal=True,
                            default_width=620, use_header_bar=True)
        dialog.add_button('Cancel', Gtk.ResponseType.CANCEL)
        save = dialog.add_button('Add', Gtk.ResponseType.ACCEPT)
        save.add_css_class('suggested-action')
        content = dialog.get_content_area()
        content.set_spacing(14)
        for setter in (content.set_margin_top, content.set_margin_bottom, content.set_margin_start, content.set_margin_end):
            setter(20)
        content.append(self.owner.label('Optional observations enrich the selected test. Unassigned probes are recorded as unavailable; required observations can be configured in Edit Test JSON.', 'dim-label'))
        choices = {}
        for key, label, active in [('dut','DUT temperature',True), ('ambient','Ambient temperature',True),
                                   ('ripple','Ripple waveform after settling',True), ('protection','Protection waveform armed before a transition',False)]:
            choices[key] = Gtk.CheckButton(label=label, active=active)
            content.append(choices[key])
        content.append(self.owner.label('Protection trigger level (V), falling edge', 'device-title'))
        level = Gtk.SpinButton.new_with_range(-1000, 1000, .1)
        level.set_digits(3)
        values = self.parameters_for_test(self.test_picker.get_active_id())
        level.set_value(values.get('nominal_output_v',5.)*(1-values.get('tolerance_pct',5.)/100))
        content.append(level)
        content.append(self.owner.label('Arm protection capture when (sweep axes/parameters)', 'device-title'))
        condition = Gtk.Entry(text='load_current_set_a >= 3.5' if any(axis['id']=='load_current_set_a' for axis in self.current_spec()['axes']) else 'True')
        content.append(condition)
        all_tests = Gtk.CheckButton(label='Apply to every test in this experiment')
        content.append(all_tests)
        content.append(self.owner.label('Ripple uses the scope’s existing settings. Single shots save a scope screenshot when supported. On Siglent, set timebase, pre-trigger position and probe scales on the scope; GATE sets the edge trigger and arms the shot.', 'dim-label'))
        error = self.owner.label('', 'error')
        content.append(error)
        busy = False
        def respond(_, response):
            nonlocal busy
            if busy:
                return
            if response != Gtk.ResponseType.ACCEPT:
                dialog.destroy()
                return
            try:
                selected = {key:check.get_active() for key,check in choices.items()}
                if not any(selected.values()):
                    raise ValueError('Select at least one secondary measurement')
                definition = copy.deepcopy(self.definition)
                def slot(base, name, capability):
                    for identity, role in definition['roles'].items():
                        if role.get('optional') and capability in role['capabilities']:
                            return identity
                    identity = base
                    index = 2
                    while identity in definition['roles']:
                        identity, index = base+'_'+str(index), index+1
                    definition['roles'][identity] = dict(name=name, capabilities=[capability], terminals=[], optional=True)
                    return identity
                thermal = slot('thermal','Temperature probes','measure.temperature') if selected['dut'] or selected['ambient'] else None
                scope = slot('scope','Waveform recorder','capture.waveform') if selected['ripple'] or selected['protection'] else None
                identities = list(definition['tests']) if all_tests.get_active() else [self.test_picker.get_active_id()]
                for identity in identities:
                    spec = test_spec(definition, identity)
                    add_observations(spec, thermal, scope, **selected, trigger_level=level.get_value(), condition=condition.get_text())
                    definition['tests'][identity] = spec
                definition = validate_definition(definition)
            except (ValueError, TypeError) as exc:
                error.set_text(str(exc))
                return
            drafts = dict(self.parameter_drafts)
            drafts[self.selected_test] = self.parameters_for_test(self.selected_test)
            bindings = {role:picker.get_active_id() for role,picker in self.pickers.items()}
            checked = self.selected_tests()
            busy = True
            save.set_sensitive(False)
            error.set_text('Saving…')
            def completed(result):
                nonlocal busy
                busy = False
                if result['state'] == 'failed':
                    error.set_text(result.get('error', 'Unable to add observations'))
                    save.set_sensitive(True)
                else:
                    view = self.owner.experiment_views.get(definition['id'])
                    if view:
                        view.prefer_demo_observations = self.prefer_demo_observations
                        view.parameter_drafts = drafts
                        view.selected_test = None
                        if view.test_picker.get_active_id() != self.selected_test:
                            view.test_picker.set_active_id(self.selected_test)
                        else:
                            view.choose_test()
                        for role, picker in view.pickers.items():
                            if bindings.get(role):
                                picker.set_active_id(bindings[role])
                        for identity, check in view.test_checks.items():
                            check.set_active(identity in checked)
                        view.device_signature = None
                        view.update(view.state)
                        view.tabs.set_current_page(view.tabs.page_num(view.instrument_page))
                    dialog.destroy()
            self.owner.command('save_experiment', experiment=definition, on_complete=completed)
        dialog.connect('response', respond)
        dialog.connect('close-request', lambda *_: busy)
        dialog.present()
        return dialog

    def waveform_results(self, result):
        captures = result.get('waveforms', [])
        if not captures:
            return
        self.results.append(self.owner.label('Secondary Waveforms', 'section-title'))
        picker = Gtk.ComboBoxText()
        for index, capture in enumerate(captures):
            picker.append(str(index), capture['label']+f" · point {capture['point_index']+1} · "+capture['quality'])
        self.results.append(picker)
        details = self.owner.label('', 'dim-label')
        self.results.append(details)
        picture = Gtk.Picture(can_shrink=True)
        picture.set_size_request(-1, 340)
        self.results.append(picture)
        screen_label = self.owner.label('Scope Screenshot', 'section-title')
        self.results.append(screen_label)
        screen = Gtk.Picture(can_shrink=True)
        screen.set_size_request(-1, 340)
        self.results.append(screen)
        open_screen = self.owner.button('Open Scope Screenshot', lambda: self.open_file(captures[int(picker.get_active_id())]['screenshot']))
        open_screen.set_halign(Gtk.Align.START)
        self.results.append(open_screen)
        export = self.owner.button('Open Full Waveform CSV', lambda: self.open_file(captures[int(picker.get_active_id())]['csv']))
        export.set_halign(Gtk.Align.START)
        self.results.append(export)
        def changed(_):
            active = picker.get_active_id()
            if active is None:
                return
            capture = captures[int(active)]
            text = capture['error'] or f"{capture.get('points', 0)} samples · {capture['channel']} · {capture['unit']}"
            if 'statistics' in capture:
                stats = capture['statistics']
                text += f" · peak-to-peak {stats['peak_to_peak']:.5g} {capture['unit']} · AC RMS {stats['ac_rms']:.5g} {capture['unit']}"
            settings = []
            for key,value in capture['setpoints'].items():
                label, unit = result.get('metrics', {}).get(key, (key, ''))
                settings.append(f'{label}: {value:g} {unit}'.strip())
            if settings:
                text += '\n'+', '.join(settings)
            source = capture.get('driver_metadata', {})
            for key in ('acquisition', 'verification'):
                if source.get(key):
                    text += '\n'+str(source[key])
            details.set_text(text)
            if capture.get('screenshot_error'):
                details.set_text(text+'\nScope screenshot unavailable: '+capture['screenshot_error'])
            elif not capture.get('screenshot') and not capture.get('screenshot_requested', capture['phase'] == 'transition'):
                details.set_text(text+'\nScope screenshot was not requested. Enable it in Instruments → Waveform Channels & Screenshots.')
            has_screen = bool(capture.get('screenshot') and Path(capture['screenshot']).is_file())
            for widget in (screen_label, screen, open_screen):
                widget.set_visible(has_screen)
            if has_screen:
                screen.set_filename(capture['screenshot'])
            has_plot = bool(capture.get('plot') and Path(capture['plot']).is_file())
            picture.set_visible(has_plot)
            if has_plot:
                picture.set_filename(capture['plot'])
            export.set_sensitive(bool(capture.get('csv')))
        picker.connect('changed', changed)
        picker.set_active(0)

    def open_report(self, path):
        # A local HTML file association can point at an editor or unrelated app.
        # Use the user's web browser, but pass the actual local report URI.
        try:
            browser = (Gio.AppInfo.get_default_for_uri_scheme('https') or
                       Gio.AppInfo.get_default_for_uri_scheme('http'))
            if browser is None:
                raise RuntimeError('No default web browser is configured. Choose one in GNOME Settings → Default Apps.')
            if browser.supports_uris():
                browser.launch_uris([Path(path).as_uri()], None)
            else:
                browser.launch([Gio.File.new_for_path(path)], None)
        except Exception as exc:
            self.run_error = 'Unable to open report: ' + str(exc)
            self.status.set_text(self.run_error)

    def open_file(self, path):
        Gio.AppInfo.launch_default_for_uri(Path(path).as_uri(), None)

    def export_csv(self, result, secondary=False):
        chooser = Gtk.FileChooserNative(title='Export Experiment Results', transient_for=self.owner,
            action=Gtk.FileChooserAction.SAVE, accept_label='Export', cancel_label='Cancel')
        chooser.set_current_name(result['id'] + '.csv')
        def response(dialog, action):
            if action == Gtk.ResponseType.ACCEPT:
                try:
                    source = result['secondary_csv'] if secondary else result['summary_csv'] if result.get('table', {}).get('data', 'summary' if result.get('summary') else 'points') == 'summary' else result['csv']
                    shutil.copyfile(source, dialog.get_file().get_path())
                    self.status.set_text('CSV exported.')
                except Exception as exc:
                    self.status.set_text('Unable to export: ' + str(exc))
            dialog.destroy()
        chooser.connect('response', response)
        chooser.show()

    def offline(self):
        self.state = {}
        self.start.set_sensitive(False)
        self.cancel.set_sensitive(False)
        self.demo.set_sensitive(False)
        self.add_secondary_button.set_sensitive(False)
        self.capture_settings_button.set_sensitive(False)
        self.edit_spec_button.set_sensitive(False)
        self.status.set_text('Start the background service in Devices & Settings to run experiments.')

    def update_ready(self):
        if not hasattr(self, 'start'):
            return
        active = self.state.get('experiment_run')
        sequence = self.state.get('experiment_sequence')
        sequence_running = bool(sequence and sequence['state'] == 'running')
        running = bool(active and active['state'] == 'running') or sequence_running
        selected = {role: picker.get_active_id() for role, picker in self.pickers.items()}
        available = {item['id']: item for item in self.state.get('instruments', []) if not item.get('error')}
        tests = self.selected_tests()
        output_tests = self.needs_outputs()
        selection = [f"{len(tests)} {'test' if len(tests) == 1 else 'tests'} selected"]
        for role, identity in selected.items():
            selection.append(self.definition['roles'][role]['name'] + ': ' + available.get(identity, {}).get('name', 'Not selected or unavailable'))
        self.run_selection.set_text('\n'.join(selection))
        self.run_description.set_text('Configure each test separately in Parameters before starting the sequence.')
        self.wiring.set_visible(output_tests)
        self.start.set_label('Run Selected Tests & Enable Outputs' if output_tests else 'Run Selected Tests')
        previous_tests = {item['test']: item for item in sequence['tests']} if sequence and sequence['experiment'] == self.definition['id'] else {}
        for identity, check in self.test_checks.items():
            check.set_sensitive(not running and not self.saving)
            self.test_states[identity].set_text(previous_tests[identity]['state'].capitalize() if identity in previous_tests
                                               else 'Selected' if check.get_active() else 'Not selected')
        for role, identity in selected.items():
            device = available.get(identity, {})
            limits = device.get('experiment_limits', {})
            if limits:
                voltage, current = limits.get('voltage', []), limits.get('current', [])
                if len(voltage) == 2 and len(current) == 2:
                    self.role_status[role].set_text(f"Driver limits: {voltage[0]:g}–{voltage[1]:g} V · {current[0]:g}–{current[1]:g} A · {limits.get('power_w', 0):g} W")
            elif device and device.get('waveform_capabilities'):
                modes = ['Snapshot' if phase == 'settled' else 'Armed trigger' for value in device['waveform_capabilities'].values() for phase in value.get('phases', [])]
                verification = ' · Bench verification pending' if any('pending' in value.get('verification', '') for value in device['waveform_capabilities'].values()) else ''
                self.role_status[role].set_text('Waveforms: '+', '.join(dict.fromkeys(modes))+verification)
            elif device:
                if device.get('device_type') == 'HANTEK:365' and 'measure.temperature' in self.definition['roles'][role]['capabilities']:
                    self.role_status[role].set_text('One thermocouple input · Select Celsius for °C metrics. A separate probe is needed for independent ambient temperature.')
                else:
                    self.role_status[role].set_text('Records the instrument’s current readings' if role not in self.controlled_roles() else 'Choose equipment')
        needed = set().union(*(required_roles(self.definition, test_spec(self.definition, identity)) for identity in tests)) if tests else set()
        compatible = all(identity in available and set(self.definition['roles'][role]['capabilities']) <=
                         set(available[identity].get('experiment_capabilities', [])) for role, identity in selected.items() if role in needed)
        mandatory = [identity for role, identity in selected.items() if not self.definition['roles'][role].get('optional', False)]
        distinct = len(set(mandatory)) == len(mandatory)
        ready = bool(tests) and compatible and distinct and (not output_tests or self.wiring.get_active())
        procedure_error = ''
        if output_tests:
            for role in self.controlled_roles():
                ready = ready and available.get(selected[role], {}).get('experiment_control_supported', False)
        ready = ready and not procedure_error
        supported = self.state.get('experiment_execution_version') == 1 and self.state.get('experiment_sequence_version') == 1 and self.state.get('experiment_spec_version') == 1 and self.state.get('secondary_measurements_version') == 1
        self.start.set_sensitive(self.owner.online and supported and ready and not running and not self.saving)
        self.cancel.set_sensitive(self.owner.online and running and active is not None and active['experiment'] == self.definition['id'])
        self.demo.set_sensitive(self.owner.online and supported and not running)
        self.add_secondary_button.set_sensitive(self.owner.online and supported and not running)
        self.capture_settings_button.set_sensitive(self.owner.online and supported and not running and not self.saving and bool(self.current_spec().get('waveforms')))
        self.test_picker.set_sensitive(not running and not self.saving)
        self.parameter_rows.set_sensitive(not running and not self.saving)
        self.edit_spec_button.set_sensitive(self.owner.online and supported and not running and not self.saving)
        self.wiring.set_sensitive(not running and not self.saving)
        for picker in self.pickers.values():
            picker.set_sensitive(not running and not self.saving)
        if running:
            self.status.set_text(('SIMULATED · ' if active['simulated'] else '') + 'Running ' + active['name'] +
                                 f" · {active['completed_points']} of up to {active['total_points']} points")
            fraction = min(1., active['completed_points']/active['total_points'])
            if sequence_running:
                self.status.set_text(f"Test {sequence['index']+1} of {len(sequence['tests'])}: " + active['test'].replace('_', ' ') +
                                     f" · {active['completed_points']} of up to {active['total_points']} points")
                fraction = (sequence['completed_tests'] + fraction)/len(sequence['tests'])
            self.progress.set_fraction(fraction)
            self.progress.set_text('Running')
        elif not supported and self.owner.online:
            self.status.set_text('Restart the background service in Devices & Settings to load experiment execution support.')
        elif self.owner.online and not self.saving:
            result = self.selected_result()
            self.status.set_text(procedure_error or ('Select at least one test.' if not tests else 'Ready to run.' if ready else 'Choose compatible, distinct equipment for each role and confirm the wiring.' if output_tests else 'Choose compatible, distinct equipment for each role.') +
                                 (' Last run: '+result['state'] + (': '+result['error'] if result['error'] else '') if result else ''))
            self.progress.set_fraction(1. if result and result['state'] == 'completed' else 0.)
            self.progress.set_text(result['state'].capitalize() if result else 'Not started')
            if sequence and sequence['experiment'] == self.definition['id'] and sequence['state'] in ('failed', 'cancelled'):
                self.status.set_text('Sequence ' + sequence['state'] + ': ' + sequence['error'])
                self.progress.set_fraction(sequence['completed_tests']/len(sequence['tests']))
                self.progress.set_text(sequence['state'].capitalize())
            if self.run_error:
                self.status.set_text(self.run_error)

    def start_run(self):
        self.tabs.set_current_page(self.tabs.page_num(self.run_page))
        self.saving = True
        self.run_error = ''
        self.status.set_text('Starting experiment…')
        self.update_ready()
        def completed(result):
            self.saving = False
            if result['state'] == 'failed':
                self.update_ready()
                self.run_error = result.get('error', 'Unable to start experiment')
                self.status.set_text(self.run_error)
            else:
                self.sequence_id = result['result']['id']
                self.history_signature = None
                self.update(self.state)
        self.owner.command('start_experiment_sequence', experiment=self.definition['id'],
            tests=[dict(test=identity, parameters=self.parameters_for_test(identity)) for identity in self.selected_tests()],
            bindings={role: picker.get_active_id() for role, picker in self.pickers.items() if picker.get_active_id()},
            enable_outputs=self.needs_outputs(), wiring_confirmed=self.wiring.get_active(), on_complete=completed)
