"""Schema-backed GTK forms for execution and result presentation settings."""
import copy
import json
from pathlib import Path

import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, Gdk

SCHEMA = json.loads((Path(__file__).parent / 'templates/test-v1.schema.json').read_text())
PRESENTATION = {'plots', 'table', 'notes'}


def resolve(schema):
    if '$ref' in schema:
        return SCHEMA['$defs'][schema['$ref'].rsplit('/', 1)[1]]
    return schema


def initial(schema):
    schema = resolve(schema)
    if 'const' in schema:
        return schema['const']
    if 'enum' in schema:
        return schema['enum'][0]
    if 'anyOf' in schema and any(option.get('type') == 'null' for option in schema['anyOf']):
        return None
    kind = schema.get('type')
    if kind == 'object':
        return {key: initial(schema['properties'][key]) for key in schema.get('required', [])}
    if kind == 'array':
        return []
    if kind in ('number', 'integer'):
        return schema.get('minimum', 0)
    if kind == 'boolean':
        return False
    return ''


TITLES = {
    'kind': 'Style', 'title': 'Chart title', 'columns': 'Choose columns', 'stage': 'When to check', 'stop_when': 'End the test early',
    'reduce': 'Group results', 'label': 'Display name', 'unit': 'Units', 'id': 'Formula name',
    'role': 'Equipment', 'control': 'Setting', 'before_point': 'Before each point',
    'derived': 'Calculated results', 'outputs': 'Enabled equipment',
    'limits': 'Operating limits', 'settle_s': 'Wait before measuring (s)',
    'samples': 'Readings per point', 'repeat': 'Capture points',
    'capture_all': 'Include all instrument readings', 'on_invalid': 'Missing readings',
    'aggregate': 'Calculate using', 'required': 'Stop if unavailable',
    'data': 'Dataset', 'x': 'Horizontal axis', 'y': 'Vertical axis',
    'series': 'Separate curves by', 'x_scale': 'Horizontal scale', 'y_scale': 'Vertical scale',
    'x_label': 'Horizontal axis label', 'y_label': 'Vertical axis label',
    'quantity': 'Measurement', 'channel': 'Instrument channel',
    'start': 'From', 'stop': 'To', 'step': 'Increment', 'default': 'Starting value',
    'min': 'Minimum', 'max': 'Maximum', 'type': 'Value type',
}
CHOICES = {'record': 'Keep unavailable readings', 'fail': 'Stop the test',
           'point': 'Averaged point readings', 'sample_mean': 'Each reading, then average',
           'points': 'Measured points', 'summary': 'Grouped results', 'secondary': 'Secondary observations',
           'number': 'Decimal number', 'integer': 'Whole number',
           'line': 'Line chart', 'scatter': 'Scatter plot', 'linear': 'Linear', 'log': 'Logarithmic'}
SINGULAR = {'parameters': 'parameter', 'axes': 'sweep', 'measurements': 'measurement',
            'derived': 'calculation', 'checks': 'condition', 'plots': 'chart',
            'setup': 'setup action', 'before_point': 'action', 'outputs': 'equipment role',
            'limits': 'equipment limit', 'notes': 'note', 'columns': 'column',
            'secondary_measurements': 'observation', 'waveforms': 'waveform capture'}


def caption(name):
    return TITLES.get(name, name.replace('_', ' ').capitalize())


def label(text, css=None):
    widget = Gtk.Label(label=text, xalign=0, wrap=True)
    if css:
        widget.add_css_class(css)
    return widget


def direct_children(widget):
    child = widget.get_first_child()
    while child:
        yield child
        child = child.get_next_sibling()


