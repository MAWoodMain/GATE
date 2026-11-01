"""AOIP calibrators. OUT? is a setpoint, never an output measurement."""
import math
import re
from gate.model import InstrumentChannel, MeasurementFunction, Reading, RangeControl, RangeOption
from gate.ranges import range_option

NUMBER = r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?'
VALUE = re.compile(rf'^({NUMBER})\s*(?:,\s*|\s+)([A-Za-z]+)$')
UNITS = {'V': ('V', 1), 'MV': ('V', .001), 'UV': ('V', .000001),
         'A': ('A', 1), 'MA': ('A', .001), 'UA': ('A', .000001), 'NA': ('A', 1e-9),
         'OHM': ('Ω', 1), 'KOHM': ('Ω', 1000), 'CEL': ('°C', 1),
         'FAR': ('°C', 1), 'K': ('°C', 1)}
LABELS = {'voltage': ('DC voltage', 'V'), 'current': ('DC current', 'A'),
          'resistance': ('Resistance', 'Ω'), 'thermocouple': ('Thermocouple', '°C'), 'rtd': ('RTD', '°C')}
PJ_INPUT = {'voltage': ('V', ('MV60', 'MV600', 'V6', 'V60', 'V_AUTO')),
            'current': ('MA', ('MA60',)), 'resistance': ('OHM', ('OHM600', 'OHM6000', 'OHM_AUTO')),
            'thermocouple': ('TC', ('TC',)), 'rtd': ('RT', ('RT',))}


def parse_value(text, overload=False):
    match = VALUE.fullmatch(text.strip())
    if not match:
        raise ValueError('Malformed AOIP value/unit response')
    raw, suffix = float(match[1]), match[2].upper()
    if suffix == 'SCAL':
        return None, '', 'scaled'
    if suffix not in UNITS:
        raise ValueError('Unsupported AOIP unit: '+suffix)
    unit, scale = UNITS[suffix]
    if not math.isfinite(raw) or overload and abs(raw) >= 1e7:
        return None, unit, 'overload'
    value = raw * scale
    if suffix == 'FAR':
        value = (value - 32) * 5/9
    elif suffix == 'K':
        value -= 273.15
    return value, unit, 'ok'


def fields(text):
    return [part.strip().upper() for part in text.split(',')]


def register(transport, command, maximum):
    value = int(transport.query(command))
    if not 0 <= value <= maximum:
        raise ValueError('Invalid AOIP register: '+command)
    return value


def model_matches(identity, model):
    parts = fields(identity)
    return (len(parts) == 4 and parts[0] in ('AOIP', 'AOIP_MESURES')
            and re.sub(r'[ -]', '', parts[1]) == model)


