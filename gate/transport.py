"""Bounded, serialized text SCPI transports. No resets or configuration on connect."""
import os
import fcntl
import struct
import select
import socket
import threading
import time

class SCPI:
    def __init__(self, resource, timeout=1.0):
        self.resource, self.timeout = resource, timeout
        self.lock = threading.Lock()
        self.buffer = b""
        self.sock = None
        self.fd = None
        if resource.startswith("tcp://"):
            from urllib.parse import urlsplit
            url = urlsplit(resource)
            self.sock = socket.create_connection((url.hostname, url.port or 5025), timeout)
            self.sock.setblocking(False)
            self.fd = self.sock.fileno()
        elif resource.startswith("/dev/usbtmc"):
            self.fd = os.open(resource, os.O_RDWR)
            try:
                self._usb_timeout(timeout)
            except Exception:
                self.close()
                raise
        else:
            raise ValueError("Use /dev/usbtmcN or tcp://host:5025")

    def _usb_timeout(self, remaining):
        # Linux USBTMC_IOCTL_SET_TIMEOUT: _IOW(91, 10, __u32).
        fcntl.ioctl(self.fd, 0x40045B0A, struct.pack("I", max(100, int(remaining * 1000))))

    def _usb_query(self, command, raw=False, maximum=65536):
        # read() initiates DEV_DEP_MSG_IN; poll() cannot precede that request.
        # SDS1202X-E reports 64-byte endpoints at high speed. Keep header +
        # padded payload below 64 bytes to avoid truncated packets in usbtmc.
        deadline = time.monotonic() + self.timeout
        payload = (command + "\n").encode("ascii")
        if os.write(self.fd, payload) != len(payload):
            raise ConnectionError("Incomplete USBTMC command write")
        response = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("USBTMC response timed out")
            self._usb_timeout(remaining)
            block = os.read(self.fd, 48)
            if not block:
                raise ConnectionError("Empty USBTMC transfer; reconnect the instrument")
            response.extend(block)
            if len(response) > maximum:
                raise ValueError("SCPI response exceeds transfer limit")
            # Linux USBTMC_IOCTL_MSG_IN_ATTR: _IOR(91, 24, __u8).
            attributes = bytearray(1)
            fcntl.ioctl(self.fd, 0x80015B18, attributes, True)
            if attributes[0] & 1:  # USBTMC EOM, independent of newline/short read.
                return bytes(response) if raw else response.decode("ascii").strip()

    def query(self, command):
        if "\n" in command or "\r" in command:
            raise ValueError("One SCPI command per transaction")
        with self.lock:
            if self.fd is None:
                raise ConnectionError("Instrument connection is closed")
            if self.sock is None:
                try:
                    self._usb_timeout(self.timeout)
                    return self._usb_query(command)
                except Exception:
                    self.close()
                    raise
            deadline = time.monotonic() + self.timeout
            payload = (command + "\n").encode("ascii")
            try:
                while payload:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not select.select([], [self.fd], [], remaining)[1]:
                        raise TimeoutError("SCPI write timed out")
                    try:
                        count = os.write(self.fd, payload)
                    except BlockingIOError:
                        continue
                    if count == 0:
                        raise ConnectionError("Instrument disconnected")
                    payload = payload[count:]
                while b"\n" not in self.buffer:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not select.select([self.fd], [], [], remaining)[0]:
                        raise TimeoutError("SCPI response timed out")
                    try:
                        block = os.read(self.fd, 4096)
                    except BlockingIOError:
                        continue
                    if not block:
                        raise ConnectionError("Instrument disconnected")
                    self.buffer += block
                    if len(self.buffer) > 65536:
                        raise ValueError("SCPI response exceeds text limit")
                line, self.buffer = self.buffer.split(b"\n", 1)
                return line.decode("ascii").strip()
            except Exception:
                self.close()  # Never reuse a timed-out session with a delayed response.
                raise

    def query_binary(self, command, maximum=16384, terminator=b'\n\n', framing='ieee'):
        """Read an IEEE definite-length block; binary payload newlines are data."""
        if not isinstance(command, str) or any(char in command for char in '\r\n') or not 1 <= maximum <= 1048576:
            raise ValueError('Use one bounded binary query')
        def extent(data):
            if framing == 'bmp':
                if len(data) < 6:
                    return None
                length = int.from_bytes(data[2:6], 'little')
                if data[:2] != b'BM' or not 54 <= length <= maximum:
                    raise ValueError('Invalid or oversized scope screenshot')
                return 0, length
            marker = data.find(b'#')
            if marker < 0:
                if len(data) > 64:
                    raise ValueError('Missing binary block header')
                return None
            if marker > 64:
                raise ValueError('Binary block prefix is too long')
            if len(data) < marker+2:
                return None
            if data[marker+1:marker+2] not in b'123456789':
                raise ValueError('Indefinite or malformed binary blocks are unsupported')
            digits = int(data[marker+1:marker+2])
            start = marker+2+digits
            if len(data) < start:
                return None
            length_text = data[marker+2:start]
            if not length_text.isdigit():
                raise ValueError('Malformed binary block length')
            length = int(length_text)
            if not 0 < length <= maximum:
                raise ValueError('Binary block exceeds the requested bound')
            return start, start+length
        if framing not in ('ieee', 'bmp'):
            raise ValueError('Unsupported binary framing')
        with self.lock:
            if self.fd is None:
                raise ConnectionError('Instrument connection is closed')
            try:
                if self.sock is None:
                    raw = self._usb_query(command, raw=True, maximum=maximum+128)
                    bounds = extent(raw)
                    if not bounds or len(raw) < bounds[1] or raw[bounds[1]:].strip(b'\r\n'):
                        raise ValueError('Truncated or malformed USB binary response')
                    return raw[bounds[0]:bounds[1]]
                deadline = time.monotonic()+self.timeout
                payload = (command+'\n').encode('ascii')
                while payload:
                    remaining = deadline-time.monotonic()
                    if remaining <= 0 or not select.select([], [self.fd], [], remaining)[1]:
                        raise TimeoutError('Binary query write timed out')
                    try:
                        count = os.write(self.fd, payload)
                    except BlockingIOError:
                        continue
                    if count <= 0:
                        raise ConnectionError('Instrument disconnected')
                    payload = payload[count:]
                while True:
                    bounds = extent(self.buffer)
                    if bounds and len(self.buffer) >= bounds[1]+len(terminator):
                        if self.buffer[bounds[1]:bounds[1]+len(terminator)] != terminator:
                            raise ValueError('Malformed binary response terminator')
                        result = self.buffer[bounds[0]:bounds[1]]
                        self.buffer = self.buffer[bounds[1]+len(terminator):]
                        return result
                    remaining = deadline-time.monotonic()
                    if remaining <= 0 or not select.select([self.fd], [], [], remaining)[0]:
                        raise TimeoutError('Binary response timed out')
                    try:
                        block = os.read(self.fd, 4096)
                    except BlockingIOError:
                        continue
                    if not block:
                        raise ConnectionError('Instrument disconnected during binary response')
                    self.buffer += block
                    if len(self.buffer) > maximum+128:
                        raise ValueError('Binary response exceeds transfer limit')
            except Exception:
                self.close()
                raise

    def write(self, command):
        if "\n" in command or "\r" in command:
            raise ValueError("One SCPI command per transaction")
        with self.lock:
            if self.fd is None:
                raise ConnectionError("Instrument connection is closed")
            payload = (command + "\n").encode("ascii")
            deadline = time.monotonic() + self.timeout
            try:
                if self.sock is None:
                    self._usb_timeout(self.timeout)
                while payload:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("SCPI write timed out")
                    if self.sock is not None and not select.select([], [self.fd], [], remaining)[1]:
                        raise TimeoutError("SCPI write timed out")
                    try:
                        count = os.write(self.fd, payload)
                    except BlockingIOError:
                        continue
                    if count == 0:
                        raise ConnectionError("Instrument disconnected")
                    payload = payload[count:]
            except Exception:
                self.close()
                raise

    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None
            self.fd = None
        elif self.fd is not None:
            os.close(self.fd)
            self.fd = None
