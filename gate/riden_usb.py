"""Bounded Modbus RTU transport for explicitly selected Riden USB serial ports."""
import fcntl
import os
import select
import struct
import termios
import threading
import time
from urllib.parse import parse_qs, urlsplit


def connection(resource):
    url = urlsplit(resource)
    if url.scheme != 'riden' or url.netloc or not url.path.startswith('/dev/') or url.fragment:
        raise ValueError('Use riden:///dev/ttyUSB1?baud=115200&address=1')
    options = parse_qs(url.query, strict_parsing=True, keep_blank_values=True)
    if set(options) - {'baud', 'address'} or any(len(v) != 1 for v in options.values()):
        raise ValueError('Riden options: baud and address only')
    baud = int(options.get('baud', ['115200'])[0])
    address = int(options.get('address', ['1'])[0])
    if baud not in (9600, 19200, 38400, 57600, 115200) or not 1 <= address <= 247:
        raise ValueError('Riden requires baud 9600/19200/38400/57600/115200 and address 1–247')
    return url.path, baud, address


def crc16(data):
    crc = 0xffff
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0xa001 if crc & 1 else 0)
    return struct.pack('<H', crc)


class FrameError(ValueError):
    """A garbled, truncated or misaligned reply: safe to retry an idempotent request."""


class RidenUSB:
    # Function 3 reads and function 6 absolute-value writes are idempotent, so a
    # lost or corrupted reply is retried instead of failing the whole poll.
    def __init__(self, resource, timeout=.5, attempts=3, quiet=.1):
        path, baud, self.address = connection(resource)
        self.resource = f'riden://{path}?baud={baud}&address={self.address}'
        self.timeout = timeout
        self.attempts = attempts
        self.quiet = quiet
        self.retries = 0
        self.lock = threading.Lock()
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        self.ready_at = 0.
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            attrs = termios.tcgetattr(self.fd)
            attrs[0] = attrs[1] = attrs[3] = 0
            attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
            attrs[4] = attrs[5] = getattr(termios, f'B{baud}')
            attrs[6][termios.VMIN] = attrs[6][termios.VTIME] = 0
            termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        except Exception:
            self.close()
            raise

    def _transaction(self, function, register, value):
        with self.lock:
            for attempt in range(self.attempts):
                if self.fd is None:
                    raise ConnectionError('Riden connection is closed')
                try:
                    return self._attempt(function, register, value)
                except (TimeoutError, FrameError):
                    if attempt+1 == self.attempts:
                        raise
                    self.retries += 1
                    self._drain()

    def _drain(self):
        """Discard late/partial replies until the line stays quiet (bounded)."""
        deadline = time.monotonic()+self.timeout
        while time.monotonic() < deadline:
            if not select.select([self.fd], [], [], self.quiet)[0]:
                break
            try:
                if not os.read(self.fd, 256):
                    raise ConnectionError('Riden USB disconnected')
            except BlockingIOError:
                pass
        self.ready_at = time.monotonic()

    def _attempt(self, function, register, value):
        time.sleep(max(0., self.ready_at-time.monotonic()))
        termios.tcflush(self.fd, termios.TCIFLUSH)
        request = struct.pack('>BBHH', self.address, function, register, value)
        pending = request + crc16(request)
        deadline = time.monotonic()+self.timeout
        def transfer_ready(read=False):
            remaining = deadline-time.monotonic()
            if remaining <= 0 or not select.select([self.fd] if read else [], [] if read else [self.fd], [], max(0., remaining))[0 if read else 1]:
                raise TimeoutError('Riden Modbus transaction timed out')
        try:
            while pending:
                transfer_ready()
                try:
                    count = os.write(self.fd, pending)
                except BlockingIOError:
                    continue
                if not count:
                    raise ConnectionError('Riden USB disconnected')
                pending = pending[count:]
            reply = b''
            expected = 3
            while len(reply) < expected:
                transfer_ready(True)
                try:
                    block = os.read(self.fd, expected-len(reply))
                except BlockingIOError:
                    continue
                if not block:
                    raise ConnectionError('Riden USB disconnected')
                reply += block
                if len(reply) >= 3:
                    expected = 5 if reply[1] & 0x80 else 8 if function == 6 else 5+reply[2]
                    if expected > 255:
                        raise FrameError('Invalid Modbus frame size')
            if crc16(reply[:-2]) != reply[-2:]:
                raise FrameError('Riden Modbus CRC mismatch')
            if reply[0] != self.address or reply[1] not in (function, function | 0x80):
                raise FrameError('Unexpected Modbus address/function')
            if reply[1] & 0x80:
                raise ValueError(f'Riden Modbus exception {reply[2]}')
            if function == 6:
                if reply[:-2] != request:
                    raise ValueError('Riden write echo mismatch')
                return
            if reply[2] != value*2:
                raise FrameError('Unexpected Modbus register count')
            return list(struct.unpack('>'+str(value)+'H', reply[3:-2]))
        finally:
            # Allow late replies to finish before flushing the next request.
            self.ready_at = time.monotonic()+.05

    def read_registers(self, register, count):
        if not 0 <= register <= 65535 or not 1 <= count <= 125 or register+count > 65536:
            raise ValueError('Invalid register read')
        return self._transaction(3, register, count)

    def write_register(self, register, value):
        if not 0 <= register <= 65535 or not 0 <= value <= 65535:
            raise ValueError('Invalid register write')
        self._transaction(6, register, value)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
