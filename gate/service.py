import json
import signal
import os
from pathlib import Path
from gi.repository import Gio, GLib
from gate.engine import Engine

BUS = "io.github.MAWoodMain.GATE"
PATH = "/io/github/MAWoodMain/GATE"
XML = f"""<node><interface name="{BUS}">
<method name="Snapshot"><arg type="s" direction="out"/></method>
<method name="Command"><arg type="s" direction="in"/><arg type="s" direction="out"/></method>
</interface></node>"""

def proxy():
    return Gio.DBusProxy.new_for_bus_sync(Gio.BusType.SESSION, Gio.DBusProxyFlags.DO_NOT_AUTO_START,
                                         None, BUS, PATH, BUS, None)

def call(client, method, payload=None):
    params = GLib.Variant("(s)", (json.dumps(payload),)) if payload is not None else None
    return json.loads(client.call_sync(method, params, Gio.DBusCallFlags.NONE, 2000, None).unpack()[0])

def serve(demo=False, interval=None, control_port=8765, allow_raw=False, ephemeral=False):
    config = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home()/".config"))) / "gate/devices.json"
    engine = Engine(interval, None if ephemeral else config)
    loop = GLib.MainLoop()
    connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    result = connection.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
        "org.freedesktop.DBus", "RequestName", GLib.Variant("(su)", (BUS, 4)),
        GLib.VariantType.new("(u)"), Gio.DBusCallFlags.NONE, -1, None)
    if result.unpack()[0] != 1:
        raise RuntimeError("GATE service is already running")
    def method(conn, sender, path, interface, name, params, invocation):
        try:
            if name == "Snapshot":
                answer = engine.snapshot()
            else:
                request = json.loads(params.unpack()[0])
                action = request.pop("action")
                engine.submit(action, **request)
                answer = {"queued": True}
            invocation.return_value(GLib.Variant("(s)", (json.dumps(answer, allow_nan=False),)))
        except Exception as exc:
            invocation.return_dbus_error(BUS + ".Error", str(exc))
    registration = connection.register_object(PATH, Gio.DBusNodeInfo.new_for_xml(XML).interfaces[0], method, None, None)
    control = None
    if control_port is not None:
        from gate.control import ControlServer
        control = ControlServer(engine, port=control_port, allow_raw=allow_raw)
        control.start()
        with engine.lock:
            engine.state["control_api"] = dict(url=control.manifest["url"], manifest=str(control.path), allow_raw=allow_raw)
    engine.submit("demo" if demo else "scan")
    for resource in sorted(engine.saved_resources):
        engine.submit("connect", resource=resource)
    engine.start()
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, lambda: (loop.quit(), False)[1])
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, lambda: (loop.quit(), False)[1])
    try:
        loop.run()
    finally:
        if control:
            control.close()
        engine.close()
        connection.unregister_object(registration)
