"""Linux serial transactions for the Fluke prompt protocol (standard library only)."""
import fcntl
import os
import re
import select
import termios
import threading
import time
from urllib.parse import parse_qs, urlsplit

PROMPT = re.compile(rb'(?:^|[\r\n])([=?!])>\r?\n')

class CommandError(ValueError):
    def __init__(self, command, prompt):
        self.prompt = prompt
        super().__init__(f"{command}: {'execution error' if prompt == '!' else 'command error'}")

class FlukeSerial:
    def __init__(self, resource, timeout=2.0):
        url = urlsplit(resource)
        if url.scheme != 'serial' or url.netloc or not url.path.startswith('/dev/'):
            raise ValueError('Use serial:///dev/ttyUSB0?baud=9600')
        options = parse_qs(url.query, strict_parsing=True)
        if set(options) - {'baud'} or any(len(value) != 1 for value in options.values()):
            raise ValueError('Serial options: baud only; framing is 8N1')
        baud = int(options.get('baud', ['9600'])[0])
        if baud not in (300, 600, 1200, 2400, 4800, 9600):
            raise ValueError('Supported baud rates: 300, 600, 1200, 2400, 4800, 9600')
        self.resource = f'serial://{url.path}?baud={baud}'
        self.timeout = timeout
        self.lock = threading.Lock()
        self.buffer = b''
        self._ready_at = 0.0
        self.fd = os.open(url.path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            attrs = termios.tcgetattr(self.fd)
            attrs[0] = attrs[1] = attrs[3] = 0
            attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
            attrs[4] = attrs[5] = getattr(termios, f'B{baud}')
            attrs[6][termios.VMIN] = attrs[6][termios.VTIME] = 0
            termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
            termios.tcflush(self.fd, termios.TCIFLUSH)
            # Fluke's documented serial device-clear discards unfinished input
            # and output, then emits a ready prompt. It does not reset functions,
            # ranges, triggers, echo or calibration settings.
            os.write(self.fd, b'\x03')
            # Discard pending replies and the clear prompt before identification.
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                if not select.select([self.fd], [], [], min(.15, deadline-time.monotonic()))[0]:
                    break
                if not os.read(self.fd, 4096):
                    raise ConnectionError('Serial adapter disconnected')
            self._ready_at = time.monotonic() + .05
        except Exception:
            self.close()
            raise

    def query(self, command):
        if '\n' in command or '\r' in command:
            raise ValueError('One serial command per transaction')
        with self.lock:
            if self.fd is None:
                raise ConnectionError('Serial connection is closed')
            delay = self._ready_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            deadline = time.monotonic() + self.timeout
            payload = (command if command == '\x03' else command + '\r').encode('ascii')
            try:
                while payload:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not select.select([], [self.fd], [], remaining)[1]:
                        raise TimeoutError('Fluke serial write timed out')
                    try:
                        count = os.write(self.fd, payload)
                    except BlockingIOError:
                        continue
                    if count == 0:
                        raise ConnectionError('Serial adapter disconnected')
                    payload = payload[count:]
                while True:
                    match = PROMPT.search(self.buffer)
                    if match:
                        frame = self.buffer[:match.start()]
                        self.buffer = self.buffer[match.end():]
                        prompt = match[1].decode('ascii')
                        self._ready_at = time.monotonic() + .05
                        if prompt != '=':
                            raise CommandError(command, prompt)
                        lines = [line.strip() for line in frame.decode('ascii').splitlines()]
                        return '\n'.join(line for line in lines if line and line.upper() != command.upper())
                    if len(self.buffer) > 65536:
                        raise ValueError('Fluke serial response exceeds text limit')
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not select.select([self.fd], [], [], remaining)[0]:
                        raise TimeoutError('Fluke serial response timed out; check baud, parity and cable')
                    try:
                        block = os.read(self.fd, 4096)
                    except BlockingIOError:
                        continue
                    if not block:
                        raise ConnectionError('Serial adapter disconnected')
                    self.buffer += block
            except CommandError:
                # The prompt consumed the entire failed transaction; still synchronized.
                raise
            except Exception:
                self.close()
                raise

    def write(self, command):
        self.query(command)  # Fluke writes still produce a completion prompt.
        return None

    def close(self):
        if self.fd is not None:
            # Keep host echo disabled: restoring a terminal's default ECHO here
            # can start an echo loop with a meter whose command echo is enabled.
            os.close(self.fd)
            self.fd = None
