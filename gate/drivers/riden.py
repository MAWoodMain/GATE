"""RD6006 only: Modbus register values and operating limits are model specific."""
import math
from gate.model import MeasurementFunction, Reading


class RidenRD6006:
    category = 'power_supply'
    simulated = False
    device_type = 'RIDEN:RD6006'
    experiment_control_supported = True
    output_control_supported = True
    experiment_capabilities = ('measure.scalar', 'measure.dc_voltage', 'measure.dc_current', 'source.dc_voltage')
    experiment_channels = {'voltage': 'voltage', 'current': 'current'}
    experiment_required_controls = ('voltage', 'current_limit')
    experiment_limits = dict(voltage=[0., 60.], current=[0., 6.], power_w=360.)
    range_unavailable_reason = 'Fixed readback resolution; output limits are separate from measurement ranges.'

    @staticmethod
    def matches(identity):
        return identity.startswith('RIDEN,RD6006,')

    def __init__(self, transport):
        info = transport.read_registers(0, 4)
        if not 60060 <= info[0] <= 60064:
            raise ValueError(f'Unsupported Riden model code {info[0]}; only RD6006 is verified')
        serial = (info[1] << 16) | info[2]
        self.name = f'RIDEN,RD6006,{serial if serial not in (0, 0xffffffff) else "UNKNOWN"},{info[3]/100:.2f}'
        self.id = transport.resource
        self.transport = transport
        self._owns_output = False

    def range_controls(self):
        return []

    def functions(self):
        return [MeasurementFunction(key, label, key, unit, instrument_channel='output') for key, label, unit in (
            ('voltage', 'Voltage', 'V'), ('current', 'Current', 'A'), ('power', 'Power', 'W'),
            ('set_voltage', 'Voltage setpoint', 'V'), ('current_limit', 'Current limit', 'A'),
            ('output_enabled', 'Output enabled', ''), ('protection', 'Protection (0 none, 1 OVP, 2 OCP)', ''),
            ('regulation', 'Regulation (0 CV, 1 CC)', ''))]

    def read(self):
        r = self.transport.read_registers(8, 11)
        if r[10] not in (0, 1):
            raise ValueError('Invalid RD6006 output state')
        return [Reading('set_voltage', r[0]/100, 'V'), Reading('current_limit', r[1]/1000, 'A'),
                Reading('voltage', r[2]/100, 'V'), Reading('current', r[3]/1000, 'A'),
                Reading('power', ((r[4] << 16) | r[5])/100, 'W'),
                Reading('output_enabled', float(r[10]), ''), Reading('protection', float(r[8]), ''),
                Reading('regulation', float(r[9]), '')]

    def _set(self, register, value):
        self.transport.write_register(register, value)
        if self.transport.read_registers(register, 1) != [value]:
            raise ValueError('RD6006 configuration readback mismatch')

    @staticmethod
    def _value(value, maximum, scale):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= maximum:
            raise ValueError(f'RD6006 setpoint must be finite, 0–{maximum}')
        return round(value*scale)

    def set_voltage(self, value):
        self._set(8, self._value(value, 60, 100))

    def set_current_limit(self, value):
        raw = self._value(value, 6, 1000)
        if raw == 0:
            raise ValueError('RD6006 current limit must be at least 0.001 A')
        self._set(9, raw)

    def set_enabled(self, enabled):
        if not isinstance(enabled, bool):
            raise ValueError('enabled must be boolean')
        if enabled:
            r = self.transport.read_registers(8, 11)
            if r[0] > 6000 or not 1 <= r[1] <= 6000 or r[8] != 0:
                raise ValueError('RD6006 setpoints or protection state prevent output enable')
            # Even a lost acknowledgement may have energised the output.
            self._owns_output = True
        self._set(18, int(enabled))
        if not enabled:
            self._owns_output = False

    def close(self):
        try:
            if self._owns_output:
                self.set_enabled(False)
        finally:
            self.transport.close()