class AOIPSource:
    category = 'calibrator'
    simulated = False
    output_control_supported = True
    experiment_control_supported = False  # No generic PSU compliance/current-limit contract.
    experiment_capabilities = ()
    range_unavailable_reason = 'Source ranges are chosen explicitly with the source command while in standby.'

    def __init__(self, transport, identity):
        self.transport, self.name, self.id = transport, identity.strip(), transport.resource
        self._owns_output = False
        self.current_functions = {}
        self._source_ready = None

    def channels(self):
        return [InstrumentChannel('source', 'Source output')]

    def functions(self):
        return [MeasurementFunction('source.'+key, label+' setpoint', 'source.'+key, unit,
                                    instrument_channel='source')
                for key, (label, unit) in LABELS.items() if key in self.source_controls] + [
            MeasurementFunction('source.enabled', 'Output operating (standby is zero, not isolation)',
                                'source.enabled', '', instrument_channel='source')]

    def range_controls(self):
        return []

    def _range_arguments(self, range_id):
        return range_id

    def _source_signature(self):
        return self._source_range()

    def _remote(self):
        self.transport.write('REM')
        self._complete()

    def _complete(self):
        if self.transport.query('*OPC?').strip() != '1':
            raise ValueError('AOIP did not confirm command completion')

    def configure_source(self, quantity, value, range_id):
        ranges = self.source_controls.get(quantity, {})
        if not isinstance(range_id, str) or range_id not in ranges:
            raise ValueError('Choose an advertised AOIP source quantity and range')
        low, high, step = ranges[range_id]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f'AOIP source value must be finite and within {low:g}–{high:g} SI units')
        value = round(value / step) * step
        if not low <= value <= high:
            raise ValueError('Rounded AOIP value exceeds the selected range')
        if self._source_state()['enabled']:
            raise ValueError('Disable the AOIP output before configuring its source')
        arguments = self._range_arguments(range_id)
        self._source_ready = None
        self._remote()
        self.transport.write(self.range_command+' '+arguments)
        self._complete()
        if self._source_range() != range_id or self._source_state()['enabled']:
            raise ValueError('AOIP did not confirm source range in standby')
        suffix = {'voltage': 'V', 'current': 'A', 'resistance': 'OHM'}[quantity]
        self.transport.write(f'OUT {value:.12g}{suffix}')
        self._complete()
        state = self._source_state()
        actual, unit, quality = parse_value(self.transport.query('OUT?'))
        if (state['enabled'] or state['fault'] or self._source_range() != range_id or quality != 'ok'
                or unit != LABELS[quantity][1] or actual is None or not math.isclose(actual, value, abs_tol=step*.51)):
            raise ValueError('AOIP source setpoint readback mismatch')
        self._source_ready = (self._source_signature(), actual, unit, step)

    def set_enabled(self, enabled):
        if not isinstance(enabled, bool):
            raise ValueError('enabled must be boolean')
        if enabled:
            if self._source_ready is None:
                raise ValueError('Configure an AOIP source setpoint in this connection before enabling')
            signature, value, unit, step = self._source_ready
            actual, actual_unit, quality = parse_value(self.transport.query('OUT?'))
            state = self._source_state()
            if (state['fault'] or self._source_signature() != signature or quality != 'ok' or actual_unit != unit
                    or actual is None or not math.isclose(actual, value, abs_tol=step*.51)):
                raise ValueError('AOIP source configuration changed; configure again before enabling')
        self._remote()
        if enabled:
            self._owns_output = True  # A lost acknowledgement can still energise it.
        self.transport.write('OPER' if enabled else 'STBY')
        self._complete()
        state = self._source_state()
        if state['enabled'] != enabled or enabled and state['fault']:
            raise ValueError('AOIP did not confirm requested output state')
        if not enabled:
            self._owns_output = False

    def _source_readings(self):
        before = self._source_range()
        state = self._source_state()
        value, unit, quality = parse_value(self.transport.query('OUT?'))
        after = self._source_range()
        quantity = self._quantity(before)
        if before != after:
            value, quality = None, 'mode_changed'
        elif state['fault']:
            value, quality = None, 'output_fault'
        if quality == 'ok' and (quantity is None or unit != LABELS[quantity][1]):
            value, quality = None, 'incompatible_unit'
        self.current_functions['source'] = 'source.'+quantity if quantity else None
        keys = {f.id for f in self.functions() if f.instrument_channel == 'source'}
        return [Reading('source.'+key, value if key == quantity else None, label_unit,
                        quality if key == quantity else 'inactive')
                for key, (_, label_unit) in LABELS.items() if 'source.'+key in keys] + [
                    Reading('source.enabled', float(state['enabled']), '')]

    def _quantity(self, range_id):
        return next((key for key, ranges in self.source_controls.items() if range_id in ranges), None)

    def read(self):
        return self._source_readings()

    def close(self):
        try:
            if self._owns_output:
                self.set_enabled(False)
        finally:
            self.transport.close()


