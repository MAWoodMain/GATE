"""Authenticated loopback HTTP API; all instrument I/O stays on the engine thread."""
import hmac
import json
import os
import secrets
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit
from gate import __version__

ACTIONS = {'scan', 'connect', 'edit_device', 'instrument_settings', 'copy_settings', 'forget', 'disconnect', 'settings', 'demo', 'record', 'stop_recording', 'select_function',
           'static', 'sweep', 'output', 'cancel_sweep', 'query', 'write'}

def runtime_directory():
    return Path(os.environ.get('XDG_RUNTIME_DIR', f'/tmp/gate-{os.getuid()}')) / 'gate'

class ControlServer:
    def __init__(self, engine, port=8765, allow_raw=False, directory=None):
        self.engine = engine
        self.allow_raw = allow_raw
        self.token = secrets.token_urlsafe(32)
        self.directory = Path(directory) if directory else runtime_directory()
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        owner = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def setup(self):
                super().setup()
                self.connection.settimeout(5)
            def log_message(self, *args):
                pass  # Never put credentials or device data into access logs.
            def reply(self, status, payload):
                data = json.dumps(payload, allow_nan=False).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(data)
                self.close_connection = True
            def authorized(self):
                expected_hosts = {f'127.0.0.1:{owner.port}', f'localhost:{owner.port}'}
                if self.headers.get('Host') not in expected_hosts or self.headers.get('Origin'):
                    self.reply(403, {'error': 'Use a local non-browser client'})
                    return False
                supplied = self.headers.get('Authorization', '')
                if not hmac.compare_digest(supplied.encode(), ('Bearer '+owner.token).encode()):
                    self.reply(401, {'error': 'Bearer authentication required'})
                    return False
                return True
            def do_GET(self):
                if not self.authorized():
                    return
                parts = urlsplit(self.path).path.strip('/').split('/')
                state = owner.engine.snapshot()
                if parts == ['v1', 'health']:
                    self.reply(200, {'api_version': 1, 'version': __version__, 'service': 'GATE', 'allow_raw': owner.allow_raw,
                                     'actions': sorted(ACTIONS if owner.allow_raw else ACTIONS-{'query','write'})})
                elif parts == ['v1', 'state']:
                    self.reply(200, state)
                elif parts == ['v1', 'instruments']:
                    self.reply(200, {'instruments': state['instruments'], 'updated_utc_ns': state.get('updated_utc_ns')})
                elif len(parts) == 3 and parts[:2] == ['v1', 'instruments']:
                    identity = unquote(parts[2])
                    key = state.get('connections', {}).get(identity, {}).get('instrument') or identity
                    device = next((d for d in state['instruments'] if d['id'] == key), None)
                    self.reply(200 if device else 404, device or {'error': 'Instrument not found'})
                elif len(parts) == 3 and parts[:2] == ['v1', 'commands']:
                    result = state.get('commands', {}).get(parts[2])
                    self.reply(200 if result else 404, result or {'error': 'Command not found or expired'})
                else:
                    self.reply(404, {'error': 'Unknown endpoint'})
            def do_POST(self):
                if not self.authorized():
                    return
                if urlsplit(self.path).path != '/v1/commands':
                    self.reply(404, {'error': 'Unknown endpoint'})
                    return
                try:
                    if self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
                        self.reply(415, {'error': 'Use application/json'})
                        return
                    if self.headers.get('Transfer-Encoding'):
                        self.reply(400, {'error': 'Chunked requests are not supported'})
                        return
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= 16384:
                        self.reply(413, {'error': 'Body must be 1–16384 bytes'})
                        return
                    body = self.rfile.read(length)
                    if len(body) != length:
                        raise ValueError('Incomplete request body')
                    command = json.loads(body)
                    if not isinstance(command, dict) or command.get('action') not in ACTIONS:
                        raise ValueError('Unknown action')
                    action = command.pop('action')
                    if action in ('query', 'write') and not owner.allow_raw:
                        self.reply(403, {'error': 'Raw commands are disabled; start service with --allow-raw to enable'})
                        return
                    command.pop('request', None)
                    request = str(uuid.uuid4())
                    owner.engine.submit(action, request=request, **command)
                    self.reply(202, {'id': request, 'state': 'queued', 'status_url': '/v1/commands/'+request})
                except (ValueError, TypeError) as exc:
                    self.reply(400, {'error': str(exc)})
                except Exception as exc:
                    self.reply(503, {'error': str(exc)})
        self.server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': .1}, daemon=True)
        self.path = self.directory / 'control.json'
        self.manifest = {'api_version': 1, 'url': f'http://127.0.0.1:{self.port}',
                         'token': self.token, 'pid': os.getpid(), 'allow_raw': allow_raw}
        temporary = self.directory / f'control-{uuid.uuid4()}.tmp'
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump(self.manifest, stream)
            os.replace(temporary, self.path)
        except Exception:
            temporary.unlink(missing_ok=True)
            self.server.server_close()
            raise
    def start(self):
        self.thread.start()
    def close(self):
        if self.thread.is_alive():
            self.server.shutdown()
            self.thread.join(timeout=2)
        self.server.server_close()
        try:
            # Do not remove another instance's newer discovery manifest.
            if json.loads(self.path.read_text()).get('token') == self.token:
                self.path.unlink()
        except (OSError, ValueError):
            pass