class FormValue(Gtk.Box):
    """A recursive form; serialization preserves omitted options and JSON types."""
    def __init__(self, schema, value, name='', roles=()):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.schema = schema = resolve(schema)
        self.original_value = copy.deepcopy(value)
        self.roles = roles
        self.children = {}
        self.rows = []
        self.name = name
        self.kind = schema.get('type')
        if self.kind == 'object' and not isinstance(value, dict):
            raise ValueError((name or 'Object') + ' must be an object')
        if self.kind == 'array' and not isinstance(value, list):
            raise ValueError((name or 'Collection') + ' must be an array')
        if 'anyOf' in schema and any(option.get('type') == 'object' for option in schema['anyOf']):
            self.kind = 'nullable_object'
            self.enabled = Gtk.CheckButton(label='Configure', active=value is not None)
            self.append(self.enabled)
            option = next(option for option in schema['anyOf'] if option.get('type') == 'object')
            self.nested = FormValue(option, value if value is not None else initial(option), roles=roles)
            self.append(self.nested)
            self.nested.set_sensitive(value is not None)
            self.enabled.connect('toggled', lambda button: self.nested.set_sensitive(button.get_active()))
        elif self.kind == 'object' and 'properties' in schema:
            optional = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            for key, child_schema in schema['properties'].items():
                required = key in schema.get('required', [])
                resolved = resolve(child_schema)
                complex_field = resolved.get('type') in ('object', 'array')
                box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL if complex_field else Gtk.Orientation.HORIZONTAL,
                              spacing=12)
                heading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                heading.set_size_request(155, -1)
                heading.set_valign(Gtk.Align.START if complex_field else Gtk.Align.CENTER)
                if required:
                    heading.append(label(caption(key)))
                    toggle = None
                else:
                    toggle = Gtk.CheckButton(label=caption(key), active=key in value)
                    heading.append(toggle)
                box.append(heading)
                field = FormValue(child_schema, value[key] if key in value else initial(child_schema), key, roles)
                field.set_hexpand(True)
                if toggle:
                    field.set_visible(toggle.get_active())
                    toggle.connect('toggled', lambda button, field=field: field.set_visible(button.get_active()))
                box.append(field)
                (self if required else optional).append(box)
                self.children[key] = (toggle, field)
            if optional.get_first_child():
                options = Gtk.Expander(label='More options', expanded=False)
                options.set_child(optional)
                self.append(options)
            # Common alternatives are a single choice, rather than independent flags.
            if {'start', 'stop', 'step', 'values'} <= set(self.children):
                self.alternative('Sweep points', [('Evenly spaced', ('start', 'stop', 'step')),
                                                 ('Specific values', ('values',))], 'values' in value)
                self.drive_equipment('role' in value)
            if {'quantity', 'channel'} <= set(self.children):
                self.alternative('Read from', [('Standard measurement', ('quantity',)),
                                             ('Instrument channel', ('channel',))], 'channel' in value)
        elif self.kind in ('object', 'array'):
            self.items = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            self.append(self.items)
            for key, item in (value.items() if self.kind == 'object' else enumerate(value)):
                self.add_row(item, str(key), expanded=False)
            self.empty = label('No ' + SINGULAR.get(name, 'item') + 's yet.', 'dim-label')
            self.append(self.empty)
            self.empty.set_visible(not self.rows)
            add = Gtk.Button(label='+ Add ' + SINGULAR.get(name, 'item'))
            add.set_halign(Gtk.Align.START)
            add.connect('clicked', lambda *_: self.add_row(self.new_item(), self.new_key()))
            self.append(add)
            self.undo = Gtk.Button(label='Undo removal', halign=Gtk.Align.START, visible=False)
            self.undo.connect('clicked', self.undo_remove)
            self.append(self.undo)
        elif 'enum' in schema:
            self.entry = Gtk.ComboBoxText()
            for option in schema['enum']:
                names = {'sample': 'Each reading', 'point': 'After each point', 'continuous': 'Throughout the test'} if name == 'stage' else CHOICES
                self.entry.append(str(option), names.get(str(option), caption(str(option))))
            self.entry.set_active_id(str(value))
            self.append(self.entry)
        elif name == 'description':
            self.entry = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=8, bottom_margin=8, left_margin=8, right_margin=8)
            self.entry.get_buffer().set_text(value)
            scroll = Gtk.ScrolledWindow(min_content_height=100)
            scroll.add_css_class('frame')
            scroll.set_child(self.entry)
            self.append(scroll)
        elif self.kind == 'boolean':
            self.entry = Gtk.Switch(active=value, halign=Gtk.Align.START)
            self.append(self.entry)
        else:
            # Expressions are entered as numbers, true/false/null, or bare formula text.
            self.original = copy.deepcopy(value)
            self.original_text = value if isinstance(value, str) else json.dumps(value)
            self.entry = Gtk.Entry(text=self.original_text, hexpand=True)
            self.entry.set_width_chars(12)
            if name in ('role', 'x', 'y', 'series', 'quantity', 'control', 'column'):
                self.picker = Gtk.ComboBoxText.new_with_entry()
                self.entry = self.picker.get_child()
                self.entry.set_text(self.original_text)
                self.picker.set_hexpand(True)
                self.append(self.picker)
                self.suggest(list(roles) if name == 'role' else ())
            self.entry.update_property([Gtk.AccessibleProperty.LABEL], [caption(name) or 'Value'])
            if 'anyOf' in schema:
                self.entry.set_placeholder_text('Number or expression, e.g. start_v + 1')
            if name == 'role':
                self.entry.set_tooltip_text('Equipment roles: ' + ', '.join(roles))
            if not hasattr(self, 'picker'):
                self.append(self.entry)
        if schema.get('description'):
            self.set_tooltip_text(schema['description'])

    def item_schema(self):
        return self.schema.get('items', self.schema.get('additionalProperties', {}))

    def suggest(self, choices):
        if not hasattr(self, 'picker'):
            return
        text = self.picker.get_active_id() or self.entry.get_text()
        self.picker.remove_all()
        entries = choices.items() if isinstance(choices, dict) else ((key, key) for key in choices)
        keys = []
        for key, name in entries:
            self.picker.append(key, name)
            keys.append(key)
        if text in keys:
            self.picker.set_active_id(text)
        else:
            self.entry.set_text(text)

    def alternative(self, title, choices, second):
        bar = Gtk.Box(spacing=12)
        heading = label(title)
        heading.set_size_request(155, -1)
        bar.append(heading)
        picker = Gtk.ComboBoxText(hexpand=True)
        for index, (name, _) in enumerate(choices):
            picker.append(str(index), name)
        bar.append(picker)
        self.prepend(bar)
        def changed(*_):
            selected = picker.get_active()
            for index, (_, keys) in enumerate(choices):
                for key in keys:
                    toggle, field = self.children[key]
                    toggle.set_active(index == selected)
                    box = field.get_parent()
                    if box.get_parent() != self:
                        box.get_parent().remove(box)
                        self.append(box)
                    if toggle.get_visible():
                        toggle.set_visible(False)
                        toggle.get_parent().append(label(caption(key)))
                    box.set_visible(index == selected)
        picker.set_active(int(second))
        changed()
        picker.connect('changed', changed)
        for child in list(direct_children(self)):
            if isinstance(child, Gtk.Expander) and child.get_child() and child.get_child().get_first_child() is None:
                self.remove(child)
        # Choosing a mode retains inactive values for switching back.
        self.mode_picker = picker

    def drive_equipment(self, active):
        enabled = Gtk.CheckButton(label='Apply sweep values to equipment', active=active)
        self.append(enabled)
        for key in ('role', 'control'):
            toggle, field = self.children[key]
            box = field.get_parent()
            box.get_parent().remove(box)
            self.append(box)
            toggle.set_visible(False)
            toggle.get_parent().append(label(caption(key)))
        def changed(*_):
            for key in ('role', 'control'):
                toggle, field = self.children[key]
                toggle.set_active(enabled.get_active())
                field.get_parent().set_visible(enabled.get_active())
            if enabled.get_active() and self.roles:
                field = self.children['role'][1]
                if not field.entry.get_text():
                    field.entry.set_text(next(iter(self.roles)))
        changed()
        enabled.connect('toggled', changed)
        self.drive_toggle = enabled

    def new_key(self):
        if self.kind != 'object':
            return ''
        used = {entry.get_text() for _, entry, _ in self.rows}
        base = SINGULAR.get(self.name, 'item').replace(' ', '_')
        index = 1
        while f'{base}_{index}' in used:
            index += 1
        return f'{base}_{index}'

    def new_item(self):
        value = initial(self.item_schema())
        if not isinstance(value, dict):
            return value
        if 'label' in value:
            value['label'] = 'New ' + SINGULAR.get(self.name, 'item')
        if self.name == 'parameters':
            value.update(default=1, min=0, max=100, step=1)
        elif self.name == 'axes':
            value.update(id='sweep_' + str(len(self.rows) + 1), start=0, stop=10, step=1)
        elif self.name == 'plots':
            value.update(title='New chart', x='elapsed_s', y='')
        if 'role' in value and self.roles:
            value['role'] = next(iter(self.roles))
        if self.name in ('measurements', 'secondary_measurements'):
            value.update(quantity='voltage', unit='V', required=True)
        return value

    def add_row(self, value, key='', expanded=True):
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                      margin_top=2, margin_bottom=6)
        row.add_css_class('builder-card')
        header = Gtk.Box(spacing=6)
        row.append(header)
        expander = Gtk.Expander(hexpand=True, expanded=expanded)
        heading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        title = label('')
        title.add_css_class('heading')
        detail = label('', 'dim-label')
        heading.append(title)
        heading.append(detail)
        expander.set_label_widget(heading)
        header.append(expander)
        key_entry = None
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                       margin_top=12, margin_bottom=8, margin_start=12, margin_end=12)
        if self.kind == 'object':
            key_entry = Gtk.Entry(text=key, placeholder_text='Name used in formulas', hexpand=True)
            key_entry.update_property([Gtk.AccessibleProperty.LABEL], ['Formula name'])
            key_row = Gtk.Box(spacing=12)
            key_label = label('Formula name')
            key_label.set_size_request(155, -1)
            key_row.append(key_label)
            key_row.append(key_entry)
            body.append(key_row)
        field = FormValue(self.item_schema(), value, 'role' if self.name == 'outputs' else 'column' if self.name == 'columns' else '', roles=self.roles)
        item = (row, key_entry, field)
        for icon, tooltip, callback in [('go-up-symbolic', 'Move earlier', lambda *_: self.move(item, -1)),
                                        ('go-down-symbolic', 'Move later', lambda *_: self.move(item, 1)),
                                        ('user-trash-symbolic', 'Remove entry', lambda *_: self.remove_row(item))]:
            button = Gtk.Button.new_from_icon_name(icon)
            button.add_css_class('flat')
            button.set_valign(Gtk.Align.START)
            button.set_tooltip_text(tooltip)
            button.connect('clicked', callback)
            header.append(button)
        body.append(field)
        row.append(body)
        body.set_visible(expanded)
        expander.connect('notify::expanded', lambda widget, *_: body.set_visible(widget.get_expanded()))
        def summary(*_):
            if hasattr(self, 'on_edit'):
                self.on_edit()
            try:
                current = field.value()
                if isinstance(current, dict):
                    title.set_text(current.get('title') or current.get('label') or
                                   (key_entry.get_text() if key_entry else caption(self.name)))
                    if self.name == 'plots':
                        names = getattr(self, 'display_names', {})
                        text = f"{caption(current.get('kind', 'line'))} · {names.get(current.get('y'), current.get('y')) or 'Choose a result'} vs {names.get(current.get('x'), current.get('x')) or 'Choose an axis'}"
                    elif self.name == 'parameters':
                        text = f"Starts at {current['default']} {current['unit']} · range {current['min']}–{current['max']}"
                    elif self.name == 'axes':
                        text = (f"{current.get('start', '?')} → {current.get('stop', '?')} {current.get('unit', '')}" if 'values' not in current
                                else f"{len(current['values'])} specific points")
                    else:
                        text = ' · '.join(str(current[k]) for k in ('role', 'quantity', 'channel', 'expression', 'control') if k in current)
                    detail.set_text(text)
                    detail.set_visible(bool(text))
                else:
                    title.set_text(str(current) or 'Choose a value')
                    detail.set_visible(False)
            except ValueError:
                title.set_text('Finish this entry')
        def watch(widget):
            if isinstance(widget, Gtk.Entry):
                widget.connect('changed', summary)
            if isinstance(widget, Gtk.ComboBoxText):
                widget.connect('changed', summary)
            child = widget.get_first_child()
            while child:
                watch(child)
                child = child.get_next_sibling()
        watch(body)
        summary()
        self.rows.append(item)
        self.items.append(row)
        if hasattr(self, 'empty'):
            self.empty.set_visible(False)
        if hasattr(self, 'on_added'):
            self.on_added()
        return field

    def move(self, item, delta):
        old = self.rows.index(item)
        new = old + delta
        if 0 <= new < len(self.rows):
            self.rows.pop(old)
            self.rows.insert(new, item)
            self.items.reorder_child_after(item[0], self.rows[new - 1][0] if new else None)

    def remove_row(self, item):
        self.removed = (item, self.rows.index(item))
        self.undo.set_visible(True)
        self.items.remove(item[0])
        self.rows.remove(item)
        self.empty.set_visible(not self.rows)
        if hasattr(self, 'on_edit'):
            self.on_edit()

    def undo_remove(self, *_):
        item, index = self.removed
        index = min(index, len(self.rows))
        self.rows.insert(index, item)
        self.items.append(item[0])
        self.items.reorder_child_after(item[0], self.rows[index - 1][0] if index else None)
        self.empty.set_visible(False)
        self.undo.set_visible(False)
        if hasattr(self, 'on_added'):
            self.on_added()

    def value(self):
        if self.kind == 'nullable_object':
            return self.nested.value() if self.enabled.get_active() else None
        if self.children:
            result = {key: value for key, value in self.original_value.items() if key not in self.children}
            result.update({key: field.value() for key, (toggle, field) in self.children.items()
                           if toggle is None or toggle.get_active()})
            return result
        if self.kind == 'array':
            return [field.value() for _, _, field in self.rows]
        if self.kind == 'object':
            result = {}
            for _, entry, field in self.rows:
                key = entry.get_text().strip()
                if not key or key in result:
                    raise ValueError('Provide distinct, nonempty identifiers in ' + (self.name or 'collection'))
                result[key] = field.value()
            return result
        if 'enum' in self.schema:
            selected = self.entry.get_active_id()
            if selected is None:
                raise ValueError('Choose a value for ' + self.name)
            return next(option for option in self.schema['enum'] if str(option) == selected)
        if self.kind == 'boolean':
            return self.entry.get_active()
        if self.name == 'description':
            buffer = self.entry.get_buffer()
            return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
        text = (self.picker.get_active_id() or self.entry.get_text()) if hasattr(self, 'picker') else self.entry.get_text()
        if text == self.original_text:
            return copy.deepcopy(self.original)
        if self.kind in ('number', 'integer'):
            try:
                value = json.loads(text)
            except ValueError:
                raise ValueError(self.name + ' must be numeric') from None
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(self.name + ' must be numeric')
            if self.kind == 'integer' and not isinstance(value, int):
                raise ValueError(self.name + ' must be an integer')
            return value
        if 'anyOf' in self.schema:
            try:
                value = json.loads(text)
                if value is None or isinstance(value, (str, int, float, bool)):
                    return value
            except ValueError:
                pass
        return text


