import ctypes as C
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from gate.usb_screen import capture, receive_screen
from tests.test_waveform_drivers import screen_bitmap


class USBScreenTests(unittest.TestCase):
    def message(self, payload):
        return struct.pack('<BBBBIBBBB', 2, 2, 253, 0, len(payload), 1, 0, 0, 0)+payload

    def test_short_packets_and_previous_zlp_preserve_complete_screen(self):
        payload = screen_bitmap()+b'\n'
        message = self.message(payload)
        parts = iter([b'', message[:8], message[8:64], message[64:]])
        writes = []
        self.assertEqual(receive_screen(writes.append, lambda: next(parts)), payload)
        self.assertEqual(writes[0][12:18], b'SCDP\n\0')
        self.assertEqual(writes[1][:3], b'\x02\x02\xfd')

    def test_bad_tags_oversize_truncation_and_incomplete_eom_are_rejected(self):
        message = self.message(screen_bitmap())
        for malformed in (b'bad header!!', message[:4]+struct.pack('<I', 1048577)+message[8:],
                          message[:8]+b'\0'+message[9:]):
            with self.assertRaises(ValueError):
                receive_screen(lambda _: None, lambda: malformed)
        parts = iter([message[:20]])
        with patch('gate.usb_screen.time.monotonic', side_effect=[0., 1., 4.]), self.assertRaises(TimeoutError):
            receive_screen(lambda _: None, lambda: next(parts, b''))

    def test_kernel_driver_restored_after_success_and_transfer_error(self):
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as folder:
                parent = Path(folder)
                (parent/'busnum').write_text('3')
                (parent/'devnum').write_text('14')
                interface = parent/'interface'
                interface.mkdir()
                (interface/'bInterfaceNumber').write_text('00')
                for name, direction, address in [('ep_01', 'out', '01'), ('ep_81', 'in', '81')]:
                    endpoint = interface/name
                    endpoint.mkdir()
                    for key, value in dict(type='Bulk', direction=direction, bEndpointAddress=address).items():
                        (endpoint/key).write_text(value)
                lib = Mock()
                for name in ('detach_kernel_driver', 'attach_kernel_driver', 'claim_interface', 'release_interface'):
                    getattr(lib, 'libusb_'+name).return_value = 0
                lib.libusb_init.side_effect = lambda context: setattr(context._obj, 'value', 1) or 0
                devices = (C.c_void_p*1)(123)
                def device_list(context, result):
                    C.cast(result, C.POINTER(C.POINTER(C.c_void_p)))[0] = C.cast(devices, C.POINTER(C.c_void_p))
                    return 1
                lib.libusb_get_device_list.side_effect = device_list
                lib.libusb_get_bus_number.return_value = 3
                lib.libusb_get_device_address.return_value = 14
                lib.libusb_open.side_effect = lambda device, handle: setattr(handle._obj, 'value', 2) or 0
                lib.libusb_kernel_driver_active.return_value = 1
                lib.libusb_error_name.return_value = b'fixture transfer failure'
                message = self.message(screen_bitmap()+b'\n')
                parts = iter([message[:64], message[64:]])
                requested = False
                def bulk(handle, endpoint, buffer, size, transferred, timeout):
                    nonlocal requested
                    if endpoint == 0x81:
                        if not requested:
                            return -7
                        if fail:
                            return -1
                        part = next(parts)
                        C.memmove(buffer, part, len(part))
                        transferred._obj.value = len(part)
                    else:
                        requested = True
                        transferred._obj.value = size
                    return 0
                lib.libusb_bulk_transfer.side_effect = bulk
                with patch('gate.usb_screen._library', return_value=lib):
                    if fail:
                        with self.assertRaises(OSError):
                            capture(interface, 3.)
                    else:
                        self.assertEqual(capture(interface, 3.), screen_bitmap()+b'\n')
                lib.libusb_release_interface.assert_called_once()
                lib.libusb_attach_kernel_driver.assert_called_once()
                lib.libusb_close.assert_called_once()
                lib.libusb_exit.assert_called_once()
