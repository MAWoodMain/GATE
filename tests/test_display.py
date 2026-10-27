"""Displayed channels are independent, persistent, and respect hardware exclusivity."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from gate.engine import Engine
from gate.model import MeasurementFunction, Reading


class DisplayTests(unittest.TestCase):
    def meter(self, resource, serial='123'):
        device = SimpleNamespace(id=resource, name=f'FLUKE,45,{serial},1.7', device_type='FLUKE:45',
            simulated=False, category='multimeter', current_function='VDC', close=Mock(),
            functions=lambda:[MeasurementFunction(code, code, f'primary.{code}', unit, True, 'primary')
                              for code, unit in [('VDC','V'), ('FREQ','Hz')]],
            read=lambda:[Reading('primary.VDC', 1, 'V')])
        device.select_function = Mock(side_effect=lambda function: setattr(device, 'current_function', function))
        return device

    def connect(self, engine, device):
        with patch('gate.discovery.connect', return_value=device):
            return engine.command('connect', dict(resource=device.id))['instrument']

    def test_zero_or_many_measurements_on_several_instruments(self):
        engine = Engine()
        engine.command('demo', {})
        chosen = {'demo:power_supply':['voltage', 'current', 'set_voltage', 'output_enabled'],
                  'demo:oscilloscope':['C1.PKPK', 'C1.FREQ'], 'demo:multimeter':[]}
        for key, functions in chosen.items():
            engine.command('display_measurements', dict(instrument=key, functions=functions))
        for key, functions in chosen.items():
            self.assertEqual(engine.instrument_profiles[key]['settings']['display_functions'], functions)
        engine.command('display_measurements', dict(instrument='demo:oscilloscope', functions=[]))
        self.assertEqual(engine.instrument_profiles['demo:oscilloscope']['settings']['display_functions'], [])
        self.assertEqual(engine.instrument_profiles['demo:power_supply']['settings']['display_functions'], chosen['demo:power_supply'])
        self.assertFalse(engine.instruments['demo:power_supply'].enabled)

    def test_switch_primary_function_and_hide_without_switching_hardware(self):
        engine = Engine()
        meter = self.meter('serial:///dev/ttyUSB0?baud=9600')
        key = self.connect(engine, meter)
        engine.command('display_measurements', dict(instrument=key, functions=['VDC']))
        meter.select_function.assert_not_called()
        engine.command('display_measurements', dict(instrument=key, functions=['FREQ']))
        meter.select_function.assert_called_once_with('FREQ')
        engine.command('display_measurements', dict(instrument=key, functions=[]))
        meter.select_function.assert_called_once_with('FREQ')
        self.assertEqual(engine.instrument_profiles[key]['settings'], dict(function='FREQ', display_functions=[]))

    def test_hiding_a_passive_reading_does_not_reassert_a_checked_hardware_mode(self):
        engine = Engine()
        meter = self.meter('/dev/usbtmc1')
        functions = meter.functions() + [MeasurementFunction('aux', 'Auxiliary', 'aux', 'V')]
        meter.functions = lambda:functions
        key = self.connect(engine, meter)
        engine.command('display_measurements', dict(instrument=key, functions=['VDC', 'aux']))
        meter.current_function = 'FREQ'  # A front-panel change after checking the readings.
        engine.command('display_measurements', dict(instrument=key, functions=['VDC']))
        meter.select_function.assert_not_called()

    def test_failed_switch_retains_previous_preference(self):
        engine = Engine()
        meter = self.meter('/dev/usbtmc1')
        key = self.connect(engine, meter)
        engine.command('display_measurements', dict(instrument=key, functions=['VDC']))
        meter.select_function.side_effect = OSError('Meter refused frequency')
        result = engine.execute('display_measurements', dict(instrument=key, functions=['FREQ']))
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(engine.instrument_profiles[key]['settings']['display_functions'], ['VDC'])

    def test_validation_rejects_impossible_duplicate_and_unknown_selections(self):
        engine = Engine()
        key = self.connect(engine, self.meter('/dev/usbtmc1'))
        for functions in (['VDC', 'FREQ'], ['VDC', 'VDC'], ['unknown'], 'VDC', None, [123], [{}]):
            with self.subTest(functions=functions):
                result = engine.execute('display_measurements', dict(instrument=key, functions=functions))
                self.assertEqual(result['state'], 'failed')
                self.assertNotIn('display_functions', engine.instrument_profiles[key]['settings'])

    def test_preferences_survive_restart_copy_and_transport_handover(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'devices.json'
            engine = Engine(profile_path=path)
            first = self.connect(engine, self.meter('/dev/usbtmc1', 'A'))
            second = self.connect(engine, self.meter('/dev/usbtmc2', 'B'))
            engine.command('display_measurements', dict(instrument=first, functions=['FREQ']))
            engine.command('copy_settings', dict(instrument=second, source=first))
            self.assertEqual(engine.instrument_profiles[second]['settings']['display_functions'], ['FREQ'])
            engine.command('display_measurements', dict(instrument=first, functions=[]))
            restarted = Engine(profile_path=path)
            replacement = self.meter('tcp://meter:5025', 'A')
            self.assertEqual(self.connect(restarted, replacement), first)
            self.assertEqual(restarted.instrument_profiles[first]['settings']['display_functions'], [])
            self.assertEqual(restarted.instrument_profiles[second]['settings']['display_functions'], ['FREQ'])
            replacement.select_function.assert_called_once_with('FREQ')

    def test_agent_function_changes_keep_a_visible_exclusive_group_consistent(self):
        engine = Engine()
        key = self.connect(engine, self.meter('/dev/usbtmc1'))
        engine.command('display_measurements', dict(instrument=key, functions=['VDC']))
        engine.command('select_function', dict(instrument=key, function='FREQ'))
        self.assertEqual(engine.instrument_profiles[key]['settings']['display_functions'], ['FREQ'])
        engine.command('display_measurements', dict(instrument=key, functions=[]))
        engine.command('select_function', dict(instrument=key, function='VDC'))
        self.assertEqual(engine.instrument_profiles[key]['settings']['display_functions'], [])
