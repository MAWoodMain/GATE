"""Range protocol confirmation, model catalogs and saved-setting lifecycle."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from gate.drivers.fluke45 import Fluke45
from gate.drivers.keysight344xxa import Keysight344xxA, MODELS
from gate.drivers.siglent import SiglentSDS
from gate.drivers.hantek365 import Hantek365
from gate.drivers.simulated import Simulated, ConverterBench, SimulatedThermometer, SimulatedWaveformRecorder
from gate.engine import Engine
from gate.model import InstrumentChannel, MeasurementFunction, Reading
from gate.ranges import range_catalog, numeric_control, range_option


class MultiChannelInstrument:
    """SMU-like fixture with independent, deliberately different function sets."""
    id = '/dev/usbtmc6'
    name = 'Fixture,SMU,serial,1'
    category = 'smu'
    simulated = True

    def __init__(self):
        self.current_functions = {'source': 'source.voltage', 'measure': 'measure.resistance'}
        self.applied = []

    def channels(self):
        return [InstrumentChannel('source', 'Source'), InstrumentChannel('measure', 'Measurement')]

    def functions(self):
        # The reading keys can be reused by mutually exclusive functions. IDs
        # remain unique across the instrument and carry channel ownership.
        return [MeasurementFunction(identity, identity, channel+'.value', unit, True, 'mode', channel)
                for channel, identity, unit in [('source', 'source.voltage', 'V'), ('source', 'source.current', 'A'),
                    ('measure', 'measure.voltage', 'V'), ('measure', 'measure.resistance', 'Ω'), ('measure', 'measure.current', 'A')]]

    def range_controls(self):
        return [numeric_control(function.id+'.range', function.label, function.unit,
                                [1., 10.] if function.instrument_channel == 'source' else [100., 1000.],
                                function.id, instrument_channel=function.instrument_channel) for function in self.functions()]

    def select_function(self, identity):
        function = next(item for item in self.functions() if item.id == identity)
        self.current_functions[function.instrument_channel] = identity
        self.applied.append(('function', identity))

    def range_functions(self):
        return dict(self.current_functions)

    def select_range(self, control_id, option_id):
        control, option = range_option(self, control_id, option_id)
        if self.current_functions[control.instrument_channel] != control.function:
            raise ValueError('Inactive function')
        self.applied.append(('range', control.id, option.id))

    def read(self):
        return [Reading('source.value', 1., 'V'), Reading('measure.value', 100., 'Ω')]

    def close(self):
        pass


class RangeTests(unittest.TestCase):
    def test_independent_channels_with_different_function_sets_and_ranges(self):
        engine = Engine()
        instrument = MultiChannelInstrument()
        with patch('gate.discovery.connect', return_value=instrument):
            key = engine.command('connect', {'resource': instrument.id})['instrument']
        card = engine.snapshot()['instruments'][0]
        self.assertEqual([len(channel['functions']) for channel in card['channels']], [2, 3])
        settings = dict(channel_functions={'source': 'source.current', 'measure': 'measure.voltage'},
                        display_functions=['source.current', 'measure.voltage'],
                        ranges={'source.current.range': '10', 'measure.voltage.range': '1000',
                                'measure.resistance.range': '100'})
        engine.command('instrument_settings', dict(instrument=key, settings=settings))
        self.assertEqual(instrument.applied, [('function', 'source.current'), ('function', 'measure.voltage'),
                                             ('range', 'source.current.range', '10'), ('range', 'measure.voltage.range', '1000')])
        instrument.applied.clear()
        engine.command('select_function', dict(instrument=key, function='measure.resistance', request='measure'))
        self.assertEqual(instrument.current_functions, {'source': 'source.current', 'measure': 'measure.resistance'})
        self.assertEqual(instrument.applied, [('function', 'measure.resistance'), ('range', 'measure.resistance.range', '100')])
        saved = engine.instrument_profiles[key]['settings']
        self.assertEqual(saved['channel_functions'], instrument.current_functions)
        self.assertEqual(saved['display_functions'], ['source.current', 'measure.resistance'])
        engine.command('display_measurements', dict(instrument=key, functions=['source.voltage', 'measure.resistance']))
        self.assertEqual(instrument.current_functions, {'source': 'source.voltage', 'measure': 'measure.resistance'})
        for invalid in ({'channel_functions': {'source': 'measure.voltage'}},
                        {'display_functions': ['source.voltage', 'source.current']}):
            with self.assertRaises(ValueError):
                engine.command('instrument_settings', dict(instrument=key, settings=invalid))

    def test_channel_function_and_range_restore_after_reconnect(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'devices.json'
            engine = Engine(profile_path=path)
            original = MultiChannelInstrument()
            with patch('gate.discovery.connect', return_value=original):
                key = engine.command('connect', {'resource': original.id})['instrument']
            settings = dict(channel_functions={'source': 'source.current', 'measure': 'measure.voltage'},
                            ranges={'source.current.range': '10', 'measure.voltage.range': '1000'})
            engine.command('instrument_settings', dict(instrument=key, settings=settings))
            restarted = Engine(profile_path=path)
            replacement = MultiChannelInstrument()
            with patch('gate.discovery.connect', return_value=replacement):
                restarted.command('connect', {'resource': replacement.id})
            self.assertEqual(replacement.current_functions, settings['channel_functions'])
            self.assertEqual(replacement.applied[-2:], [('range', 'source.current.range', '10'),
                                                       ('range', 'measure.voltage.range', '1000')])
            restarted.cycle()
            self.assertEqual(restarted.snapshot()['instruments'][0]['current_functions'], settings['channel_functions'])

    def test_top_bar_applies_auto_after_function_change_and_saves_only_on_confirmation(self):
        engine = Engine()
        meter = MultiChannelInstrument()
        with patch('gate.discovery.connect', return_value=meter):
            key = engine.command('connect', {'resource': meter.id})['instrument']
        engine.command('display_measurements', dict(instrument=key,
            functions=['source.current', 'measure.resistance'], ranges={'source.current.range': 'auto'}))
        self.assertEqual(meter.applied, [('function', 'source.current'), ('range', 'source.current.range', 'auto')])
        self.assertEqual(engine.instrument_profiles[key]['settings']['ranges'], {'source.current.range': 'auto'})
        before = dict(engine.instrument_profiles[key]['settings'])
        meter.select_range = Mock(side_effect=OSError('Range confirmation failed'))
        with self.assertRaises(OSError):
            engine.command('display_measurements', dict(instrument=key,
                functions=['source.voltage', 'measure.resistance'], ranges={'source.voltage.range': 'auto'}))
        self.assertEqual(engine.instrument_profiles[key]['settings'], before)
        with self.assertRaises(ValueError):
            engine.command('display_measurements', dict(instrument=key, functions=[], ranges=[]))

    def test_top_bar_auto_can_apply_to_an_already_active_or_passive_function(self):
        engine = Engine()
        engine.command('demo', {})
        meter = engine.instruments['demo:multimeter']
        meter.select_range('voltage_dc', '1')
        engine.command('display_measurements', dict(instrument=meter.id, functions=['voltage_dc'], ranges={'voltage_dc': 'auto'}))
        self.assertEqual(meter.selected_range, 'auto')

    def keysight(self, model='34461A'):
        transport = Mock(resource='/dev/usbtmc8', timeout=1.)
        replies = {'SENS:FUNC?': '"VOLT:DC"', 'SENS:VOLT:DC:RANG:AUTO?': '0',
                   'SENS:VOLT:DC:RANG?': '10'}
        transport.query.side_effect = replies.__getitem__
        return Keysight344xxA(transport, f'Keysight,{model},123,1'), transport, replies

    def test_model_specific_catalogs_are_passive(self):
        for model in MODELS:
            meter, transport, _ = self.keysight(model)
            catalog = {control.id: control for control in meter.range_controls()}
            values = lambda key: [option.value for option in catalog[key].options if not option.automatic]
            self.assertEqual(values('VDC'), [.1, 1., 10., 100., 1000.])
            self.assertEqual(values('VAC')[-1], 750.)
            self.assertEqual(values('ADC')[0], 1e-6 if model in {'34465A', '34470A'} else .01 if model == '34401A' else 1e-4)
            self.assertEqual(10. in values('ADC'), model in {'34461A', '34465A', '34470A'})
            self.assertEqual('CAP' in catalog, model != '34401A')
            if model != '34401A':
                self.assertEqual(values('CAP')[-1], 1e-5 if model in {'34410A', '34411A'} else 1e-4)
            transport.query.assert_not_called()
            transport.write.assert_not_called()
            self.assertEqual(range_catalog(meter)[0]['options'][0]['id'], 'auto')

    def test_keysight_fixed_auto_and_rejected_readback(self):
        meter, transport, replies = self.keysight()
        meter.select_range('VDC', '10')
        transport.write.assert_called_once_with('SENS:VOLT:DC:RANG 10')
        replies['SENS:VOLT:DC:RANG:AUTO?'] = '1'
        meter.select_range('VDC', 'auto')
        transport.write.assert_called_with('SENS:VOLT:DC:RANG:AUTO ON')
        for changes in ({'SENS:VOLT:DC:RANG:AUTO?': '0'}, {'SENS:FUNC?': '"RES"'}):
            replies.update(changes)
            with self.assertRaises(ValueError):
                meter.select_range('VDC', 'auto')
        replies.update({'SENS:FUNC?': '"VOLT:DC"', 'SENS:VOLT:DC:RANG?': '100'})
        with self.assertRaises(ValueError):
            meter.select_range('VDC', '10')

    def test_unknown_choices_do_no_io(self):
        meter, transport, _ = self.keysight()
        for control, choice in [('VDC', '10;*RST'), ('TEMPC', '10'), ('VDC', 10), ('BOGUS', 'auto')]:
            with self.assertRaises(ValueError):
                meter.select_range(control, choice)
        transport.query.assert_not_called()
        transport.write.assert_not_called()

    def test_keysight_ten_amp_choice_never_switches_terminals(self):
        meter, transport, replies = self.keysight()
        replies.update({'SENS:FUNC?': '"CURR:DC"', 'SENS:CURR:DC:TERM?': '3',
                        'SENS:CURR:DC:RANG:AUTO?': '0', 'SENS:CURR:DC:RANG?': '10'})
        with self.assertRaisesRegex(ValueError, '10 A current terminal'):
            meter.select_range('ADC', '10')
        transport.write.assert_not_called()
        replies['SENS:CURR:DC:TERM?'] = '10'
        meter.select_range('ADC', '10')
        transport.write.assert_called_once_with('SENS:CURR:DC:RANG 10')

    def test_fluke_range_codes_preserve_rate_and_confirm_state(self):
        transport = Mock(resource='serial:///dev/ttyUSB0')
        replies = {'FUNC1?': 'VDC', 'AUTO?': '0', 'RANGE1?': '2', 'RANGE 2': '', 'FIXED': '', 'AUTO': ''}
        transport.query.side_effect = replies.__getitem__
        meter = Fluke45(transport, 'FLUKE,45,123,1.7')
        catalog = {control.id: control for control in meter.range_controls()}
        self.assertIn('1 V / 3 V', catalog['VDC'].options[2].label)
        self.assertNotIn('DIODE', catalog)
        meter.select_range('VDC', '2')
        self.assertTrue(meter._settling)
        replies['AUTO?'] = '1'
        meter.select_range('VDC', 'auto')
        replies['RANGE1?'], replies['AUTO?'] = '3', '0'
        with self.assertRaises(ValueError):
            meter.select_range('VDC', '2')
        self.assertFalse(any(call.args[0].startswith('RATE') for call in transport.query.call_args_list))

    def test_scope_input_scale_respects_probe_factor_and_capture_ownership(self):
        transport = Mock(resource='/dev/usbtmc1')
        replies = {'C1:ATTN?': 'C1:ATTN 10', 'C1:VDIV?': 'C1:VDIV 5V'}
        transport.query.side_effect = replies.__getitem__
        scope = SiglentSDS(transport, 'SIGLENT,SDS1202X-E,123,1')
        self.assertEqual([control.id for control in scope.range_controls()], ['C1', 'C2'])
        scope.select_range('C1', '0.5')
        transport.write.assert_called_once_with('C1:VDIV 5V')
        replies['C1:VDIV?'] = 'C1:VDIV 10V'
        with self.assertRaises(ValueError):
            scope.select_range('C1', '0.5')
        scope._waveform_requests['capture'] = {}
        transport.reset_mock()
        with self.assertRaises(ValueError):
            scope.select_range('C1', '1')
        transport.query.assert_not_called()
        transport.write.assert_not_called()

    def test_explicit_unsupported_catalogs_and_demo_overload(self):
        self.assertEqual(Hantek365.range_controls(None), [])
        bench = ConverterBench()
        for instrument in (bench.supply, bench.load, Simulated('oscilloscope'),
                           SimulatedThermometer(bench), SimulatedWaveformRecorder(bench)):
            self.assertEqual(range_catalog(instrument), [])
            self.assertTrue(instrument.range_unavailable_reason)
        meter = Simulated('multimeter')
        meter.select_range('voltage_dc', '1')
        self.assertEqual((meter.read()[0].value, meter.read()[0].quality), (None, 'overload'))
        meter.select_range('voltage_dc', 'auto')
        self.assertEqual(meter.read()[0].quality, 'ok')

    def connect(self, engine, resource='/dev/usbtmc8'):
        meter, transport, replies = self.keysight()
        transport.resource = resource
        meter.id = resource
        with patch('gate.discovery.connect', return_value=meter):
            key = engine.command('connect', dict(resource=resource))['instrument']
        return key, meter, transport, replies

    def test_saved_range_reconnect_offline_validation_and_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'devices.json'
            engine = Engine(profile_path=path)
            key, meter, transport, replies = self.connect(engine)
            self.assertTrue(engine.snapshot()['instruments'][0]['range_controls'])
            engine.command('select_range', dict(instrument=key, control='VDC', range='10'))
            settings = {'ranges': {'VDC': '10'}}
            self.assertEqual(engine.instrument_profiles[key]['settings'], settings)
            replies['SENS:VOLT:DC:RANG?'] = '100'
            with self.assertRaises(ValueError):
                engine.command('select_range', dict(instrument=key, control='VDC', range='1'))
            self.assertEqual(engine.instrument_profiles[key]['settings'], settings)
            restarted = Engine(profile_path=path)
            with self.assertRaises(ValueError):
                restarted.command('instrument_settings', dict(instrument=key, settings={'ranges': {'VDC': 'garbage'}}))
            _, _, replacement, _ = self.connect(restarted, 'tcp://meter:5025')
            replacement.write.assert_called_once_with('SENS:VOLT:DC:RANG 10')
            self.assertEqual(restarted.instrument_profiles[key]['settings'], settings)

    def test_settings_validate_before_writes_and_apply_after_function(self):
        engine = Engine()
        key, meter, transport, replies = self.connect(engine)
        for selected in ([], {'VDC': 10}, {'UNKNOWN': 'auto'}, {'VDC': 'auto;*RST'}):
            with self.assertRaises(ValueError):
                engine.command('instrument_settings', dict(instrument=key, settings={'function': 'VAC', 'ranges': selected}))
        transport.write.assert_not_called()
        settings = {'function': 'VDC', 'ranges': {'VDC': '10', 'VAC': '1'}}
        engine.command('instrument_settings', dict(instrument=key, settings=settings))
        self.assertEqual([call.args[0] for call in transport.write.call_args_list],
                         ['SENS:FUNC "VOLT:DC"', 'SENS:VOLT:DC:RANG 10'])
        transport.reset_mock()
        request = 'switch'
        engine.command('select_function', dict(instrument=key, function='VDC', request=request))
        self.assertEqual(engine.snapshot()['controls'][request]['state'], 'applied')
        transport.write.assert_any_call('SENS:VOLT:DC:RANG 10')

    def test_range_commands_blocked_during_run_and_sequence(self):
        engine = Engine()
        engine.command('demo', {})
        args = dict(instrument='demo:multimeter', control='voltage_dc', range='1')
        for running in ('run', 'sequence'):
            engine.experiment_run = SimpleNamespace(active=True) if running == 'run' else None
            engine.experiment_sequence = {'state': 'running'} if running == 'sequence' else None
            with self.assertRaisesRegex(ValueError, 'Stop the active experiment'):
                engine.command('select_range', args)
        self.assertEqual(engine.instruments['demo:multimeter'].selected_range, 'auto')


if __name__ == '__main__':
    unittest.main()
