"""Aim-TTi New PL-P series; commands/ratings from instruction manual Issue 21."""
import math
import re
from gate.model import InstrumentChannel, MeasurementFunction, Reading

# Exact models only: the original PL-P and PLH/QL families are different devices.
MODELS = {'PL068-P': (6., 8., 1), 'PL155-P': (15., 5., 1),
          'PL303-P': (30., 3., 1), 'PL601-P': (60., 1.5, 1),
          'PL303QMD-P': (30., 3., 2), 'PL303QMT-P': (30., 3., 3)}
MAKERS = {'THURLBY THANDAR', 'THURLBY-THANDAR', 'TTI', 'AIM-TTI', 'AIM TTI'}
FIELDS = (('voltage', 'Voltage', 'V'), ('current', 'Current', 'A'),
          ('power', 'Power (V × A)', 'W'), ('set_voltage', 'Voltage setpoint', 'V'),
          ('current_limit', 'Current limit', 'A'), ('output_enabled', 'Output enabled', ''))
NUMBER = r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?'


def model_name(identity):
    fields = [part.strip().upper() for part in identity.split(',')]
    if len(fields) != 4 or fields[0] not in MAKERS:
        return None
    model = fields[1].removesuffix('(G)')
    return model if model in MODELS else None


class TTiPLP:
    category = 'power_supply'
    simulated = False
    range_unavailable_reason = 'Current range is preserved; output range switching is not exposed.'
    experiment_required_controls = ('voltage', 'current_limit')

    @staticmethod
    def matches(identity):
        return model_name(identity) is not None

    def __init__(self, transport, identity):
        model = model_name(identity)
        if model is None:
            raise ValueError('Unsupported TTi PL-P model')
        self.id, self.name = transport.resource, identity
        self.device_type = 'TTI:' + model
        self._transport = transport  # Typed controls only; no raw-output bypass.
        self._owns_output = False
        self.max_voltage, self.max_current, self.output_count = MODELS[model]
        single = self.output_count == 1
        self.output_control_supported = self.experiment_control_supported = single
        self.experiment_capabilities = ('measure.scalar', 'measure.dc_voltage', 'measure.dc_current')
        self.experiment_channels = {'voltage': 'voltage', 'current': 'current'} if single else {}
        self.experiment_limits = {}
        self.output_configuration = {}
        if single:
            self.experiment_capabilities += ('source.dc_voltage',)
            self.experiment_limits = dict(voltage=[0., self.max_voltage], current=[.001, self.max_current],
                                          power_w=self.max_voltage * self.max_current)
            self.output_configuration = dict(
                enabled_reading='output_enabled', voltage_reading='voltage', current_reading='current',
                controls=dict(
                    voltage=dict(label='Output voltage', unit='V', min=0., max=self.max_voltage,
                                 step=.001, reading='set_voltage'),
                    current_limit=dict(label='Current limit', unit='A', min=.001, max=self.max_current,
                                       step=.001 if self.max_current == 8 else .0001, reading='current_limit')))

    def channels(self):
        return [InstrumentChannel(f'output{n}', f'Output {n}') for n in range(1, self.output_count + 1)]

    def _key(self, n, key):
        return key if self.output_count == 1 else f'output{n}.{key}'

    def functions(self):
        return [MeasurementFunction(self._key(n, key), label, self._key(n, key), unit,
                                    instrument_channel=f'output{n}')
                for n in range(1, self.output_count + 1) for key, label, unit in FIELDS]

    def range_controls(self):
        return []

    def _number(self, command, prefix='', suffix=''):
        response = self._transport.query(command).strip()
        match = re.fullmatch(re.escape(prefix) + r'\s*(' + NUMBER + ')' + re.escape(suffix), response)
        if not match or not math.isfinite(float(match[1])):
            raise ValueError(f'Invalid PL-P response to {command}')
        return float(match[1])

    def _choice(self, command, choices):
        response = self._transport.query(command).strip()
        if response not in choices:
            raise ValueError(f'Invalid PL-P response to {command}')
        return int(response)

    def read(self):
        config = self._choice('CONFIG?', ('0', '1', '2'))
        readings = []
        for n in range(1, self.output_count + 1):
            if n == 2 and config == 1:
                readings.extend(Reading(self._key(n, key), None, unit, 'unavailable') for key, _, unit in FIELDS)
                continue  # Output 2 queries are invalid in parallel mode.
            voltage = self._number(f'V{n}O?', suffix='V')
            current = self._number(f'I{n}O?', suffix='A')
            values = dict(voltage=voltage, current=current, power=voltage * current,
                          set_voltage=self._number(f'V{n}?', prefix=f'V{n} '),
                          current_limit=self._number(f'I{n}?', prefix=f'I{n} '),
                          output_enabled=float(self._choice(f'OP{n}?', ('0', '1'))))
            readings.extend(Reading(self._key(n, key), values[key], unit) for key, _, unit in FIELDS)
        return readings

    def _controllable(self):
        if not self.output_control_supported:
            raise ValueError('Multi-output PL-P control requires channel-targeted output support')

    @staticmethod
    def _value(value, minimum, maximum, step):
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                or not minimum <= value <= maximum):
            raise ValueError(f'PL-P setpoint must be finite, {minimum}–{maximum}')
        return round(value / step) * step

    def _set(self, command, value, query, prefix='', tolerance=0.):
        self._transport.write(f'{command} {value:g}')
        actual = self._number(query, prefix=prefix)
        if not math.isclose(actual, value, rel_tol=0., abs_tol=tolerance + 1e-9):
            raise ValueError('PL-P configuration readback mismatch')

    def set_voltage(self, value):
        self._controllable()
        value = self._value(value, 0., self.max_voltage, .001)
        self._set('V1', value, 'V1?', 'V1 ', .0001)

    def _current_maximum(self):
        low = self._choice('IRANGE1?', ('1', '2')) == 1
        return min(self.max_current, .8 if self.max_current == 8 else .5) if low else self.max_current

    def set_current_limit(self, value):
        self._controllable()
        # Validate input before any transport call, then respect the existing range.
        step = .001 if self.max_current == 8 else .0001
        value = self._value(value, .001, self.max_current, step)
        if value > self._current_maximum():
            raise ValueError('PL-P current limit exceeds the selected current range')
        self._set('I1', value, 'I1?', 'I1 ', step / 10)

    def set_enabled(self, enabled):
        self._controllable()
        if not isinstance(enabled, bool):
            raise ValueError('enabled must be boolean')
        if enabled:
            self._value(self._number('V1?', 'V1 '), 0., self.max_voltage, .001)
            self._value(self._number('I1?', 'I1 '), .001, self._current_maximum(), .0001)
            # Mark ownership before writing: an uncertain enable still needs cleanup.
            self._owns_output = True
        self._transport.write(f'OP1 {int(enabled)}')
        if self._choice('OP1?', ('0', '1')) != int(enabled):
            raise ValueError('PL-P output readback mismatch')
        if not enabled:
            self._owns_output = False

    def close(self):
        try:
            if self._owns_output:
                self.set_enabled(False)
        finally:
            self._transport.close()
