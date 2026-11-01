"""AOIP RS-232 text transactions, including firmware missing reply terminators."""
import fcntl
import math
import os
import select
import termios
import threading
import time
from urllib.parse import parse_qs, urlsplit


def connection(resource):
    url = urlsplit(resource)
    if url.scheme != 'aoip' or url.netloc or not url.path.startswith('/dev/') or url.fragment:
        raise ValueError('Use aoip:///dev/ttyUSB0?baud=9600')
    options = parse_qs(url.query, strict_parsing=True)
    if set(options) - {'baud', 'bits', 'parity', 'stop', 'flow', 'idle'} or any(len(v) != 1 for v in options.values()):
        raise ValueError('AOIP serial options: baud, bits, parity, stop, flow, idle')
    baud = int(options.get('baud', ['9600'])[0])
    bits = int(options.get('bits', ['8'])[0])
    parity = options.get('parity', ['none'])[0]
    stop = int(options.get('stop', ['1'])[0])
    flow = options.get('flow', ['none'])[0]
    idle = float(options.get('idle', ['0.2'])[0])
    if baud not in (300, 600, 1200, 2400, 4800, 9600, 19200):
        raise ValueError('AOIP baud must be 300–19200 in standard steps')
    if bits not in (7, 8) or parity not in ('none', 'even', 'odd') or stop not in (1, 2):
        raise ValueError('AOIP framing: 7/8 bits, none/even/odd parity, 1/2 stop bits')
    if flow not in ('none', 'xonxoff', 'rtscts'):
        raise ValueError('AOIP flow: none, xonxoff or rtscts')
    if not math.isfinite(idle) or not .05 <= idle <= 2:
        raise ValueError('AOIP idle must be 0.05–2 seconds')
    normalized = f'aoip://{url.path}?baud={baud}&bits={bits}&parity={parity}&stop={stop}&flow={flow}&idle={idle:g}'
    return normalized, url.path, baud, bits, parity, stop, flow, idle


class AOIPSerial:
    def __init__(self, resource, timeout=3.0):
        (self.resource, path, baud, bits, parity, stop, flow, self.idle) = connection(resource)
        if not math.isfinite(timeout) or timeout <= self.idle:
            raise ValueError('AOIP transaction timeout must exceed the idle interval')
        self.timeout = timeout
        self.lock = threading.Lock()
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            attrs = termios.tcgetattr(self.fd)
            attrs[0] = termios.INPCK if parity != 'none' else 0
            if flow == 'xonxoff':
                attrs[0] |= termios.IXON | termios.IXOFF
                attrs[6][termios.VSTART], attrs[6][termios.VSTOP] = b'\x11', b'\x13'
            attrs[1] = attrs[3] = 0
            attrs[2] = (termios.CS7 if bits == 7 else termios.CS8) | termios.CREAD | termios.CLOCAL
            if parity != 'none':
                attrs[2] |= termios.PARENB | (termios.PARODD if parity == 'odd' else 0)
            if stop == 2:
                attrs[2] |= termios.CSTOPB
            if flow == 'rtscts':
                attrs[2] |= termios.CRTSCTS
            attrs[4] = attrs[5] = getattr(termios, f'B{baud}')
            attrs[6][termios.VMIN] = attrs[6][termios.VTIME] = 0
            termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
            termios.tcflush(self.fd, termios.TCIFLUSH)
            # No clear, reset, REM or output commands during discovery.
        except Exception:
            self.close()
            raise

    def _transaction(self, command, reply):
        if not isinstance(command, str) or not command or any(c in command for c in '\r\n'):
            raise ValueError('One AOIP command message per transaction')
        payload = (command + '\n').encode('ascii')
        if len(payload) > 128:
            raise ValueError('AOIP command exceeds the 128-byte input buffer')
        with self.lock:
            if self.fd is None:
                raise ConnectionError('AOIP connection is closed')
            deadline = time.monotonic() + self.timeout
            try:
                while payload:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not select.select([], [self.fd], [], remaining)[1]:
                        raise TimeoutError('AOIP write timed out')
                    try:
                        count = os.write(self.fd, payload)
                    except BlockingIOError:
                        continue
                    if count <= 0:
                        raise ConnectionError('AOIP serial adapter disconnected')
                    payload = payload[count:]
                if not reply:
                    return None
                data = bytearray()
                last_byte = None
                while True:
                    now = time.monotonic()
                    if now >= deadline:
                        raise TimeoutError('AOIP response timed out before a complete quiet interval')
                    if last_byte is not None and now - last_byte >= self.idle:
                        # Wait for quiet even after CR/LF, consuming split CRLF.
                        # A delimiter alone is never a successful response.
                        response = bytes(data).decode('ascii').strip()
                        if response:
                            if '\n' in response or '\r' in response:
                                raise ValueError('Unexpected multiline AOIP response')
                            return response
                        data.clear()
                        last_byte = None
                    wait = min(deadline - now, self.idle - (now-last_byte)) if last_byte is not None else deadline-now
                    if not select.select([self.fd], [], [], max(0, wait))[0]:
                        continue
                    try:
                        block = os.read(self.fd, 4096)
                    except BlockingIOError:
                        continue
                    if not block:
                        raise ConnectionError('AOIP serial adapter disconnected')
                    data.extend(block)
                    last_byte = time.monotonic()
                    if len(data) > 65536:
                        raise ValueError('AOIP response exceeds text limit')
            except Exception:
                self.close()  # Never associate a late response with another query.
                raise

    def query(self, command):
        return self._transaction(command, True)

    def write(self, command):
        return self._transaction(command, False)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
