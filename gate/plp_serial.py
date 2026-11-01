"""New PL-P USB virtual COM / RS-232: fixed 9600 8N1, LF/CRLF framing."""
import fcntl
import os
import select
import termios
import threading
import time
from urllib.parse import parse_qs, urlsplit


def connection(resource):
    url = urlsplit(resource)
    options = parse_qs(url.query, strict_parsing=True, keep_blank_values=True)
    if (url.scheme != 'plp' or url.netloc or not url.path.startswith('/dev/') or url.fragment
            or set(options) - {'baud'} or options.get('baud', ['9600']) != ['9600']):
        raise ValueError('Use plp:///dev/ttyACM0?baud=9600 (fixed 9600 baud, 8N1)')
    return f'plp://{url.path}?baud=9600', url.path


class PLPSerial:
    def __init__(self, resource, timeout=1.0):
        self.resource, path = connection(resource)
        self.timeout = timeout
        self.lock = threading.Lock()
        self.buffer = b''
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            attrs = termios.tcgetattr(self.fd)
            attrs[0] = termios.IXON | termios.IXOFF
            attrs[1] = attrs[3] = 0
            attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
            attrs[4] = attrs[5] = termios.B9600
            attrs[6][termios.VMIN] = attrs[6][termios.VTIME] = 0
            attrs[6][termios.VSTART], attrs[6][termios.VSTOP] = b'\x11', b'\x13'
            termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
            termios.tcflush(self.fd, termios.TCIFLUSH)
            # No reset, clear, break or output commands during connection.
        except Exception:
            self.close()
            raise

    def _transaction(self, command, reply):
        if (not isinstance(command, str) or not command or len(command) > 128
                or any(c in command for c in '\r\n;')):
            raise ValueError('One bounded PL-P command per transaction')
        payload = (command + '\n').encode('ascii')
        with self.lock:
            if self.fd is None:
                raise ConnectionError('PL-P connection is closed')
            deadline = time.monotonic() + self.timeout
            try:
                while payload:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not select.select([], [self.fd], [], remaining)[1]:
                        raise TimeoutError('PL-P write timed out')
                    try:
                        count = os.write(self.fd, payload)
                    except BlockingIOError:
                        continue
                    if count <= 0:
                        raise ConnectionError('PL-P disconnected')
                    payload = payload[count:]
                if not reply:
                    return None
                while b'\n' not in self.buffer:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not select.select([self.fd], [], [], remaining)[0]:
                        raise TimeoutError('PL-P response timed out')
                    try:
                        block = os.read(self.fd, 4096)
                    except BlockingIOError:
                        continue
                    if not block:
                        raise ConnectionError('PL-P disconnected')
                    self.buffer += block
                    if len(self.buffer) > 4096:
                        raise ValueError('PL-P response exceeds text limit')
                line, self.buffer = self.buffer.split(b'\n', 1)
                return line.decode('ascii').strip()
            except Exception:
                self.close()  # A late response must never confirm a later command.
                raise

    def query(self, command):
        return self._transaction(command, True)

    def write(self, command):
        return self._transaction(command, False)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
