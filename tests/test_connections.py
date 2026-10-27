"""Device history and connection editing must survive unplug/restart/failure."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from gate.connections import communication_type, validate_resource
from gate.engine import Engine
from gate.model import Reading


class ConnectionTests(unittest.TestCase):
    def instrument(self, resource):
        return SimpleNamespace(id=resource, name='Bench fixture', category='multimeter',
                               simulated=False, close=Mock(), read=Mock(return_value=[Reading('voltage', 1, 'V')]))

    def test_types_and_validated_serial_defaults(self):
        for resource, kind in [('/dev/usbtmc3', 'TMC'), ('serial:///dev/ttyUSB0', 'Serial'),
                               ('tcp://scope:5025', 'Network'), ('demo:multimeter', 'Demo')]:
            self.assertEqual(communication_type(resource), kind)
        self.assertEqual(validate_resource('serial:///dev/ttyUSB0'), 'serial:///dev/ttyUSB0?baud=9600')
        self.assertEqual(validate_resource('tcp://[::1]:5025'), 'tcp://[::1]:5025')
        for value in ('/tmp/file', 'tcp://host:0', 'tcp://host:65536', 'tcp://user@host',
                      'tcp://host/path', 'serial:///dev/ttyUSB0?baud=1234',
                      'serial:///dev/ttyUSB0?baud=9600&parity=N', 'serial:///dev/ttyUSB0#bad'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_resource(value)

    def test_tmc_history_and_name_survive_unplug_and_restart(self):
        resource = '/dev/usbtmc3'
        device = self.instrument(resource)
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)/'devices.json'
            engine = Engine(profile_path=profile)
            with patch('gate.discovery.usb_resources', return_value=[resource]), patch('gate.discovery.connect', return_value=device):
                engine.command('scan', {})
            device.read.side_effect = OSError('Unplugged')
            engine.cycle()
            engine.cycle()
            self.assertEqual(engine.snapshot()['instruments'], [])
            restarted = Engine(profile_path=profile)
            self.assertEqual(restarted.snapshot()['configured_resources'], [resource])
            self.assertEqual(restarted.snapshot()['saved_devices'][resource]['name'], 'Bench fixture')
            restarted.command('disconnect', dict(instrument=resource))
            forgotten = Engine(profile_path=profile)
            self.assertFalse(forgotten.saved_devices)
            self.assertFalse(forgotten.saved_resources)

    def test_failed_edit_preserves_new_settings_for_retry(self):
        old, new = 'serial:///dev/ttyUSB0?baud=9600', 'serial:///dev/ttyUSB1?baud=4800'
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)/'devices.json'
            engine = Engine(profile_path=profile)
            device = self.instrument(old)
            with patch('gate.discovery.connect', return_value=device):
                engine.command('connect', dict(resource=old))
            with patch('gate.discovery.connect', side_effect=OSError('Missing')):
                result = engine.execute('edit_device', dict(instrument=old, resource=new))
            self.assertEqual(result['state'], 'failed')
            device.close.assert_called_once()
            self.assertNotIn(old, engine.instruments)
            restarted = Engine(profile_path=profile)
            self.assertEqual(restarted.saved_resources, {new})
            self.assertEqual(restarted.saved_devices[new]['name'], 'Bench fixture')
            replacement = self.instrument(new)
            with patch('gate.discovery.connect', return_value=replacement):
                restarted.command('connect', dict(resource=new))
            self.assertIn(new, restarted.instruments)

    def test_invalid_or_duplicate_edit_keeps_existing_connection_open(self):
        engine = Engine()
        old = '/dev/usbtmc3'
        device = self.instrument(old)
        engine.instruments[old] = device
        engine.saved_resources.update([old, 'tcp://scope:5025'])
        for value in ('bad', 'tcp://scope:5025'):
            with self.assertRaises(ValueError):
                engine.command('edit_device', dict(instrument=old, resource=value))
        self.assertIs(engine.instruments[old], device)
        device.close.assert_not_called()

    def test_reconnect_replaces_failed_transport_and_failed_add_is_remembered(self):
        engine = Engine()
        old = self.instrument('/dev/usbtmc3')
        engine.instruments[old.id] = old
        replacement = self.instrument(old.id)
        with patch('gate.discovery.connect', return_value=replacement) as connect:
            engine.command('connect', dict(resource=old.id, reconnect=True))
        connect.assert_called_once_with(old.id)
        old.close.assert_called_once()
        self.assertIs(engine.instruments[old.id], replacement)
        with patch('gate.discovery.connect', side_effect=OSError('Offline')):
            result = engine.execute('connect', dict(resource='tcp://missing:5025'))
        self.assertEqual(result['state'], 'failed')
        self.assertIn('tcp://missing:5025', engine.snapshot()['configured_resources'])
