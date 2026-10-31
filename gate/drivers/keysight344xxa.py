"""Keysight/Agilent/HP bench DMMs using model-specific SCPI capabilities.

See docs/keysight344xxa.md for supported models, acquisition and verification.
"""
import math
import re
from gate.model import MeasurementFunction, Reading

MODELS = {'34401A', '34410A', '34411A', '34460A', '34461A', '34465A', '34470A'}
MAKERS = {'KEYSIGHT TECHNOLOGIES', 'KEYSIGHT', 'AGILENT TECHNOLOGIES', 'AGILENT',
          'HEWLETT-PACKARD', 'HP'}
# ID: (label, channel, unit, SCPI function)
FUNCTIONS = {
    'VDC': ('DC voltage', 'voltage_dc', 'V', 'VOLT:DC'),
    'VAC': ('AC voltage', 'voltage_ac', 'V', 'VOLT:AC'),
    'ADC': ('DC current', 'current_dc', 'A', 'CURR:DC'),
    'AAC': ('AC current', 'current_ac', 'A', 'CURR:AC'),
    'OHMS': ('Resistance (2-wire)', 'resistance', 'Ω', 'RES'),
    'OHMS4': ('Resistance (4-wire)', 'resistance_4wire', 'Ω', 'FRES'),
    'FREQ': ('Frequency', 'frequency', 'Hz', 'FREQ'),
    'PER': ('Period', 'period', 's', 'PER'),
    'DIODE': ('Diode test', 'diode', 'V', 'DIOD'),
    'CONT': ('Continuity', 'continuity', 'Ω', 'CONT'),
    'CAP': ('Capacitance', 'capacitance', 'F', 'CAP'),
    'TEMPC': ('Temperature (Celsius)', 'temperature', '°C', 'TEMP'),
    'TEMPF': ('Temperature (Fahrenheit)', 'temperature', '°F', 'TEMP'),
}
NUMBER = re.compile(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?\Z')


def parse_value(text):
    token = text.strip()
    if not NUMBER.fullmatch(token):
        raise ValueError('Invalid Keysight scalar reading')
    value = float(token)
    if abs(value) == 9.91e37:  # SCPI NaN sentinel, distinct from overload.
        return None, 'unavailable'
    if not math.isfinite(value) or abs(value) >= 9.9e37:
        return None, 'overload'
    return value, 'ok'


class Keysight344xxA:
    category = 'multimeter'
    simulated = False
    experiment_capabilities = ('measure.scalar', 'measure.dc_voltage', 'measure.dc_current')
    experiment_channels = dict(dc_voltage='voltage_dc', dc_current='current_dc')

    @staticmethod
    def matches(identity):
        fields = [part.strip().upper() for part in identity.split(',')]
        return len(fields) == 4 and fields[0] in MAKERS and fields[1] in MODELS

    def __init__(self, transport, identity):
        if not self.matches(identity):
            raise ValueError('Unsupported Keysight DMM identity')
        self.transport, self.id, self.name = transport, transport.resource, identity.strip()
        self.model = identity.split(',')[1].strip().upper()
        # Keep profiles model-specific, but independent of HP/Agilent branding.
        self.device_type = 'KEYSIGHT:'+self.model
        self.current_function = None
        self._functions = {key: value for key, value in FUNCTIONS.items()
                           if self.model != '34401A' or key not in ('CAP', 'TEMPC', 'TEMPF')}
        self._truevolt = self.model in {'34460A', '34461A', '34465A', '34470A'}

    def functions(self):
        return [MeasurementFunction(key, label, channel, unit, True, 'primary')
                for key, (label, channel, unit, _) in self._functions.items()]

    def _function(self):
        native = self.transport.query('SENS:FUNC?').strip().strip('"').upper()
        if native == 'TEMP':
            unit = self.transport.query('UNIT:TEMP?').strip().upper()
            return {'C': 'TEMPC', 'F': 'TEMPF'}.get(unit)
        return next((key for key, spec in self._functions.items() if spec[3] == native), None)

    def select_function(self, function):
        if function not in self._functions:
            raise ValueError('Unsupported Keysight measurement function')
        self.transport.write('SENS:FUNC "'+self._functions[function][3]+'"')
        if function in ('TEMPC', 'TEMPF'):
            self.transport.write('UNIT:TEMP '+('C' if function == 'TEMPC' else 'F'))
        actual = self._function()
        self.current_function = actual
        if actual != function:
            raise ValueError('Keysight DMM did not confirm the requested measurement function')

    def _modified(self, function):
        # Refuse transformed values rather than labelling dB/scaled data as SI.
        command = 'CALC:SCAL:STAT?' if self._truevolt else 'CALC:STAT?'
        modified = self._enabled(command)
        if self.model != '34401A' and function not in ('CONT', 'DIODE'):
            modified |= self._enabled('SENS:'+self._functions[function][3]+':NULL:STAT?')
        return modified

    def _enabled(self, command):
        response = self.transport.query(command).strip().upper()
        if response not in ('0', '1', 'OFF', 'ON'):
            raise ValueError('Invalid Keysight boolean response')
        return response in ('1', 'ON')

    def read(self):
        function = self._function()
        self.current_function = function
        if function is None:
            return [Reading('measurement', None, '', 'unsupported_configuration')]
        _, channel, unit, _ = self._functions[function]
        # READ? starts a fresh acquisition, retaining range/integration settings.
        # Never wait for an external trigger or silently average a sample array.
        if (self.transport.query('TRIG:SOUR?').strip().upper() != 'IMM' or
                float(self.transport.query('TRIG:COUN?')) != 1 or
                float(self.transport.query('SAMP:COUN?')) != 1):
            return [Reading(channel, None, unit, 'unsupported_configuration')]
        if self._modified(function):
            return [Reading(channel, None, unit, 'unsupported_configuration')]
        timeout = self.transport.timeout
        try:
            # High NPLC, autorange and low-bandwidth AC conversions can be slow.
            self.transport.timeout = max(timeout, 15.)
            value, quality = parse_value(self.transport.query('READ?'))
        finally:
            self.transport.timeout = timeout
        actual = self._function()
        self.current_function = actual
        if actual != function or self._modified(function):
            value, quality = None, 'mode_changed'
        return [Reading(channel, value, unit, quality)]

    def close(self):
        self.transport.close()
