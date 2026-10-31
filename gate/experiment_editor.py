"""Structured GTK experiment creation, independent of physical instruments."""
import copy
import re
import uuid
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk
from gate.experiments import BUCK_CONVERTER, validate_definition
from gate.procedures import PROCEDURES, test_spec, load_template
from gate.test_builder import TestBuilder

CAPABILITIES = {
    'measure.temperature': 'Temperature measurements',
    'capture.waveform': 'Waveform capture',
    'measure.scalar': 'Capture current instrument readings',
    'source.dc_voltage': 'Variable DC voltage source',
    'sink.dc_current': 'DC current load',
    'measure.dc_voltage': 'DC voltage measurement',
    'measure.dc_current': 'DC current measurement',
}


def identifier(text):
    value = re.sub(r'[^a-z0-9]+', '_', text.strip().lower()).strip('_')
    if not value or not value[0].isalpha() or len(value) > 64:
        raise ValueError('Use names starting with a letter, up to 64 letters, numbers or spaces.')
    return value


def terminals(text, optional=False):
    if optional and not text.strip():
        return []
    values = [identifier(item) for item in text.split(',')]
    if len(set(values)) != len(values):
        raise ValueError('Terminal names must be distinct within each device.')
    return values


class ExperimentEditor(Gtk.Dialog):
    def __init__(self, owner):
        super().__init__(title='New Experiment', transient_for=owner, modal=True,
                         default_width=740, default_height=720, use_header_bar=True)
        self.owner = owner
        self.saving = False
        self.identity = 'experiment_' + uuid.uuid4().hex
        self.roles, self.wires, self.tests = [], [], []
        self.add_button('Cancel', Gtk.ResponseType.CANCEL)
        self.save_button = self.add_button('Create', Gtk.ResponseType.ACCEPT)
        self.save_button.add_css_class('suggested-action')
        self.connect('response', self.respond)
        self.connect('close-request', lambda *_: self.saving)
        root = self.get_content_area()
        root.set_spacing(12)
        for setter in (root.set_margin_top, root.set_margin_bottom, root.set_margin_start, root.set_margin_end):
            setter(20)
        self.tabs = Gtk.Notebook(vexpand=True)
        root.append(self.tabs)
        self.general = self.page('Overview')
        self.name = self.field(self.general, 'Experiment name', 'e.g. Converter characterisation')
        self.description = self.field(self.general, 'Purpose', 'What will this arrangement be used to test?')
        self.dut_name = self.field(self.general, 'Device under test', 'e.g. Buck converter')
        self.dut_terminals = self.field(self.general, 'DUT terminals', 'e.g. Input positive, Input negative, Output positive, Output negative')
        self.general.append(owner.label('Separate terminal names with commas. Use the same names when describing the wiring.', 'dim-label'))
        self.general.append(owner.button('Use Buck Converter Example', self.load_example))
        self.equipment = self.page('Equipment')
        self.equipment.append(owner.label('Define roles by function. Actual instruments can be chosen later.', 'dim-label'))
        self.role_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.equipment.append(self.role_box)
        self.equipment.append(owner.button('Add Equipment Role', self.add_role))
        self.wiring = self.page('Wiring')
        self.wiring.append(owner.label('Choose the two terminals joined by each wire. Every declared terminal must be connected.', 'dim-label'))
        self.wire_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.wiring.append(self.wire_box)
        self.wiring.append(owner.button('Add Connection', self.add_wire))
        self.test_page = self.page('Tests')
        self.test_page.append(owner.label('Describe tests that share this arrangement, including what to vary and measure.', 'dim-label'))
        self.test_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.test_page.append(self.test_box)
        self.test_page.append(owner.button('Add Test', self.add_test))
        self.error = owner.label('', 'error')
        self.error.set_visible(False)
        root.append(self.error)
        self.tabs.connect('switch-page', self.switch_page)
        self.add_role()
        self.add_test()

    def page(self, title):
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_top=16, margin_bottom=16, margin_start=8, margin_end=8)
        scroll.set_child(box)
        self.tabs.append_page(scroll, Gtk.Label(label=title))
        return box

    def field(self, box, title, placeholder='', value=''):
        box.append(self.owner.label(title, 'device-title'))
        entry = Gtk.Entry(placeholder_text=placeholder, text=value, hexpand=True)
        entry.update_property([Gtk.AccessibleProperty.LABEL], [title])
        box.append(entry)
        return entry

    def card(self, parent, collection):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        parent.append(box)
        item = {'box': box}
        collection.append(item)
        remove = self.owner.icon_button('user-trash-symbolic', 'Remove',
            lambda: (parent.remove(box), collection.remove(item)))
        remove.set_halign(Gtk.Align.END)
        box.append(remove)
        return item

    def add_role(self, definition=None, identity=None):
        definition = definition or {}
        item = self.card(self.role_box, self.roles)
        item['id'] = identity or 'equipment_' + str(len(self.roles))
        item['name'] = self.field(item['box'], 'Role name', 'e.g. Input supply', definition.get('name', ''))
        item['key'] = self.field(item['box'], 'Role identifier for test JSON', 'e.g. supply', item['id'])
        item['optional'] = Gtk.CheckButton(label='Optional observation equipment (may be unassigned)', active=definition.get('optional', False))
        item['box'].append(item['optional'])
        item['terminals'] = self.field(item['box'], 'Terminals', 'e.g. Positive, Negative',
                                      ', '.join(value.replace('_', ' ') for value in definition.get('terminals', [])))
        item['checks'] = {}
        for capability, label in CAPABILITIES.items():
            check = Gtk.CheckButton(label=label, active=capability in definition.get('capabilities', []))
            item['box'].append(check)
            item['checks'][capability] = check
        item['extra'] = self.field(item['box'], 'Other required functions (optional)', 'Comma-separated capability names',
            ', '.join(cap for cap in definition.get('capabilities', []) if cap not in CAPABILITIES))
        return item

    def endpoints(self):
        entries = []
        components = [('dut', self.dut_name, self.dut_terminals, False)] + [
            (role['key'].get_text().strip(), role['name'], role['terminals'], role['optional'].get_active()) for role in self.roles]
        for identity, name, field, optional in components:
            for terminal in terminals(field.get_text(), optional):
                entries.append((identity + '.' + terminal, name.get_text().strip() + ' · ' + terminal.replace('_', ' ')))
        return entries

    def refresh_wires(self):
        endpoints = self.endpoints()
        for wire in self.wires:
            for side in ('from', 'to'):
                picker = wire[side]
                chosen = picker.get_active_id()
                picker.remove_all()
                for key, label in endpoints:
                    picker.append(key, label)
                if chosen:
                    picker.set_active_id(chosen)

    def switch_page(self, notebook, page, number):
        if number == 2:
            try:
                self.refresh_wires()
                self.show_error('')
            except ValueError as exc:
                self.show_error('Complete the DUT and equipment terminal names first. ' + str(exc))

    def add_wire(self, endpoints=None):
        try:
            choices = self.endpoints()
        except ValueError as exc:
            self.show_error('Complete the DUT and equipment terminal names first. ' + str(exc))
            return
        item = self.card(self.wire_box, self.wires)
        for index, side in enumerate(('from', 'to')):
            item['box'].append(self.owner.label('From' if side == 'from' else 'To', 'device-title'))
            picker = Gtk.ComboBoxText(hexpand=True)
            picker.update_property([Gtk.AccessibleProperty.LABEL], ['Connection ' + side])
            for key, label in choices:
                picker.append(key, label)
            if endpoints:
                picker.set_active_id(endpoints[index])
            item['box'].append(picker)
            item[side] = picker
        return item

    def add_test(self, name='', description='', spec=None):
        item = self.card(self.test_box, self.tests)
        item['name'] = self.field(item['box'], 'Test name', 'e.g. Efficiency sweep', name)
        item['box'].append(self.owner.label('Starting template', 'device-title'))
        item['procedure'] = Gtk.ComboBoxText()
        for key, label in PROCEDURES.items():
            item['procedure'].append(key, label)
        item['procedure'].append('custom', 'Custom JSON')
        item['procedure'].set_active_id('custom' if spec else 'capture')
        item['box'].append(item['procedure'])
        item['box'].append(self.owner.label('Description', 'device-title'))
        item['description'] = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR,
                                           top_margin=8, bottom_margin=8, left_margin=8, right_margin=8)
        item['description'].get_buffer().set_text(description)
        item['description'].update_property([Gtk.AccessibleProperty.LABEL], ['Test procedure'])
        scroll = Gtk.ScrolledWindow(min_content_height=120)
        scroll.add_css_class('frame')
        scroll.set_child(item['description'])
        item['box'].append(scroll)
        item['builder'] = TestBuilder(spec or load_template('capture'),
            [role['key'].get_text().strip() for role in self.roles])
        item['specification'] = item['builder'].raw
        item['box'].append(item['builder'])
        def template_changed(picker):
            kind = picker.get_active_id()
            if kind == 'custom':
                return
            try:
                roles = {role['key'].get_text().strip(): {'capabilities':[key for key, check in role['checks'].items() if check.get_active()]}
                         for role in self.roles}
                template = load_template(kind, roles)
                item['builder'].set_spec(template)
                self.show_error('')
            except ValueError as exc:
                self.show_error(str(exc))
        item['procedure'].connect('changed', template_changed)
        return item

    def load_example(self):
        example = copy.deepcopy(BUCK_CONVERTER)
        for collection, parent in ((self.roles, self.role_box), (self.wires, self.wire_box), (self.tests, self.test_box)):
            for item in collection:
                parent.remove(item['box'])
            collection.clear()
        self.name.set_text(example['name'])
        self.description.set_text(example['description'])
        self.dut_name.set_text(example['dut']['name'])
        self.dut_terminals.set_text(', '.join(value.replace('_', ' ') for value in example['dut']['terminals']))
        for identity, role in example['roles'].items():
            self.add_role(role, identity)
        for wire in example['connections']:
            self.add_wire(wire)
        for name in example['tests']:
            spec = test_spec(example, name)
            self.add_test(name.replace('_', ' ').capitalize(), spec['description'], spec)
        self.show_error('')

    def definition(self):
        self.refresh_wires()
        roles, tests = {}, {}
        for role in self.roles:
            capabilities = [key for key, check in role['checks'].items() if check.get_active()]
            capabilities += [item.strip() for item in role['extra'].get_text().split(',') if item.strip()]
            role_id = role['key'].get_text().strip()
            if role_id in roles:
                raise ValueError('Choose distinct role identifiers')
            roles[role_id] = dict(name=role['name'].get_text().strip(), capabilities=capabilities,
                                     terminals=terminals(role['terminals'].get_text(), role['optional'].get_active()))
            if role['optional'].get_active():
                roles[role_id]['optional'] = True
        for test in self.tests:
            key = identifier(test['name'].get_text())
            if key in tests:
                raise ValueError('Choose distinct test names.')
            buffer = test['description'].get_buffer()
            description = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False).strip()
            specification = test['builder'].get_spec()
            if description:
                specification['description'] = description
            tests[key] = specification
        return validate_definition(dict(id=self.identity, name=self.name.get_text().strip(),
            description=self.description.get_text().strip(),
            dut=dict(name=self.dut_name.get_text().strip(), terminals=terminals(self.dut_terminals.get_text())),
            roles=roles, connections=[[wire['from'].get_active_id(), wire['to'].get_active_id()] for wire in self.wires], tests=tests))

    def show_error(self, text):
        self.error.set_text(text)
        self.error.set_visible(bool(text))

    def respond(self, dialog, response):
        if self.saving:
            return
        if response != Gtk.ResponseType.ACCEPT:
            self.destroy()
            return
        try:
            definition = self.definition()
            if not self.owner.online:
                raise ValueError('Start the background service in Devices & Settings before creating an experiment.')
            if not self.owner.experiments_supported:
                raise ValueError('The running background service does not support experiments. Use Restart in Devices & Settings to load the updated service, then try Create again.')
        except ValueError as exc:
            self.show_error(str(exc))
            return
        self.saving = True
        self.tabs.set_sensitive(False)
        self.save_button.set_sensitive(False)
        self.get_widget_for_response(Gtk.ResponseType.CANCEL).set_sensitive(False)
        self.show_error('Saving…')
        def completed(result):
            self.saving = False
            if result['state'] == 'succeeded':
                row = self.owner.experiment_rows.get(self.identity)
                if row:
                    self.owner.navigation.select_row(row)
                self.destroy()
            else:
                self.tabs.set_sensitive(True)
                self.save_button.set_sensitive(True)
                self.get_widget_for_response(Gtk.ResponseType.CANCEL).set_sensitive(True)
                error = result.get('error', 'Unable to save experiment.')
                if error == 'Unknown action':
                    error = 'The running background service does not support experiments. Use Restart in Devices & Settings to load the updated service, then try Create again.'
                self.show_error(error)
        self.owner.command('save_experiment', experiment=definition, on_complete=completed)
