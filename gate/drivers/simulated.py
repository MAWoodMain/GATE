import math
import time
from gate.model import Reading, MeasurementFunction

class Simulated:
    simulated = True
    def __init__(self, category):
        self.category = category
        self.experiment_capabilities = (
            ('source.dc_voltage', 'measure.dc_voltage', 'measure.dc_current')
            if category == 'power_supply' else ()) + ('measure.scalar',)
        self.id = "demo:" + category
        self.name = "SIMULATED · " + category.replace("_", " ").title()
        self.voltage = 0.0
        self.enabled = False
    def functions(self):
        channels = {
            'multimeter': [('voltage_dc', 'DC voltage', 'V')],
            'power_supply': [('set_voltage', 'Voltage setpoint', 'V'), ('output_enabled', 'Output enabled', ''),
                             ('voltage', 'Voltage', 'V'), ('current', 'Current', 'A')],
            'oscilloscope': [('C1.PKPK', 'C1 · Peak-to-peak', 'V'), ('C1.FREQ', 'C1 · Frequency', 'Hz')],
        }
        return [MeasurementFunction(channel, label, channel, unit) for channel, label, unit in channels[self.category]]

    def read(self):
        wobble = math.sin(time.monotonic()) * 0.003
        if self.category == "power_supply":
            return [Reading("set_voltage", self.voltage, "V"),
                    Reading("output_enabled", float(self.enabled), ""),
                    Reading("voltage", self.voltage + wobble if self.enabled else 0., "V"),
                    Reading("current", self.voltage / 100 if self.enabled else 0., "A")]
        if self.category == "multimeter":
            return [Reading("voltage_dc", 3.3 + wobble, "V")]
        return [Reading("C1.PKPK", 2.0 + wobble, "V"), Reading("C1.FREQ", 1000., "Hz")]
    def set_voltage(self, voltage):
        if self.category != "power_supply" or not math.isfinite(voltage) or not 0 <= voltage <= 30:
            raise ValueError("Demo PSU range: 0–30 V")
        self.voltage = voltage
    def set_enabled(self, enabled):
        self.enabled = bool(enabled)
    def close(self):
        self.enabled = False


class ConverterBench:
    """Coupled 5 V buck converter simulation, independent of the original demo bench."""
    def __init__(self):
        self.supply = ConverterInstrument(self, 'power_supply')
        self.load = ConverterInstrument(self, 'electronic_load')

    def output(self):
        supply, load = self.supply, self.load
        voltage = min(5., max(0., supply.voltage - .5)) if supply.enabled else 0.
        current = load.current if load.enabled and voltage else 0.
        # Simulate the DUT's 3 A output knee and the supply's input current limit.
        voltage *= max(.1, 1. - max(0., current - 3.) * .3)
        efficiency = .88 + .03 * min(current, 1.)
        input_current = voltage * current / (efficiency * supply.voltage) if supply.enabled and supply.voltage else 0.
        if input_current > supply.current_limit:
            voltage *= supply.current_limit / input_current
            input_current = supply.current_limit
        return voltage, current, input_current


class ConverterInstrument(Simulated):
    experiment_control_supported = True
    experiment_channels = {'voltage': 'voltage', 'current': 'current'}
    def __init__(self, bench, category):
        super().__init__(category)
        self.bench = bench
        self.id = 'demo:converter_supply' if category == 'power_supply' else 'demo:electronic_load'
        self.name = 'SIMULATED · Converter ' + ('supply' if category == 'power_supply' else 'load')
        self.current = 0.
        self.current_limit = 3.
        self.experiment_required_controls = ('voltage', 'current_limit') if category == 'power_supply' else ('current',)
        self.experiment_capabilities = ('measure.scalar', 'measure.dc_voltage', 'measure.dc_current',
            'source.dc_voltage' if category == 'power_supply' else 'sink.dc_current')
        self.experiment_limits = dict(voltage=[0., 30.], current=[0., 5.], power_w=150.)

    def functions(self):
        return [MeasurementFunction(key, label, key, unit) for key, label, unit in
                [('voltage', 'DC voltage', 'V'), ('current', 'DC current', 'A'),
                 ('output_enabled', 'Output enabled', '')]]

    def read(self):
        voltage, current, input_current = self.bench.output()
        if self.category == 'power_supply':
            voltage, current = (self.voltage if self.enabled else 0.), input_current
        return [Reading('voltage', voltage, 'V'), Reading('current', current, 'A'),
                Reading('output_enabled', float(self.enabled), '')]

    def set_current(self, value):
        if self.category != 'electronic_load' or not math.isfinite(value) or not 0 <= value <= 5:
            raise ValueError('Demo load range: 0–5 A')
        self.current = value

    def set_current_limit(self, value):
        if not math.isfinite(value) or not 0 < value <= 5:
            raise ValueError('Demo current limit range: >0–5 A')
        self.current_limit = value


