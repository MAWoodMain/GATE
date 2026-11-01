import os
import pty
import select
import threading
import unittest
from unittest.mock import Mock, patch

from gate.connections import communication_type, validate_resource
from gate.discovery import connect
from gate.drivers.tti_plp import MODELS, TTiPLP
from gate.engine import Engine
from gate.identity import identify
from gate.plp_serial import PLPSerial


class Supply:
    resource = 'plp:///dev/fixture?baud=9600'

    def __init__(self, model='PL303-P'):
        self.identity = f'THURLBY THANDAR,{model},279730,4.08 - 1.00'
        self.commands = []
        self.writes = []
        self.config = '2' if 'QM' in model else '1'
        self.range = '2'
        self.voltage, self.current, self.enabled = 3.3, .05, 0
        self.close = Mock()

    def query(self, command):
        self.commands.append(command)
        if command == '*IDN?': return self.identity
        if command == 'CONFIG?': return self.config
        if command == 'IRANGE1?': return self.range
        if command.startswith('OP'): return str(self.enabled)
        if command.endswith('O?'): return '3.299V' if command[0] == 'V' else '0.002A'
        return f'{command[:-1]} {self.voltage if command[0] == "V" else self.current:.4f}'

    def write(self, command):
        self.writes.append(command)
        key, value = command.split()
        if key == 'V1': self.voltage = float(value)
        elif key == 'I1': self.current = float(value)
        elif key == 'OP1': self.enabled = int(value)


def device(model='PL303-P'):
    t = Supply(model)
    return t, TTiPLP(t, t.identity)


