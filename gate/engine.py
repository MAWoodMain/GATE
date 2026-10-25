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
from gate.drivers.simulated import Simulated
from gate.model import Reading, MeasurementFunction
from gate.storage import Recorder

class Engine:
    def __init__(self, interval=None, profile_path=None):
        self.profile_path = Path(profile_path) if profile_path else None
        profile = {}
        if self.profile_path and self.profile_path.exists():
            profile = json.loads(self.profile_path.read_text())
        self.saved_resources = set(profile.get("resources", []))
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
                          interval_s=self.interval, configured_resources=sorted(self.saved_resources), controls={}, commands={}, status="Ready", inventory=discovery.usb_inventory(), demo=False)
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
    def save_profile(self):
        with self.lock:
            self.state["configured_resources"] = sorted(self.saved_resources)
            self.state["interval_s"] = self.interval
        if self.profile_path:
            self.profile_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.profile_path.with_suffix(".tmp")
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump(dict(resources=sorted(self.saved_resources), interval=self.interval), stream)
            os.replace(temporary, self.profile_path)

    def command(self, action, args):
        if action == "demo":
            for category in ("multimeter", "power_supply", "oscilloscope"):
                key = "demo:" + category
                if key not in self.instruments:
                    self.instruments[key] = Simulated(category)
            self.state["demo"] = True
        elif action in ("scan", "connect"):
            resources = discovery.usb_resources() if action == "scan" else [args["resource"]]
            failures = []
            for resource in resources:
                if resource in self.instruments:
                    continue
                try:
                    instrument = discovery.connect(resource)
                    self.instruments[instrument.id] = instrument
                    if instrument.id.startswith(("serial://", "tcp://")):
                        self.saved_resources.add(instrument.id)
                        self.save_profile()
                except Exception as exc:
                    failures.append(f"{resource}: {exc}")
            self.state["inventory"] = discovery.usb_inventory()
            self._status("; ".join(failures) if failures else
                         f"{len(self.instruments)} instruments connected" if resources else
                         "No USBTMC nodes accessible. Check USB permissions and usbtmc module.")
        elif action == "disconnect":
            key = args["instrument"]
            instrument = self.instruments.pop(key, None)
            if instrument:
                instrument.close()
                self.sweeps.pop(key, None)
            if args.get("forget", True):
                self.saved_resources.discard(key)
                self.save_profile()
            if self.recorder:
                self.recorder.event(time.monotonic_ns()-self.origin, key, "disconnect")
            self._status("Device disconnected")
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
                instrument = self.instruments[args["instrument"]]
                functions = {function.id: function for function in self.functions(instrument)}
                function = functions[args["function"]]
                if function.configurable:
                    instrument.select_function(function.id)
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
            instrument = self.instruments[args["instrument"]]
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
            instrument = self.instruments[args["instrument"]]
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
            cards.append(dict(id=key, name=instrument.name, category=instrument.category,
                              simulated=instrument.simulated, readings=[asdict(r) for r in readings],
                              error=error, sweep=key in self.sweeps,
                              functions=[asdict(function) for function in self.functions(instrument, readings)],
                              current_function=getattr(instrument, "current_function", None),
                              read_duration_ms=(end-start)/1e6))
            if error:
                instrument.close()
                self.sweeps.pop(key, None)
                del self.instruments[key]
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