class SimulatedThermometer:
    id = 'demo:temperatures'
    name = 'SIMULATED · DUT and ambient probes'
    category = 'thermometer'
    simulated = True
    experiment_capabilities = ('measure.scalar', 'measure.temperature')
    experiment_channels = {'dut_temperature':'dut_temperature', 'ambient_temperature':'ambient_temperature'}
    def __init__(self, bench):
        self.bench = bench
    def functions(self):
        return [MeasurementFunction(key, label, key, '°C') for key, label in
                [('dut_temperature', 'DUT temperature'), ('ambient_temperature', 'Ambient temperature')]]
    def read(self):
        voltage, current, input_current = self.bench.output()
        ambient = 22.5 + .03*math.sin(time.monotonic())
        loss = max(0., self.bench.supply.voltage*input_current-voltage*current)
        return [Reading('ambient_temperature', ambient, '°C'), Reading('dut_temperature', ambient+3+7*loss, '°C')]
    def close(self):
        pass


class SimulatedWaveformRecorder:
    id = 'demo:waveforms'
    name = 'SIMULATED · Ripple and protection recorder'
    category = 'oscilloscope'
    simulated = True
    experiment_capabilities = ('measure.scalar', 'capture.waveform')
    waveform_capabilities = {'C1':dict(unit='V', phases=['settled', 'transition'], max_points=16384,
                                     configurable_window=True, verification='simulated')}
    def __init__(self, bench):
        self.bench = bench
        self.pending = {}
    def functions(self):
        return [MeasurementFunction('voltage', 'Output voltage', 'voltage', 'V')]
    def read(self):
        return [Reading('voltage', self.bench.output()[0], 'V')]
    def begin_waveform(self, request):
        import uuid
        token = uuid.uuid4().hex
        self.pending[token] = dict(request=dict(request), before=self.bench.output()[0])
        return token
    def poll_waveform(self, token):
        from gate.waveforms import Waveform
        item = self.pending[token]
        request = item['request']
        voltage, current, _ = self.bench.output()
        before = item['before']
        transition = request['phase'] == 'transition'
        if transition:
            level = request['trigger']['level']
            crossed = before >= level > voltage if request['trigger']['slope'] == 'falling' else before <= level < voltage
            if not crossed:
                return None
        window = request['window_s'] or (.02 if transition else .002)
        pretrigger = request['pretrigger'] if request['pretrigger'] is not None else .25
        count = request['points']
        dt = window/(count-1)
        t0 = -window*pretrigger if transition else -window/2
        values = []
        amplitude = (.01+.005*current)/2
        for index in range(count):
            t = t0+index*dt
            if transition:
                value = before if t < 0 else voltage+(before-voltage)*math.exp(-t/(window/30))*math.cos(2*math.pi*t/(window/12))
            else:
                value = voltage+amplitude*math.sin(2*math.pi*100000*t)
            values.append(value)
        self.pending.pop(token)
        return Waveform('C1', values, dt, t0, 'V', trigger_utc_ns=time.time_ns() if transition else None,
                        metadata=dict(simulated=True, trigger_observed=transition, settings='synthetic converter model'))
    def cancel_waveform(self, token):
        self.pending.pop(token, None)
    def close(self):
        self.pending.clear()
