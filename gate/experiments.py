"""Model-independent experiment definitions; checking a setup performs no I/O."""
import copy
import re
from gate.procedures import PROCEDURES


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
            if not isinstance(component, dict) or set(component) != {'name', 'capabilities', 'terminals'}:
                raise ValueError('Each role requires name, capabilities and terminals')
            _text(component['name'], 'Role name')
            capabilities = component['capabilities']
            if not isinstance(capabilities, list) or not capabilities:
                raise ValueError('Each role requires a non-empty capability list')
            for capability in capabilities:
                _text(capability, 'Capability')
            if len(set(capabilities)) != len(capabilities):
                raise ValueError('Duplicate capability')
        terminals = component['terminals']
        if not isinstance(terminals, list) or not terminals:
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
    for identity, description in tests.items():
        _identifier(identity)
        if isinstance(description, dict):
            if set(description) != {'description', 'procedure'} or not isinstance(description['procedure'], str) or description['procedure'] not in PROCEDURES:
                raise ValueError('Test requires description and a supported procedure')
            _text(description['description'], 'Test description')
        else:
            _text(description, 'Test description')
    return copy.deepcopy(value)


def check_bindings(definition, bindings, instruments, resolve):
    """Check live driver declarations, never infer capabilities from make/category."""
    if not isinstance(bindings, dict) or set(bindings) - set(definition['roles']):
        raise ValueError('Bindings must map declared equipment roles to instrument IDs')
    resolved = {}
    errors = []
    for role, requirement in definition['roles'].items():
        key = bindings.get(role)
        if key is None:
            errors.append(f'{role}: no instrument assigned')
            continue
        if not isinstance(key, str):
            raise ValueError('Binding instrument IDs must be strings')
        key = resolve(key)
        resolved[role] = key
        instrument = instruments.get(key)
        if instrument is None:
            errors.append(f'{role}: instrument is not connected')
            continue
        missing = set(requirement['capabilities']) - set(getattr(instrument, 'experiment_capabilities', ()))
        if missing:
            errors.append(f'{role}: missing capabilities: {", ".join(sorted(missing))}')
    if len(set(resolved.values())) != len(resolved):
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
        'efficiency': 'Sweep input voltage and output load current. Measure input and output voltage/current; efficiency = (Vout * Iout) / (Vin * Iin). Exclude invalid readings and zero input power.',
        'maximum_output_current': 'At each input voltage, increase load current within DUT and equipment limits until a defined regulation or protection threshold is reached; record the last acceptable output current. Thresholds, limits and settling must be specified before execution.',
    },
})
