import os
import select
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from gate.discovery import connect
from gate.drivers.fluke45 import Fluke45, parse_value, FUNCTIONS
from gate.engine import Engine
from gate.serial import CommandError, FlukeSerial

class SerialMeter:
    """A real pseudo-terminal exercises termios, echo and prompt framing."""
    def __init__(self, echo=True):
        self.master, self.slave = os.openpty()
        self.resource = 'serial://' + os.ttyname(self.slave) + '?baud=9600'
        self.echo = echo
        self.commands = []
        self.refuse_changes = False
        self.responses = {'*IDN?': 'FLUKE, 45, fixture, 1.7 D1.0', 'FUNC1?': 'VDC',
                          'MOD?': '0', 'VAL?': '+0.17E-3', 'FUNC2?': None}
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run)
        self.thread.start()
    def run(self):
        buffer = b''
        while not self.stop.is_set():
            if not select.select([self.master], [], [], .05)[0]:
                continue
            buffer += os.read(self.master, 4096)
            if b'\x03' in buffer:
                _, buffer = buffer.split(b'\x03', 1)
                self.commands.append('\x03')
                os.write(self.master, b'\r\n=>\r\n')
            while b'\r' in buffer:
                command, buffer = buffer.split(b'\r', 1)
                command = command.decode('ascii')
                self.commands.append(command)
                if command == 'SILENT?':
                    continue
                if command in FUNCTIONS and not self.refuse_changes:
                    self.responses['FUNC1?'] = command
                    self.responses['MOD?'] = '0'
                    self.responses['VAL?'] = '+1E+9' if command == 'OHMS' else '+0.17E-3'
                response = None if command in FUNCTIONS and self.refuse_changes else self.responses.get(command, '')
                prompt = '!>' if response is None else '?>' if command == 'BAD?' else '=>'
                payload = ((command + '\r\n' if self.echo else '') +
                           (response + '\r\n' if response else '') + prompt + '\r\n').encode()
                # Split both payload and prompt across separate writes.
                for offset in range(0, len(payload), 3):
                    os.write(self.master, payload[offset:offset+3])
                    self.stop.wait(.001)
    def close(self):
        self.stop.set()
        self.thread.join(timeout=2)
        os.close(self.master)
        os.close(self.slave)
    def __enter__(self):
        return self
    def __exit__(self, *args):
        self.close()

