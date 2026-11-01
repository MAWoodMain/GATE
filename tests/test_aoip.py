"""Manual-derived protocol fixtures; these tests do not verify hardware accuracy."""
import os
import select
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from gate.aoip_serial import AOIPSerial
from gate.channels import channel_catalog
from gate.connections import communication_type, validate_resource
from gate.discovery import connect
from gate.drivers.aoip import AOIPSN8310, AOIPPJ6301, parse_value
from gate.engine import Engine


class Calibrator:
    resource = 'aoip:///dev/fixture?baud=9600'

    def __init__(self, pj=False):
        self.pj = pj
        self.identity = 'AOIP_MESURES,'+('PJ 6301' if pj else 'SN 8310')+',S123456,1.0'
        self.range = 'V6' if pj else 'V10'
        self.output = '0,V'
        self.input = 'V6'
        self.config = {'V': 'V6', 'MA': 'MA60,TXA', 'OHM': 'OHM600,WIRE4',
                       'TC': 'TC,TCX_OFF,TYP_K,CEL,JR_ON', 'RT': 'RT,WIRE4,TYP_PT100,CEL'}
        self.measurement = '1.25,V'
        self.pcr, self.status = 256, 1
        self.standby = True
        self.commands = []
        self.refuse = False
        self.close = Mock()

    def query(self, command):
        self.commands.append(command)
        if command == '*IDN?': return self.identity
        if command == '*OPC?': return '1'
        if command == 'OUT?': return self.output
        if command == 'RANGE?': return self.range+',WIRE4'
        if command == 'CFG_OUT?': return self.range
        if command == 'CFG_IN?': return self.input
        if command.startswith('CFG_IN? '): return self.config[command.split()[1]]
        if command == 'PCR?': return str(self.pcr)
        if command == 'ISR?': return str(self.status if self.pj else (8 if self.standby else 0))
        if command == 'MEAS?': return self.measurement
        raise AssertionError(command)

    def write(self, command):
        self.commands.append(command)
        if self.refuse: return
        if command.startswith(('RANGE ', 'RANGE_OUT ')):
            self.range = command.split()[1].split(',')[0]
        elif command.startswith('OUT '):
            text = command[4:]
            for suffix in ('OHM', 'A', 'V'):
                if text.endswith(suffix):
                    self.output = text[:-len(suffix)]+','+suffix
                    break
        elif command == 'OPER':
            self.standby = False
            self.pcr &= ~256
        elif command == 'STBY':
            self.standby = True
            self.pcr |= 256
        elif command.startswith('RANGE_IN '):
            config = command[9:]
            self.input = config.split(',')[0]
            for key, codes in [('V', ('V6', 'V60', 'MV600', 'MV60', 'V_AUTO')),
                               ('OHM', ('OHM600', 'OHM6000', 'OHM_AUTO')),
                               ('MA', ('MA60',)), ('TC', ('TC',)), ('RT', ('RT',))]:
                if self.input in codes:
                    self.config[key] = config
                    break


