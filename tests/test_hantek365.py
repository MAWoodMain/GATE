import ctypes
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from gate.connections import validate_resource, communication_type
from gate.discovery import connect
from gate.drivers.hantek365 import Hantek365, parse_packet
from gate.hantek_usb import HantekUSB, BulkTransfer, device_info, resources
from gate.engine import Engine
from gate.experiments import BUCK_CONVERTER


def packet(digits='0250', decimal=0x34, units=2, flags=0x20, multiplier=0, negative=False, nano=0):
    return bytes([0xA0, ord('-' if negative else '+')])+digits.encode()+bytes([32, decimal, flags, nano, multiplier, units, 0, 13, 10])


class HantekTests(unittest.TestCase):
    def meter(self):
        transport = Mock(resource='hantek://usb/3-1')
        transport.request.return_value = packet()
        return Hantek365(transport), transport

    def test_actual_365b_temperature_frames_and_bounds(self):
        reading, function = parse_packet(bytes.fromhex('a0 2b 30 32 35 32 20 34 20 00 00 02 00 0d 0a'))
        self.assertEqual((reading.value, reading.unit, function), (25.2, '°C', 'TEMPC'))
        reading, function = parse_packet(bytes.fromhex('a0 2b 30 37 36 38 20 34 20 00 00 01 00 0d 0a'))
        self.assertEqual((reading.value, reading.unit, function), (76.8, '°F', 'TEMPF'))
        reading, _ = parse_packet(bytes.fromhex('a0 2d 30 34 30 38 20 30 00 00 00 02 00 0d 0a'))
        self.assertIsNone(reading.value)
        self.assertEqual(reading.quality, 'out_of_range')
        self.assertEqual(parse_packet(packet(negative=True))[0].value, -25.)

    def test_si_prefix_decimal_modes_overload_and_relative(self):
        for kwargs, expected, function in [
            (dict(digits='1234', units=0x80, flags=0x30, multiplier=0x40), .1234, 'VDC'),
            (dict(digits='1234', units=0x80, flags=0x28), 123.4, 'VAC'),
            (dict(digits='1234', units=0x40, flags=0x30, multiplier=0x40), .1234, 'MADC'),
            (dict(digits='1234', units=0x20, multiplier=0x20), 123400., 'OHMS'),
            (dict(digits='1234', units=4, nano=2), 123.4e-9, 'CAP'),
            (dict(digits='0010', units=0x20, multiplier=8), 1., 'CONT'),
            (dict(digits='0500', units=0x80, flags=0, decimal=0x31), .5, 'DIODE'),
        ]:
            with self.subTest(kwargs=kwargs):
                reading, actual = parse_packet(packet(**kwargs))
                self.assertAlmostEqual(reading.value, expected)
                self.assertEqual(actual, function)
        self.assertEqual(parse_packet(packet(digits='0???'))[0].quality, 'overload')
        self.assertEqual(parse_packet(packet(flags=0x24))[0].quality, 'relative')
        for malformed in (b'', packet()[:-1], packet()[:-2]+b'xx', packet(units=0),
                          packet(decimal=0x33), packet(flags=0x38), packet(multiplier=0x60), packet(digits='!123')):
            with self.assertRaises(ValueError):
                parse_packet(malformed)

    def test_connect_preserves_mode_and_no_data_is_not_a_stale_sample(self):
        meter, transport = self.meter()
        transport.set_mode.assert_not_called()
        transport.request.return_value = None
        reading = meter.read()[0]
        self.assertEqual((reading.channel, reading.value, reading.unit, reading.quality), ('temperature', None, '°C', 'not_ready'))
        transport.request.side_effect = TimeoutError('No response')
        self.assertEqual(meter.read()[0].quality, 'not_ready')
        self.assertEqual(meter.read()[0].quality, 'not_ready')
        with self.assertRaises(TimeoutError):
            meter.read()

    def test_celsius_default_converts_native_fahrenheit_without_mode_change(self):
        transport = Mock(resource='hantek://usb/3-1')
        transport.request.return_value = packet('0770', units=1)
        meter = Hantek365(transport)
        self.assertEqual((meter.last_reading.value, meter.last_reading.unit, meter.current_function), (25., '°C', 'TEMPC'))
        self.assertEqual(meter._native_function, 'TEMPF')
        transport.set_mode.assert_not_called()
        transport.request.return_value = packet('0090', units=1, flags=0x24)
        relative = meter.read()[0]
        self.assertEqual((relative.value, relative.unit, relative.quality), (5., '°C', 'relative'))
        transport.request.return_value = packet('0???', units=1)
        unavailable = meter.read()[0]
        self.assertEqual((unavailable.value, unavailable.unit, unavailable.quality), (None, '°C', 'overload'))

    def test_fahrenheit_remains_an_explicit_choice_and_celsius_can_be_restored(self):
        meter, transport = self.meter()
        transport.request.return_value = packet('0770', units=1)
        with patch('gate.drivers.hantek365.time.sleep'):
            meter.select_function('TEMPF')
        self.assertEqual((meter.last_reading.value, meter.last_reading.unit, meter.current_function), (77., '°F', 'TEMPF'))
        transport.request.return_value = packet('0250')
        with patch('gate.drivers.hantek365.time.sleep'):
            meter.select_function('TEMPC')
        self.assertEqual((meter.last_reading.value, meter.last_reading.unit, meter.current_function), (25., '°C', 'TEMPC'))

    def test_celsius_conversion_cannot_falsely_confirm_a_hardware_mode_change(self):
        meter, transport = self.meter()
        transport.request.return_value = packet('0770', units=1)
        with patch('gate.drivers.hantek365.time.sleep'), \
             patch('gate.drivers.hantek365.time.monotonic', side_effect=[0., 1., 6.]), self.assertRaises(ValueError):
            meter.select_function('TEMPC')
        self.assertEqual(meter.last_reading.unit, '°C')
        self.assertEqual(meter._native_function, 'TEMPF')

    def test_mode_change_discards_two_conversions_and_verifies_units(self):
        meter, transport = self.meter()
        transport.request.side_effect = [packet('2340', 0x30), packet('0234', 0x30), None, packet('0234')]
        with patch('gate.drivers.hantek365.time.sleep'):
            meter.select_function('TEMPC')
        transport.set_mode.assert_called_once_with(0xF5)
        self.assertEqual(meter.last_reading.value, 23.4)
        with self.assertRaises(ValueError):
            meter.select_function('RESET')

    def test_high_current_selection_cycles_protocol_subranges(self):
        meter, transport = self.meter()
        transport.request.return_value = packet(digits='0123', decimal=0x32, units=0x40, flags=0x30)
        with patch('gate.drivers.hantek365.time.sleep'):
            meter.select_function('ADC')
        self.assertEqual([call.args[0] for call in transport.set_mode.call_args_list], [0xC0, 0xC1, 0xC2, 0xC3])
        self.assertEqual((meter.last_reading.value, meter.last_reading.unit, meter.current_function), (1.23, 'A', 'ADC'))

    def test_binary_packet_fragmentation_ack_and_no_data(self):
        transport = HantekUSB.__new__(HantekUSB)
        transport.buffer, transport.timeout = bytearray(), 1.
        frame = packet()
        transport.transfer = Mock(side_effect=[b'', b'\xdd'+frame[:5], frame[5:]])
        self.assertEqual(transport.request(), frame)
        self.assertEqual(transport.transfer.call_args_list[0].args, (b'\x01\x0f',))
        transport.transfer = Mock(side_effect=[b'', b'\xaa'])
        self.assertIsNone(transport.request())
        transport.transfer = Mock(side_effect=[b'', b'\xaa\xdd'])
        transport.set_mode(0xF5)

    def test_usb_ioctl_alignment_and_interface_cleanup(self):
        info = dict(node='/dev/bus/usb/003/015')
        def ioctl(fd, request, argument, *args):
            if request == 0xC0005502 | ctypes.sizeof(BulkTransfer) << 16:
                bulk = BulkTransfer.from_buffer_copy(argument)
                if bulk.endpoint == 0x81:
                    ctypes.memmove(bulk.data, b'\xaa', 1)
                    return 1
                self.assertEqual(ctypes.string_at(bulk.data, bulk.length), b'\x01\x0f')
                return bulk.length
            return 0
        with patch('gate.hantek_usb.device_info', return_value=info), patch('gate.hantek_usb.os.open', return_value=123), \
             patch('gate.hantek_usb.os.close') as close, patch('gate.hantek_usb.fcntl.ioctl', side_effect=ioctl) as calls:
            transport = HantekUSB('hantek://usb/3-1')
            self.assertIsNone(transport.request())
            transport.close()
            transport.close()
            close.assert_called_once_with(123)
            self.assertEqual(calls.call_args_list[0].args[1], 0x8004550F)
            self.assertEqual(calls.call_args_list[-1].args[1], 0x80045510)

    def test_discovery_guard_usb_port_identity_and_replug_address(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            port = root/'3-1.2'
            port.mkdir()
            values = dict(idVendor='0483', idProduct='5722', manufacturer='Hantek', product='DMM USB', serial='Y', busnum='3', devnum='15')
            for key, value in values.items():
                (port/key).write_text(value)
            interface = root/'3-1.2:1.0'
            interface.mkdir()
            (interface/'bInterfaceClass').write_text('ff')
            for endpoint in ('81', '02'):
                ep = interface/('ep_'+endpoint)
                ep.mkdir()
                (ep/'bEndpointAddress').write_text(endpoint)
                (ep/'bmAttributes').write_text('02')
            with patch('gate.hantek_usb.USB_ROOT', root):
                self.assertEqual(resources(), ['hantek://usb/3-1.2'])
                self.assertEqual(device_info(resources()[0])['node'], '/dev/bus/usb/003/015')
                (port/'devnum').write_text('27')
                self.assertEqual(device_info(resources()[0])['node'], '/dev/bus/usb/003/027')
                (port/'manufacturer').write_text('STMicroelectronics')
                self.assertEqual(resources(), [])
        self.assertEqual(communication_type('hantek://usb/3-1.2'), 'USB')
        self.assertEqual(validate_resource('hantek://usb/3-1.2'), 'hantek://usb/3-1.2')
        for invalid in ('hantek://usb/../etc', 'hantek://usb/3-1?reset=1', 'hantek://user@usb/3-1', 'hantek://usb/3-1#x'):
            with self.assertRaises(ValueError):
                validate_resource(invalid)

    def test_scan_dispatch_does_not_send_scpi_to_binary_meter(self):
        meter, transport = self.meter()
        with patch('gate.discovery.HantekUSB', return_value=transport), patch('gate.discovery.SCPI') as scpi:
            self.assertIsInstance(connect('hantek://usb/3-1'), Hantek365)
            scpi.assert_not_called()

    def test_temperature_secondary_metric_records_without_fake_ambient_probe(self):
        meter, transport = self.meter()
        transport.request.return_value = packet('0770', units=1)
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(.1, experiment_directory=directory)
            engine.command('demo_experiment', {})
            engine.instruments[meter.id] = meter
            engine.command('start_experiment', dict(experiment='buck_converter', test='efficiency',
                bindings=dict(supply='demo:converter_supply', load='demo:electronic_load', thermal=meter.id),
                parameters=dict(input_start_v=8., input_stop_v=8., load_start_a=.1, load_stop_a=.1, settle_s=.1),
                wiring_confirmed=True, enable_outputs=True))
            run = engine.experiment_run
            run.due = 0.
            engine.cycle()
            self.assertEqual(run.metadata['state'], 'completed', run.metadata['error'])
            self.assertEqual(run.points[0]['dut_temperature_c'], 25.)
            self.assertEqual(run.metadata['metrics']['dut_temperature_c'][1], '°C')
            self.assertIn('°C', Path(run.metadata['report']).read_text())
            self.assertIsNone(run.points[0]['ambient_temperature_c'])
            engine.close()