class FlukeTests(unittest.TestCase):
    def test_primary_function_catalog_and_confirmed_switches(self):
        with SerialMeter() as bench:
            meter = connect(bench.resource)
            try:
                functions = meter.functions()
                self.assertEqual({f.id for f in functions}, set(FUNCTIONS))
                self.assertTrue(all(f.configurable and f.channel.startswith('primary.') for f in functions))
                for function in functions:
                    meter.select_function(function.id)
                    self.assertEqual(meter.current_function, function.id)
                    self.assertEqual(bench.responses['FUNC1?'], function.id)
                before = list(bench.commands)
                with self.assertRaises(ValueError):
                    meter.select_function('OHMS;*RST')
                self.assertEqual(bench.commands, before)
                self.assertFalse(any(command.endswith('2') for command in bench.commands))
            finally:
                meter.close()

    def test_service_function_switch_confirmation_failure_and_logging(self):
        with SerialMeter() as bench, tempfile.TemporaryDirectory() as folder:
            engine = Engine()
            engine.command('connect',dict(resource=bench.resource))
            path = str(Path(folder)/'switch.sqlite')
            engine.command('record',dict(path=path))
            try:
                engine.cycle()
                engine.command('select_function',dict(instrument=bench.resource, function='OHMS', request='ohms'))
                result = engine.snapshot()['controls']['ohms']
                self.assertEqual((result['state'],result['channel']),('applied','primary.OHMS'))
                engine.cycle()
                device = engine.snapshot()['instruments'][0]
                self.assertEqual(device['current_function'],'OHMS')
                self.assertEqual(len(device['functions']),10)
                reading = device['readings'][0]
                self.assertEqual((reading['channel'],reading['unit'],reading['quality']),('primary.OHMS','Ω','overload'))
                bench.refuse_changes = True
                engine.command('select_function',dict(instrument=bench.resource, function='VDC', request='refused'))
                self.assertEqual(engine.snapshot()['controls']['refused']['state'],'failed')
                self.assertEqual(bench.responses['FUNC1?'],'OHMS')
                engine.command('select_function',dict(instrument=bench.resource, function='invalid', request='invalid'))
                self.assertEqual(engine.snapshot()['controls']['invalid']['state'],'failed')
                engine.cycle()
                self.assertFalse(engine.snapshot()['instruments'][0]['error'])
            finally:
                engine.command('stop_recording',{})
                for instrument in engine.instruments.values():instrument.close()
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('SELECT detail FROM events').fetchall(),[('select_function: OHMS',)])
                self.assertEqual(db.execute("SELECT COUNT(*) FROM samples WHERE channel='primary.OHMS'").fetchone()[0],2)

    def test_mode_settling_uses_bounded_longer_timeout_once(self):
        with SerialMeter() as bench:
            meter = connect(bench.resource)
            try:
                original = meter.transport.timeout
                meter.select_function('OHMS')
                query = meter.transport.query
                observed = []
                def watched(command):
                    observed.append(meter.transport.timeout)
                    return query(command)
                meter.transport.query = watched
                meter.read()
                self.assertTrue(all(timeout == 10 for timeout in observed))
                self.assertEqual(meter.transport.timeout, original)
                observed.clear()
                meter.read()
                self.assertTrue(all(timeout == original for timeout in observed))
            finally:
                meter.close()

    def test_identity_match(self):
        self.assertTrue(Fluke45.matches('FLUKE, 45, 6929061, 1.7 D1.0'))
        self.assertFalse(Fluke45.matches('FLUKE, 8845A, fixture, 1.0'))

    def test_real_serial_framing_with_echo_on_and_off(self):
        for echo in (True, False):
            with self.subTest(echo=echo), SerialMeter(echo) as bench:
                meter = connect(bench.resource)
                try:
                    readings = meter.read()
                    self.assertEqual(readings[0].channel, 'primary.VDC')
                    self.assertEqual(readings[0].value, .00017)
                    self.assertEqual(readings[0].unit, 'V')
                    self.assertEqual(readings[1].quality, 'disabled')
                    self.assertNotIn('FUNC2?', bench.commands)
                    self.assertTrue(all(c in ('\x03', '*IDN?', 'FUNC1?', 'MOD?', 'VAL?') for c in bench.commands))
                finally:
                    meter.close()

    def test_execution_and_command_errors_do_not_desynchronize(self):
        with SerialMeter() as bench:
            transport = FlukeSerial(bench.resource)
            try:
                for command, prompt in (('FUNC2?', '!'), ('BAD?', '?')):
                    with self.assertRaises(CommandError) as error:
                        transport.query(command)
                    self.assertEqual(error.exception.prompt, prompt)
                    self.assertEqual(transport.query('FUNC1?'), 'VDC')
            finally:
                transport.close()

    def test_missing_prompt_times_out_and_closes_port(self):
        with SerialMeter() as bench:
            transport = FlukeSerial(bench.resource, timeout=.15)
            with self.assertRaises(TimeoutError):
                transport.query('SILENT?')
            self.assertIsNone(transport.fd)

    def test_both_displays_format2_and_modifier_units(self):
        with SerialMeter() as bench:
            bench.responses.update({'VAL?': '+1.2345E+0 VDC, +1.0000E+3 HZ', 'FUNC2?': 'FREQ'})
            meter = connect(bench.resource)
            try:
                readings = meter.read()
                self.assertEqual([(r.channel, r.value, r.unit) for r in readings],
                    [('primary.VDC',1.2345,'V'),('secondary.FREQ',1000.,'Hz')])
                for modifier, channel, unit in (('8','primary.decibels.mod8','dB'),('16','primary.power.mod16','W'),('32','primary.VDC.mod32','V')):
                    bench.responses['MOD?'] = modifier
                    readings = meter.read()
                    self.assertEqual((readings[0].channel,readings[0].unit),(channel,unit))
            finally:
                meter.close()

    def test_invalid_values_and_overload_are_not_zero(self):
        self.assertEqual(parse_value('+1.00000E+9','VDC'), (None,'overload'))
        self.assertEqual(parse_value('+12.345E+6 OHM','OHMS'), (12345000.,'ok'))
        self.assertEqual(parse_value('+1E-9','OHMS'), (None,'underload'))
        self.assertEqual(parse_value('+1E-9','VDC'), (1e-9,'ok'))
        self.assertEqual(parse_value('0.3 AAC','VDC'), (None,'mode_changed'))
        with self.assertRaises(ValueError):
            parse_value('garbage','VDC')

    def test_front_panel_function_change_is_not_mislabeled(self):
        class Changing:
            resource = 'fixture'
            def __init__(self):
                self.func = iter(['VDC','ADC'])
            def query(self, command):
                if command == 'FUNC1?':
                    return next(self.func)
                return {'MOD?': '0','VAL?': '1.0'}[command]
        readings = Fluke45(Changing(),'FLUKE,45,fixture,1.7').read()
        self.assertIsNone(readings[0].value)
        self.assertEqual(readings[0].quality,'mode_changed')

    def test_meter_logs_with_other_instruments_on_one_timebase(self):
        with SerialMeter() as bench, tempfile.TemporaryDirectory() as folder:
            engine = Engine()
            engine.command('demo',{})
            engine.command('connect',dict(resource=bench.resource))
            path = str(Path(folder)/'run.sqlite')
            engine.command('record',dict(path=path))
            try:
                engine.cycle()
            finally:
                engine.command('stop_recording',{})
                for instrument in engine.instruments.values():
                    instrument.close()
            with closing(sqlite3.connect(path)) as db:
                row = db.execute('SELECT COUNT(DISTINCT tick_ns), COUNT(DISTINCT instrument) FROM samples').fetchone()
                self.assertEqual(row,(1,4))
                value, unit = db.execute("SELECT value,unit FROM samples WHERE channel='primary.VDC'").fetchone()
                self.assertEqual((value,unit),(.00017,'V'))

if __name__ == '__main__':
    unittest.main()