class TestBuilder(Gtk.Box):
    def __init__(self, spec, roles=()):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.roles = roles
        self.add_css_class('test-builder')
        provider = Gtk.CssProvider()
        provider.load_from_data(b'.test-builder .builder-card { border: 1px solid alpha(currentColor, 0.15); border-radius: 10px; padding: 12px; } .test-builder .builder-page { padding: 24px; } .test-builder .builder-nav { padding: 8px; border-right: 1px solid alpha(currentColor, 0.12); } .test-builder .builder-nav row { padding: 10px; border-radius: 6px; }')
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.tabs = Gtk.Notebook(vexpand=True)
        self.append(self.tabs)
        self.tabs.connect('switch-page', lambda *_: self.update_suggestions())
        self.error = Gtk.Label(xalign=0, wrap=True)
        self.error.add_css_class('error')
        review = Gtk.Box(spacing=12, margin_top=8, margin_start=12, margin_end=12, margin_bottom=8)
        check = Gtk.Button(label='Check test')
        check.connect('clicked', self.check_test)
        review.append(check)
        review.append(self.error)
        self.append(review)
        self.raw = Gtk.TextView(monospace=True, wrap_mode=Gtk.WrapMode.NONE)
        self.raw.get_buffer().connect('changed', self.raw_changed)
        self.set_spec(spec)

    def raw_changed(self, *_):
        self.raw_dirty = True

    def set_spec(self, spec):
        if not isinstance(spec, dict):
            raise ValueError('A test specification must be a JSON object')
        forms = {}
        for title, keys in [('Testing', set(SCHEMA['properties']) - PRESENTATION - {'schema_version'}),
                            ('Output visualization', PRESENTATION)]:
            schema = dict(type='object', properties={key: value for key, value in SCHEMA['properties'].items() if key in keys},
                          required=[key for key in SCHEMA['required'] if key in keys])
            form = FormValue(schema, {key: value for key, value in spec.items() if key in keys}, roles=self.roles)
            forms[title] = form
        # Build first so a malformed import leaves the existing form intact.
        self.original = copy.deepcopy(spec)
        if self.raw.get_parent():
            self.raw.get_parent().set_child(None)
        while self.tabs.get_n_pages():
            self.tabs.remove_page(0)
        self.forms = forms
        self.navigation = {}
        for title, form in forms.items():
            content = self.workflow(title, form)
            self.tabs.append_page(content, Gtk.Label(label=title))
        advanced = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        advanced.append(Gtk.Label(label='Load JSON to update the forms. Refresh JSON to copy current form edits here.', wrap=True))
        bar = Gtk.Box(spacing=8)
        for label, callback in [('Load JSON into builder', self.load_json), ('Refresh JSON from builder', self.refresh_json)]:
            button = Gtk.Button(label=label)
            button.connect('clicked', callback)
            bar.append(button)
        advanced.append(bar)
        scroll = Gtk.ScrolledWindow(min_content_height=300, vexpand=True)
        scroll.set_child(self.raw)
        advanced.append(scroll)
        self.tabs.append_page(advanced, Gtk.Label(label='Advanced JSON'))
        self.raw.get_buffer().set_text(json.dumps(spec, indent=2))
        self.raw_dirty = False
        self.error.set_text('')
        self.update_suggestions()
        self.update_preview()

    def workflow(self, title, form):
        if title == 'Testing':
            steps = [
                ('About this test', 'Give the test a name and describe what you want to learn.', ('label', 'description')),
                ('Adjustable inputs', 'Values you can change before each run. Use their formula names in sweeps and calculations.', ('parameters',)),
                ('Sweep', 'Choose what changes between measurement points. Sweeps run in the order shown; the first is the outer loop.', ('axes',)),
                ('Equipment controls', 'Set up the bench, choose which outputs to enable, and declare their operating limits. Outputs still require confirmation before a run.', ('outputs', 'limits', 'setup', 'before_point')),
                ('Measurements', 'Choose what to read from each equipment role. Adding a measurement does not change an instrument’s front-panel function.', ('measurements', 'acquisition')),
                ('Calculations', 'Turn readings into results with formulas, or combine sweep points into grouped results.', ('derived', 'reduce')),
                ('Stop conditions', 'Checks stop a test on failure. An early-stop condition ends it successfully when the target is reached.', ('checks', 'stop_when')),
                ('Extra observations', 'Optional temperature readings and waveform captures alongside the main measurements.', ('secondary_measurements', 'waveforms')),
            ]
        else:
            steps = [
                ('Charts', 'Choose the results to plot. These settings are shared by the Results tab and exported reports.', ('plots',)),
                ('Result table', 'Choose the dataset and column order for the default table and CSV export. Full datasets remain available.', ('table',)),
                ('Report notes', 'Add explanations that should accompany the results.', ('notes',)),
            ]
        root = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        nav = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE, valign=Gtk.Align.FILL)
        nav.add_css_class('navigation-sidebar')
        nav.add_css_class('builder-nav')
        nav.set_size_request(180, -1)
        root.append(nav)
        stack = Gtk.Stack(hexpand=True, vexpand=True)
        root.append(stack)
        self.navigation[title] = (nav, stack)
        for index, (name, description, keys) in enumerate(steps):
            row = Gtk.ListBoxRow()
            row.set_child(label(name))
            row.page_name = str(index)
            nav.append(row)
            page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
            page.add_css_class('builder-page')
            page.append(label(name, 'title-2'))
            page.append(label(description, 'dim-label'))
            for key in keys:
                toggle, field = form.children[key]
                box = field.get_parent()
                box.get_parent().remove(box)
                # Collection pages need only their action and cards, not a repeated heading.
                if len(keys) == 1 and toggle is None:
                    box.remove(box.get_first_child())
                page.append(box)
            if keys == ('plots',):
                page.append(label('Chart layout', 'heading'))
                self.preview = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
                self.preview.add_css_class('builder-card')
                self.preview_title = label('')
                self.preview_title.add_css_class('heading')
                self.preview_y = label('')
                self.preview_x = label('')
                self.preview.append(self.preview_title)
                self.preview.append(self.preview_y)
                placeholder = Gtk.Label(label='Measurement data will appear here', height_request=80)
                placeholder.add_css_class('dim-label')
                self.preview.append(placeholder)
                self.preview.append(self.preview_x)
                page.append(self.preview)
                page.append(label('Axis labels preview the first chart. Measurement data appears after a run.', 'dim-label'))
            scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
            scroll.set_child(page)
            stack.add_named(scroll, str(index))
        def selected(_, row):
            if row:
                stack.set_visible_child_name(row.page_name)
                self.update_suggestions()
                if hasattr(self, 'preview'):
                    self.update_preview()
        nav.connect('row-selected', selected)
        nav.select_row(nav.get_row_at_index(0))
        return root

    def update_suggestions(self):
        if not hasattr(self, 'forms'):
            return
        try:
            spec = self.form_spec()
        except ValueError:
            return
        metrics = {'elapsed_s': 'Elapsed time (s)', 'quality': 'Reading quality'}
        for axis in spec.get('axes', []):
            metrics[axis.get('id', '')] = axis.get('label') or axis.get('id', '')
        for key in ('measurements', 'derived', 'secondary_measurements'):
            for name, item in spec.get(key, {}).items():
                metrics[name] = item.get('label', name) + (f" ({item['unit']})" if item.get('unit') else '')
        for name, item in spec.get('reduce', {}).get('metrics', {}).items():
            metrics[name] = item.get('label', name)
        self.metric_labels = metrics
        def visit(field):
            field.display_names = metrics
            field.on_added = self.update_suggestions
            field.on_edit = self.update_preview
            if field.name in ('x', 'y', 'series', 'column'):
                field.suggest(metrics)
            elif field.name == 'role':
                field.suggest({key: item.get('name', key) for key, item in self.roles.items()} if isinstance(self.roles, dict) else self.roles)
            elif field.name == 'quantity':
                field.suggest(['voltage', 'current', 'temperature'])
            for _, child in field.children.values():
                visit(child)
            for _, _, child in field.rows:
                visit(child)
        for form in self.forms.values():
            visit(form)

    def update_preview(self):
        if not hasattr(self, 'preview_title'):
            return
        try:
            plots = self.forms['Output visualization'].children['plots'][1].value()
        except ValueError:
            return
        plot = plots[0] if plots else {}
        self.preview_title.set_text(plot.get('title') or 'Your chart')
        self.preview_y.set_text('↑  ' + (plot.get('y_label') or getattr(self, 'metric_labels', {}).get(plot.get('y'), plot.get('y')) or 'Choose a vertical result'))
        self.preview_x.set_text('→  ' + (plot.get('x_label') or getattr(self, 'metric_labels', {}).get(plot.get('x'), plot.get('x')) or 'Choose a horizontal axis'))

    def check_test(self, *_):
        from gate.test_spec import validate_test, parameter_values, expand_axes
        try:
            spec, _ = validate_test(self.get_spec(), self.roles)
            values = parameter_values(spec, {})
            if spec['axes']:
                count = len(expand_axes(spec, values))
            else:
                from gate.expressions import Expression
                count = Expression(spec['acquisition']['repeat'], values).evaluate(values)
            self.error.remove_css_class('error')
            self.error.set_text(f"Definition valid · {count} points at starting values. Equipment is checked before a run.")
        except (ValueError, TypeError) as exc:
            self.error.add_css_class('error')
            self.error.set_text('Needs attention: ' + str(exc))

    def form_spec(self):
        spec = copy.deepcopy(self.original)
        for form in self.forms.values():
            for key in form.schema['properties']:
                spec.pop(key, None)
            spec.update(form.value())
        return spec

    def json_spec(self):
        buffer = self.raw.get_buffer()
        spec = json.loads(buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False))
        if not isinstance(spec, dict):
            raise ValueError('A test specification must be a JSON object')
        return spec

    def get_spec(self):
        if self.raw_dirty:
            raise ValueError('Load the edited JSON into the builder before saving.')
        return self.form_spec()

    def load_json(self, *_):
        try:
            self.set_spec(self.json_spec())
        except (ValueError, TypeError, KeyError) as exc:
            self.error.set_text('Unable to load JSON: ' + str(exc))

    def refresh_json(self, *_):
        try:
            if self.raw_dirty:
                raise ValueError('Load the edited JSON before refreshing to preserve your changes.')
            self.raw.get_buffer().set_text(json.dumps(self.form_spec(), indent=2))
            self.raw_dirty = False
            self.error.set_text('')
        except ValueError as exc:
            self.error.set_text(str(exc))
