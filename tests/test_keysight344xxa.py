"""Protocol fixtures and end-to-end TCP acquisition, without bench hardware."""
import socket
import sqlite3
import tempfile
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
from gate.discovery import connect, driver_types
from gate.drivers.keysight344xxa import Keysight344xxA, MODELS, parse_value
from gate.engine import Engine
from gate.identity import identify


class MeterTransport:
    resource = '/dev/usbtmc8'
    timeout = 1.

    def __init__(self):
        self.commands = []
        self.responses = {'SENS:FUNC?': '"VOLT:DC"', 'UNIT:TEMP?': 'C',
                          'TRIG:SOUR?': 'IMM', 'TRIG:COUN?': '+1', 'SAMP:COUN?': '+1',
                          'CALC:SCAL:STAT?': '0', 'CALC:STAT?': '0', 'READ?': '+1.23456789E+00'}
        self.closed = False

    def query(self, command):
        self.commands.append(command)
        response = self.responses.get(command, '0' if command.endswith(':NULL:STAT?') else None)
        if response is None:
            raise AssertionError('Unexpected query '+command)
        if isinstance(response, Exception):
            raise response
        return response.pop(0) if isinstance(response, list) else response

    def write(self, command):
        self.commands.append(command)
        if command.startswith('SENS:FUNC '):
            self.responses['SENS:FUNC?'] = command.split(' ', 1)[1]
        elif command.startswith('UNIT:TEMP '):
            self.responses['UNIT:TEMP?'] = command.split()[-1]
        else:
            raise AssertionError('Unexpected write '+command)

    def close(self):
        self.closed = True


