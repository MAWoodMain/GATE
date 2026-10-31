"""GTK run setup, live execution, output charts and cancellation; private session bus."""
import copy
import json
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
from gate.procedures import load_template

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
        definition['roles'] = {key:role for key,role in definition['roles'].items() if not role.get('optional', False)}
        definition['tests'] = {key:load_template(key) for key in definition['tests']}
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
        dialogs = []

        def check():
            global phase, ticks, view
            ticks += 1
            try:
                assert time.monotonic()-started < 35, 'Run UI test timed out at ' + phase
                if not window.online:
                    return True
                view = window.experiment_views['buck_converter']
                if phase == 'equipment':
                    if 'capture' not in view.definition['tests']:
                        return True
                    window.navigation.select_row(window.experiment_rows['buck_converter'])
                    assert [view.tabs.get_tab_label_text(view.tabs.get_nth_page(index))
                            for index in range(view.tabs.get_n_pages())] == ['Summary', 'Instruments', 'Parameters', 'Run', 'Results']
                    assert view.tabs.get_current_page() == view.tabs.page_num(view.summary_page)
                    assert view.demo.is_ancestor(view.instrument_page)
                    assert view.test_picker.is_ancestor(view.parameter_page)
                    assert view.parameter_rows.is_ancestor(view.parameter_page)
                    assert view.start.is_ancestor(view.run_page)
                    assert view.status.is_ancestor(view.run_page)
                    assert view.metrics.is_ancestor(view.summary_page)
                    assert not view.start.get_sensitive()
                    dialog = view.edit_specification()
                    dialogs.append(dialog)
                    content = dialog.get_content_area()
                    editor = content.get_first_child().get_next_sibling().get_child()
                    editor.get_buffer().set_text('{invalid JSON')
                    dialog.response(Gtk.ResponseType.ACCEPT)
                    assert content.get_last_child().get_text()
                    spec = view.current_spec()
                    spec['parameters']['calibration_factor'] = dict(label='Calibration factor', unit='', default=1., min=.1, max=10., step=.1, type='number')
                    spec['derived']['scaled_output_v'] = dict(label='Scaled output', unit='V', expression='output_voltage_v * calibration_factor', aggregate='point')
                    spec['plots'].append(dict(kind='scatter', data='points', x='output_current_a', y='scaled_output_v', title='Scaled output'))
                    editor.get_buffer().set_text(json.dumps(spec))
                    dialog.response(Gtk.ResponseType.ACCEPT)
                    phase = 'spec_saved'
                elif phase == 'spec_saved':
                    if 'calibration_factor' not in view.parameters:
                        return True
                    assert view.parameters['calibration_factor'].get_value() == 1.
                    view.parameters['input_start_v'].set_value(9.)
                    dialog = view.add_secondary_measurements()
                    dialogs.append(dialog)
                    child = dialog.get_content_area().get_first_child()
                    while child:
                        if isinstance(child, Gtk.CheckButton) and child.get_label() == 'Apply to every test in this experiment':
                            child.set_active(True)
                        child = child.get_next_sibling()
                    dialog.response(Gtk.ResponseType.ACCEPT)
                    phase = 'observations_saved'
                elif phase == 'observations_saved':
                    if 'thermal' not in view.pickers:
                        return True
                    assert view.parameters['input_start_v'].get_value() == 9., 'Adding observers must preserve main parameters'
                    view.tabs.set_current_page(view.tabs.page_num(view.instrument_page))
                    view.demo.emit('clicked')
                    phase = 'ready'
                elif phase == 'ready':
                    if view.pickers['supply'].get_active_id() != 'demo:converter_supply' or view.pickers['load'].get_active_id() != 'demo:electronic_load':
                        return True
                    view.tabs.set_current_page(view.tabs.page_num(view.parameter_page))
                    view.parameters['input_start_v'].set_value(8.)
                    view.parameters['input_stop_v'].set_value(8.)
                    view.parameters['load_stop_a'].set_value(.1)
                    view.parameters['settle_s'].set_value(.1)
                    view.parameters['samples_per_point'].set_value(2)
                    view.test_picker.set_active_id('maximum_output_current')
                    view.parameters['input_start_v'].set_value(10.)
                    view.parameters['input_stop_v'].set_value(10.)
                    view.parameters['load_stop_a'].set_value(.1)
                    view.parameters['settle_s'].set_value(.1)
                    view.test_picker.set_active_id('efficiency')
                    assert view.parameters['samples_per_point'].get_value() == 2
                    assert view.parameters['input_start_v'].get_value() == 8.
                    view.test_checks['maximum_output_current'].set_active(True)
                    assert not view.start.get_sensitive(), 'Output sweeps require wiring confirmation'
                    view.tabs.set_current_page(view.tabs.page_num(view.run_page))
                    assert 'SIMULATED' in view.run_selection.get_text()
                    view.wiring.set_active(True)
                    assert view.start.get_sensitive()
                    view.start.emit('clicked')
                    phase = 'efficiency'
                elif phase == 'efficiency':
                    state = call(proxy(), 'Snapshot')
                    run = state.get('experiment_run')
                    sequence = state.get('experiment_sequence')
                    if not sequence or sequence['state'] != 'completed' or not run or run['state'] == 'running' or not view.last_result or view.last_result[:2] != (run['id'], 'completed'):
                        return True
                    assert run['state'] == 'completed', run['error']
                    assert view.tabs.get_current_page() == view.tabs.page_num(view.result_page)
                    assert len(run['points']) == 1
                    assert sequence['completed_tests'] == 2
                    assert [item['test'] for item in sequence['tests']] == ['efficiency', 'maximum_output_current']
                    assert all(item['state'] == 'completed' for item in sequence['tests'])
                    assert run['parameters']['input_start_v'] == 10.
                    first = next(item for item in state['experiment_runs'] if item['id'] == sequence['tests'][0]['run_id'])
                    assert first['parameters']['input_start_v'] == 8.
                    assert first['points'][0]['scaled_output_v'] == 5.
                    assert first['points'][0]['dut_temperature_c'] > first['points'][0]['ambient_temperature_c']
                    assert first['points'][0]['ripple_peak_to_peak'] > 0
                    assert first['waveforms'][0]['quality'] == 'ok'
                    assert len(first['presentation']) >= 2
                    assert Path(run['report']).is_file() and Path(run['csv']).is_file()
                    assert 'SIMULATED' in view.results.get_first_child().get_text()
                    assert isinstance(view.results.get_first_child().get_next_sibling(), Gtk.Picture)
                    assert 'efficiency' in view.metrics.get_text().lower()
                    child = view.results.get_first_child()
                    while child and not (isinstance(child, Gtk.Label) and child.get_text() == 'Secondary Waveforms'):
                        child = child.get_next_sibling()
                    assert child is not None
                    wave_picker = child.get_next_sibling()
                    assert isinstance(wave_picker, Gtk.ComboBoxText)
                    assert wave_picker.get_active_id() == '0'
                    assert isinstance(wave_picker.get_next_sibling().get_next_sibling(), Gtk.Picture)
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
                    if not run or run['test'] != 'maximum_output_current' or run['state'] != 'running' or not view.cancel.get_sensitive():
                        return True
                    assert not view.start.get_sensitive()
                    assert all(not check.get_sensitive() for check in view.test_checks.values())
                    view.cancel.emit('clicked')
                    phase = 'cancel_end'
                elif phase == 'cancel_end':
                    run = call(proxy(), 'Snapshot').get('experiment_run')
                    if run['state'] != 'cancelled' or (view.state.get('experiment_sequence') or {}).get('state') != 'cancelled':
                        return True
                    assert all(check.get_sensitive() for check in view.test_checks.values())
                    assert call(proxy(), 'Snapshot')['experiment_sequence']['tests'][1]['state'] == 'skipped'
                    view.test_picker.set_active_id('capture')
                    for identity, check in view.test_checks.items():
                        check.set_active(identity == 'capture')
                    assert 'capture_points' in view.parameters
                    assert not view.wiring.get_visible()
                    view.parameters['capture_points'].set_value(2)
                    view.start.emit('clicked')
                    phase = 'capture'
                elif phase == 'capture':
                    run = call(proxy(), 'Snapshot').get('experiment_run')
                    if not run or run['test'] != 'capture' or run['state'] == 'running':
                        return True
                    assert run['state'] == 'completed', run['error']
                    assert len(run['points']) == 2
                    assert 'supply.voltage' in run['metrics']
                    # A screen image returned by a driver is decoded and shown
                    # separately from the reconstructed waveform plot.
                    from tests.test_waveform_drivers import screen_bitmap
                    image_path = Path(folder)/'scope-screen.bmp'
                    image_path.write_bytes(screen_bitmap())
                    screen_result = copy.deepcopy(next(item for item in call(proxy(), 'Snapshot')['experiment_runs']
                        if item['test'] == 'efficiency' and item['state'] == 'completed'))
                    screen_result['waveforms'][0]['screenshot'] = str(image_path)
                    view.waveform_results(screen_result)
                    open_screen = view.results.get_last_child().get_prev_sibling()
                    assert open_screen.get_label() == 'Open Scope Screenshot'
                    assert open_screen.get_visible()
                    screen = open_screen.get_prev_sibling()
                    assert isinstance(screen, Gtk.Picture) and screen.get_paintable() is not None
                    print('PASS: test checkboxes, sequential execution with separate parameters/results, queue cancellation, capture and report launching')
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
            for identity, check in view.test_checks.items():
                check.set_active(identity in ('maximum_output_current', 'capture'))
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