class AOIPSN8310(AOIPSource):
    device_type = 'AOIP:SN8310'
    range_command = 'RANGE'
    # Manual section 2.3, normal polarity. Values and resolution in V or A.
    source_controls = {
        'voltage': {'MV100': [-.011, .11, 1e-7], 'V1': [-.11, 1.1, 1e-6],
                    'V10': [-1.1, 11., 1e-5], 'V100': [-5., 110., 1e-4]},
        'current': {'MA1': [-.00011, .0011, 1e-9], 'MA10': [-.0011, .011, 1e-8],
                    'MA100': [-.011, .11, 1e-7]}}

    @staticmethod
    def matches(identity):
        return model_matches(identity, 'SN8310')

    def _range_arguments(self, range_id):
        wire = self._source_signature()[1]
        if range_id == 'MV100' and wire != 'WIRE2':
            raise ValueError('SN-8310 100 mV uses the SENSE terminals in 2-wire configuration; configure wiring on the front panel first')
        return range_id+','+wire

    def _source_signature(self):
        result = fields(self.transport.query('RANGE?'))
        if len(result) != 2 or result[0] not in {r for ranges in self.source_controls.values() for r in ranges} or result[1] not in ('WIRE2', 'WIRE4'):
            raise ValueError('Unsupported SN-8310 range/configuration')
        return tuple(result)

    def _source_range(self):
        return self._source_signature()[0]

    def _source_state(self):
        value = register(self.transport, 'ISR?', 511)
        # SAT or inverted polarity: refuse enable.
        return dict(enabled=not bool(value & 8), fault=bool(value & (64 | 1)))


