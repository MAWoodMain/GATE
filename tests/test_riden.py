import os
import pty
import select
import threading
import time
import unittest
from unittest.mock import Mock, patch

from gate.connections import communication_type, validate_resource
from gate.drivers.riden import RidenRD6006
from gate.engine import Engine
from gate.riden_usb import FrameError, RidenUSB, crc16


class Registers:
    resource = 'riden:///dev/fixture?baud=115200&address=1'
    def __init__(self, model=60061):
        self.r = [0]*120
        self.r[:4] = [model, 0, 4684, 125]
        self.r[8:19] = [330, 50, 329, 2, 1, 100, 6500, 0, 0, 0, 0]
        self.writes = []
        self.close = Mock()
    def read_registers(self, start, count):
        return self.r[start:start+count]
    def write_register(self, register, value):
        self.writes.append((register, value))
        self.r[register] = value


class DriverTests(unittest.TestCase):
    def test_passive_identity_catalog_and_scaling(self):
        t = Registers()
        d = RidenRD6006(t)
        self.assertEqual(d.name, 'RIDEN,RD6006,4684,1.25')
        self.assertEqual(d.range_controls(), [])
        self.assertTrue(all(f.instrument_channel == 'output' for f in d.functions()))
        values = {r.channel: r.value for r in d.read()}
        self.assertEqual(values['voltage'], 3.29)
        self.assertEqual(values['current'], .002)
        self.assertEqual(values['power'], 656.36)
        d.close()
        self.assertEqual(t.writes, [])
        for code in (60065, 60066, 60120, 0):
            with self.assertRaises(ValueError): RidenRD6006(Registers(code))

    def test_limits_confirmation_and_cleanup(self):
        t = Registers()
        d = RidenRD6006(t)
        for v in (-1, 60.01, float('nan'), float('inf'), True):
            with self.assertRaises(ValueError): d.set_voltage(v)
        for v in (0, .0001, 6.001, -1, float('nan')):
            with self.assertRaises(ValueError): d.set_current_limit(v)
        self.assertFalse(t.writes)
        d.set_voltage(3.3)
        d.set_current_limit(.05)
        d.set_enabled(True)
        self.assertEqual(t.r[18], 1)
        d.close()
        self.assertEqual(t.r[18], 0)
        t.close.assert_called_once()
        t = Registers()
        d = RidenRD6006(t)
        t.write_register = Mock()
        with self.assertRaisesRegex(ValueError, 'readback'): d.set_voltage(1)

    def test_interlocks_and_uncertain_enable_cleanup(self):
        for index, value in ((8, 6001), (9, 0), (9, 6001), (16, 1)):
            t = Registers(); d = RidenRD6006(t); t.r[index] = value
            with self.assertRaises(ValueError): d.set_enabled(True)
            self.assertFalse(t.writes)
        t = Registers(); d = RidenRD6006(t)
        write = t.write_register
        def lost_ack(register, value):
            write(register, value)
            if value == 1: raise TimeoutError('lost acknowledgement')
        t.write_register = lost_ack
        with self.assertRaises(TimeoutError): d.set_enabled(True)
        d.close()
        self.assertEqual(t.r[18], 0)

    def test_engine_controls_guard_and_model_envelope(self):
        t = Registers(); d = RidenRD6006(t)
        e = Engine(.1); e.instruments[d.id] = d
        args = dict(instrument=d.id)
        with self.assertRaisesRegex(ValueError, 'wiring_confirmed'):
            e.command('output', dict(args, enabled=True))
        self.assertFalse(t.writes)
        e.command('static', dict(args, voltage=1, current_limit=.05))
        e.command('output', dict(args, enabled=True, wiring_confirmed=True))
        e.command('sweep', dict(args, start=1, stop=60, step=1, dwell=.1))
        with self.assertRaises(ValueError):
            e.command('sweep', dict(args, start=1, stop=61, step=1, dwell=.1))
        e.command('output', dict(args, enabled=False))
        self.assertFalse(e.sweeps)
        d.close()

    def test_discovery_routes_binary_transport_and_closes_unknown_models(self):
        from gate.discovery import connect
        t = Registers()
        with patch('gate.discovery.RidenUSB', return_value=t), patch('gate.discovery.SCPI') as scpi:
            device = connect(t.resource)
            self.assertIsInstance(device, RidenRD6006)
            scpi.assert_not_called()
            self.assertEqual(t.writes, [])
        t = Registers(60065)
        with patch('gate.discovery.RidenUSB', return_value=t):
            with self.assertRaises(ValueError): connect(t.resource)
        t.close.assert_called_once()

    def test_cleanup_failure_still_closes_transport_and_raw_is_unavailable(self):
        t = Registers(); d = RidenRD6006(t)
        e = Engine(); e.instruments[d.id] = d
        with self.assertRaisesRegex(ValueError, 'no raw command'):
            e.command('query', dict(instrument=d.id, command='*IDN?'))
        d.set_enabled(True)
        t.write_register = Mock(side_effect=TimeoutError('unplugged'))
        with self.assertRaises(TimeoutError): d.close()
        t.close.assert_called_once()

    def test_addresses(self):
        self.assertEqual(communication_type('riden:///dev/ttyUSB1'), 'USB')
        self.assertEqual(validate_resource('riden:///dev/ttyUSB1'), 'riden:///dev/ttyUSB1?baud=115200&address=1')
        for suffix in ('?address=0', '?address=248', '?baud=123', '?address=1&address=2', '#x', '?parity=N', '?baud=', '?address='):
            with self.assertRaises(ValueError): validate_resource('riden:///dev/ttyUSB1'+suffix)


