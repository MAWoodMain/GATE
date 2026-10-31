"""Packet-aware screen dumps for the SDS1202X-E's undersized USB packets.

The ordinary Linux USBTMC read path truncates large replies on this device.
Temporarily claim the same physical interface with libusb; always restore its
kernel driver. This does not reset the USB device or program acquisition.
"""
import ctypes as C
import ctypes.util
import struct
import time
from pathlib import Path


def receive_screen(write, read, maximum=1048576, deadline=None):
    deadline = time.monotonic()+3. if deadline is None else deadline
    command = b'SCDP\n'
    write(struct.pack('<BBBBIBBBB', 1, 1, 254, 0, len(command), 1, 0, 0, 0)+command+b'\0'*((-len(command)) % 4))
    write(struct.pack('<BBBBIBBBB', 2, 2, 253, 0, maximum, 0, 0, 0, 0))
    data = bytearray()
    while True:
        block = read()
        if not block:
            # The scope can return ZLPs while preparing its screen image.
            if time.monotonic() >= deadline:
                raise TimeoutError('Scope returned no complete USB screen before the deadline')
            time.sleep(.001)
            continue
        data.extend(block)
        if len(data) > maximum+16:
            raise ValueError('Scope USB screen exceeds transfer limit')
        if len(data) < 12:
            continue
        if data[:4] != b'\x02\x02\xfd\x00':
            raise ValueError('Invalid scope USB screen message/tag')
        length = struct.unpack_from('<I', data, 4)[0]
        if not 1 <= length <= maximum or not data[8] & 1:
            raise ValueError('Scope USB screen is incomplete or oversized')
        if len(data) >= 12+length:
            return bytes(data[12:12+length])


def _library():
    location = ctypes.util.find_library('usb-1.0')
    if not location:
        raise RuntimeError('USB scope screenshots require libusb-1.0-0')
    lib = C.CDLL(location)
    pointer, integer = C.c_void_p, C.c_int
    signatures = [
        ('init', [C.POINTER(pointer)], integer), ('exit', [pointer], None),
        ('get_device_list', [pointer, C.POINTER(C.POINTER(pointer))], C.c_ssize_t),
        ('free_device_list', [C.POINTER(pointer), integer], None),
        ('get_bus_number', [pointer], C.c_uint8), ('get_device_address', [pointer], C.c_uint8),
        ('open', [pointer, C.POINTER(pointer)], integer), ('close', [pointer], None),
        ('kernel_driver_active', [pointer, integer], integer),
        ('detach_kernel_driver', [pointer, integer], integer),
        ('attach_kernel_driver', [pointer, integer], integer),
        ('claim_interface', [pointer, integer], integer),
        ('release_interface', [pointer, integer], integer),
        ('bulk_transfer', [pointer, C.c_ubyte, pointer, integer, C.POINTER(integer), C.c_uint], integer),
        ('error_name', [integer], C.c_char_p),
    ]
    for name, args, result in signatures:
        function = getattr(lib, 'libusb_'+name)
        function.argtypes, function.restype = args, result
    return lib


def capture(interface_path, timeout):
    """Capture the exact interface identified before releasing its USBTMC fd."""
    interface_path = Path(interface_path)
    parent = interface_path.parent
    bus, address = (int((parent/name).read_text()) for name in ('busnum', 'devnum'))
    interface = int((interface_path/'bInterfaceNumber').read_text(), 16)
    endpoints = {}
    for path in interface_path.glob('ep_*'):
        if (path/'type').read_text().strip() == 'Bulk':
            endpoints[(path/'direction').read_text().strip()] = int((path/'bEndpointAddress').read_text(), 16)
    if set(endpoints) != {'in', 'out'}:
        raise ValueError('Scope USB interface requires one bulk endpoint in each direction')
    lib = _library()
    context, handle = C.c_void_p(), C.c_void_p()
    devices = C.POINTER(C.c_void_p)()
    detached = claimed = False
    deadline = time.monotonic()+timeout
    def check(code):
        if code < 0:
            name = lib.libusb_error_name(code).decode('ascii', errors='replace')
            if code == -7:
                raise TimeoutError('Scope USB screenshot timed out')
            raise OSError(f'Scope USB screenshot: {name}')
        return code
    cleanup_errors = []
    try:
        check(lib.libusb_init(C.byref(context)))
        count = check(lib.libusb_get_device_list(context, C.byref(devices)))
        device = next((devices[index] for index in range(count)
            if lib.libusb_get_bus_number(devices[index]) == bus and lib.libusb_get_device_address(devices[index]) == address), None)
        if device is None:
            raise ConnectionError('Scope disconnected before screenshot transfer')
        check(lib.libusb_open(device, C.byref(handle)))
        if check(lib.libusb_kernel_driver_active(handle, interface)):
            check(lib.libusb_detach_kernel_driver(handle, interface))
            detached = True
        check(lib.libusb_claim_interface(handle, interface))
        claimed = True
        def transfer(payload=None, timeout_limit=None):
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Scope USB screenshot timed out')
            if timeout_limit is not None:
                remaining = min(remaining, timeout_limit)
            size = 16384 if payload is None else len(payload)
            buffer = C.create_string_buffer(size if payload is None else payload)
            transferred = C.c_int()
            check(lib.libusb_bulk_transfer(handle, endpoints['in' if payload is None else 'out'], buffer, size,
                C.byref(transferred), max(1, int(remaining*1000))))
            if payload is not None and transferred.value != size:
                raise ConnectionError('Incomplete scope USB screenshot request')
            return buffer.raw[:transferred.value]
        # Drain the previous kernel-managed transaction's trailer before sending
        # a fresh request. Some SDS firmware leaves short packets/ZLPs queued.
        drain_until = min(deadline, time.monotonic()+1.)
        drained = 0
        while time.monotonic() < drain_until:
            try:
                drained += len(transfer(timeout_limit=.05))
            except TimeoutError:
                break
            if drained > 1048592:
                raise ConnectionError('Scope has an oversized pending USB response')
        return receive_screen(transfer, lambda: transfer(), deadline=deadline)
    finally:
        for enabled, operation in ((claimed, 'release_interface'), (detached, 'attach_kernel_driver')):
            if enabled:
                code = getattr(lib, 'libusb_'+operation)(handle, interface)
                if code < 0:
                    cleanup_errors.append(operation+': '+lib.libusb_error_name(code).decode('ascii'))
        if handle:
            lib.libusb_close(handle)
        if devices:
            lib.libusb_free_device_list(devices, 1)
        if context:
            lib.libusb_exit(context)
        if cleanup_errors:
            raise RuntimeError('Restoring scope USB interface failed: '+'; '.join(cleanup_errors))
