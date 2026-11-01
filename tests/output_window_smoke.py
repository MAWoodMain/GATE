"""Private session-bus demo integration; no real hardware or active Shell changes."""
import subprocess
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gate.service import proxy, call
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import GLib, Gtk
from gate.output_window import OutputWindow

process = subprocess.Popen([sys.executable, '-m', 'gate', 'service', '--demo', '--ephemeral',
                            '--interval', '.1', '--no-control-api'])
try:
    for _ in range(60):
        try:
            if len(call(proxy(), 'Snapshot')['instruments']) == 3:
                break
        except Exception:
            pass
        time.sleep(.1)
    else:
        raise AssertionError('Demo service failed to start')
    app = Gtk.Application(application_id='io.github.MAWoodMain.GATE.OutputSmoke')
    app.register(None)
    window = OutputWindow(app, 'demo:power_supply')
    window.present()
    def wait_until(predicate):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            while GLib.MainContext.default().iteration(False):
                pass
            if predicate():
                return
            time.sleep(.02)
        raise AssertionError(window.status.get_text())
    wait_until(lambda: bool(window.rows))
    assert window.state_label.get_text() == '○ Output OFF'
    assert 'ovp' in window.rows and 'ocp' in window.rows
    window.rows['priority']['editor'].set_active(True)
    wait_until(lambda: window.pending is None and window.rows['priority']['editor'].get_active())
    window.rows['ovp']['editor'].set_text('10')
    window.apply_control('ovp')
    wait_until(lambda: window.pending is None)
    row = window.rows['voltage']
    row['editor'].set_text('3.3')
    wait_until(lambda: not window.polling)
    window.render(call(proxy(), 'Snapshot'))
    assert row['editor'].get_text() == '3.3', 'Polling overwrote a typed edit'
    window.apply_control('voltage')
    wait_until(lambda: window.pending is None)
    window.toggle_output()
    wait_until(lambda: 'OUTPUT ON' in window.state_label.get_text())
    assert 'Calculated power:' in window.readback.get_text()
    row['editor'].set_text('nan')
    window.apply_control('voltage')
    assert not window.pending
    assert 'limits' in window.status.get_text()
    window.toggle_output()
    wait_until(lambda: 'Output OFF' in window.state_label.get_text())
    state = call(proxy(), 'Snapshot')
    card = next(c for c in state['instruments'] if c['id'] == window.instrument)
    # Exercise optional controls without a real instrument or transport.
    card['output_configuration']['controls'].update(
        priority=dict(label='Priority', choices=['voltage', 'current']),
        ovp=dict(label='OVP', min=1., max=30., unit='V', reading='ovp'))
    card['output_settings'] = {'priority': 'current'}
    window.render(state)
    assert window.rows['priority']['editor'].get_active()
    assert 'ovp' in window.rows
    card['readings'] = [dict(channel='output_enabled', value=1., unit='', quality='ok'),
                        dict(channel='voltage', value=5., unit='V', quality='overload'),
                        dict(channel='current', value=.1, unit='A', quality='ok')]
    window.render(state)
    assert 'Calculated power: unavailable' in window.readback.get_text()
    state['instruments'] = []
    window.render(state)
    assert not window.toggle.get_sensitive()
    assert 'offline' in window.state_label.get_text()
    window.close()
finally:
    process.terminate()
    process.wait(timeout=5)
print('Output window smoke passed')
