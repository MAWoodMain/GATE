"""GTK run setup, live execution, output charts and cancellation; private session bus."""
import copy
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gate.service import proxy, call
from gate.experiments import BUCK_CONVERTER

with tempfile.TemporaryDirectory() as folder:
    os.environ['XDG_DATA_HOME'] = folder
    process = subprocess.Popen([sys.executable, '-m', 'gate', 'service', '--ephemeral', '--no-control-api', '--interval', '.1'])
    try:
        for _ in range(50):
            try:
                call(proxy(), 'Snapshot')
                break
            except Exception:
                time.sleep(.1)
        else:
            raise AssertionError('Service did not start')
        definition = copy.deepcopy(BUCK_CONVERTER)
        definition['tests']['capture'] = dict(description='Capture current readings', procedure='capture')
        call(proxy(), 'Command', dict(action='save_experiment', experiment=definition))
        from gate.app import Window
        from gi.repository import Gtk, GLib
        app = Gtk.Application(application_id='io.github.MAWoodMain.GATE.RunSmoke')
        app.register(None)
        window = Window(app)
        window.present()
        loop = GLib.MainLoop()
        errors = []
        phase, started, ticks = 'equipment', time.monotonic(), 0
        view = None

        def check():
            global phase, ticks, view
            ticks += 1
            try:
                assert time.monotonic()-started < 25, 'Run UI test timed out at ' + phase
                if not window.online:
                    return True
                view = window.experiment_views['buck_converter']
                if phase == 'equipment':
                    window.navigation.select_row(window.experiment_rows['buck_converter'])
                    assert not view.start.get_sensitive()
                    view.demo.emit('clicked')
                    phase = 'ready'
                elif phase == 'ready':
                    if view.pickers['supply'].get_active_id() != 'demo:converter_supply' or view.pickers['load'].get_active_id() != 'demo:electronic_load':
                        return True
                    view.parameters['input_start_v'].set_value(8.)
                    view.parameters['input_stop_v'].set_value(8.)
                    view.parameters['load_stop_a'].set_value(.1)
                    view.parameters['settle_s'].set_value(.1)
                    view.parameters['samples_per_point'].set_value(2)
                    assert not view.start.get_sensitive(), 'Output sweeps require wiring confirmation'
                    view.wiring.set_active(True)
                    assert view.start.get_sensitive()
                    view.start.emit('clicked')
                    phase = 'efficiency'
                elif phase == 'efficiency':
                    run = call(proxy(), 'Snapshot').get('experiment_run')
                    if not run or run['state'] == 'running' or not view.last_result or view.last_result[:2] != (run['id'], 'completed'):
                        return True
                    assert run['state'] == 'completed', run['error']
                    assert len(run['points']) == 1
                    assert Path(run['report']).is_file() and Path(run['csv']).is_file()
                    assert 'SIMULATED' in view.results.get_first_child().get_text()
                    assert isinstance(view.results.get_first_child().get_next_sibling(), Gtk.Picture)
                    assert 'efficiency' in view.metrics.get_text()
                    report_button = view.results.get_last_child().get_first_child()
                    assert report_button.get_label() == 'Open Full Report'
                    browser = Mock()
                    browser.supports_uris.return_value = True
                    with patch('gate.experiment_view.Gio.AppInfo.get_default_for_uri_scheme', return_value=browser) as lookup, \
                         patch('gate.experiment_view.Gio.AppInfo.launch_default_for_uri') as file_handler:
                        report_button.emit('clicked')
                        lookup.assert_called_once_with('https')
                        browser.launch_uris.assert_called_once_with([Path(run['report']).as_uri()], None)
                        file_handler.assert_not_called()
                    with patch('gate.experiment_view.Gio.AppInfo.get_default_for_uri_scheme', return_value=None):
                        report_button.emit('clicked')
                        assert 'No default web browser' in view.status.get_text()
                    view.run_error = ''

                    # Historical result selection and refresh leave parameter edits alone.
                    view.parameters['load_stop_a'].set_value(.8)
                    window.refresh()
                    assert view.parameters['load_stop_a'].get_value() == .8
                    if os.environ.get('GATE_SCREENSHOT'):
                        GLib.timeout_add(250, screenshot)
                        phase = 'screenshot'
                    else:
                        begin_cancel()
                elif phase == 'cancel_start':
                    run = call(proxy(), 'Snapshot').get('experiment_run')
                    if not run or run['procedure'] != 'maximum_output_current' or run['state'] != 'running' or not view.cancel.get_sensitive():
                        return True
                    assert not view.start.get_sensitive()
                    view.cancel.emit('clicked')
                    phase = 'cancel_end'
                elif phase == 'cancel_end':
                    run = call(proxy(), 'Snapshot').get('experiment_run')
                    if run['state'] != 'cancelled':
                        return True
                    view.test_picker.set_active_id('capture')
                    assert 'capture_points' in view.parameters
                    assert not view.wiring.get_visible()
                    view.parameters['capture_points'].set_value(2)
                    view.start.emit('clicked')
                    phase = 'capture'
                elif phase == 'capture':
                    run = call(proxy(), 'Snapshot').get('experiment_run')
                    if not run or run['procedure'] != 'capture' or run['state'] == 'running':
                        return True
                    assert run['state'] == 'completed', run['error']
                    assert len(run['points']) == 2
                    assert 'supply.voltage' in run['metrics']
                    print('PASS: compatible equipment selection, parameters, wiring confirmation, live efficiency/chart/table, cancel, capture')
                    window.close()
                    loop.quit()
                    return False
            except Exception as exc:
                errors.append(exc)
                window.close()
                loop.quit()
                return False
            return True

        def begin_cancel():
            global phase
            view.test_picker.set_active_id('maximum_output_current')
            view.parameters['settle_s'].set_value(5.)
            view.wiring.set_active(True)
            view.start.emit('clicked')
            phase = 'cancel_start'

        def screenshot():
            try:
                from gi.repository import Graphene
                paintable = Gtk.WidgetPaintable.new(window)
                snapshot = Gtk.Snapshot.new()
                paintable.snapshot(snapshot, window.get_width(), window.get_height())
                rect = Graphene.Rect()
                rect.init(0, 0, window.get_width(), window.get_height())
                texture = window.get_native().get_renderer().render_texture(snapshot.to_node(), rect)
                texture.save_to_png(os.environ['GATE_SCREENSHOT'])
                # Also render the results section alone for visual inspection.
                paintable = Gtk.WidgetPaintable.new(view.results)
                snapshot = Gtk.Snapshot.new()
                paintable.snapshot(snapshot, view.results.get_width(), view.results.get_height())
                rect.init(0, 0, view.results.get_width(), view.results.get_height())
                texture = window.get_native().get_renderer().render_texture(snapshot.to_node(), rect)
                texture.save_to_png(os.environ['GATE_SCREENSHOT'] + '.results.png')
                begin_cancel()
            except Exception as exc:
                errors.append(exc)
                window.close()
                loop.quit()
            return False

        GLib.timeout_add(150, check)
        loop.run()
        if errors:
            raise errors[0]
    finally:
        process.terminate()
        process.wait(timeout=10)