class AOIPPJ6301(AOIPSource):
    device_type = 'AOIP:PJ6301'
    range_command = 'RANGE_OUT'
    experiment_capabilities = ('measure.scalar', 'measure.dc_voltage', 'measure.dc_current',
                               'measure.resistance', 'measure.temperature')
    source_controls = {
        'voltage': {'MV600': [-.1, .6, 1e-6], 'V6': [-1., 6., 1e-5], 'V60': [-10., 60., 1e-4]},
        'current': {'MA60': [0., .06, 1e-7]},
        'resistance': {'OHM600': [0., 600., .001], 'OHM6000': [0., 6000., .01]}}

    @staticmethod
    def matches(identity):
        return model_matches(identity, 'PJ6301')

    def channels(self):
        return super().channels()+[InstrumentChannel('measure', 'Measurement input')]

    def functions(self):
        # Temperature source monitoring is passive; sensor simulation setup is not exposed.
        return super().functions()+[
            MeasurementFunction('source.'+key, label+' setpoint', 'source.'+key, unit, instrument_channel='source')
            for key, (label, unit) in LABELS.items() if key in ('thermocouple', 'rtd')] + [
            MeasurementFunction('measure.'+key, label, 'measure.'+key, unit, True, 'mode', 'measure')
            for key, (label, unit) in LABELS.items()]

    def _quantity(self, range_id):
        return {'TC': 'thermocouple', 'RT': 'rtd'}.get(range_id) or super()._quantity(range_id)

    def _source_range(self):
        result = self.transport.query('CFG_OUT?').strip().upper()
        if self._quantity(result) is None:
            raise ValueError('Unsupported PJ-6301 output range')
        return result

    def _source_state(self):
        pcr = register(self.transport, 'PCR?', 65535)
        status = register(self.transport, 'ISR?', 65535)
        return dict(enabled=not bool(pcr & 256), fault=bool(status & (512 | 1024) or pcr & (512 | 1024)))

    def _input(self):
        code = self.transport.query('CFG_IN?').strip().upper()
        for key, (_, ranges) in PJ_INPUT.items():
            if code in ranges:
                return key, code
        raise ValueError('Unsupported PJ-6301 input function')

    def range_functions(self):
        key, _ = self._input()
        self.current_functions['measure'] = 'measure.'+key
        return dict(self.current_functions)

    def range_controls(self):
        labels = {'MV60': ('60 mV', .06), 'MV600': ('600 mV', .6), 'V6': ('6 V', 6.), 'V60': ('60 V', 60.),
                  'MA60': ('60 mA', .06), 'OHM600': ('600 Ω', 600.), 'OHM6000': ('6 kΩ', 6000.)}
        return [RangeControl('measure.'+key, LABELS[key][0], LABELS[key][1], tuple(
                    RangeOption(code, 'Autorange' if code.endswith('_AUTO') else labels[code][0],
                                None if code.endswith('_AUTO') else labels[code][1], code.endswith('_AUTO'))
                    for code in ranges), 'measure.'+key, 'measure')
                for key, (_, ranges) in PJ_INPUT.items() if key in ('voltage', 'current', 'resistance')]

    def _input_config(self, key):
        code = PJ_INPUT[key][0]
        config = fields(self.transport.query('CFG_IN? '+code))
        # Whitelist complete stored function arguments; never replay arbitrary text.
        allowed = {'WIRE2', 'WIRE3', 'WIRE4', 'TXA', 'TXP', 'TCX_ON', 'TCX_OFF',
                   'CEL', 'FAR', 'K', 'JR_ON', 'JR_OFF'} | {
                       'TYP_'+t for t in ('K', 'T', 'J', 'E', 'N', 'L', 'S', 'R', 'B', 'PL', 'MO',
                                         'PT100', 'PT200', 'PT500', 'PT1000', 'NI100')}
        if not config or config[0] not in PJ_INPUT[key][1] or any(c not in allowed for c in config[1:]):
            raise ValueError('Unsupported PJ-6301 input configuration')
        return config

    def _select_input(self, key, config):
        # TXP would turn on the 24 V loop supply; only TXA is selectable by GATE.
        if key == 'current' and config != ['MA60', 'TXA']:
            raise ValueError('Set PJ-6301 input to active-transmitter (TXA) mode first; loop supply is not controlled by GATE')
        self._remote()
        self.transport.write('RANGE_IN '+','.join(config))
        self._complete()
        if self._input_config(key) != config or self._input()[0] != key:
            raise ValueError('PJ-6301 input configuration readback mismatch')
        self.current_functions['measure'] = 'measure.'+key

    def select_function(self, function):
        key = function.removeprefix('measure.') if isinstance(function, str) else ''
        if function != 'measure.'+key or key not in PJ_INPUT:
            raise ValueError('Unsupported PJ-6301 measurement function')
        self._select_input(key, self._input_config(key))

    def select_range(self, control_id, option_id):
        control, option = range_option(self, control_id, option_id)
        key = control.function.removeprefix('measure.')
        if self._input()[0] != key:
            raise ValueError('Select the matching input function before its range')
        config = self._input_config(key)
        config[0] = option.id
        self._select_input(key, config)

    def read(self):
        readings = self._source_readings()
        key, code = self._input()
        config = self._input_config(key)
        pcr = register(self.transport, 'PCR?', 65535)
        status = register(self.transport, 'ISR?', 65535)
        value, unit, quality = parse_value(self.transport.query('MEAS?'), overload=True)
        after_pcr = register(self.transport, 'PCR?', 65535)
        if self._input() != (key, code) or self._input_config(key) != config or (pcr ^ after_pcr) & 14:
            value, quality = None, 'mode_changed'
        elif pcr & 8:
            value, quality = None, 'scaled'
        elif pcr & 4:
            value, quality = None, 'relative'
        elif status & 4:
            value, quality = None, 'overload'
        elif pcr & 2:
            value, quality = None, 'held'
        elif not status & 1:
            value, quality = None, 'stale'
        if quality == 'ok' and unit != LABELS[key][1]:
            value, quality = None, 'incompatible_unit'
        self.current_functions['measure'] = 'measure.'+key
        readings += [Reading('measure.'+k, value if k == key else None, u, quality if k == key else 'inactive')
                     for k, (_, u) in LABELS.items()]
        return readings