class AOIPDriverTests(unittest.TestCase):
    def test_exact_identity_discovery_passive_catalogs_and_no_resets(self):
        for pj, cls in ((False, AOIPSN8310), (True, AOIPPJ6301)):
            t = Calibrator(pj)
            with patch('gate.discovery.AOIPSerial', return_value=t):
                d = connect(t.resource)
            self.assertIsInstance(d, cls)
            self.assertEqual(t.commands, ['*IDN?'])
            channels = channel_catalog(d)
            d.range_controls()
            self.assertEqual(t.commands, ['*IDN?'])
            self.assertEqual([c['id'] for c in channels], ['source', 'measure'] if pj else ['source'])
            self.assertTrue(all(not f.configurable for f in d.functions() if f.instrument_channel == 'source'))
            d.read()
            self.assertTrue(all('?' in c for c in t.commands))
            d.close()
            self.assertNotIn('STBY', t.commands)  # Passive connections do not take ownership.
        for identity in ('AOIP,SN8311,S1,1', 'OTHER,PJ6301,S1,1', 'OMEGA,CL8500,S1,1', 'AOIP,PJ63010,S1,1'):
            self.assertFalse(AOIPSN8310.matches(identity))
            self.assertFalse(AOIPPJ6301.matches(identity))
        t = Calibrator(); t.identity = 'AOIP,UNKNOWN,S1,1'
        with patch('gate.discovery.AOIPSerial', return_value=t), self.assertRaises(ValueError):
            connect(t.resource)
        t.close.assert_called_once()

    def test_limits_units_and_rejected_source_changes_do_not_write(self):
        for pj, cls, rng, low, high in ((False, AOIPSN8310, 'V100', -5., 110.),
                                       (True, AOIPPJ6301, 'V60', -10., 60.)):
            t = Calibrator(pj); d = cls(t, t.identity)
            for value in (low-.001, high+.001, float('nan'), float('inf'), True, '1'):
                with self.assertRaises(ValueError): d.configure_source('voltage', value, rng)
            for quantity, code in (('voltage', 'V100;OPER'), ('current_limit', rng)):
                with self.assertRaises(ValueError): d.configure_source(quantity, 0, code)
            self.assertFalse(t.commands)
            for value in (low, 0, high):
                d.configure_source('voltage', value, rng)
                self.assertNotIn('OPER', t.commands)
                self.assertEqual(parse_value(t.output)[:2], (value, 'V'))
            d.configure_source('current', .02, 'MA60' if pj else 'MA100')
            self.assertAlmostEqual(parse_value(t.output)[0], .02)
            self.assertIn('OUT 0.02A', t.commands)
            readings = {r.channel: r for r in d.read()}
            self.assertEqual(readings['source.current'].value, .02)
            self.assertIsNone(readings['source.voltage'].value)
            self.assertNotIn('measure.current', readings if not pj else {})

    def test_output_confirmation_ownership_changed_setpoint_and_wiring_guard(self):
        for pj, cls, rng in ((False, AOIPSN8310, 'V10'), (True, AOIPPJ6301, 'V6')):
            t = Calibrator(pj); d = cls(t, t.identity)
            e = Engine(); e.instruments[d.id] = d
            args = dict(instrument=d.id)
            with self.assertRaisesRegex(ValueError, 'wiring_confirmed'):
                e.command('output', dict(args, enabled=True))
            with self.assertRaisesRegex(ValueError, 'Configure'):
                d.set_enabled(True)
            e.command('source', dict(args, quantity='voltage', value=1.25, range=rng))
            t.output = '2,V'
            with self.assertRaisesRegex(ValueError, 'changed'): d.set_enabled(True)
            t.output = '1.25,V'
            e.command('output', dict(args, enabled=True, wiring_confirmed=True))
            self.assertFalse(t.standby)
            before = t.commands[:]
            with self.assertRaisesRegex(ValueError, 'Disable'): d.configure_source('voltage', 2, rng)
            self.assertEqual(t.commands[len(before):].count('OUT 2V'), 0)
            with self.assertRaisesRegex(ValueError, 'typed source'):
                e.command('sweep', dict(args, start=0, stop=1, step=1, dwell=1))
            d.close()
            self.assertTrue(t.standby)
            t.close.assert_called_once()
            t = Calibrator(pj); d = cls(t, t.identity)
            t.refuse = True
            with self.assertRaisesRegex(ValueError, 'readback'): d.configure_source('voltage', 1, rng)
            self.assertIsNone(d._source_ready)

    def test_enable_lost_ack_still_attempts_verified_cleanup(self):
        t = Calibrator(True); d = AOIPPJ6301(t, t.identity)
        d.configure_source('voltage', 1, 'V6')
        write = t.write
        def lost_ack(command):
            write(command)
            if command == 'OPER': raise TimeoutError('lost ack')
        t.write = lost_ack
        with self.assertRaises(TimeoutError): d.set_enabled(True)
        d.close()
        self.assertTrue(t.standby)
        self.assertIn('STBY', t.commands)

    def test_pj_independent_input_ranges_preserve_wiring_and_source(self):
        t = Calibrator(True); d = AOIPPJ6301(t, t.identity)
        d.select_function('measure.resistance')
        d.select_range('measure.resistance', 'OHM6000')
        self.assertEqual(t.config['OHM'], 'OHM6000,WIRE4')
        self.assertEqual(t.range, 'V6')
        self.assertEqual(d.current_functions['measure'], 'measure.resistance')
        d.select_function('measure.voltage')
        d.select_range('measure.voltage', 'V_AUTO')
        self.assertEqual(t.config['V'], 'V_AUTO')
        t.config['MA'] = 'MA60,TXP'
        before = t.commands[:]
        with self.assertRaisesRegex(ValueError, 'loop supply'): d.select_function('measure.current')
        self.assertFalse(any(c.startswith('RANGE_IN') for c in t.commands[len(before):]))
        with self.assertRaises(ValueError): d.select_function('measure.voltage;OPER')
        t.refuse = True
        with self.assertRaisesRegex(ValueError, 'readback'): d.select_function('measure.rtd')

    def test_pj_measurement_quality_and_units(self):
        for pcr, status, text, quality in ((256, 1, '2.5,V', 'ok'), (256, 5, '10000000,V', 'overload'),
                (260, 1, '2.5,V', 'relative'), (264, 1, '2.5,V', 'scaled'),
                (258, 1, '2.5,V', 'held'), (256, 0, '2.5,V', 'stale'),
                (256, 1, '2.5,MA', 'incompatible_unit')):
            t = Calibrator(True); d = AOIPPJ6301(t, t.identity)
            t.pcr, t.status, t.measurement = pcr, status, text
            r = next(r for r in d.read() if r.channel == 'measure.voltage')
            self.assertEqual(r.quality, quality)
            self.assertEqual(r.value, 2.5 if quality == 'ok' else None)
        self.assertEqual(parse_value('20 mA'), (.02, 'A', 'ok'))
        self.assertEqual(parse_value('1.25,V'), (1.25, 'V', 'ok'))
        self.assertEqual(parse_value('32,FAR'), (0., '°C', 'ok'))
        self.assertEqual(parse_value('273.15,K'), (0., '°C', 'ok'))
        self.assertEqual(parse_value('100,SCAL'), (None, '', 'scaled'))
        for text in ('V', '1', 'NaN,V', '1,BOGUS'):
            with self.assertRaises(ValueError): parse_value(text)
        t = Calibrator(True); d = AOIPPJ6301(t, t.identity)
        t.input, t.measurement = 'TC', '32,FAR'
        self.assertEqual(next(r for r in d.read() if r.channel == 'measure.thermocouple').value, 0.)
        t.range, t.output = 'RT', '25,CEL'
        self.assertEqual(next(r for r in d.read() if r.channel == 'source.rtd').value, 25.)

    def test_input_mode_race_and_source_fault_do_not_produce_valid_measurements(self):
        t = Calibrator(True); d = AOIPPJ6301(t, t.identity)
        query = t.query
        def change_mode(command):
            result = query(command)
            if command == 'MEAS?':
                t.input = 'TC'
            return result
        t.query = change_mode
        self.assertEqual(next(r for r in d.read() if r.channel == 'measure.voltage').quality, 'mode_changed')
        t = Calibrator(True); d = AOIPPJ6301(t, t.identity)
        t.status |= 1024  # Output amplifier saturation.
        r = next(r for r in d.read() if r.channel == 'source.voltage')
        self.assertIsNone(r.value)
        self.assertEqual(r.quality, 'output_fault')
        with self.assertRaisesRegex(ValueError, 'readback'):
            d.configure_source('voltage', 1, 'V6')

    def test_source_command_run_guard_and_snapshot(self):
        t = Calibrator(True); d = AOIPPJ6301(t, t.identity)
        e = Engine(); e.instruments[d.id] = d
        e.cycle()
        card = e.snapshot()['instruments'][0]
        self.assertEqual(set(card['current_functions']), {'source', 'measure'})
        self.assertEqual(card['source_controls']['voltage']['V60'], [-10., 60., .0001])
        e.experiment_run = SimpleNamespace(active=True)
        with self.assertRaisesRegex(ValueError, 'Stop the active experiment'):
            e.command('source', dict(instrument=d.id, quantity='voltage', value=1, range='V6'))
        self.assertNotIn('REM', t.commands)


