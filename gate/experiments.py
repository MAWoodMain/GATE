"""Model-independent experiment definitions; checking a setup performs no I/O."""
import copy
import re
from gate.procedures import test_spec, load_template
from gate.test_spec import validate_test


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 2000:
        raise ValueError(f'{label} must be non-empty text (at most 2000 characters)')
    return value


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', value):
        raise ValueError('IDs must use lowercase letters, digits and underscores, starting with a letter')
    return value


def validate_definition(value):
    """Validate and detach a JSON definition before storing it."""
    if not isinstance(value, dict) or set(value) != {'id', 'name', 'description', 'dut', 'roles', 'connections', 'tests'}:
        raise ValueError('Experiment requires id, name, description, dut, roles, connections and tests')
    _identifier(value['id'])
    _text(value['name'], 'Name')
    _text(value['description'], 'Description')
    dut = value['dut']
    if not isinstance(dut, dict) or set(dut) != {'name', 'terminals'}:
        raise ValueError('DUT requires name and terminals')
    _text(dut['name'], 'DUT name')
    roles = value['roles']
    if not isinstance(roles, dict) or not roles or 'dut' in roles:
        raise ValueError('Provide equipment roles; dut is reserved for the device under test')
    endpoints = set()
    for identity, component in [('dut', dut), *roles.items()]:
        _identifier(identity)
        if identity != 'dut':
            if not isinstance(component, dict) or {'name', 'capabilities', 'terminals'} - set(component) or set(component) - {'name', 'capabilities', 'terminals', 'optional'}:
                raise ValueError('Each role requires name, capabilities and terminals')
            if type(component.get('optional', False)) is not bool:
                raise ValueError('Role optional must be a boolean')
            _text(component['name'], 'Role name')
            capabilities = component['capabilities']
            if not isinstance(capabilities, list) or not capabilities:
                raise ValueError('Each role requires a non-empty capability list')
            for capability in capabilities:
                _text(capability, 'Capability')
            if len(set(capabilities)) != len(capabilities):
                raise ValueError('Duplicate capability')
        terminals = component['terminals']
        if not isinstance(terminals, list) or not terminals and (identity == 'dut' or not component.get('optional', False)):
            raise ValueError('Each component requires terminals')
        for terminal in terminals:
            _identifier(terminal)
            endpoint = identity + '.' + terminal
            if endpoint in endpoints:
                raise ValueError('Duplicate terminal')
            endpoints.add(endpoint)
    connections = value['connections']
    if not isinstance(connections, list) or not connections:
        raise ValueError('Provide wiring connections')
    used = set()
    for connection in connections:
        if (not isinstance(connection, list) or len(connection) != 2
                or any(not isinstance(item, str) or item not in endpoints for item in connection)
                or connection[0] == connection[1]):
            raise ValueError('Each connection must join two distinct declared component.terminals')
        used.update(connection)
    if used != endpoints:
        raise ValueError('Every declared terminal must appear in the wiring')
    tests = value['tests']
    if not isinstance(tests, dict) or not tests:
        raise ValueError('Provide named test descriptions')
    normalized = copy.deepcopy(value)
    for identity, description in tests.items():
        _identifier(identity)
        if isinstance(description, str):
            _text(description, 'Test description')
        spec, _ = validate_test(test_spec(value, identity), roles)
        normalized['tests'][identity] = spec
    return normalized


def check_bindings(definition, bindings, instruments, resolve):
    """Check live driver declarations, never infer capabilities from make/category."""
    if not isinstance(bindings, dict) or set(bindings) - set(definition['roles']):
        raise ValueError('Bindings must map declared equipment roles to instrument IDs')
    resolved = {}
    errors = []
    for role, requirement in definition['roles'].items():
        key = bindings.get(role)
        if key is None:
            if requirement.get('optional', False):
                continue
            errors.append(f'{role}: no instrument assigned')
            continue
        if not isinstance(key, str):
            raise ValueError('Binding instrument IDs must be strings')
        key = resolve(key)
        instrument = instruments.get(key)
        if instrument is None:
            if requirement.get('optional', False):
                continue
            errors.append(f'{role}: instrument is not connected')
            continue
        resolved[role] = key
        missing = set(requirement['capabilities']) - set(getattr(instrument, 'experiment_capabilities', ()))
        if missing:
            if requirement.get('optional', False):
                resolved.pop(role, None)
                continue
            errors.append(f'{role}: missing capabilities: {", ".join(sorted(missing))}')
    mandatory = [identity for role, identity in resolved.items() if not definition['roles'][role].get('optional', False)]
    if len(set(mandatory)) != len(mandatory):
        errors.append('Equipment roles require distinct instruments')
    return dict(experiment=definition['id'], bindings=resolved, compatible=not errors, errors=errors)


BUCK_CONVERTER = validate_definition({
    'id': 'buck_converter',
    'name': 'Buck converter characterisation',
    'description': 'A variable DC supply feeds a converter under test; an electronic load draws current from its output.',
    'dut': {'name': 'Buck converter', 'terminals': ['input_positive', 'input_negative', 'output_positive', 'output_negative']},
    'roles': {
        'supply': {'name': 'Input DC supply', 'capabilities': ['source.dc_voltage', 'measure.dc_voltage', 'measure.dc_current'],
                   'terminals': ['positive', 'negative']},
        'load': {'name': 'Output electronic load', 'capabilities': ['sink.dc_current', 'measure.dc_voltage', 'measure.dc_current'],
                 'terminals': ['positive', 'negative']},
    },
    'connections': [
        ['supply.positive', 'dut.input_positive'], ['supply.negative', 'dut.input_negative'],
        ['load.positive', 'dut.output_positive'], ['load.negative', 'dut.output_negative'],
    ],
    'tests': {
        'efficiency': load_template('efficiency'),
        'maximum_output_current': load_template('maximum_output_current'),
    },
})


# Optional probes extend existing arrangements without changing their core wiring.
BUCK_CONVERTER['roles'].update({
    'thermal': dict(name='Temperature probes', capabilities=['measure.temperature'], terminals=[], optional=True),
    'scope': dict(name='Waveform recorder', capabilities=['capture.waveform'], terminals=[], optional=True),
})
from gate.secondary import add_converter_secondary
for _test_id, _test in BUCK_CONVERTER['tests'].items():
    add_converter_secondary(_test, _test_id)
BUCK_CONVERTER = validate_definition(BUCK_CONVERTER)
