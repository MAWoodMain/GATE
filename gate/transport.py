"""Bounded, serialized text SCPI transports. No resets or configuration on connect."""
import os
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
            self.fd = os.open(resource, os.O_RDWR | os.O_NONBLOCK)
        else:
            raise ValueError("Use /dev/usbtmcN or tcp://host:5025")

    def query(self, command):
        if "\n" in command or "\r" in command:
            raise ValueError("One SCPI command per transaction")
        with self.lock:
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
                    # USBTMC transfer termination can end a response without LF.
                    if self.sock is None and len(block) < 4096 and b"\n" not in self.buffer:
                        self.buffer += b"\n"
                    if len(self.buffer) > 65536:
                        raise ValueError("SCPI response exceeds text limit")
                line, self.buffer = self.buffer.split(b"\n", 1)
                return line.decode("ascii").strip()
            except Exception:
                self.close()  # Never reuse a timed-out session with a delayed response.
                raise

    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None
            self.fd = None
        elif self.fd is not None:
            os.close(self.fd)
            self.fd = None
