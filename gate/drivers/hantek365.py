"""Hantek 365-series USB DMM: SI values, explicit modes, no reset on connect."""
import time
from gate.model import Reading, MeasurementFunction, DEFAULT_TEMPERATURE_UNIT

FUNCTIONS = {
    'VDC': ('DC voltage', 'voltage_dc', 'V', 0xA0),
    'VAC': ('AC voltage', 'voltage_ac', 'V', 0xB0),
    'MADC': ('DC current (mA terminals)', 'current_dc', 'A', 0xC0),
    'ADC': ('DC current (A terminals)', 'current_dc', 'A', 0xC3),
    'MAAC': ('AC current (mA terminals)', 'current_ac', 'A', 0xD0),
    'AAC': ('AC current (A terminals)', 'current_ac', 'A', 0xD3),
    'OHMS': ('Resistance', 'resistance', 'Ω', 0xE0),
    'DIODE': ('Diode test', 'diode', 'V', 0xF0),
    'CAP': ('Capacitance', 'capacitance', 'F', 0xF1),
    'CONT': ('Continuity', 'continuity', 'Ω', 0xF2),
    'TEMPC': ('Temperature (Celsius)', 'temperature', DEFAULT_TEMPERATURE_UNIT, 0xF5),
    'TEMPF': ('Temperature (Fahrenheit)', 'temperature', '°F', 0xF6),
}


def parse_packet(packet):
    if len(packet) != 15 or packet[0] != 0xA0 or packet[-2:] != b'\r\n':
        raise ValueError('Invalid Hantek measurement frame')
    sign, digits, decimal, flags, nano, multiplier, units = packet[1], packet[2:6], packet[7], packet[8], packet[9], packet[10], packet[11]
    if sign not in (ord('+'), ord('-'), ord(' '), 0, 2, 4) or decimal not in (0x30, 0x31, 0x32, 0x34, 0x38):
        raise ValueError('Invalid Hantek sign or decimal point')
    scale = {0:1., 0x08:1., 0x10:1e6, 0x20:1e3, 0x40:1e-3, 0x80:1e-6}.get(multiplier)
    if scale is None or nano & 2 and multiplier not in (0, 8):
        raise ValueError('Invalid Hantek multiplier')
    if nano & 2:
        scale = 1e-9
    if flags & 8 and flags & 16:
        raise ValueError('Conflicting Hantek AC/DC indicators')
    if units == 2:
        function = 'TEMPC'
    elif units == 1:
        function = 'TEMPF'
    elif units == 4:
        function = 'CAP'
    elif units == 0x20:
        function = 'CONT' if multiplier == 8 else 'OHMS'
    elif units == 0x80:
        function = 'VAC' if flags & 8 else 'VDC' if flags & 16 else 'DIODE'
    elif units == 0x40:
        function = ('MAAC' if scale < 1 else 'AAC') if flags & 8 else ('MADC' if scale < 1 else 'ADC')
    else:
        raise ValueError('Unknown Hantek measurement units')
    _, channel, unit, _ = FUNCTIONS[function]
    if function.startswith('TEMP') and scale != 1:
        raise ValueError('Temperature cannot have an SI multiplier')
    quality = 'relative' if flags & 4 else 'ok'
    if all(48 <= digit <= 57 for digit in digits):
        decimal_places = {0x30:0, 0x31:3, 0x32:2, 0x34:1, 0x38:0}[decimal]
        value = int(digits)*scale/(10**decimal_places)
        if sign in (ord('-'), 4):
            value = -value
    elif b'?' in digits or b'OL' in digits.upper():
        value, quality = None, 'overload'
    else:
        raise ValueError('Invalid Hantek display digits')
    if value is not None and function in ('TEMPC', 'TEMPF'):
        low, high = (-200., 1000.) if function == 'TEMPC' else (-328., 1832.)
        if not low <= value <= high:
            value, quality = None, 'out_of_range'
    return Reading(channel, value, unit, quality), function


class Hantek365:
    device_type = 'HANTEK:365'
    category = 'multimeter'
    simulated = False
    experiment_capabilities = ('measure.scalar', 'measure.temperature', 'measure.dc_voltage', 'measure.dc_current')
    # A single thermocouple is a DUT probe by default. Never present that same
    # sensor as a second independent ambient probe.
    experiment_channels = dict(temperature='temperature', dut_temperature='temperature',
                               dc_voltage='voltage_dc', dc_current='current_dc')

    @staticmethod
    def matches(identity):
        return identity.startswith('Hantek,365 series,')

    def __init__(self, transport, identity='Hantek,365 series,,USB'):
        self.transport, self.id, self.name = transport, transport.resource, identity
        self.current_function = None
        self._native_function = None
        self._temperature_function = 'TEMPC'
        self.last_reading = Reading('measurement', None, '', 'unavailable')
        self.last_packet = None
        self._timeouts = 0
        self.read()  # Validate the binary protocol without changing mode.

    def functions(self):
        return [MeasurementFunction(key, label, channel, unit, True, 'primary')
                for key, (label, channel, unit, _) in FUNCTIONS.items()]

    def read(self):
        try:
            packet = self.transport.request()
        except TimeoutError:
            self._timeouts += 1
            if self._timeouts >= 3:
                raise
            packet = None
        else:
            self._timeouts = 0
        if packet is None:
            previous = self.last_reading
            return [Reading(previous.channel, None, previous.unit, 'not_ready')]
        return self.accept_packet(packet)

    def accept_packet(self, packet):
        reading, function = parse_packet(packet)
        self._native_function = function
        if function in ('TEMPC', 'TEMPF'):
            preferred = self._temperature_function
            if function != preferred:
                value = reading.value
                if value is not None:
                    relative = reading.quality == 'relative'
                    if preferred == 'TEMPC':
                        value = (value if relative else value-32)*5/9
                    else:
                        value = value*9/5+(0 if relative else 32)
                reading = Reading(reading.channel, value, FUNCTIONS[preferred][2], reading.quality)
            function = preferred
        self.current_function, self.last_reading, self.last_packet = function, reading, packet
        return [reading]

    def select_function(self, function):
        if function not in FUNCTIONS:
            raise ValueError('Unsupported Hantek measurement function')
        mode = FUNCTIONS[function][3]
        # The high-current ranges require cycling through the mA sub-ranges.
        modes = [mode] if mode & 0xF0 == 0xF0 else list(range(mode & 0xF0, mode+1))
        for step in modes:
            self.transport.set_mode(step)
            time.sleep(.05)
        # Discard the previous display and half-settled conversions following ACK.
        time.sleep(1.)
        deadline = time.monotonic()+5.
        conversions = 0
        while time.monotonic() < deadline:
            readings = self.read()
            if readings[0].quality != 'not_ready' and self._native_function == function:
                conversions += 1
                if conversions >= 3:
                    if function in ('TEMPC', 'TEMPF'):
                        self._temperature_function = function
                        self.accept_packet(self.last_packet)
                    return
            time.sleep(.1)
        raise ValueError('Hantek did not confirm the requested measurement function')

    def close(self):
        self.transport.close()
