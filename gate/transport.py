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

    def _usb_query(self, command):
        # read() initiates DEV_DEP_MSG_IN; poll() cannot precede that request.
        # SDS1202X-E reports 64-byte endpoints at high speed. Keep header +
        # padded payload below 64 bytes to avoid truncated packets in usbtmc.
        deadline = time.monotonic() + self.timeout
        payload = (command + "\n").encode("ascii")
        if os.write(self.fd, payload) != len(payload):
            raise ConnectionError("Incomplete USBTMC command write")
        response = b""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("USBTMC response timed out")
            self._usb_timeout(remaining)
            block = os.read(self.fd, 48)
            if not block:
                raise ConnectionError("Empty USBTMC transfer; reconnect the instrument")
            response += block
            if len(response) > 65536:
                raise ValueError("SCPI response exceeds text limit")
            # Linux USBTMC_IOCTL_MSG_IN_ATTR: _IOR(91, 24, __u8).
            attributes = bytearray(1)
            fcntl.ioctl(self.fd, 0x80015B18, attributes, True)
            if attributes[0] & 1:  # USBTMC EOM, independent of newline/short read.
                return response.decode("ascii").strip()

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
