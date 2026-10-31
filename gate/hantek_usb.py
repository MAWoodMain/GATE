"""Bounded Hantek 365 vendor USB transport using Linux usbdevfs."""
import ctypes
import errno
import fcntl
import os
import struct
import time
from pathlib import Path
from gate.connections import validate_resource

USB_ROOT = Path('/sys/bus/usb/devices')


def device_info(resource):
    validate_resource(resource)
    port = resource.removeprefix('hantek://usb/')
    root = USB_ROOT/port
    def value(name):
        return (root/name).read_text().strip()
    if (value('idVendor'), value('idProduct'), value('manufacturer'), value('product')) != ('0483', '5722', 'Hantek', 'DMM USB'):
        raise ValueError('USB port is not a matching Hantek 365 meter')
    interface = USB_ROOT/(port+':1.0')
    if int((interface/'bInterfaceClass').read_text(), 16) != 255:
        raise ValueError('Unsupported Hantek USB interface')
    endpoints = {int((path/'bEndpointAddress').read_text(), 16):int((path/'bmAttributes').read_text(), 16)
                 for path in interface.glob('ep_*')}
    if endpoints != {0x81:2, 0x02:2}:
        raise ValueError('Unsupported Hantek USB endpoints')
    return dict(port=port, node=f"/dev/bus/usb/{int(value('busnum')):03d}/{int(value('devnum')):03d}",
                usb_serial=value('serial') if (root/'serial').exists() else '')


def resources():
    result = []
    for path in USB_ROOT.glob('*/idVendor'):
        resource = 'hantek://usb/'+path.parent.name
        try:
            device_info(resource)
            result.append(resource)
        except (OSError, ValueError):
            pass
    return sorted(result)


class BulkTransfer(ctypes.Structure):
    _fields_ = [('endpoint', ctypes.c_uint), ('length', ctypes.c_uint),
                ('timeout_ms', ctypes.c_uint), ('data', ctypes.c_void_p)]


class HantekUSB:
    def __init__(self, resource, timeout=1.):
        self.resource, self.timeout = resource, timeout
        self.info = device_info(resource)
        self.fd = None
        self.buffer = bytearray()
        try:
            self.fd = os.open(self.info['node'], os.O_RDWR | os.O_CLOEXEC)
            fcntl.ioctl(self.fd, 0x8004550F, struct.pack('I', 0))  # CLAIMINTERFACE
        except PermissionError as exc:
            self.close()
            raise PermissionError('Hantek USB access denied. Install data/70-gate-hantek.rules, reload udev rules and reconnect the meter.') from exc
        except Exception:
            self.close()
            raise

    def transfer(self, payload=None, timeout=None):
        if self.fd is None:
            raise ConnectionError('Hantek is disconnected')
        size = 64 if payload is None else len(payload)
        buffer = ctypes.create_string_buffer(size if payload is None else payload)
        argument = BulkTransfer(0x81 if payload is None else 0x02, size,
                                max(1, int((self.timeout if timeout is None else timeout)*1000)), ctypes.addressof(buffer))
        raw = bytearray(ctypes.string_at(ctypes.byref(argument), ctypes.sizeof(argument)))
        count = fcntl.ioctl(self.fd, 0xC0005502 | ctypes.sizeof(argument) << 16, raw, True)
        if not 0 <= count <= size or payload is not None and count != size:
            raise ConnectionError('Incomplete Hantek USB transfer')
        return buffer.raw[:count]

    def token(self, deadline):
        while time.monotonic() < deadline:
            if self.buffer:
                marker = self.buffer[0]
                size = 15 if marker == 0xA0 else 1
                if marker not in (0xA0, 0xAA, 0xDD):
                    raise ValueError('Unexpected Hantek USB response')
                if len(self.buffer) >= size:
                    result = bytes(self.buffer[:size])
                    del self.buffer[:size]
                    return result
            try:
                self.buffer.extend(self.transfer(timeout=max(.001, deadline-time.monotonic())))
            except OSError as exc:
                if exc.errno not in (errno.ETIMEDOUT, errno.EINTR):
                    raise
            if len(self.buffer) > 256:
                raise ValueError('Hantek response buffer exceeds its limit')
        raise TimeoutError('Hantek response timed out')

    def request(self):
        deadline = time.monotonic()+self.timeout
        self.transfer(b'\x01\x0f')
        while True:
            token = self.token(deadline)
            if token == b'\xaa':
                return None
            if token[0] == 0xA0:
                return token

    def set_mode(self, mode):
        deadline = time.monotonic()+self.timeout
        self.transfer(bytes([3, mode]))
        while self.token(deadline) != b'\xdd':
            pass

    def query(self, command):
        raise ValueError('Hantek uses a binary protocol. Select its measurement function in Instrument Settings.')

    write = query

    def close(self):
        if self.fd is not None:
            try:
                fcntl.ioctl(self.fd, 0x80045510, struct.pack('I', 0))  # RELEASEINTERFACE
            except OSError:
                pass
            finally:
                os.close(self.fd)
                self.fd = None
