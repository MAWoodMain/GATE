"""Fluke 45 display acquisition; preserves front-panel settings."""
import math
import re
from gate.model import Reading, MeasurementFunction

FUNCTIONS = {
    'VDC': 'V', 'VAC': 'V', 'VACDC': 'V', 'ADC': 'A', 'AAC': 'A',
    'AACDC': 'A', 'OHMS': 'Ω', 'FREQ': 'Hz', 'DIODE': 'V', 'CONT': 'V',
}
FUNCTION_LABELS = {
    'VDC': 'DC voltage', 'VAC': 'AC voltage', 'VACDC': 'AC + DC voltage',
    'ADC': 'DC current', 'AAC': 'AC current', 'AACDC': 'AC + DC current',
    'OHMS': 'Resistance', 'FREQ': 'Frequency', 'DIODE': 'Diode test', 'CONT': 'Continuity',
}
VALUE = re.compile(r'^([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:E[+-]?\d+)?)\s*([A-Z]*)$', re.I)

def parse_value(text, function):
    token = text.strip().upper()
    if token in ('OL', 'OVERLOAD', '+INF', '-INF', 'INF', 'NAN'):
        return None, 'overload'
    match = VALUE.fullmatch(token)
    if not match:
        raise ValueError(f'Unexpected Fluke 45 reading: {text[:100]}')
    suffix = {'OHM': 'OHMS'}.get(match[2], match[2])
    expected = {'FREQ': 'HZ', 'DIODE': 'VDC', 'CONT': 'VDC'}.get(function, function)
    if suffix and suffix != expected:
        return None, 'mode_changed'
    value = float(match[1])
    # Fluke 45 manual, numeric output: +1E+9 = overload, +1E-9 =
    # underload in the high resistance ranges. Preserve genuine tiny volts.
    if not math.isfinite(value) or abs(value) >= 1e9:
        return None, 'overload'
    if function == 'OHMS' and value == 1e-9:
        return None, 'underload'
    return value, 'ok'

class Fluke45:
    device_type = 'FLUKE:45'
    category = 'multimeter'
    simulated = False

    @staticmethod
    def matches(identity):
        fields = [field.strip().upper() for field in identity.split(',')]
        return len(fields) == 4 and fields[0] == 'FLUKE' and fields[1] == '45'

    def __init__(self, transport, identity):
        self.transport = transport
        self.id = transport.resource
        self.name = identity.strip()
        self.current_function = None
        self._settling = False

    def functions(self):
        # Primary display only. Secondary display readings remain passive.
        return [MeasurementFunction(code, FUNCTION_LABELS[code], f'primary.{code}', unit, True)
                for code, unit in FUNCTIONS.items()]

    def select_function(self, function):
        if function not in FUNCTIONS:
            raise ValueError('Unsupported Fluke 45 primary measurement function')
        # Function mnemonics are fixed by the driver, never arbitrary SCPI input.
        self.transport.query(function)
        actual = self.transport.query('FUNC1?').strip().upper()
        if actual != function:
            raise ValueError(f'Fluke 45 did not select {function}; reports {actual}')
        self.current_function = actual
        self._settling = True

    def read(self):
        timeout = getattr(self.transport, 'timeout', None)
        if self._settling and timeout is not None:
            # First resistance/range conversion after a mode change can take
            # longer than a normal display query. Keep this bounded.
            self.transport.timeout = max(timeout, 10.0)
        try:
            return self._read_displays()
        finally:
            if timeout is not None:
                self.transport.timeout = timeout
            self._settling = False

    def _read_displays(self):
        primary = self.transport.query('FUNC1?').strip().upper()
        self.current_function = primary
        modifiers = int(self.transport.query('MOD?'))
        values = [value.strip() for value in self.transport.query('VAL?').split(',')]
        if primary not in FUNCTIONS or not 0 <= modifiers <= 127 or len(values) not in (1, 2):
            raise ValueError('Unsupported Fluke 45 display configuration')
        secondary = self.transport.query('FUNC2?').strip().upper() if len(values) == 2 else None
        # Recheck units after fetching: changing a front-panel function mid-cycle
        # must not silently attach the previous function's units to a new value.
        changed = (self.transport.query('FUNC1?').strip().upper() != primary or
                   int(self.transport.query('MOD?')) != modifiers)
        readings = []
        for index, function in enumerate((primary, secondary)):
            display = 'primary' if index == 0 else 'secondary'
            if function is None:
                readings.append(Reading('secondary.disabled', None, '', 'disabled'))
                continue
            if function not in FUNCTIONS:
                raise ValueError(f'Unknown Fluke 45 function: {function}')
            value, quality = parse_value(values[index], function)
            unit = FUNCTIONS[function]
            channel = f'{display}.{function}'
            if index == 0:
                if modifiers & 16:
                    unit, channel = 'W', 'primary.power'
                elif modifiers & 8:
                    unit, channel = 'dB', 'primary.decibels'
                # Include modifier state so held/relative/min/max values are
                # distinguishable from ordinary measurements in saved series.
                if modifiers:
                    channel += f'.mod{modifiers}'
            if changed:
                value, quality = None, 'mode_changed'
            readings.append(Reading(channel, value, unit, quality))
        return readings

    def close(self):
        self.transport.close()
