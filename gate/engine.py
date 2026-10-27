"""One acquisition owner: commands and instrument reads never race."""
import copy
import json
import os
import math
import queue
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from gate import discovery
from gate.connections import validate_resource
from gate.identity import identify, migrate
from gate.drivers.simulated import Simulated
from gate.model import Reading, MeasurementFunction
from gate.storage import Recorder

class Engine:
    def __init__(self, interval=None, profile_path=None):
        self.profile_path = Path(profile_path) if profile_path else None
        profile = {}
        if self.profile_path and self.profile_path.exists():
            profile = json.loads(self.profile_path.read_text())
        profile = migrate(profile)
        self.saved_resources = set(profile.get("resources", [])) | set(profile['connections'])
        self.saved_devices = profile.get("devices", {})
        self.instrument_profiles = profile['instrument_profiles']
        self.connections = profile['connections']
        self.active_resources = {}
        if interval is None:
            interval = float(profile.get("interval", 1.0))
        if not math.isfinite(interval) or interval < 0.1:
            raise ValueError("Interval must be at least 0.1 s")
        self.interval = interval
        self.instruments = {}
        self.sweeps = {}
        self.recorder = None
        self.origin = time.monotonic_ns()
        self.utc = time.time_ns()
        self.tick = 0
        self.commands = queue.Queue(maxsize=100)
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.lock = threading.Lock()
        self.state = dict(instruments=[], recording=False, log_path="", tick=0,
                          interval_s=self.interval, configured_resources=sorted(self.saved_resources), saved_devices=copy.deepcopy(self.saved_devices), instrument_profiles=copy.deepcopy(self.instrument_profiles), connections=copy.deepcopy(self.connections), controls={}, commands={}, status="Ready", inventory=discovery.usb_inventory(), demo=False)
        self.thread = None
    def submit(self, action, **args):
        request = args.get("request")
        with self.lock:
            if request:
                self.state["commands"][request] = dict(state="queued", action=action)
                while len(self.state["commands"]) > 100:
                    del self.state["commands"][next(iter(self.state["commands"]))]
            try:
                self.commands.put_nowait((action, args))
                self.wake_event.set()
            except Exception:
                if request:
                    self.state["commands"].pop(request, None)
                raise

    def execute(self, action, args):
        request = args.get("request")
        if request:
            with self.lock:
                self.state["commands"][request] = dict(state="running", action=action)
        try:
            data = self.command(action, args)
            control = self.snapshot()["controls"].get(request)
            if control and control["state"] == "failed":
                raise ValueError(control["error"])
            result = dict(state="succeeded", action=action, result=data)
        except Exception as exc:
            self._status(str(exc))
            result = dict(state="failed", action=action, error=str(exc))
        result["finished_utc_ns"] = time.time_ns()
        if request:
            with self.lock:
                self.state["commands"][request] = result
        return result
    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.state)
    def _status(self, message):
        with self.lock:
            self.state["status"] = message
    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()
    def close(self):
        self.stop_event.set()
        self.wake_event.set()
        if self.thread:
            self.thread.join()
    def resolve(self, key):
        if key in self.instruments or key in self.instrument_profiles:
            return key
        return self.connections.get(key, {}).get('instrument') or key

    def drop_connection(self, resource):
        key = self.resolve(resource)
        # Removing an inactive address must never interrupt another transport.
        if self.active_resources.get(key, key) == resource:
            instrument = self.instruments.pop(key, None)
            if instrument:
                instrument.close()
            self.active_resources.pop(key, None)
            self.sweeps.pop(key, None)
            with self.lock:
                self.state['instruments'] = [card for card in self.state['instruments'] if card['id'] != key]
        return key

    def save_profile(self):
        for resource in self.saved_resources:
            connection = self.connections.setdefault(resource, dict(instrument=None, enabled=True))
            profile = self.instrument_profiles.get(connection.get('instrument'))
            if profile:
                self.saved_devices[resource] = dict(name=profile['name'] or profile['identity'],
                    category=profile.get('category', ''), instrument_id=connection['instrument'])
        with self.lock:
            self.state['configured_resources'] = sorted(self.saved_resources)
            self.state['saved_devices'] = copy.deepcopy(self.saved_devices)
            self.state['connections'] = copy.deepcopy(self.connections)
            self.state['instrument_profiles'] = copy.deepcopy(self.instrument_profiles)
            self.state['interval_s'] = self.interval
            cards = {card['id']: card for card in self.state['instruments'] if card['id'] in self.instruments}
            for key, resource in self.active_resources.items():
                instrument = self.instruments[key]
                profile = self.instrument_profiles[key]
                card = cards.setdefault(key, dict(id=key, category=instrument.category,
                    simulated=instrument.simulated, readings=[], error='', sweep=False,
                    functions=[asdict(item) for item in self.functions(instrument)],
                    current_function=getattr(instrument, 'current_function', None), read_duration_ms=0))
                card.update(name=profile['name'] or instrument.name, custom_name=profile['name'], resource=resource, instrument_id=key,
                            device_type=profile['type'], serial=profile['serial'], settings=copy.deepcopy(profile['settings']))
            self.state['instruments'] = list(cards.values())
        if self.profile_path:
            self.profile_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.profile_path.with_suffix('.tmp')
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump(dict(schema_version=2, resources=sorted(self.saved_resources),
                    devices=self.saved_devices, connections=self.connections,
                    instrument_profiles=self.instrument_profiles, interval=self.interval), stream)
            os.replace(temporary, self.profile_path)

    def validate_settings(self, key, settings):
        if not isinstance(settings, dict) or set(settings) - {'function'}:
            raise ValueError('Supported instrument setting: function')
        if 'function' not in settings:
            return
        function = settings['function']
        instrument = self.instruments.get(key)
        if instrument:
            functions = self.functions(instrument)
        else:
            profile = self.instrument_profiles[key]
            functions = profile.get('functions', [])
            functions = [MeasurementFunction(**item) for item in functions]
        if not any(item.id == function for item in functions):
            raise ValueError('Unsupported measurement function for this instrument')

    def apply_settings(self, instrument, settings):
        if 'function' in settings:
            function = next((item for item in self.functions(instrument) if item.id == settings['function']), None)
            if function is None:
                raise ValueError('Saved measurement function is unsupported by this driver')
            if function.configurable:
                instrument.select_function(function.id)

    def connect_resource(self, resource, reconnect=False, automatic=False):
        current = self.resolve(resource)
        if resource in self.saved_resources and automatic and not self.connections.get(resource, {}).get('enabled', True):
            return
        if current in self.instruments and self.active_resources.get(current, current) == resource:
            if not reconnect:
                return
            self.drop_connection(resource)
        self.saved_resources.add(resource)
        self.connections.setdefault(resource, dict(instrument=None, enabled=True))
        self.save_profile()
        instrument = discovery.connect(resource)
        address = instrument.id
        record = identify(instrument.name, getattr(instrument, 'device_type', None))
        # Drivers without a usable serial stay endpoint-local; never merge unrelated hardware.
        key = record.pop('id') if record else address
        if not record:
            record = dict(type=getattr(instrument, 'device_type', type(instrument).__name__),
                manufacturer='', model=instrument.name, serial=None, identity=instrument.name,
                name='', settings={}, auto_connect=True)
        record['category'] = instrument.category
        record['functions'] = [asdict(item) for item in self.functions(instrument)]
        profile = self.instrument_profiles.get(key, record)
        if automatic and not profile.get('auto_connect', True):
            instrument.close()
            self.connections[resource]['instrument'] = key
            self.connections[resource]['enabled'] = False
            self.save_profile()
            return
        try:
            self.apply_settings(instrument, profile['settings'])
        except Exception:
            instrument.close()
            raise
        previous_resource = self.active_resources.get(key)
        previous = self.instruments.pop(key, None)
        if previous:
            previous.close()
            self.sweeps.pop(key, None)
        for connection in self.connections.values():
            if connection.get('instrument') == key:
                connection['enabled'] = False
        profile.update(category=record['category'], functions=record['functions'], identity=record['identity'], auto_connect=True)
        self.instrument_profiles[key] = profile
        self.saved_resources.discard(resource)
        self.saved_resources.add(address)
        self.connections.pop(resource, None)
        self.connections[address] = dict(instrument=key, enabled=True)
        if address != resource:
            self.saved_devices.pop(resource, None)
        instrument.id = key
        instrument.resource = address
        if previous_resource and previous_resource != address:
            with self.lock:
                self.state['instruments'] = [card for card in self.state['instruments'] if card['id'] != key]
        self.active_resources[key] = address
        self.instruments[key] = instrument
        if self.recorder and previous_resource != address:
            self.recorder.event(time.monotonic_ns()-self.origin, key, f'connect: {address}')
        self.save_profile()
        return dict(instrument=key, resource=address)

    def command(self, action, args):
        if action == "demo":
            for category in ("multimeter", "power_supply", "oscilloscope"):
                key = "demo:" + category
                if key not in self.instruments:
                    self.instruments[key] = Simulated(category)
                    self.instrument_profiles.setdefault(key, dict(type=key, manufacturer='GATE', model=category,
                        serial='demo', identity=self.instruments[key].name, name='', settings={}, functions=[],
                        category=category, auto_connect=False))
            self.save_profile()
            self.state["demo"] = True
        elif action == "edit_device":
            old = args.get('resource_before', args['instrument'])
            resource = validate_resource(args['resource'])
            if old not in self.saved_resources:
                raise ValueError('Unknown connection')
            if resource != old and resource in self.saved_resources:
                raise ValueError('That connection is already configured')
            key = self.drop_connection(old)
            details = self.saved_devices.pop(old, {})
            connection = self.connections.pop(old, dict(instrument=None, enabled=True))
            self.saved_resources.discard(old)
            self.saved_resources.add(resource)
            self.connections[resource] = connection
            self.saved_devices[resource] = details
            self.save_profile()
            if self.recorder:
                self.recorder.event(time.monotonic_ns()-self.origin, key, f'edit connection: {resource}')
            return self.connect_resource(resource)
        elif action in ('scan', 'connect'):
            resources = discovery.usb_resources() if action == 'scan' else [validate_resource(args['resource'])]
            failures, result = [], None
            for resource in resources:
                try:
                    result = self.connect_resource(resource, args.get('reconnect', False), args.get('automatic', False))
                except Exception as exc:
                    failures.append(f'{resource}: {exc}')
            self.state['inventory'] = discovery.usb_inventory()
            self._status('; '.join(failures) if failures else
                         f'{len(self.instruments)} instruments connected' if resources else
                         'No USBTMC nodes accessible. Check USB permissions and usbtmc module.')
            if failures and action == 'connect':
                raise ConnectionError('; '.join(failures))
            return result
        elif action == 'disconnect':
            requested = args['instrument']
            key = self.resolve(requested)
            resource = requested if requested in self.connections else self.active_resources.get(key, requested)
            self.drop_connection(resource)
            if args.get('forget', True):
                self.saved_resources.discard(resource)
                self.saved_devices.pop(resource, None)
                self.connections.pop(resource, None)
            else:
                if resource in self.connections:
                    self.connections[resource]['enabled'] = False
            if key in self.instrument_profiles:
                self.instrument_profiles[key]['auto_connect'] = any(c.get('instrument') == key and c.get('enabled') for c in self.connections.values())
            self.save_profile()
            if self.recorder:
                self.recorder.event(time.monotonic_ns()-self.origin, key, 'disconnect')
            self._status('Device disconnected')
        elif action == 'forget':
            scope = args['scope']
            if scope not in ('connection', 'instrument'):
                raise ValueError('Choose connection or instrument')
            requested = args['instrument']
            key = self.resolve(requested)
            if scope == 'connection':
                resource = args.get('resource', requested)
                if resource not in self.connections:
                    raise ValueError('Unknown connection')
                if requested in self.instrument_profiles and self.resolve(resource) != key:
                    raise ValueError('Connection belongs to a different instrument')
                return self.command('disconnect', dict(instrument=resource, forget=True))
            if key not in self.instrument_profiles:
                raise ValueError('Unknown instrument')
            for resource in list(self.connections):
                if self.connections[resource].get('instrument') == key:
                    self.drop_connection(resource)
                    self.connections.pop(resource)
                    self.saved_resources.discard(resource)
                    self.saved_devices.pop(resource, None)
            orphan = self.instruments.pop(key, None)
            if orphan:
                orphan.close()
            self.active_resources.pop(key, None)
            self.sweeps.pop(key, None)
            self.instrument_profiles.pop(key)
            self.save_profile()
            self._status('Instrument and its settings forgotten')
        elif action in ('instrument_settings', 'copy_settings'):
            key = self.resolve(args['instrument'])
            profile = self.instrument_profiles[key]
            if action == 'copy_settings':
                source = self.instrument_profiles[self.resolve(args['source'])]
                if source['type'] != profile['type']:
                    raise ValueError('Settings can only be copied between instruments of the same type')
                settings = copy.deepcopy(source['settings'])
                name = profile['name']
            else:
                settings = copy.deepcopy(args.get('settings', profile['settings']))
                name = args.get('name', profile['name'])
                if not isinstance(name, str) or len(name.strip()) > 80 or any(ord(c) < 32 for c in name):
                    raise ValueError('Instrument name must be at most 80 printable characters')
                name = name.strip()
            self.validate_settings(key, settings)
            instrument = self.instruments.get(key)
            if instrument and settings != profile['settings']:
                self.apply_settings(instrument, settings)
            profile.update(name=name, settings=settings)
            self.save_profile()
            if self.recorder:
                self.recorder.event(time.monotonic_ns()-self.origin, key, f'{action}: {settings}')
            self._status('Instrument settings saved')
            return dict(instrument=key, settings=settings, name=name)
        elif action == "settings":
            interval = float(args["interval"])
            if not math.isfinite(interval) or not .1 <= interval <= 60:
                raise ValueError("Acquisition interval must be 0.1–60 seconds")
            if self.recorder:
                raise ValueError("Stop recording before changing the acquisition interval")
            self.interval = interval
            self.save_profile()
            self._status("Acquisition settings saved")
        elif action == "select_function":
            request = str(args.get("request") or uuid.uuid4())
            result = dict(instrument=args.get("instrument"), function=args.get("function"))
            try:
                instrument = self.instruments[self.resolve(args["instrument"])]
                functions = {function.id: function for function in self.functions(instrument)}
                function = functions[args["function"]]
                if function.configurable:
                    instrument.select_function(function.id)
                profile = self.instrument_profiles.get(instrument.id)
                if profile is not None:
                    profile['settings']['function'] = function.id
                    self.save_profile()
                if self.recorder:
                    self.recorder.event(time.monotonic_ns()-self.origin, instrument.id,
                                        f"select_function: {function.id}")
                result.update(state="applied", channel=function.channel)
                self._status(f"{instrument.name.split(',')[0]}: {function.label} selected")
            except Exception as exc:
                result.update(state="failed", error=str(exc))
                self._status(f"Function selection failed: {exc}")
            with self.lock:
                self.state["controls"][request] = result
                while len(self.state["controls"]) > 100:
                    del self.state["controls"][next(iter(self.state["controls"]))]
        elif action in ("query", "write"):
            instrument = self.instruments[self.resolve(args["instrument"])]
            transport = getattr(instrument, "transport", None)
            if transport is None:
                raise ValueError("This instrument has no raw command transport")
            text = args["command"]
            if not isinstance(text, str) or not 0 < len(text) <= 256 or any(c in text for c in "\r\n;"):
                raise ValueError("Use one command, max 256 characters, without semicolons/newlines")
            if (action == "query") != ("?" in text):
                raise ValueError("Queries must contain ?; writes must not contain ?")
            response = transport.query(text) if action == "query" else transport.write(text)
            if self.recorder:
                self.recorder.event(time.monotonic_ns()-self.origin, instrument.id, f"{action}: {text}")
            return dict(instrument=instrument.id, response=response)
        elif action == "record":
            if self.recorder:
                raise ValueError("Already recording")
            if not self.instruments:
                raise ValueError("Connect instruments first")
            path = Path(args["path"]).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            self.recorder = Recorder(path)
            self.origin, self.utc, self.tick = time.monotonic_ns(), time.time_ns(), 0
            self.recorder.start(self.utc, self.origin, self.interval)
            self.state.update(recording=True, log_path=str(path))
        elif action == "stop_recording":
            if self.recorder:
                self.recorder.close()
                self.recorder = None
            self.state["recording"] = False
        elif action in ("static", "sweep", "output", "cancel_sweep"):
            instrument = self.instruments[self.resolve(args["instrument"])]
            if instrument.category != "power_supply" or not instrument.simulated:
                raise ValueError("Output control currently supported on the demo PSU only")
            if action == "static":
                voltage = float(args["voltage"])
                instrument.set_voltage(voltage)
                self.sweeps.pop(instrument.id, None)
            elif action == "output":
                if not isinstance(args["enabled"], bool):
                    raise ValueError("enabled must be boolean")
                instrument.set_enabled(args["enabled"])
                if not args["enabled"]:
                    self.sweeps.pop(instrument.id, None)
            elif action == "cancel_sweep":
                self.sweeps.pop(instrument.id, None)
            else:
                start, stop, step, dwell = [float(args[k]) for k in ("start", "stop", "step", "dwell")]
                if not all(math.isfinite(x) for x in (start, stop, step, dwell)):
                    raise ValueError("Sweep values must be finite")
                if not (0 <= start <= 30 and 0 <= stop <= 30 and step != 0 and
                        (stop-start)*step >= 0 and dwell >= self.interval):
                    raise ValueError("Sweep requires 0–30 V, a step towards stop, dwell >= polling interval")
                instrument.set_voltage(start)
                self.sweeps[instrument.id] = dict(value=start, stop=stop, step=step,
                                                dwell=dwell, due=time.monotonic()+dwell)
            if self.recorder:
                self.recorder.event(time.monotonic_ns()-self.origin, instrument.id, f"{action}: {args}")
        else:
            raise ValueError("Unknown action")

    def functions(self, instrument, readings=()):
        provider = getattr(instrument, "functions", None)
        if provider:
            return provider()
        return [MeasurementFunction(reading.channel, reading.channel, reading.channel, reading.unit)
                for reading in readings]

    def cycle(self):
        tick_ns = time.monotonic_ns() - self.origin
        cards = []
        for key, instrument in list(self.instruments.items()):
            start = time.monotonic_ns() - self.origin
            error = ""
            try:
                sweep = self.sweeps.get(key)
                if sweep and time.monotonic() >= sweep["due"]:
                    value = sweep["value"] + sweep["step"]
                    value = min(value, sweep["stop"]) if sweep["step"] > 0 else max(value, sweep["stop"])
                    instrument.set_voltage(value)
                    sweep["value"] = value
                    sweep["due"] = time.monotonic() + sweep["dwell"]
                    if value == sweep["stop"]:
                        del self.sweeps[key]
                    if self.recorder:
                        self.recorder.event(time.monotonic_ns()-self.origin, key, f"sweep set_voltage: {value}")
                readings = instrument.read()
            except Exception as exc:
                error = str(exc)
                self._status(f"{instrument.name}: {error}")
                readings = [Reading("connection", None, "", "error")]
            end = time.monotonic_ns() - self.origin
            if self.recorder:
                for reading in readings:
                    self.recorder.sample(self.tick, tick_ns, key, reading, start, end)
            profile = self.instrument_profiles.get(key, {})
            cards.append(dict(id=key, name=profile.get('name') or instrument.name, custom_name=profile.get('name', ''),
                              resource=self.active_resources.get(key, key), instrument_id=key,
                              device_type=profile.get('type'), serial=profile.get('serial'),
                              settings=copy.deepcopy(profile.get('settings', {})), category=instrument.category,
                              simulated=instrument.simulated, readings=[asdict(r) for r in readings],
                              error=error, sweep=key in self.sweeps,
                              functions=[asdict(function) for function in self.functions(instrument, readings)],
                              current_function=getattr(instrument, "current_function", None),
                              read_duration_ms=(end-start)/1e6))
            if error:
                instrument.close()
                self.sweeps.pop(key, None)
                del self.instruments[key]
                self.active_resources.pop(key, None)
        if self.recorder:
            self.recorder.flush()
        with self.lock:
            self.state.update(instruments=cards, tick=self.tick, elapsed_s=tick_ns/1e9,
                              updated_utc_ns=time.time_ns())
        self.tick += 1

    def run(self):
        deadline = time.monotonic()
        try:
            while not self.stop_event.is_set():
                self.wake_event.clear()
                previous_interval = self.interval
                for _ in range(100):
                    try:
                        action, args = self.commands.get_nowait()
                    except queue.Empty:
                        break
                    self.execute(action, args)
                if self.interval != previous_interval:
                    deadline = time.monotonic() + self.interval
                now = time.monotonic()
                if now >= deadline:
                    self.cycle()
                    deadline += self.interval
                    now = time.monotonic()
                    if deadline < now:
                        with self.lock:
                            self.state["overrun"] = True
                        deadline = now + self.interval
                # Commands wake the owner independently of the sample schedule.
                self.wake_event.wait(max(0, deadline-time.monotonic()))
        except Exception as exc:
            self._status(f"Acquisition stopped: {exc}")
        finally:
            if self.recorder:
                self.recorder.close()
            for instrument in self.instruments.values():
                instrument.close()
