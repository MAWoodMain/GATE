"""Run from repository root under a private dbus-run-session."""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gate.service import proxy, call
process = subprocess.Popen([sys.executable, '-m', 'gate', 'service', '--demo', '--interval', '0.1', '--no-control-api', '--ephemeral'])
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
    dialogs = []  # Retain wrappers until Wayland has dispatched pending configure events.
    def check():
        try:
            assert len(window.labels)==3, window.status.get_text()
            assert window.get_title() == 'GATE · Devices & Settings'
            assert all('Connected' in label.get_text() or 'Demo' in label.get_text() for label in window.labels.values())
            assert not hasattr(window, 'voltage'), 'Measurement/output controls belong outside device manager'
            # Exercise missing-device sections, structured editor and service actions without touching hardware.
            fixture = dict(instruments=[], configured_resources=['/dev/usbtmc9', 'serial:///dev/ttyUSB9?baud=9600', 'tcp://missing:5025'],
                           saved_devices={'/dev/usbtmc9': {'name': 'Remembered scope'}})
            window.render_devices(fixture)
            assert window.titles['/dev/usbtmc9'].get_text() == 'Remembered scope'
            assert all('Disconnected' in label.get_text() for label in window.labels.values())
            assert all(window.sections[kind].get_first_child() for kind in ('TMC', 'Serial', 'Network'))
            dialog = window.edit_connection('Serial', 'serial:///dev/ttyUSB9?baud=9600')
            dialogs.append(dialog)
            entry = dialog.get_content_area().get_first_child().get_next_sibling()
            assert isinstance(entry, Gtk.Entry)
            entry.set_text('/dev/ttyUSB8')
            window.render_devices(fixture)
            assert dialog.get_visible() and entry.get_text() == '/dev/ttyUSB8', 'Refresh must preserve editor state'
            from unittest.mock import Mock
            original_command, original_systemctl, original_refresh = window.command, window.run_systemctl, window.refresh
            window.command = Mock()
            dialog.response(Gtk.ResponseType.ACCEPT)
            window.command.assert_called_once_with('edit_device', instrument='serial:///dev/ttyUSB9?baud=9600', resource='serial:///dev/ttyUSB8?baud=9600')
            window.refresh = Mock()
            for action in ('start', 'stop', 'restart'):
                window.run_systemctl = Mock(side_effect=lambda arguments, callback: callback(True, '', ''))
                window.service_action(action)
                assert window.run_systemctl.call_args.args[0] == [action, 'gate.service']
                assert not window.service_busy
            window.run_systemctl = Mock(side_effect=lambda arguments, callback: callback(False, '', 'Unit not found'))
            window.service_action('start')
            assert 'Unit not found' in window.message.get_text()
            window.online = False
            window.service_state = 'inactive'
            window.update_sensitivity()
            assert window.service_buttons['start'].get_sensitive()
            assert not window.service_buttons['stop'].get_sensitive()
            window.command, window.run_systemctl, window.refresh = original_command, original_systemctl, original_refresh
            window.online = True
            window.render_devices(fixture)
            window.update_sensitivity()
            window.notify('')
            print('PASS: D-Bus recording, grouped/missing devices, persistent editor, service actions and errors')
        except Exception as exc:
            errors.append(exc)
        finally:
            # Let layout/paint settle after replacing rows before taking a snapshot.
            window.pending = True
            GLib.timeout_add(300, finish)
    def finish():
        try:
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
        return False
    GLib.timeout_add(1800,check)
    loop.run()
    if errors:
        raise errors[0]
finally:
    process.terminate()
    process.wait(timeout=10)