class DriverTests(unittest.TestCase):
    def test_exact_models_passive_catalogs_and_identity(self):
        for model, (voltage, current, count) in MODELS.items():
            for suffix in ('', '(G)'):
                t, d = device(model + suffix)
                self.assertEqual(t.commands, [])
                self.assertEqual(len(d.channels()), count)
                self.assertEqual(len(d.functions()), count * 6)
                self.assertEqual(len({f.id for f in d.functions()}), count * 6)
                self.assertEqual(d.output_control_supported, count == 1)
                self.assertEqual(d.range_controls(), [])
                if count == 1:
                    self.assertEqual(d.experiment_limits['voltage'], [0., voltage])
                    self.assertEqual(d.experiment_limits['current'], [.001, current])
                d.close()
                self.assertFalse(t.writes)
        for model in ('PL303', 'PL303P', 'PLH250-P', 'QL355-P', 'PL999-P', 'PL303-P-BOGUS'):
            self.assertFalse(TTiPLP.matches(f'THURLBY THANDAR,{model},1,1.0'))
        self.assertFalse(TTiPLP.matches('OTHER,PL303-P,1,1.0'))
        t, d = device()
        self.assertEqual(identify(t.identity, d.device_type)['id'],
                         identify(t.identity.replace('THURLBY THANDAR', 'AIM-TTI'), d.device_type)['id'])
        self.assertIsNone(identify(t.identity.replace('279730', 'UNKNOWN'), d.device_type))

    def test_readback_and_parallel_unavailable_output(self):
        t, d = device()
        readings = {r.channel: r for r in d.read()}
        self.assertAlmostEqual(readings['power'].value, 3.299 * .002)
        self.assertEqual(readings['set_voltage'].value, 3.3)
        self.assertEqual(readings['output_enabled'].value, 0.)
        for model in ('PL303QMD-P', 'PL303QMT-P'):
            t, d = device(model)
            for mode in ('0', '1', '2'):
                t.config = mode; t.commands.clear()
                readings = {r.channel: r for r in d.read()}
                self.assertEqual(len(readings), d.output_count * 6)
                if mode == '1':
                    self.assertEqual(readings['output2.voltage'].quality, 'unavailable')
                    self.assertIsNone(readings['output2.voltage'].value)
                    self.assertNotIn('V2O?', t.commands)
                else:
                    self.assertEqual(readings['output2.voltage'].value, 3.299)
                if d.output_count == 3:
                    self.assertEqual(readings['output3.voltage'].value, 3.299)
            for method, arg in ((d.set_voltage, 1), (d.set_current_limit, .1), (d.set_enabled, True)):
                with self.assertRaisesRegex(ValueError, 'channel-targeted'): method(arg)
            self.assertEqual(t.writes, [])

    def test_limits_range_preservation_and_confirmed_writes(self):
        for model, (vmax, imax, _) in MODELS.items():
            if 'QM' in model: continue
            t, d = device(model)
            for value in (-1, vmax + .001, float('nan'), float('inf'), True, '1'):
                with self.assertRaises(ValueError): d.set_voltage(value)
            for value in (0, .0001, imax + .001, float('nan'), True):
                with self.assertRaises(ValueError): d.set_current_limit(value)
            self.assertEqual(t.commands, [])
            self.assertEqual(t.writes, [])
            d.set_voltage(vmax); d.set_current_limit(imax)
            self.assertEqual((t.voltage, t.current), (vmax, imax))
            t.range = '1'
            with self.assertRaisesRegex(ValueError, 'selected current range'): d.set_current_limit(1.)
            d.set_current_limit(.1)
            self.assertFalse(any(c.startswith('IRANGE') for c in t.writes))
            t.write = Mock()
            with self.assertRaisesRegex(ValueError, 'readback'): d.set_voltage(1.)

    def test_enable_interlocks_and_uncertain_enable_cleanup(self):
        t, d = device()
        for attr, value in (('voltage', 31), ('current', 0), ('current', 4)):
            old = getattr(t, attr); setattr(t, attr, value)
            with self.assertRaises(ValueError): d.set_enabled(True)
            setattr(t, attr, old)
        self.assertEqual(t.writes, [])
        with self.assertRaises(ValueError): d.set_enabled(1)
        write = t.write
        def lost_ack(command):
            write(command)
            if command == 'OP1 1': raise TimeoutError('lost acknowledgement')
        t.write = lost_ack
        with self.assertRaises(TimeoutError): d.set_enabled(True)
        d.close()
        self.assertEqual(t.enabled, 0)
        t.close.assert_called_once()
        t, d = device(); d.set_enabled(True)
        t.write = Mock(side_effect=TimeoutError('unplugged'))
        with self.assertRaises(TimeoutError): d.close()
        t.close.assert_called_once()

    def test_bad_replies_are_not_valid_measurements_or_confirmations(self):
        for reply in ('nanV', 'infV', '3.3', 'V1 3.3', '3.3V;1', ''):
            t, d = device(); original = t.query
            t.query = lambda c: reply if c == 'V1O?' else original(c)
            with self.assertRaises(ValueError): d.read()
        t, d = device(); t.query = Mock(return_value='2')
        with self.assertRaises(ValueError): d.set_enabled(False)
        t, d = device(); t.write = Mock()
        with self.assertRaisesRegex(ValueError, 'readback'): d.set_enabled(True)

    def test_discovery_engine_confirmation_and_experiment_envelope(self):
        for scheme, transport_name in (('plp:///dev/fixture', 'PLPSerial'), ('tcp://fixture:9221', 'SCPI')):
            t = Supply(); t.resource = scheme
            with patch('gate.discovery.' + transport_name, return_value=t):
                d = connect(scheme)
            self.assertIsInstance(d, TTiPLP)
            self.assertEqual(t.commands, ['*IDN?'])
            e = Engine(.1); e.instruments[d.id] = d
            args = dict(instrument=d.id)
            with self.assertRaisesRegex(ValueError, 'wiring_confirmed'):
                e.command('output', dict(args, enabled=True))
            e.command('static', dict(args, voltage=1, current_limit=.05))
            e.command('configure_output', dict(args, control='voltage', value=2))
            e.command('output', dict(args, enabled=True, operator_confirmed=True))
            e.command('sweep', dict(args, start=1, stop=30, step=1, dwell=.1))
            with self.assertRaises(ValueError):
                e.command('sweep', dict(args, start=1, stop=31, step=1, dwell=.1))
            e.command('output', dict(args, enabled=False))
            self.assertFalse(e.sweeps)
            d.close()
        t = Supply('PL999-P')
        with patch('gate.discovery.PLPSerial', return_value=t):
            with self.assertRaises(ValueError): connect(t.resource)
        t.close.assert_called_once()

    def test_addresses(self):
        self.assertEqual(communication_type('plp:///dev/ttyACM0'), 'Serial')
        self.assertEqual(validate_resource('plp:///dev/ttyACM0'), 'plp:///dev/ttyACM0?baud=9600')
        for suffix in ('?baud=19200', '?baud=', '?baud=9600&baud=9600', '?flow=none', '#fragment'):
            with self.assertRaises(ValueError): validate_resource('plp:///dev/ttyACM0' + suffix)


class TransportTests(unittest.TestCase):
    def exchange(self, reply, operation):
        master, slave = pty.openpty()
        t = PLPSerial('plp://' + os.ttyname(slave), timeout=.15)
        requests = []
        def server():
            if select.select([master], [], [], .5)[0]:
                request = os.read(master, 128); requests.append(request)
                for byte in reply: os.write(master, bytes([byte]))
        worker = threading.Thread(target=server); worker.start()
        try:
            return operation(t)
        finally:
            worker.join(1); t.close(); os.close(master); os.close(slave)
            self.assertFalse(worker.is_alive())
            self.requests = requests

    def test_fragmented_crlf_and_no_prompt_or_clear(self):
        self.assertEqual(self.exchange(b'3.299V\r\n', lambda t: t.query('V1O?')), '3.299V')
        self.assertEqual(self.requests, [b'V1O?\n'])
        self.exchange(b'', lambda t: t.write('OP1 0'))
        self.assertEqual(self.requests, [b'OP1 0\n'])

    def test_timeout_and_oversize_close_session(self):
        for reply, error in ((b'', TimeoutError), (b'x' * 4097, ValueError)):
            def operation(t):
                with self.assertRaises(error): t.query('V1O?')
                self.assertIsNone(t.fd)
                with self.assertRaises(ConnectionError): t.query('I1O?')
            self.exchange(reply, operation)


if __name__ == '__main__':
    unittest.main()