class KeysightTests(unittest.TestCase):
    def meter(self, model='34461A', maker='Keysight Technologies'):
        return Keysight344xxA(MeterTransport(), f'{maker},{model},MY123,1.0')

    def test_exact_identity_and_registration(self):
        self.assertIn(Keysight344xxA, driver_types())
        for model in MODELS:
            for maker in ('Keysight Technologies', 'Agilent Technologies', 'HEWLETT-PACKARD'):
                self.assertTrue(Keysight344xxA.matches(f'{maker},{model},MY123,1.0'))
        for identity in ('Other,34461A,123,1', 'Keysight,34461AB,123,1',
                         'Keysight,34420A,123,1', 'Keysight,34450A,123,1', 'Keysight,34461A'):
            self.assertFalse(Keysight344xxA.matches(identity))
        first, second = self.meter(), self.meter(maker='Agilent Technologies')
        self.assertEqual(identify(first.name, first.device_type)['id'],
                         identify(second.name, second.device_type)['id'])
        self.assertNotEqual(first.device_type, self.meter('34465A').device_type)
        self.assertEqual(first.transport.commands, [])

    def test_model_catalog_and_confirmed_selection(self):
        for model in MODELS:
            meter = self.meter(model)
            catalog = meter.functions()
            self.assertEqual(len(catalog), 10 if model == '34401A' else 13)
            self.assertTrue(all(f.configurable and f.exclusive_group == 'primary' for f in catalog))
            for function in catalog:
                meter.select_function(function.id)
                self.assertEqual(meter.current_function, function.id)
                reading = meter.read()[0]
                self.assertEqual((reading.channel, reading.unit, reading.quality),
                                 (function.channel, function.unit, 'ok'))
        meter = self.meter('34401A')
        for function in ('CAP', 'TEMPC', 'VDC;*RST', ''):
            with self.assertRaises(ValueError):
                meter.select_function(function)
        self.assertEqual(meter.transport.commands, [])

    def test_selection_failure_and_front_panel_race(self):
        meter = self.meter()
        with patch.object(meter.transport, 'write'):
            with self.assertRaises(ValueError):
                meter.select_function('OHMS')
        meter.transport.responses['SENS:FUNC?'] = ['"VOLT:DC"', '"RES"']
        reading = meter.read()[0]
        self.assertEqual((reading.channel, reading.unit, reading.value, reading.quality),
                         ('voltage_dc', 'V', None, 'mode_changed'))
        self.assertEqual(meter.current_function, 'OHMS')

    def test_incompatible_settings_never_start_acquisition_or_write(self):
        for command, response in [('TRIG:SOUR?', 'EXT'), ('TRIG:COUN?', 'INF'),
                                  ('SAMP:COUN?', '10'), ('CALC:SCAL:STAT?', '1'),
                                  ('SENS:VOLT:DC:NULL:STAT?', '1'), ('SENS:FUNC?', '"VOLT:DC:RAT"')]:
            with self.subTest(command=command):
                meter = self.meter()
                meter.transport.responses[command] = response
                self.assertEqual(meter.read()[0].quality, 'unsupported_configuration')
                self.assertNotIn('READ?', meter.transport.commands)
                self.assertTrue(all(command.endswith('?') for command in meter.transport.commands))
        meter = self.meter('34401A')
        meter.transport.responses['CALC:STAT?'] = '1'
        self.assertEqual(meter.read()[0].quality, 'unsupported_configuration')

    def test_scaling_or_temperature_unit_changes_discard_reading(self):
        meter = self.meter()
        meter.transport.responses['CALC:SCAL:STAT?'] = ['0', '1']
        self.assertEqual(meter.read()[0].quality, 'mode_changed')
        meter = self.meter()
        meter.select_function('TEMPC')
        meter.transport.responses['UNIT:TEMP?'] = ['C', 'F']
        self.assertEqual(meter.read()[0].quality, 'mode_changed')
        meter.transport.responses['UNIT:TEMP?'] = 'K'
        self.assertEqual(meter.read()[0].quality, 'unsupported_configuration')

    def test_overload_malformed_data_and_timeout_restore(self):
        for text in ('9.9E37', '-9.90000000E+37', '1e999'):
            self.assertEqual(parse_value(text), (None, 'overload'))
        self.assertEqual(parse_value('9.91E37'), (None, 'unavailable'))
        self.assertEqual(parse_value(' -1.2E-12 '), (-1.2e-12, 'ok'))
        for text in ('', '1,2', '1V', 'NaN', 'garbage'):
            with self.assertRaises(ValueError):
                parse_value(text)
        meter = self.meter()
        meter.transport.responses['READ?'] = TimeoutError('unplugged')
        with self.assertRaises(TimeoutError):
            meter.read()
        self.assertEqual(meter.transport.timeout, 1.)
        meter.close()
        self.assertTrue(meter.transport.closed)

    def test_engine_discovery_and_function_settings(self):
        meter = self.meter()
        engine = Engine()
        with patch('gate.discovery.SCPI', return_value=meter.transport):
            meter.transport.responses['*IDN?'] = meter.name
            result = engine.command('connect', {'resource': meter.transport.resource})
        key = result['instrument']
        engine.command('select_function', dict(instrument=key, function='OHMS4'))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'keysight.sqlite'
            engine.command('record', {'path': str(path)})
            engine.cycle()
            engine.command('stop_recording', {})
            with sqlite3.connect(path) as database:
                row = database.execute('SELECT channel,value,unit,quality FROM samples').fetchone()
                self.assertEqual(row, ('resistance_4wire', 1.23456789, 'Ω', 'ok'))
        card = engine.snapshot()['instruments'][0]
        self.assertEqual(card['current_function'], 'OHMS4')
        self.assertEqual(card['readings'][0]['channel'], 'resistance_4wire')
        self.assertEqual(card['readings'][0]['unit'], 'Ω')
        self.assertEqual(card['settings']['function'], 'OHMS4')
        self.assertEqual(card['experiment_channels']['dc_voltage'], 'voltage_dc')
        engine.instruments[key].close()

    def test_usb_transport_identity_selection_and_acquisition(self):
        responses = [b'Keysight Technologies,34465A,MY123,1.0', b'"VOLT:DC"',
                     b'"VOLT:DC"', b'IMM', b'1', b'1', b'0', b'0',
                     b'+2.500E+00', b'"VOLT:DC"', b'0', b'0']
        commands = []
        def write(fd, data):
            commands.append(data.decode().strip())
            return len(data)
        def ioctl(fd, request, argument, *extra):
            if request == 0x80015B18:
                argument[0] = 1  # Each fixture is one complete USBTMC message.
        with patch('gate.transport.os.open', return_value=123), \
             patch('gate.transport.os.write', side_effect=write), \
             patch('gate.transport.os.read', side_effect=responses), \
             patch('gate.transport.os.close'), \
             patch('gate.transport.fcntl.ioctl', side_effect=ioctl):
            meter = connect('/dev/usbtmc8')
            meter.select_function('VDC')
            self.assertEqual(meter.read()[0].value, 2.5)
            meter.close()
        self.assertEqual(commands[:2], ['*IDN?', 'SENS:FUNC "VOLT:DC"'])
        self.assertEqual(sum(command == 'READ?' for command in commands), 1)

    def test_real_tcp_transport_fragmentation(self):
        server = socket.socket()
        server.bind(('127.0.0.1', 0))
        server.listen(1)
        fixture = MeterTransport()
        fixture.responses['*IDN?'] = 'Agilent Technologies,34410A,MY123,1.0'
        errors = []
        def respond():
            try:
                connection, _ = server.accept()
                with connection, connection.makefile('rb') as stream:
                    for line in stream:
                        command = line.decode().strip()
                        if command.endswith('?'):
                            data = (fixture.query(command)+'\n').encode()
                            connection.sendall(data[:3])
                            connection.sendall(data[3:])
                        else:
                            fixture.write(command)
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=respond, daemon=True)
        thread.start()
        meter = None
        try:
            meter = connect('tcp://127.0.0.1:'+str(server.getsockname()[1]))
            meter.select_function('ADC')
            reading = meter.read()[0]
            self.assertEqual((reading.channel, reading.unit, reading.value), ('current_dc', 'A', 1.23456789))
        finally:
            if meter:
                meter.close()
            server.close()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertNotIn('*RST', fixture.commands)


if __name__ == '__main__':
    unittest.main()
