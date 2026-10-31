"""Schema-backed GTK forms for execution and result presentation settings."""
import copy
import json
from pathlib import Path

import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk

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


def caption(name):
    return name.replace('_', ' ').capitalize()


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
            for key, child_schema in schema['properties'].items():
                required = key in schema.get('required', [])
                box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_start=12)
                if required:
                    box.append(Gtk.Label(label=caption(key), xalign=0))
                    toggle = None
                else:
                    toggle = Gtk.CheckButton(label=caption(key) + ' (optional)', active=key in value)
                    box.append(toggle)
                field = FormValue(child_schema, value[key] if key in value else initial(child_schema), key, roles)
                if toggle:
                    field.set_sensitive(toggle.get_active())
                    toggle.connect('toggled', lambda button, field=field: field.set_sensitive(button.get_active()))
                box.append(field)
                self.append(box)
                self.children[key] = (toggle, field)
        elif self.kind in ('object', 'array'):
            self.items = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            self.append(self.items)
            for key, item in (value.items() if self.kind == 'object' else enumerate(value)):
                self.add_row(item, str(key))
            add = Gtk.Button(label='Add ' + (caption(name) if name else 'item'))
            add.connect('clicked', lambda *_: self.add_row(initial(self.item_schema())))
            self.append(add)
        elif 'enum' in schema:
            self.entry = Gtk.ComboBoxText()
            for option in schema['enum']:
                self.entry.append(str(option), str(option))
            self.entry.set_active_id(str(value))
            self.append(self.entry)
        elif self.kind == 'boolean':
            self.entry = Gtk.CheckButton(label='Enabled', active=value)
            self.append(self.entry)
        else:
            # Expressions are entered as numbers, true/false/null, or bare formula text.
            self.original = copy.deepcopy(value)
            self.original_text = value if isinstance(value, str) else json.dumps(value)
            self.entry = Gtk.Entry(text=self.original_text, hexpand=True)
            self.entry.update_property([Gtk.AccessibleProperty.LABEL], [caption(name) or 'Value'])
            if 'anyOf' in schema:
                self.entry.set_placeholder_text('Number or expression, e.g. start_v + 1')
            if name == 'role':
                self.entry.set_tooltip_text('Equipment roles: ' + ', '.join(roles))
            self.append(self.entry)
        if schema.get('description'):
            self.set_tooltip_text(schema['description'])

    def item_schema(self):
        return self.schema.get('items', self.schema.get('additionalProperties', {}))

    def add_row(self, value, key=''):
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        row.add_css_class('frame')
        bar = Gtk.Box(spacing=6)
        row.append(bar)
        key_entry = None
        if self.kind == 'object':
            key_entry = Gtk.Entry(text=key, placeholder_text='Identifier', hexpand=True)
            key_entry.update_property([Gtk.AccessibleProperty.LABEL], ['Identifier'])
            bar.append(key_entry)
        field = FormValue(self.item_schema(), value, roles=self.roles)
        item = (row, key_entry, field)
        for label, callback in [('↑', lambda *_: self.move(item, -1)),
                                ('↓', lambda *_: self.move(item, 1)),
                                ('Remove', lambda *_: self.remove_row(item))]:
            button = Gtk.Button(label=label)
            button.connect('clicked', callback)
            bar.append(button)
        row.append(field)
        self.rows.append(item)
        self.items.append(row)
        return field

    def move(self, item, delta):
        old = self.rows.index(item)
        new = old + delta
        if 0 <= new < len(self.rows):
            self.rows.pop(old)
            self.rows.insert(new, item)
            self.items.reorder_child_after(item[0], self.rows[new - 1][0] if new else None)

    def remove_row(self, item):
        self.items.remove(item[0])
        self.rows.remove(item)

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
        text = self.entry.get_text()
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
        self.append(Gtk.Label(label='Expand a section to edit it. Use Add/Remove and arrows to manage entries. '
                              'Optional fields are enabled by their checkbox. Enter formulas without JSON quotes.',
                              xalign=0, wrap=True))
        self.tabs = Gtk.Notebook(vexpand=True)
        self.append(self.tabs)
        self.error = Gtk.Label(xalign=0, wrap=True)
        self.error.add_css_class('error')
        self.append(self.error)
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
            # Keep the top-level sections compact while editing one part of a test.
            for key, (_, field) in form.children.items():
                box = field.get_parent()
                form.remove(box)
                expander = Gtk.Expander(label=caption(key))
                expander.set_child(box)
                form.append(expander)
            forms[title] = form
        # Build first so a malformed import leaves the existing form intact.
        self.original = copy.deepcopy(spec)
        if self.raw.get_parent():
            self.raw.get_parent().set_child(None)
        while self.tabs.get_n_pages():
            self.tabs.remove_page(0)
        self.forms = forms
        for title, form in forms.items():
            scroll = Gtk.ScrolledWindow(min_content_height=300, vexpand=True)
            scroll.set_child(form)
            self.tabs.append_page(scroll, Gtk.Label(label=title))
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
