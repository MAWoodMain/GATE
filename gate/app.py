"""GTK4 frontend. Instrument IO lives in the acquisition service."""
import json
import os
import time
from pathlib import Path
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk
from gate.service import BUS, PATH

CSS = b"""
window { background: #111820; color: #edf4fa; }
headerbar { background: #18232e; }
.hero { font-size: 30px; font-weight: 800; }
.muted { color: #9eafbd; }
.card { background: #1c2935; border-radius: 14px; padding: 20px; }
.value { font-family: monospace; font-size: 23px; color: #67e8bc; }
.tag { color: #f8ca76; font-weight: bold; }
button.suggested-action { background: #267d65; color: white; }
"""

class Window(Gtk.ApplicationWindow):
    def __init__(self, application):
        super().__init__(application=application, title="GATE · Gnome Automatic Test Equipment",
                         default_width=1020, default_height=780)
        self.pending = False
        self.connected = False
        self.recording = False
        self.labels = {}
        self.ids = None
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(self.get_display(), provider, 600)
        header = Gtk.HeaderBar()
        self.set_titlebar(header)
        header.pack_start(self.button("Scan USB", lambda: self.command("scan")))
        header.pack_end(self.button("Add demo bench", lambda: self.command("demo")))
        scroll = Gtk.ScrolledWindow()
        self.set_child(scroll)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                        margin_top=28, margin_bottom=28, margin_start=28, margin_end=28)
        scroll.set_child(outer)
        heading = self.label("GATE", "hero")
        heading.set_selectable(False)
        outer.append(heading)
        outer.append(self.label("Gnome Automatic Test Equipment · Your bench, on one timeline", "muted"))
        self.status = self.label("Connecting to acquisition service…", "muted")
        outer.append(self.status)
        row = Gtk.Box(spacing=8)
        self.resource = Gtk.Entry(placeholder_text="USB: /dev/usbtmcN · LAN: tcp://host:5025 · Fluke 45: serial:///dev/ttyUSB0?baud=9600", hexpand=True)
        row.append(self.resource)
        row.append(self.button("Connect", lambda: self.command("connect", resource=self.resource.get_text())))
        outer.append(row)
        outer.append(self.label("Fluke 45 serial: enter serial:///dev/ttyUSB0?baud=9600 and click Connect (8 data bits, no parity, 1 stop bit).", "muted"))
        self.cards = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        outer.append(self.cards)
        logging = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        logging.add_css_class("card")
        logging.append(self.label("Shared timeline recording"))
        logging.append(self.label("Host timestamps · per-instrument acquisition windows · SQLite → CSV", "muted"))
        row = Gtk.Box(spacing=8)
        base = Path(os.environ.get("XDG_DATA_HOME", str(Path.home()/".local/share"))) / "gate/recordings"
        self.logfile = Gtk.Entry(text=str(base / (time.strftime("bench-%Y%m%d-%H%M%S")+".sqlite")), hexpand=True)
        row.append(self.logfile)
        self.record = self.button("Start recording", self.toggle_record)
        self.record.add_css_class("suggested-action")
        row.append(self.record)
        row.append(self.button("Export CSV", self.export))
        logging.append(row)
        outer.append(logging)
        controls = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        controls.add_css_class("card")
        controls.append(self.label("Output workbench · SIMULATED PSU", "tag"))
        controls.append(self.label("Static and stepped sweeps. Setting a voltage never enables the output.", "muted"))
        row = Gtk.Box(spacing=8)
        self.voltage = Gtk.SpinButton.new_with_range(0, 30, 0.1)
        row.append(self.label("Voltage (V)"))
        row.append(self.voltage)
        row.append(self.button("Apply", lambda: self.command("static", instrument="demo:power_supply", voltage=self.voltage.get_value())))
        row.append(self.button("Enable output", lambda: self.command("output", instrument="demo:power_supply", enabled=True)))
        row.append(self.button("Disable output", lambda: self.command("output", instrument="demo:power_supply", enabled=False)))
        controls.append(row)
        row = Gtk.Box(spacing=8)
        self.sweep = {}
        for name, value, low, high, step in (("start",0,0,30,.1),("stop",5,0,30,.1),("step",1,-30,30,.1),("dwell",1,.1,3600,.1)):
            row.append(self.label(name.title() + (" (s)" if name == "dwell" else " (V)")))
            spin = Gtk.SpinButton.new_with_range(low, high, step)
            spin.set_value(value)
            self.sweep[name] = spin
            row.append(spin)
        row.append(self.button("Run sweep", lambda: self.command("sweep", instrument="demo:power_supply", **{k:v.get_value() for k,v in self.sweep.items()})))
        row.append(self.button("Cancel", lambda: self.command("cancel_sweep", instrument="demo:power_supply")))
        controls.append(row)
        outer.append(controls)
        outer.append(self.label("Real hardware: SDS1202X-E scope and Fluke 45 multimeter. Additional drivers can be added as plugins.", "muted"))
        self.closed = False
        self.connect("close-request", self.on_close)
        self.timer = GLib.timeout_add(1000, self.refresh)
        self.refresh()

    def on_close(self, *args):
        self.closed = True
        GLib.source_remove(self.timer)
        return False

    def label(self, text, css=None):
        label = Gtk.Label(label=text, xalign=0, wrap=True, selectable=True)
        if css:
            label.add_css_class(css)
        return label
    def button(self, text, callback):
        button = Gtk.Button(label=text)
        button.connect("clicked", lambda *_: callback())
        return button
    def bus_call(self, method, payload, callback):
        connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        params = GLib.Variant("(s)", (json.dumps(payload),)) if payload is not None else None
        def finished(conn, result):
            try:
                value = json.loads(conn.call_finish(result).unpack()[0])
                callback(value, None)
            except Exception as exc:
                callback(None, exc)
        connection.call(BUS, PATH, BUS, method, params, GLib.VariantType.new("(s)"),
                        Gio.DBusCallFlags.NONE, 2000, None, finished)
    def command(self, action, **args):
        self.bus_call("Command", dict(action=action, **args),
                      lambda value, error: self.status.set_text(str(error) if error else "Command queued"))
    def toggle_record(self):
        self.command("stop_recording" if self.recording else "record", path=self.logfile.get_text())
    def export(self):
        from gate.storage import export_csv
        try:
            path = self.logfile.get_text()
            export_csv(path, path + ".csv")
            self.status.set_text("Exported " + path + ".csv")
        except Exception as exc:
            self.status.set_text(str(exc))
    def refresh(self):
        if self.pending or self.closed:
            return not self.closed
        self.pending = True
        def updated(state, error):
            self.pending = False
            if self.closed:
                return
            if error:
                self.status.set_text("Service offline. Run: gate service (or systemctl --user start gate)")
                for label in self.labels.values():
                    label.set_text("Offline · readings stale")
                return
            self.recording = state["recording"]
            self.record.set_label("Stop recording" if self.recording else "Start recording")
            stale = (time.time_ns() - state.get("updated_utc_ns", 0)) / 1e9 > 5
            self.status.set_text(("STALE · " if stale else "") + state["status"] + " · " +
                                 ("RECORDING" if self.recording else "Live") + " · Cycle " + str(state["tick"]))
            instruments = state["instruments"]
            ids = [i["id"] for i in instruments]
            if ids != self.ids:
                while self.cards.get_first_child():
                    self.cards.remove(self.cards.get_first_child())
                self.labels = {}
                for instrument in instruments:
                    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
                    box.add_css_class("card")
                    box.append(self.label(instrument["name"], "tag" if instrument["simulated"] else None))
                    box.append(self.label(instrument["id"], "muted"))
                    label = self.label("", "value")
                    self.labels[instrument["id"]] = label
                    box.append(label)
                    self.cards.append(box)
                if not ids:
                    self.cards.append(self.label("No instruments connected. Scan USB, connect by address, or add the demo bench.", "muted"))
                self.ids = ids
            for instrument in instruments:
                text = "    ·    ".join(f"{r['channel']}  {r['value']:.6g} {r['unit']}" if r['value'] is not None
                       else f"{r['channel']}  — {r['quality']}" for r in instrument["readings"])
                self.labels[instrument["id"]].set_text(instrument["error"] or text)
        self.bus_call("Snapshot", None, updated)
        return True

def launch():
    app = Gtk.Application(application_id=BUS + ".App")
    app.connect("activate", lambda app: Window(app).present())
    app.run(None)