class SerialFixture:
    def __init__(self, chunks):
        self.master, self.slave = os.openpty()
        self.resource = 'aoip://'+os.ttyname(self.slave)+'?baud=9600&idle=0.05'
        self.chunks = chunks
        self.commands = []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run)
        self.thread.start()

    def run(self):
        data = b''
        while not self.stop.is_set():
            if not select.select([self.master], [], [], .02)[0]: continue
            data += os.read(self.master, 4096)
            while b'\n' in data:
                command, data = data.split(b'\n', 1)
                self.commands.append(command)
                if b'?' in command:
                    for delay, block in self.chunks:
                        if self.stop.wait(delay): return
                        os.write(self.master, block)

    def __enter__(self): return self
    def __exit__(self, *args):
        self.stop.set(); self.thread.join(2)
        os.close(self.master); os.close(self.slave)


class AOIPSerialTests(unittest.TestCase):
    def test_fragmented_cr_lf_crlf_and_missing_terminators(self):
        for ending in (b'', b'\r', b'\n', b'\r\n'):
            with self.subTest(ending=ending), SerialFixture([(.01, b'1.25'), (.025, b',V'), (.01, ending)]) as bench:
                t = AOIPSerial(bench.resource, timeout=.4)
                for _ in range(2): self.assertEqual(t.query('OUT?'), '1.25,V')
                t.write('REM')
                t.close()
                self.assertEqual(bench.commands[:2], [b'OUT?', b'OUT?'])

    def test_silence_and_continuous_partial_stream_close_transport(self):
        for chunks in ([], [(.02, b'1')]*20, [(.01, b'\r\n')]):
            with SerialFixture(chunks) as bench:
                t = AOIPSerial(bench.resource, timeout=.15)
                with self.assertRaises(TimeoutError): t.query('OUT?')
                self.assertIsNone(t.fd)
                with self.assertRaises(ConnectionError): t.query('OUT?')

    def test_oversize_and_malformed_replies_close_transport(self):
        for payload in (b'X'*65537, b'1,V\n2,V', b'\xff'):
            with SerialFixture([(.001, payload)]) as bench:
                t = AOIPSerial(bench.resource, timeout=1)
                with self.assertRaises((ValueError, UnicodeError)): t.query('OUT?')
                self.assertIsNone(t.fd)

    def test_connection_options_validation_and_injection(self):
        resource = validate_resource('aoip:///dev/ttyUSB0')
        self.assertEqual(communication_type(resource), 'Serial')
        self.assertIn('idle=0.2', resource)
        for opts in ('baud=115200', 'bits=9', 'parity=bad', 'stop=3', 'idle=nan', 'idle=0',
                     'flow=bad', 'baud=9600&baud=4800', 'command=OPER'):
            with self.assertRaises(ValueError): validate_resource('aoip:///dev/ttyUSB0?'+opts)
        with SerialFixture([]) as bench:
            t = AOIPSerial(bench.resource, timeout=.2)
            with self.assertRaises(ValueError): t.query('OUT?\nOPER')
            t.close()
        self.assertEqual(bench.commands, [])


if __name__ == '__main__':
    unittest.main()
