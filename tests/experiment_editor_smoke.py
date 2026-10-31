"""Run under a private dbus-run-session with a GTK display."""
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gate.service import proxy, call

process = subprocess.Popen([sys.executable, '-m', 'gate', 'service', '--demo', '--ephemeral', '--no-control-api'])
try:
    for _ in range(60):
        try:
            if call(proxy(), 'Snapshot')['instruments']:
                break
        except Exception:
            pass
        time.sleep(.1)
    else:
        raise AssertionError('Service did not start')
    from gate.app import Window
    from gi.repository import Gtk, GLib
    app = Gtk.Application(application_id='io.github.MAWoodMain.GATE.EditorSmoke')
    app.register(None)
    window = Window(app)
    window.present()
    loop = GLib.MainLoop()
    errors, dialogs = [], []
    attempts = 0
    editor = None

    def begin():
        global editor
        try:
            assert window.online
            editor = window.new_experiment()
            dialogs.append(editor)
            editor.response(Gtk.ResponseType.ACCEPT)
            assert editor.error.get_visible() and not editor.saving
            editor.name.set_text('Simple supply test')
            editor.description.set_text('Exercise a supply and DUT')
            editor.dut_name.set_text('DUT')
            editor.dut_terminals.set_text('Input positive, Input negative')
            role = editor.roles[0]
            role['name'].set_text('Supply')
            role['terminals'].set_text('Positive, Negative')
            role['checks']['source.dc_voltage'].set_active(True)
            test = editor.tests[0]
            test['name'].set_text('Voltage sweep')
            test['description'].get_buffer().set_text('Vary the supply and measure DUT behaviour.')
            editor.add_wire([role['id'] + '.positive', 'dut.input_positive'])
            editor.add_wire([role['id'] + '.negative', 'dut.input_negative'])
            assert len(editor.definition()['connections']) == 2
            editor.load_example()
            editor.tests[0]['specification'].get_buffer().set_text('{invalid JSON')
            editor.response(Gtk.ResponseType.ACCEPT)
            assert editor.error.get_visible() and not editor.saving
            editor.load_example()
            definition = editor.definition()
            assert definition['tests']['efficiency']['schema_version'] == 1
            assert len(definition['connections']) == 4
            assert {key for key, role in definition['roles'].items() if not role.get('optional', False)} == {'supply', 'load'}
            assert definition['roles']['thermal']['optional'] and not definition['roles']['thermal']['terminals']
            assert len(definition['tests']) == 2
            # Role and terminal edits invalidate old wiring instead of retargeting it.
            editor.dut_terminals.set_text('Different terminal')
            try:
                editor.definition()
            except ValueError:
                pass
            else:
                raise AssertionError('Stale wiring must be rejected')
            editor.load_example()
            editor.name.set_text('Created in desktop smoke test')
            extra = editor.add_role()
            extra['box'].get_first_child().emit('clicked')
            extra = editor.add_test('Temporary', 'Temporary procedure')
            extra['box'].get_first_child().emit('clicked')
            wire = editor.add_wire()
            wire['box'].get_first_child().emit('clicked')
            window.online = False
            editor.response(Gtk.ResponseType.ACCEPT)
            assert 'background service' in editor.error.get_text()
            window.online = True
            window.experiments_supported = False
            editor.response(Gtk.ResponseType.ACCEPT)
            assert not editor.saving and 'Restart' in editor.error.get_text()
            window.experiments_supported = True
            # Service failures retain the completed form and allow retry.
            command = window.command
            window.command = Mock(side_effect=lambda *args, **kwargs: kwargs['on_complete'](
                dict(state='failed', error='Fixture save failure')))
            editor.response(Gtk.ResponseType.ACCEPT)
            assert editor.get_visible() and not editor.saving
            assert editor.error.get_text() == 'Fixture save failure'
            assert editor.save_button.get_sensitive()
            window.command = command
            editor.response(Gtk.ResponseType.ACCEPT)
            assert editor.saving
            GLib.timeout_add(100, saved)
        except Exception as exc:
            errors.append(exc)
            window.close()
            loop.quit()
        return False

    def saved():
        global attempts
        attempts += 1
        try:
            if editor.saving:
                assert attempts < 60, 'Experiment creation timed out'
                return True
            assert not editor.get_visible(), editor.error.get_text()
            assert window.pages.get_visible_child_name() == 'experiment:' + editor.identity
            definition = call(proxy(), 'Snapshot')['experiments'][editor.identity]
            assert definition['name'] == 'Created in desktop smoke test'
            assert len(definition['connections']) == 4
            assert {key for key, role in definition['roles'].items() if not role.get('optional', False)} == {'supply', 'load'}
            assert definition['roles']['thermal']['optional'] and not definition['roles']['thermal']['terminals']
            assert len(definition['tests']) == 2
            cancelled = window.new_experiment()
            dialogs.append(cancelled)
            cancelled.response(Gtk.ResponseType.CANCEL)
            assert cancelled.identity not in call(proxy(), 'Snapshot')['experiments']
            print('PASS: experiment editor validation, wiring changes, add/remove, failure retry, live save and navigation, cancel')
        except Exception as exc:
            errors.append(exc)
        window.close()
        loop.quit()
        return False

    GLib.timeout_add(1000, begin)
    loop.run()
    if errors:
        raise errors[0]
finally:
    process.terminate()
    process.wait(timeout=10)
