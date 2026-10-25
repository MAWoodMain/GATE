"""Run from repository root under a private dbus-run-session."""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gate.service import proxy, call
process = subprocess.Popen([sys.executable, '-m', 'gate', 'service', '--demo', '--interval', '0.1'])
try:
    for attempt in range(50):
        try:
            state=call(proxy(),'Snapshot')
            if len(state['instruments'])==3:
                break
        except Exception:
            pass
        time.sleep(.1)
    else:
        raise AssertionError('Service did not start')
    with tempfile.TemporaryDirectory() as folder:
        path=str(Path(folder)/'smoke.sqlite')
        call(proxy(),'Command',dict(action='record',path=path))
        time.sleep(.3)
        assert call(proxy(),'Snapshot')['recording']
        call(proxy(),'Command',dict(action='stop_recording'))
        time.sleep(.2)
        assert not call(proxy(),'Snapshot')['recording']
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import GLib, Gtk
    from gate.app import Window
    app=Gtk.Application(application_id='io.github.MAWoodMain.GATE.Smoke')
    app.register(None)
    window=Window(app)
    window.present()
    loop=GLib.MainLoop()
    errors = []
    def check():
        try:
            assert len(window.labels)==3, window.status.get_text()
            print('PASS: D-Bus service, recording, GTK window and live cards')
            if os.environ.get('GATE_SCREENSHOT'):
                from gi.repository import Gdk, Graphene
                paintable=Gtk.WidgetPaintable.new(window)
                snapshot=Gtk.Snapshot.new()
                paintable.snapshot(snapshot, window.get_width(), window.get_height())
                node=snapshot.to_node()
                rect=Graphene.Rect()
                rect.init(0,0,window.get_width(),window.get_height())
                texture=window.get_native().get_renderer().render_texture(node,rect)
                texture.save_to_png(os.environ['GATE_SCREENSHOT'])
        except Exception as exc:
            errors.append(exc)
        finally:
            window.close()
            loop.quit()
    GLib.timeout_add(1800,check)
    loop.run()
    if errors:
        raise errors[0]
finally:
    process.terminate()
    process.wait(timeout=10)