class TransportTests(unittest.TestCase):
    def exchange(self, response, operation=None, **options):
        """Serve every request (including retries) from response(request, index)."""
        master, slave = pty.openpty()
        transport = RidenUSB('riden://'+os.ttyname(slave), **dict(dict(timeout=.15, quiet=.02), **options))
        requests = []
        stop = threading.Event()
        def server():
            request = b''
            while not stop.is_set():
                if not select.select([master], [], [], .02)[0]:
                    continue
                request += os.read(master, 8-len(request))
                if len(request) < 8:
                    continue
                requests.append(request)
                reply = response(request, len(requests)-1)
                request = b''
                if reply:
                    for byte in reply: os.write(master, bytes([byte]))
        thread = threading.Thread(target=server); thread.start()
        try:
            self.transport = transport
            return (operation or (lambda t: t.read_registers(8, 2)))(transport)
        finally:
            stop.set(); thread.join(timeout=1)
            transport.close(); os.close(master); os.close(slave)
            self.assertFalse(thread.is_alive())
            self.requests = requests
            self.assertTrue(all(r[-2:] == crc16(r[:-2]) for r in requests))

    def test_crc_and_fragmented_read_write(self):
        self.assertEqual(crc16(bytes.fromhex('010300000004')), bytes.fromhex('4409'))
        body = bytes.fromhex('010304014a0032')
        self.assertEqual(self.exchange(lambda *_: body+crc16(body)), [330, 50])
        self.assertEqual(len(self.requests), 1)
        self.exchange(lambda request, _: request, lambda t: t.write_register(18, 0))

    def test_persistent_bad_frames_exceptions_timeout_and_wrong_echo(self):
        for body, error in ((bytes.fromhex('02030400010002'), 'address/function'),
                            (bytes.fromhex('0103020001'), 'register count')):
            with self.assertRaisesRegex(FrameError, error):
                self.exchange(lambda *_, b=body: b+crc16(b))
            self.assertEqual(len(self.requests), 3)
        with self.assertRaisesRegex(FrameError, 'CRC'):
            self.exchange(lambda *_: bytes.fromhex('010304000100020000'))
        with self.assertRaises(TimeoutError): self.exchange(lambda *_: b'')
        self.assertEqual(len(self.requests), 3)
        # Device-reported exceptions and wrong write echoes are definitive: no retry.
        body = bytes.fromhex('018302')
        with self.assertRaisesRegex(ValueError, 'exception'):
            self.exchange(lambda *_: body+crc16(body))
        self.assertEqual(len(self.requests), 1)
        body = bytes.fromhex('010600120001')
        with self.assertRaisesRegex(ValueError, 'echo'):
            self.exchange(lambda *_: body+crc16(body), lambda t: t.write_register(18, 0))
        self.assertEqual(len(self.requests), 1)

    def test_transient_lost_corrupt_and_late_replies_recover(self):
        good = bytes.fromhex('010304014a0032'); good += crc16(good)
        # Lost reply, then success.
        self.assertEqual(self.exchange(lambda _, i: b'' if i == 0 else good), [330, 50])
        self.assertEqual((len(self.requests), self.transport.retries), (2, 1))
        # Corrupted CRC, then success.
        self.assertEqual(self.exchange(lambda _, i: good[:-1]+b'\x00' if i == 0 else good), [330, 50])
        self.assertEqual(len(self.requests), 2)
        # Truncated reply whose tail never arrives, then success.
        self.assertEqual(self.exchange(lambda _, i: good[:4] if i == 0 else good), [330, 50])
        # A reply arriving after the timeout is drained, not parsed as the next frame.
        def late(_, i):
            if i == 0:
                time.sleep(.2)
                return bytes.fromhex('0103040001000209')
            return good
        self.assertEqual(self.exchange(late), [330, 50])
        # Writes are idempotent absolute values and are retried after a lost echo.
        self.exchange(lambda request, i: b'' if i == 0 else request, lambda t: t.write_register(18, 1))
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.requests[0], self.requests[1])
