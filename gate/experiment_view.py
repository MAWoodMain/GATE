"""GNOME settings-style setup, progress and results for an experiment."""
import shutil
from pathlib import Path
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gio, GLib, Gtk
from gate.procedures import PROCEDURES, parameter_specs, test_spec, METRICS, control_roles


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
        self.run_error = ''
        self.tabs = Gtk.Notebook()
        self.setup_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=28, margin_top=20)
        self.result_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20, margin_top=20)
        self.tabs.append_page(self.setup_page, Gtk.Label(label='Setup & Run'))
        self.tabs.append_page(self.result_page, Gtk.Label(label='Results'))
        self.append(self.tabs)
        self.shown_results = None
        _, roles = owner.section('Choose Equipment', 'Assign a connected instrument to each functional role.', self.setup_page)
        for role, requirement in definition['roles'].items():
            picker = Gtk.ComboBoxText(hexpand=True)
            picker.update_property([Gtk.AccessibleProperty.LABEL], [requirement['name']])
            picker.connect('changed', lambda *_: self.update_ready())
            details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            status = owner.label('Waiting for equipment…', 'dim-label')
            details.append(status)
            details.append(picker)
            roles.append(owner.setting_row(requirement['name'], details))
            self.pickers[role], self.role_status[role] = picker, status
        self.demo = owner.button('Add Simulated Converter Bench', lambda: owner.command('demo_experiment'))
        self.demo.set_halign(Gtk.Align.START)
        self.setup_page.append(self.demo)
        _, settings = owner.section('Test & Parameters', 'The procedure determines what to measure and how to present the results.', self.setup_page)
        self.test_picker = Gtk.ComboBoxText(hexpand=True)
        for identity in definition['tests']:
            kind = test_spec(definition, identity)['procedure']
            self.test_picker.append(identity, identity.replace('_', ' ').capitalize() + ' · ' + PROCEDURES[kind])
        self.test_picker.set_active(0)
        settings.append(owner.setting_row('Test', self.test_picker))
        self.parameter_rows = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, show_separators=True)
        self.parameter_rows.add_css_class('settings-list')
        self.setup_page.append(self.parameter_rows)
        self.metrics = owner.label('', 'dim-label')
        self.setup_page.append(self.metrics)
        self.wiring = Gtk.CheckButton(label='I have checked the DUT wiring and the configured voltage, current and power limits.')
        self.wiring.connect('toggled', lambda *_: self.update_ready())
        self.setup_page.append(self.wiring)
        actions = Gtk.Box(spacing=8)
        self.start = owner.button('Run', self.start_run)
        self.start.add_css_class('suggested-action')
        self.cancel = owner.button('Stop Experiment', lambda: owner.command('cancel_experiment'))
        self.cancel.add_css_class('destructive-action')
        actions.append(self.start)
        actions.append(self.cancel)
        self.setup_page.append(actions)
        self.status = owner.label('Waiting for service…', 'dim-label')
        self.append(self.status)
        self.progress = Gtk.ProgressBar(show_text=True)
        self.append(self.progress)
        _, results = owner.section('Results', 'Each run retains its equipment, parameters, raw readings, calculated metrics and charts.', self.result_page)
        self.history_picker = Gtk.ComboBoxText(hexpand=True)
        self.history_picker.connect('changed', lambda *_: self.show_result())
        results.append(self.history_picker)
        self.results = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.result_page.append(self.results)
        self.test_picker.connect('changed', self.choose_test)
        self.choose_test()
        self.offline()

    def kind(self):
        return test_spec(self.definition, self.test_picker.get_active_id())['procedure']

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
        for key, (label, default, low, high, step, integer) in parameter_specs(self.kind()).items():
            spin = Gtk.SpinButton.new_with_range(low, high, step)
            spin.set_digits(0 if integer else 3)
            if key == 'settle_s':
                default = max(default, self.state.get('interval_s', 1.))
            spin.set_value(self.parameter_drafts.get(self.selected_test, {}).get(key, default))
            spin.update_property([Gtk.AccessibleProperty.LABEL], [label])
            self.parameter_rows.append(self.owner.setting_row(label, self.owner.label('', 'dim-label'), spin))
            self.parameters[key] = spin
        capture = self.kind() == 'capture'
        self.wiring.set_visible(not capture)
        self.wiring.set_active(False)
        self.start.set_label('Start Capture' if capture else 'Run & Enable Outputs')
        self.device_signature = None
        if self.state:
            self.update(self.state)
        self.update_metrics()
        self.update_ready()

    def update_metrics(self):
        if self.kind() == 'capture':
            names = []
            for role, picker in self.pickers.items():
                device = next((item for item in self.state.get('instruments', []) if item['id'] == picker.get_active_id()), {})
                for reading in device.get('readings', []):
                    if reading.get('quality') != 'disabled':
                        names.append(self.definition['roles'][role]['name'] + ' · ' + reading['channel'] +
                                     (f" ({reading['unit']})" if reading['unit'] else ''))
            self.metrics.set_text('Records current instrument readings on each polling cycle: ' +
                                  (', '.join(names) if names else 'choose equipment to see its current reading channels') +
                                  '. Output: a time-series chart for each metric, a table and CSV.')
        else:
            self.metrics.set_text('Records input/output voltage and current, input/output power, efficiency and regulation status. ' +
                ('Output: efficiency versus load current, grouped by input voltage, with a table and CSV.' if self.kind() == 'efficiency' else
                 'Output: maximum acceptable current versus input voltage, with regulation thresholds and sweep-limit bounds, a table and CSV.'))

    def update(self, state):
        self.state = state
        signature = [(item['id'], item['name'], item.get('error', ''), tuple(item.get('experiment_capabilities', [])), item.get('experiment_control_supported', False))
                     for item in state.get('instruments', [])]
        if signature != self.device_signature:
            self.device_signature = signature
            for role, picker in self.pickers.items():
                chosen = picker.get_active_id()
                required = set(self.definition['roles'][role]['capabilities'])
                candidates = [item for item in state.get('instruments', []) if not item.get('error') and
                              required <= set(item.get('experiment_capabilities', [])) and
                              (self.kind() == 'capture' or not required.intersection({'source.dc_voltage', 'sink.dc_current'}) or item.get('experiment_control_supported', False))]
                picker.remove_all()
                for device in candidates:
                    picker.append(device['id'], device['name'])
                if chosen and chosen not in {item['id'] for item in candidates}:
                    picker.append(chosen, 'Unavailable · ' + chosen)
                if chosen:
                    picker.set_active_id(chosen)
                elif len(candidates) == 1:
                    picker.set_active(0)
                self.role_status[role].set_text('Choose equipment' if candidates else 'No connected equipment provides the required functions and controls')
        self.update_metrics()
        current = state.get('experiment_run')
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
        if current and current['id'] == self.run_id and current['state'] != 'running' and self.shown_results != current['id']:
            self.shown_results = current['id']
            self.tabs.set_current_page(1)
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
        if result['procedure'] == 'maximum_output_current':
            self.results.append(self.owner.label('“At least sweep limit” means the DUT was still in regulation at the highest configured current; the true maximum was not reached.', 'dim-label'))
        rows = result.get('summary', []) if result['procedure'] == 'maximum_output_current' else result.get('points', [])
        if rows:
            columns = list(dict.fromkeys(key for row in rows for key in row))
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
        actions = Gtk.Box(spacing=8)
        if Path(result.get('report', '')).is_file():
            actions.append(self.owner.button('Open Full Report', lambda: self.open_report(result['report'])))
            actions.append(self.owner.button('Export CSV…', lambda: self.export_csv(result)))
        if result.get('path'):
            actions.append(self.owner.button('Open Results Folder', lambda: self.open_file(str(Path(result['path']).parent))))
        self.results.append(actions)

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

    def export_csv(self, result):
        chooser = Gtk.FileChooserNative(title='Export Experiment Results', transient_for=self.owner,
            action=Gtk.FileChooserAction.SAVE, accept_label='Export', cancel_label='Cancel')
        chooser.set_current_name(result['id'] + '.csv')
        def response(dialog, action):
            if action == Gtk.ResponseType.ACCEPT:
                try:
                    source = result['summary_csv'] if result['procedure'] == 'maximum_output_current' else result['csv']
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
        self.status.set_text('Start the background service in Devices & Settings to run experiments.')

    def update_ready(self):
        if not hasattr(self, 'start'):
            return
        active = self.state.get('experiment_run')
        running = bool(active and active['state'] == 'running')
        selected = {role: picker.get_active_id() for role, picker in self.pickers.items()}
        available = {item['id']: item for item in self.state.get('instruments', []) if not item.get('error')}
        for role, identity in selected.items():
            device = available.get(identity, {})
            limits = device.get('experiment_limits', {})
            if limits:
                voltage, current = limits.get('voltage', []), limits.get('current', [])
                if len(voltage) == 2 and len(current) == 2:
                    self.role_status[role].set_text(f"Driver limits: {voltage[0]:g}–{voltage[1]:g} V · {current[0]:g}–{current[1]:g} A · {limits.get('power_w', 0):g} W")
            elif device:
                self.role_status[role].set_text('Records the instrument’s current readings' if self.kind() == 'capture' else 'Choose equipment')
        compatible = all(identity in available and set(self.definition['roles'][role]['capabilities']) <=
                         set(available[identity].get('experiment_capabilities', [])) for role, identity in selected.items())
        distinct = len(set(selected.values())) == len(selected)
        ready = compatible and distinct and (self.kind() == 'capture' or self.wiring.get_active())
        procedure_error = ''
        if self.kind() != 'capture':
            try:
                control_roles(self.definition)
            except ValueError as exc:
                procedure_error = str(exc)
            for role, identity in selected.items():
                if set(self.definition['roles'][role]['capabilities']).intersection({'source.dc_voltage', 'sink.dc_current'}):
                    ready = ready and available.get(identity, {}).get('experiment_control_supported', False)
        ready = ready and not procedure_error
        supported = self.state.get('experiment_execution_version') == 1
        self.start.set_sensitive(self.owner.online and supported and ready and not running and not self.saving)
        self.cancel.set_sensitive(self.owner.online and running and active['experiment'] == self.definition['id'])
        self.demo.set_sensitive(self.owner.online and supported and not running)
        self.test_picker.set_sensitive(not running and not self.saving)
        self.parameter_rows.set_sensitive(not running and not self.saving)
        self.wiring.set_sensitive(not running and not self.saving)
        for picker in self.pickers.values():
            picker.set_sensitive(not running and not self.saving)
        if running:
            self.status.set_text(('SIMULATED · ' if active['simulated'] else '') + 'Running ' + active['name'] +
                                 f" · {active['completed_points']} of up to {active['total_points']} points")
            self.progress.set_fraction(min(1., active['completed_points']/active['total_points']))
            self.progress.set_text(active['state'].capitalize())
        elif not supported and self.owner.online:
            self.status.set_text('Restart the background service in Devices & Settings to load experiment execution support.')
        elif self.owner.online and not self.saving:
            result = self.selected_result()
            self.status.set_text(procedure_error or ('Ready to run.' if ready else 'Choose compatible, distinct equipment for each role and confirm the wiring.' if self.kind() != 'capture' else 'Choose compatible, distinct equipment for each role.') +
                                 (' Last run: '+result['state'] + (': '+result['error'] if result['error'] else '') if result else ''))
            self.progress.set_fraction(1. if result and result['state'] == 'completed' else 0.)
            self.progress.set_text(result['state'].capitalize() if result else 'Not started')
            if self.run_error:
                self.status.set_text(self.run_error)

    def start_run(self):
        self.tabs.set_current_page(0)
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
                self.run_id = result['result']['id']
                self.history_signature = None
                self.update(self.state)
        self.owner.command('start_experiment', experiment=self.definition['id'], test=self.test_picker.get_active_id(),
            bindings={role: picker.get_active_id() for role, picker in self.pickers.items()},
            parameters={key: int(spin.get_value()) if parameter_specs(self.kind())[key][5] else spin.get_value()
                        for key, spin in self.parameters.items()},
            enable_outputs=self.kind() != 'capture', wiring_confirmed=self.wiring.get_active(), on_complete=completed)
