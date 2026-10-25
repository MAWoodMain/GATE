import csv
import json
import math
import socket
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from gate.drivers.siglent import SiglentSDS, parse_measurement
from gate.engine import Engine
from gate.storage import export_csv
from gate.transport import SCPI

class CoreTests(unittest.TestCase):
    def test_device_profiles_and_settings_survive_restart_without_output_replay(self):
        with tempfile.TemporaryDirectory() as folder:
            profile=Path(folder)/'devices.json'
            engine=Engine(profile_path=profile)
            engine.saved_resources.add('serial:///dev/fixture?baud=9600')
            engine.command('settings',dict(interval=.5))
            restarted=Engine(profile_path=profile)
            self.assertEqual(restarted.interval,.5)
            self.assertEqual(restarted.saved_resources,{'serial:///dev/fixture?baud=9600'})
            self.assertFalse(restarted.instruments)
            restarted.command('disconnect',dict(instrument='serial:///dev/fixture?baud=9600'))
            self.assertFalse(Engine(profile_path=profile).saved_resources)

    def test_siglent_measurement_variants(self):
        for response in ('C1:PAVA PKPK,2.30E+00V', 'C1:PAVA PKPK,2.30E+00V,OK'):
            self.assertEqual(parse_measurement(response, 'PKPK', 'V'), 2.3)
        self.assertIsNone(parse_measurement('C1:PAVA FREQ,UNDEF', 'FREQ', 'Hz'))
        self.assertIsNone(parse_measurement('C1:PAVA FREQ,9.9E37Hz', 'FREQ', 'Hz'))
        with self.assertRaises(ValueError):
            parse_measurement('C1:PAVA PKPK,2A', 'PKPK', 'V')
        with self.assertRaises(ValueError):
            parse_measurement('garbage', 'PKPK', 'V')
        self.assertTrue(SiglentSDS.matches('Siglent Technologies,SDS1202X-E,123,1.0'))
        self.assertFalse(SiglentSDS.matches('Siglent,SDS1204X-E,123,1.0'))

    def test_shared_timebase_and_output_events(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'run.sqlite'
            engine = Engine(.1)
            engine.command('demo', {})
            engine.command('record', {'path': str(path)})
            engine.command('static', {'instrument': 'demo:power_supply', 'voltage': 5})
            self.assertFalse(engine.instruments['demo:power_supply'].enabled)
            engine.command('output', {'instrument':'demo:power_supply', 'enabled':True})
            engine.cycle()
            engine.cycle()
            engine.command('stop_recording', {})
            with sqlite3.connect(path) as db:
                rows = db.execute('SELECT tick,COUNT(DISTINCT tick_ns),COUNT(DISTINCT instrument) FROM samples GROUP BY tick').fetchall()
                self.assertEqual(rows, [(0,1,3),(1,1,3)])
                self.assertEqual(db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 2)
                for tick, start, end in db.execute('SELECT tick_ns,read_start_ns,read_end_ns FROM samples'):
                    self.assertLessEqual(tick,start)
                    self.assertLessEqual(start,end)
                self.assertEqual(db.execute("SELECT value FROM samples WHERE channel='set_voltage' LIMIT 1").fetchone()[0],5)
            export_csv(path, str(path)+'.csv')
            with open(str(path)+'.csv') as stream:
                rows = list(csv.DictReader(stream))
                self.assertEqual(len(rows),14)
                self.assertIn('tick_utc_ns',rows[0])

    def test_sweep_boundaries_and_cancellation(self):
        engine = Engine(.1)
        engine.command('demo', {})
        args = dict(instrument='demo:power_supply',start=0,stop=1,step=.6,dwell=.1)
        engine.command('sweep', args)
        for expected in (.6,1):
            engine.sweeps['demo:power_supply']['due'] = 0
            engine.cycle()
            self.assertEqual(engine.instruments['demo:power_supply'].voltage, expected)
        self.assertFalse(engine.sweeps)
        engine.command('sweep', dict(args,start=1,stop=0,step=-.6))
        engine.sweeps['demo:power_supply']['due'] = 0
        engine.cycle()
        self.assertAlmostEqual(engine.instruments['demo:power_supply'].voltage,.4)
        engine.command('output',dict(instrument='demo:power_supply',enabled=False))
        self.assertFalse(engine.sweeps)
        for changes in (dict(step=0),dict(step=-1),dict(stop=31),dict(dwell=.01),dict(step=math.nan)):
            with self.assertRaises(ValueError):
                engine.command('sweep',dict(args,**changes))

    def test_instrument_error_does_not_break_other_logging(self):
        engine = Engine(.1)
        engine.command('demo', {})
        instrument=engine.instruments['demo:multimeter']
        def broken():
            raise ConnectionError('unplugged')
        instrument.read=broken
        engine.cycle()
        cards=engine.snapshot()['instruments']
        self.assertEqual(len(cards),3)
        self.assertEqual(cards[0]['readings'][0]['quality'],'error')
        self.assertEqual(cards[0]['readings'][0]['value'],None)
        self.assertNotIn('demo:multimeter',engine.instruments)
        self.assertEqual(len(cards[1]['readings']),4)

    def test_scpi_fragmentation_and_real_driver_queries(self):
        server=socket.socket()
        server.bind(('127.0.0.1',0)); server.listen(1)
        commands=[]
        def respond():
            conn,_=server.accept()
            with conn,conn.makefile('rb') as stream:
                for line in stream:
                    cmd=line.decode().strip();commands.append(cmd)
                    if cmd=='*IDN?':
                        response='Siglent Technologies,SDS1202X-E,serial,version\n'
                    else:
                        parameter=cmd.split()[-1]
                        response=f'C1:PAVA {parameter},1.0E+00'+('Hz' if parameter=='FREQ' else 'V')+'\n'
                    midpoint=len(response)//2
                    conn.sendall(response[:midpoint].encode());conn.sendall(response[midpoint:].encode())
        thread=threading.Thread(target=respond);thread.start()
        from gate.discovery import connect
        try:
            instrument=connect('tcp://127.0.0.1:'+str(server.getsockname()[1]))
            self.assertEqual(len(instrument.read()),6)
            instrument.close()
        finally:
            server.close();thread.join(timeout=3)
        self.assertEqual(commands,['*IDN?','C1:PAVA? PKPK','C1:PAVA? RMS','C1:PAVA? FREQ','C2:PAVA? PKPK','C2:PAVA? RMS','C2:PAVA? FREQ'])

    def test_usb_message_spans_transfers_without_newline(self):
        from unittest.mock import patch
        identity = b'Siglent Technologies,SDS1202X-E,SDS1EDEC4R1460,1.3.26'
        chunks = [identity[:48], identity[48:]]
        attributes = iter([0, 1])
        def ioctl(fd, request, argument, *extra):
            if request == 0x80015B18:
                argument[0] = next(attributes)
        with patch('gate.transport.os.open', return_value=123), \
             patch('gate.transport.os.write', return_value=6) as write, \
             patch('gate.transport.os.read', side_effect=chunks), \
             patch('gate.transport.os.close'), \
             patch('gate.transport.fcntl.ioctl', side_effect=ioctl), \
             patch('gate.transport.select.select', side_effect=AssertionError('USB must initiate read without poll')):
            transport = SCPI('/dev/usbtmc3')
            self.assertEqual(transport.query('*IDN?'), identity.decode())
            write.assert_called_once_with(123, b'*IDN?\n')
            transport.close()

    def test_usb_failure_closes_session(self):
        from unittest.mock import patch
        with patch('gate.transport.os.open', return_value=123), \
             patch('gate.transport.os.write', return_value=6), \
             patch('gate.transport.os.read', side_effect=TimeoutError('USB timeout')), \
             patch('gate.transport.os.close') as close, \
             patch('gate.transport.fcntl.ioctl'):
            transport = SCPI('/dev/usbtmc3')
            with self.assertRaises(TimeoutError):
                transport.query('*IDN?')
            close.assert_called_once_with(123)
            self.assertIsNone(transport.fd)

    def test_timeout_discards_connection(self):
        server=socket.socket();server.bind(('127.0.0.1',0));server.listen(1)
        release=threading.Event()
        def silent():
            conn,_=server.accept()
            with conn:
                release.wait(2)
        thread=threading.Thread(target=silent);thread.start()
        transport=SCPI('tcp://127.0.0.1:'+str(server.getsockname()[1]),timeout=.05)
        try:
            with self.assertRaises(TimeoutError):
                transport.query('*IDN?')
            self.assertIsNone(transport.fd)
        finally:
            release.set();thread.join();server.close()

if __name__ == '__main__':
    unittest.main()
