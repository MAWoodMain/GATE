"""Physical identity, connection handover, migration, settings isolation and removal."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from gate.engine import Engine
from gate.identity import identify
from gate.model import MeasurementFunction, Reading


class IdentityTests(unittest.TestCase):
    def device(self, resource, serial='123', model='45'):
        functions = [MeasurementFunction(code, code, f'primary.{code}', unit, True)
                     for code, unit in [('VDC', 'V'), ('FREQ', 'Hz')]]
        return SimpleNamespace(id=resource, name=f'FLUKE,{model},{serial},1.7', device_type=f'FLUKE:{model}',
                               category='multimeter', simulated=False, close=Mock(),
                               functions=lambda: functions, select_function=Mock(),
                               read=lambda: [Reading('primary.VDC', 1., 'V')])

    def connect(self, engine, resource, **kwargs):
        device = self.device(resource, **kwargs)
        with patch('gate.discovery.connect', return_value=device):
            result = engine.command('connect', dict(resource=resource))
        return result['instrument'], device

    def test_name_and_function_follow_serial_across_all_transports_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'devices.json'
            engine = Engine(profile_path=path)
            serial = 'serial:///dev/ttyUSB0?baud=9600'
            key, old = self.connect(engine, serial)
            engine.command('instrument_settings', dict(instrument=key, name='Reference meter', settings={'function':'FREQ'}))
            old.select_function.assert_called_once_with('FREQ')
            engine.cycle()
            for resource in ('/dev/usbtmc9', 'tcp://bench:5025', 'serial:///dev/ttyUSB8?baud=4800'):
                same_key, new = self.connect(engine, resource)
                self.assertEqual(key, same_key)
                old.close.assert_called_once()
                new.select_function.assert_called_once_with('FREQ')
                state = engine.snapshot()
                self.assertEqual(len(state['instruments']), 1)
                self.assertEqual(state['instruments'][0]['name'], 'Reference meter')
                self.assertEqual(state['instruments'][0]['resource'], resource)
                self.assertEqual(state['instruments'][0]['id'], key)
                old = new
            restarted = Engine(profile_path=path)
            self.assertEqual(restarted.instrument_profiles[key]['settings'], {'function':'FREQ'})
            same_key, device = self.connect(restarted, '/dev/usbtmc4')
            self.assertEqual(same_key, key)
            device.select_function.assert_called_once_with('FREQ')
            self.assertEqual(restarted.snapshot()['instruments'][0]['name'], 'Reference meter')

    def test_copy_compatible_settings_without_copying_identity_name_or_connections(self):
        engine = Engine()
        first, _ = self.connect(engine, '/dev/usbtmc1', serial='A')
        second, device = self.connect(engine, 'tcp://meter:5025', serial='B')
        other, _ = self.connect(engine, '/dev/usbtmc3', serial='C', model='87')
        engine.command('instrument_settings', dict(instrument=first, name='Source', settings={'function':'FREQ'}))
        engine.command('instrument_settings', dict(instrument=second, name='Target'))
        before = copy.deepcopy(engine.connections)
        engine.command('copy_settings', dict(instrument=second, source=first))
        device.select_function.assert_called_once_with('FREQ')
        target = engine.instrument_profiles[second]
        self.assertEqual(target['settings'], {'function':'FREQ'})
        self.assertEqual(target['serial'], 'B')
        self.assertEqual(target['name'], 'Target')
        self.assertEqual(engine.connections, before)
        engine.instrument_profiles[first]['settings']['function'] = 'VDC'
        self.assertEqual(target['settings']['function'], 'FREQ')
        with self.assertRaises(ValueError):
            engine.command('copy_settings', dict(instrument=second, source=other))

    def test_connection_removal_preserves_profile_and_active_other_method(self):
        engine = Engine()
        old = '/dev/usbtmc1'
        key, _ = self.connect(engine, old)
        current = 'tcp://scope:5025'
        _, device = self.connect(engine, current)
        engine.command('instrument_settings', dict(instrument=key, name='My instrument'))
        engine.command('forget', dict(instrument=key, resource=old, scope='connection'))
        device.close.assert_not_called()
        self.assertIn(key, engine.instruments)
        self.assertNotIn(old, engine.connections)
        self.assertEqual(engine.instrument_profiles[key]['name'], 'My instrument')
        engine.command('forget', dict(instrument=key, resource=current, scope='connection'))
        device.close.assert_called_once()
        self.assertIn(key, engine.instrument_profiles)
        self.assertFalse(engine.connections)
        self.assertFalse(engine.snapshot()['instruments'])
        engine.command('forget', dict(instrument=key, scope='instrument'))
        self.assertFalse(engine.instrument_profiles)

    def test_forget_instrument_removes_all_addresses_and_settings(self):
        engine = Engine()
        key, _ = self.connect(engine, '/dev/usbtmc1')
        _, device = self.connect(engine, 'tcp://meter:5025')
        engine.command('forget', dict(instrument=key, scope='instrument'))
        device.close.assert_called_once()
        self.assertFalse(engine.instruments)
        self.assertFalse(engine.connections)
        self.assertFalse(engine.saved_resources)
        self.assertFalse(engine.instrument_profiles)
        self.assertFalse(engine.snapshot()['instruments'])

    def test_different_serial_at_same_address_does_not_inherit_settings(self):
        engine = Engine()
        key, _ = self.connect(engine, '/dev/usbtmc1', serial='A')
        engine.command('instrument_settings', dict(instrument=key, name='Original', settings={'function':'FREQ'}))
        engine.command('disconnect', dict(instrument=key, forget=False))
        other, device = self.connect(engine, '/dev/usbtmc1', serial='B')
        self.assertNotEqual(key, other)
        self.assertFalse(engine.instrument_profiles[other]['settings'])
        self.assertEqual(engine.instrument_profiles[other]['name'], '')
        device.select_function.assert_not_called()
        self.assertEqual(engine.instrument_profiles[key]['name'], 'Original')

    def test_legacy_profile_migration_and_disconnect_persist(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'devices.json'
            address = 'serial:///dev/ttyUSB0?baud=9600'
            path.write_text(json.dumps(dict(resources=[address], devices={address:dict(name='FLUKE,45,123,1.7', category='multimeter')}, interval=.5)))
            engine = Engine(profile_path=path)
            expected = identify('FLUKE,45,123,1.7')['id']
            key, _ = self.connect(engine, address)
            self.assertEqual(key, expected)
            self.assertEqual(len(engine.instrument_profiles), 1)
            engine.command('disconnect', dict(instrument=key, forget=False))
            restarted = Engine(profile_path=path)
            with patch('gate.discovery.connect') as connect:
                restarted.command('connect', dict(resource=address, automatic=True))
                connect.assert_not_called()
            with patch('gate.discovery.connect', return_value=self.device('/dev/usbtmc7')):
                restarted.connect_resource('/dev/usbtmc7', automatic=True)
            self.assertFalse(restarted.instruments)
            self.assertEqual(restarted.interval, .5)
            self.assertEqual(json.loads(path.read_text())['schema_version'], 2)

    def test_invalid_settings_and_failed_handover_preserve_profile(self):
        engine = Engine()
        key, device = self.connect(engine, '/dev/usbtmc1')
        for settings in ({'output_enabled': True}, {'function':'OHMS'}):
            with self.assertRaises(ValueError):
                engine.command('instrument_settings', dict(instrument=key, settings=settings))
        engine.command('instrument_settings', dict(instrument=key, settings={'function':'FREQ'}))
        replacement = self.device('tcp://meter:5025')
        replacement.select_function.side_effect = OSError('Cannot select')
        with patch('gate.discovery.connect', return_value=replacement), self.assertRaises(ConnectionError):
            engine.command('connect', dict(resource='tcp://meter:5025'))
        self.assertIs(engine.instruments[key], device)
        device.close.assert_not_called()
        replacement.close.assert_called_once()
        self.assertEqual(engine.instrument_profiles[key]['settings'], {'function':'FREQ'})

    def test_unplugged_instrument_can_be_renamed_and_reconnected(self):
        engine = Engine()
        key, device = self.connect(engine, '/dev/usbtmc1')
        device.read = Mock(side_effect=OSError('Unplugged'))
        engine.cycle()
        self.assertNotIn(key, engine.active_resources)
        engine.command('instrument_settings', dict(instrument=key, name='Missing reference'))
        same, replacement = self.connect(engine, '/dev/usbtmc2')
        self.assertEqual(same, key)
        self.assertEqual(engine.snapshot()['instruments'][0]['name'], 'Missing reference')

    def test_unknown_serials_never_merge(self):
        engine = Engine()
        a, _ = self.connect(engine, '/dev/usbtmc1', serial='UNKNOWN')
        b, _ = self.connect(engine, '/dev/usbtmc2', serial='UNKNOWN')
        self.assertNotEqual(a, b)
        self.assertIsNone(engine.instrument_profiles[a]['serial'])
