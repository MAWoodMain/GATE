import socket
import threading
import unittest
from unittest.mock import Mock, patch
from gate.transport import SCPI
from gate.drivers.siglent import SiglentSDS, waveform_scalar
from gate.waveforms import Waveform, validate_waveform, validate_screenshot


def screen_bitmap():
    import struct
    # A complete 1x1, 24-bit black Windows bitmap with a padded scanline.
    return struct.pack('<2sIHHI', b'BM', 58, 0, 0, 54)+struct.pack('<IiiHHIIiiII', 40, 1, 1, 1, 24, 0, 4, 0, 0, 0, 0)+b'\0'*4


class WaveformDriverTests(unittest.TestCase):
    def request(self):
        return dict(phase='settled', channel='C1', unit='V', points=16, window_s=None, pretrigger=None, trigger=None)
    def fixture(self):
        replies = {'C1:UNIT?':'C1:UNIT V', 'C1:TRA?':'C1:TRA ON', 'WFSU?':'WFSU SP,4,NP,100,FP,0', 'C1:VDIV?':'C1:VDIV 500MV',
            'C1:OFST?':'C1:OFST -500MV', 'TDIV?':'TDIV 1US', 'TRDL?':'TRDL 2US', 'SARA?':'SARA 1.00MSa/s', 'SANU? C1':'SANU 3pts'}
        transport = Mock()
        transport.timeout = 1.
        transport.query.side_effect = replies.__getitem__
        transport.query_binary.return_value = bytes([252, 0, 4])
        return SiglentSDS(transport, 'Siglent Technologies,SDS1202X-E,fixture,1.3.26'), transport
    def test_native_siglent_scaling_and_transfer_settings_restoration(self):
        scope, transport = self.fixture()
        token = scope.begin_waveform(self.request())
        waveform = scope.poll_waveform(token)
        self.assertEqual(waveform.values, [.42, .5, .58])
        self.assertAlmostEqual(waveform.start_time_s, -5e-6)
        self.assertEqual(waveform.sample_interval_s, 1e-6)
        self.assertIsNone(waveform.trigger_utc_ns)
        self.assertIn('freshness', waveform.metadata['acquisition'])
        self.assertEqual([call.args[0] for call in transport.write.call_args_list],
                         ['WFSU SP,1,NP,16,FP,0','WFSU SP,4,NP,100,FP,0'])
        transport.query_binary.assert_called_once_with('C1:WF? DAT2', maximum=16)
    def test_siglent_rejects_unconfigured_transition_and_restores_transfer_on_error(self):
        scope, transport = self.fixture()
        with self.assertRaises(ValueError):
            scope.begin_waveform(dict(self.request(), phase='transition'))
        transport.write.assert_not_called()
        token = scope.begin_waveform(self.request())
        transport.query_binary.side_effect = ValueError('Malformed block')
        with self.assertRaises(ValueError):
            scope.poll_waveform(token)
        self.assertEqual(transport.write.call_args.args[0], 'WFSU SP,4,NP,100,FP,0')
    def test_binary_usb_uses_eom_and_preserves_embedded_newlines(self):
        payload = b'\x00\n\r\xff\n\x01'
        raw = b'C1:WF DAT2,#16'+payload+b'\n\n'
        attrs = iter([0,1])
        def ioctl(fd, request, argument, *extra):
            if request == 0x80015B18:
                argument[0] = next(attrs)
        with patch('gate.transport.os.open',return_value=123), patch('gate.transport.os.close'), \
             patch('gate.transport.os.write', side_effect=lambda fd,data:len(data)), \
             patch('gate.transport.os.read', side_effect=[raw[:15],raw[15:]]), \
             patch('gate.transport.fcntl.ioctl',side_effect=ioctl):
            transport = SCPI('/dev/usbtmc9')
            self.assertEqual(transport.query_binary('C1:WF? DAT2', maximum=16), payload)
            transport.close()
    def test_binary_oversize_closes_transport(self):
        raw = b'C1:WF DAT2,#510000'
        def ioctl(fd, request, argument, *extra):
            if request == 0x80015B18:
                argument[0] = 1
        with patch('gate.transport.os.open',return_value=123), patch('gate.transport.os.close') as close, \
             patch('gate.transport.os.write',side_effect=lambda fd,data:len(data)), \
             patch('gate.transport.os.read',return_value=raw), patch('gate.transport.fcntl.ioctl',side_effect=ioctl):
            transport = SCPI('/dev/usbtmc9')
            with self.assertRaises(ValueError):
                transport.query_binary('C1:WF? DAT2', maximum=16)
            self.assertIsNone(transport.fd)
            close.assert_called_once()
    def test_binary_tcp_fragmentation_and_next_text_response(self):
        payload = bytes([0,10,13,255,1,10,0])
        server = socket.socket()
        server.bind(('127.0.0.1',0))
        server.listen(1)
        def worker():
            conn, _ = server.accept()
            with conn, conn.makefile('rb') as stream:
                self.assertEqual(stream.readline(), b'C1:WF? DAT2\n')
                raw = b'C1:WF DAT2,#17'+payload+b'\n\n'
                for fragment in (raw[:3],raw[3:15],raw[15:]):
                    conn.sendall(fragment)
                self.assertEqual(stream.readline(), b'*IDN?\n')
                conn.sendall(b'Fixture,scope,1,1\n')
        thread = threading.Thread(target=worker)
        thread.start()
        transport = None
        try:
            transport = SCPI('tcp://127.0.0.1:'+str(server.getsockname()[1]))
            self.assertEqual(transport.query_binary('C1:WF? DAT2',maximum=16),payload)
            self.assertEqual(transport.query('*IDN?'),'Fixture,scope,1,1')
        finally:
            if transport:
                transport.close()
            server.close()
            thread.join(timeout=2)
    def test_waveform_validation_and_scale_units(self):
        request = self.request()
        value = Waveform('C1',[-1.,1.],.001,-.001,'V')
        stats = validate_waveform(value,request)
        self.assertEqual(stats['peak_to_peak'],2.)
        self.assertEqual(stats['ac_rms'],1.)
        for malformed in (Waveform('C2',[-1.,1.],.001,0.,'V'), Waveform('C1',[float('nan'),1.],.001,0.,'V'),
                          Waveform('C1',[-1.,1.],0.,0.,'V')):
            with self.assertRaises(ValueError):
                validate_waveform(malformed,request)
        self.assertAlmostEqual(waveform_scalar('TDIV 5NS','time'),5e-9)
        self.assertEqual(waveform_scalar('SARA 1.0GSa/s','rate'),1e9)
        with self.assertRaises(ValueError):
            waveform_scalar('VDIV 1A','voltage')

    def test_off_channels_are_never_measured_or_enabled_by_polling(self):
        scope, transport = self.fixture()
        def reply(command):
            if command.endswith(':TRA?'):
                return 'ON' if command.startswith('C1') else 'OFF'
            parameter = command.split()[-1]
            return 'PAVA '+parameter+',1'+('Hz' if parameter == 'FREQ' else 'V')
        transport.query.side_effect = reply
        readings = scope.read()
        self.assertEqual([reading.quality for reading in readings], ['ok']*3+['disabled']*3)
        self.assertFalse(any(call.args[0].startswith('C2:PAVA') for call in transport.query.call_args_list))
        transport.write.assert_not_called()

    def test_experiment_channels_are_applied_and_restored(self):
        scope, transport = self.fixture()
        states = {'C1':'OFF', 'C2':'ON'}
        transport.query.side_effect = lambda command: states[command.split(':')[0]]
        def write(command):
            channel, state = command.split(':TRA ')
            states[channel] = state
        transport.write.side_effect = write
        scope.begin_experiment_channels({'C1', 'C1.RMS'})
        self.assertEqual(states, {'C1':'ON', 'C2':'OFF'})
        scope.end_experiment_channels()
        self.assertEqual(states, {'C1':'OFF', 'C2':'ON'})

    def armed_fixture(self):
        scope, transport = self.fixture()
        native = transport.query.side_effect
        replies = {'TRMD?':'TRMD AUTO', 'TRSE?':'TRSE EDGE,SR,C2,HT,OFF',
                   'C1:TRLV?':'C1:TRLV 4.75V', 'C1:TRSL?':'C1:TRSL POS', 'C1:TRCP?':'C1:TRCP AC'}
        events = iter([1, 8192, 0, 1])  # stale event, ready, waiting, fresh event
        modes = iter(['AUTO', 'SINGLE', 'STOP'])
        def reply(command):
            if command == 'INR?': return 'INR '+str(next(events))
            if command == 'TRMD?': return 'TRMD '+next(modes)
            return replies[command] if command in replies else native(command)
        transport.query.side_effect = reply
        def write(command):
            for key in ('TRSE', 'C1:TRLV', 'C1:TRSL', 'C1:TRCP'):
                if command.startswith(key+' '):
                    replies[key+'?'] = command
        transport.write.side_effect = write
        transport.query_binary.side_effect = lambda command, **kwargs: screen_bitmap() if command == 'SCDP' else bytes([252,0,4])
        request = dict(self.request(), phase='transition', trigger=dict(level=4.75, slope='falling'), screenshot=True)
        return scope, transport, request

    def test_single_shot_waits_for_fresh_stopped_acquisition_and_saves_screen(self):
        scope, transport, request = self.armed_fixture()
        previous = transport.query.side_effect
        transport.query.side_effect = lambda command: 'SANU 160pts' if command == 'SANU? C1' else previous(command)
        token = scope.begin_waveform(request)
        self.assertIn('TRMD SINGLE', [call.args[0] for call in transport.write.call_args_list])
        transport.query.reset_mock()
        self.assertEqual([reading.quality for reading in scope.read()], ['unavailable']*3+['disabled']*3)
        self.assertFalse(any('PAVA' in call.args[0] for call in transport.query.call_args_list))
        self.assertIsNone(scope.poll_waveform(token))
        waveform = scope.poll_waveform(token)
        self.assertAlmostEqual(waveform.sample_interval_s, 1e-5)
        self.assertIn('WFSU SP,10,NP,16,FP,0', [call.args[0] for call in transport.write.call_args_list])
        self.assertEqual(waveform.screenshot, screen_bitmap())
        self.assertIn('fresh acquisition', waveform.metadata['acquisition'])
        transport.query_binary.assert_any_call('SCDP', maximum=1048576, terminator=b'\n', framing='bmp')
        # Trigger settings remain fixed until the image and data are transferred.
        self.assertNotEqual(transport.write.call_args.args[0], 'TRMD AUTO')
        scope.cancel_waveform(token)
        self.assertEqual(transport.write.call_args.args[0], 'TRMD AUTO')
        self.assertIn('C1:TRCP AC', [call.args[0] for call in transport.write.call_args_list])

    def test_early_trigger_and_not_ready_restore_without_capturing(self):
        for early in (True, False):
            scope, transport, request = self.armed_fixture()
            previous = transport.query.side_effect
            transport.query.side_effect = lambda command: ('INR 1' if early else 'INR 0') if command == 'INR?' else previous(command)
            with patch('gate.drivers.siglent.time.monotonic', side_effect=[0., 0., 2.]):
                with self.assertRaises((ValueError, TimeoutError)):
                    scope.begin_waveform(request)
            self.assertEqual(transport.write.call_args.args[0], 'TRMD AUTO')
            self.assertFalse(scope._waveform_requests)
            transport.query_binary.assert_not_called()

    def test_cancel_restores_armed_trigger_and_image_failure_keeps_waveform(self):
        scope, transport, request = self.armed_fixture()
        token = scope.begin_waveform(request)
        scope.cancel_waveform(token)
        self.assertFalse(scope._waveform_requests)
        self.assertEqual(transport.write.call_args.args[0], 'TRMD AUTO')
        scope, transport, request = self.armed_fixture()
        token = scope.begin_waveform(request)
        self.assertIsNone(scope.poll_waveform(token))
        transport.query_binary.side_effect = lambda command, **kwargs: b'not an image' if command == 'SCDP' else bytes([0,1])
        waveform = scope.poll_waveform(token)
        self.assertIsNone(waveform.screenshot)
        self.assertIn('BMP', waveform.screenshot_error)
        scope.cancel_waveform(token)

    def test_scope_screen_fragmentation_and_next_text_response(self):
        image = screen_bitmap()
        validate_screenshot(image)
        with self.assertRaises(ValueError): validate_screenshot(image[:-1])
        server = socket.socket()
        server.bind(('127.0.0.1', 0))
        server.listen(1)
        errors = []
        def worker():
            try:
                conn, _ = server.accept()
                with conn, conn.makefile('rb') as stream:
                    self.assertEqual(stream.readline(), b'SCDP\n')
                    for part in (image[:3], image[3:29], image[29:]+b'\n'):
                        conn.sendall(part)
                    self.assertEqual(stream.readline(), b'*IDN?\n')
                    conn.sendall(b'Fixture\n')
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=worker)
        thread.start()
        transport = SCPI('tcp://127.0.0.1:'+str(server.getsockname()[1]))
        try:
            self.assertEqual(transport.query_binary('SCDP', maximum=1048576, terminator=b'\n', framing='bmp'), image)
            self.assertEqual(transport.query('*IDN?'), 'Fixture')
        finally:
            transport.close()
            server.close()
            thread.join(timeout=2)
        self.assertFalse(errors)
