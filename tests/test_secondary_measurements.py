import copy
import csv
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock
from gate.engine import Engine
from gate.experiments import BUCK_CONVERTER
from gate.test_spec import validate_test


class SecondaryTests(unittest.TestCase):
    def test_temperature_defaults_are_celsius_in_templates_and_demo(self):
        import json
        from gate.model import DEFAULT_TEMPERATURE_UNIT
        from gate.secondary import add_observations
        from gate.procedures import load_template
        self.assertEqual(DEFAULT_TEMPERATURE_UNIT, '°C')
        for path in (Path(__file__).parents[1]/'gate/templates').glob('*-with-observations.json'):
            spec = json.loads(path.read_text())
            self.assertTrue(all(metric['unit'] == DEFAULT_TEMPERATURE_UNIT for metric in spec['secondary_measurements'].values()))
            self.assertEqual(spec['derived']['temperature_rise_c']['unit'], DEFAULT_TEMPERATURE_UNIT)
        spec = load_template('capture')
        add_observations(spec, thermal_role='thermal', scope_role=None, ripple=False)
        self.assertTrue(all(metric['unit'] == DEFAULT_TEMPERATURE_UNIT for metric in spec['secondary_measurements'].values()))
        probe = self.engine.instruments['demo:temperatures']
        self.assertTrue(all(reading.unit == DEFAULT_TEMPERATURE_UNIT for reading in probe.read()))
        self.assertTrue(all(function.unit == DEFAULT_TEMPERATURE_UNIT for function in probe.functions()))

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.engine = Engine(.1, experiment_directory=self.folder.name)
        self.engine.command('demo_experiment', {})
        self.bindings = dict(supply='demo:converter_supply', load='demo:electronic_load',
                             thermal='demo:temperatures', scope='demo:waveforms')
        self.args = dict(experiment='buck_converter', test='efficiency', bindings=self.bindings,
            parameters=dict(input_start_v=8., input_stop_v=8., load_start_a=.5, load_stop_a=.5, settle_s=.1),
            enable_outputs=True, wiring_confirmed=True)
    def tearDown(self):
        self.engine.command('cancel_experiment', {})
        self.folder.cleanup()
    def start(self):
        self.engine.command('start_experiment', self.args)
        return self.engine.experiment_run
    def tick(self):
        self.engine.experiment_run.due = 0.
        self.engine.cycle()
    def finish(self):
        for _ in range(20):
            if not self.engine.experiment_run.active:
                return self.engine.experiment_run
            self.tick()
        self.fail('Run did not complete')
    def test_temperatures_waveforms_statistics_and_full_resolution_exports(self):
        run = self.start()
        self.finish()
        self.assertEqual(run.metadata['state'], 'completed', run.metadata['error'])
        point = run.points[0]
        self.assertEqual(point['quality'], 'ok')
        self.assertEqual(point['secondary_quality'], 'ok')
        self.assertGreater(point['dut_temperature_c'], point['ambient_temperature_c'])
        self.assertAlmostEqual(point['temperature_rise_c'], point['dut_temperature_c']-point['ambient_temperature_c'])
        self.assertAlmostEqual(point['ripple_peak_to_peak'], .0125, places=6)
        self.assertGreater(point['ripple_rms'], point['ripple_ac_rms'])
        self.assertAlmostEqual(point['ripple_mean'], 5., places=4)
        waveform = run.metadata['waveforms'][0]
        self.assertEqual(waveform['point_index'], 0)
        self.assertEqual(waveform['setpoints']['input_voltage_set_v'], 8.)
        self.assertEqual(waveform['quality'], 'ok')
        self.assertTrue(Path(waveform['plot']).is_file())
        with Path(waveform['csv']).open() as stream:
            self.assertEqual(len(list(csv.reader(stream))), 2049)
        with closing(sqlite3.connect(run.directory/'run.sqlite')) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM waveform_samples').fetchone()[0], 2048)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM secondary_samples').fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT DISTINCT location FROM secondary_samples ORDER BY location').fetchall(), [('Ambient',), ('DUT',)])
        self.assertIn('Full-resolution waveform CSV', Path(run.metadata['report']).read_text())

    def test_screen_artifact_and_channel_lifecycle_are_saved_with_capture(self):
        from dataclasses import replace
        from tests.test_waveform_drivers import screen_bitmap
        recorder = self.engine.instruments['demo:waveforms']
        recorder.begin_experiment_channels = Mock()
        recorder.end_experiment_channels = Mock()
        original = recorder.poll_waveform
        recorder.poll_waveform = lambda token: replace(original(token), screenshot=screen_bitmap())
        definition = copy.deepcopy(BUCK_CONVERTER)
        definition['tests']['efficiency']['waveforms']['ripple']['screenshot'] = True
        self.engine.command('save_experiment', dict(experiment=definition))
        run = self.start()
        recorder.begin_experiment_channels.assert_called_once_with({'C1'})
        self.finish()
        recorder.end_experiment_channels.assert_called_once()
        capture = run.metadata['waveforms'][0]
        self.assertEqual(Path(capture['screenshot']).read_bytes(), screen_bitmap())
        self.assertIn('Scope screenshot from this capture', Path(run.metadata['report']).read_text())
        with closing(sqlite3.connect(run.directory/'run.sqlite')) as db:
            import json
            stored = json.loads(db.execute('SELECT metadata FROM waveforms').fetchone()[0])
            self.assertEqual(stored['screenshot'], capture['screenshot'])

    def test_partial_channel_setup_is_restored_before_run_fails(self):
        recorder = self.engine.instruments['demo:waveforms']
        recorder.begin_experiment_channels = Mock(side_effect=ValueError('Channel verification failed'))
        recorder.end_experiment_channels = Mock()
        run = self.start()
        self.assertEqual(run.metadata['state'], 'failed')
        recorder.end_experiment_channels.assert_called_once()
        self.assertFalse(self.engine.instruments['demo:converter_supply'].enabled)
    def test_unassigned_observations_do_not_block_main_test_or_fabricate_zero(self):
        self.args['bindings'] = {key:value for key,value in self.bindings.items() if key in ('supply','load')}
        run = self.start()
        self.finish()
        self.assertEqual(run.metadata['state'], 'completed')
        self.assertEqual(run.points[0]['quality'], 'ok')
        self.assertEqual(run.points[0]['secondary_quality'], 'unavailable')
        self.assertIsNone(run.points[0]['dut_temperature_c'])
        self.assertIsNone(run.points[0]['ripple_peak_to_peak'])
        self.assertEqual(run.metadata['waveforms'][0]['quality'], 'unassigned')
    def test_required_temperature_or_waveform_is_validated_before_enable(self):
        definition = copy.deepcopy(BUCK_CONVERTER)
        definition['tests']['efficiency']['secondary_measurements']['dut_temperature_c']['required'] = True
        self.engine.command('save_experiment', dict(experiment=definition))
        self.args['bindings'] = {'supply':self.bindings['supply'], 'load':self.bindings['load']}
        with self.assertRaisesRegex(ValueError, 'thermal'):
            self.start()
        self.assertFalse(self.engine.instruments['demo:converter_supply'].enabled)
        self.assertEqual(list(Path(self.folder.name).iterdir()), [])
        definition['tests']['efficiency']['secondary_measurements']['dut_temperature_c']['required'] = False
        definition['tests']['efficiency']['waveforms']['ripple']['required'] = True
        self.engine.command('save_experiment', dict(experiment=definition))
        with self.assertRaisesRegex(ValueError, 'scope'):
            self.start()
    def test_optional_sensor_disconnect_preserves_primary_results(self):
        self.engine.instruments['demo:temperatures'].read = Mock(side_effect=OSError('Probe unplugged'))
        run = self.start()
        self.finish()
        self.assertEqual(run.metadata['state'], 'completed')
        self.assertEqual(run.points[0]['quality'], 'ok')
        self.assertIsNone(run.points[0]['ambient_temperature_c'])
        self.assertEqual(run.points[0]['ripple_status'], 'ok')
    def test_optional_timeout_and_required_timeout_have_different_outcomes(self):
        recorder = self.engine.instruments['demo:waveforms']
        recorder.poll_waveform = Mock(return_value=None)
        run = self.start()
        self.tick()
        self.assertIsNotNone(run.pending_point)
        run.captures.pending[0]['deadline'] = 0.
        self.tick()
        self.assertEqual(run.metadata['state'], 'completed')
        self.assertEqual(run.points[0]['ripple_status'], 'timeout')
        definition = copy.deepcopy(BUCK_CONVERTER)
        definition['tests']['efficiency']['waveforms']['ripple']['required'] = True
        self.engine.command('save_experiment', dict(experiment=definition))
        run = self.start()
        self.tick()
        run.captures.pending[0]['deadline'] = 0.
        self.tick()
        self.assertEqual(run.metadata['state'], 'failed')
        self.assertIn('deadline', run.metadata['error'])
        self.assertFalse(self.engine.instruments['demo:converter_supply'].enabled)
    def test_cancel_cleans_armed_capture_and_disables_outputs(self):
        recorder = self.engine.instruments['demo:waveforms']
        recorder.poll_waveform = Mock(return_value=None)
        run = self.start()
        self.tick()
        self.assertTrue(recorder.pending)
        self.engine.command('cancel_experiment', {})
        self.assertEqual(run.metadata['state'], 'cancelled')
        self.assertFalse(recorder.pending)
        self.assertFalse(self.engine.instruments['demo:electronic_load'].enabled)
        self.assertEqual(run.metadata['waveforms'][0]['quality'], 'cancelled')
    def test_protection_is_armed_before_change_and_read_during_settling(self):
        self.args['test'] = 'maximum_output_current'
        self.args['parameters'].update(load_start_a=3., load_stop_a=4., load_step_a=.5, settle_s=5.)
        scope = self.engine.instruments['demo:waveforms']
        original = scope.begin_waveform
        armed_loads = []
        def begin(request):
            if request['phase'] == 'transition':
                armed_loads.append(self.engine.instruments['demo:electronic_load'].current)
            return original(request)
        scope.begin_waveform = begin
        run = self.start()
        self.tick()  # 3 A accepted; next point arms before applying 3.5 A.
        self.assertEqual(armed_loads, [0.])
        self.engine.cycle()  # Still settling, but a latched trigger is retrieved now.
        captured = [item for item in run.metadata['waveforms'] if item['measurement']=='overcurrent']
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]['quality'], 'ok')
        self.assertLess(captured[0]['start_time_s'], 0.)
        self.assertIsNotNone(captured[0]['trigger_utc_ns'])
        self.finish()
        self.assertEqual(run.metadata['state'], 'completed', run.metadata['error'])
        self.assertEqual(run.summary[0]['maximum_current_a'], 3.)
    def test_temperature_samples_continue_through_settling(self):
        run = self.start()
        run.due = 1e20
        for _ in range(3):
            self.engine.cycle()
        self.assertEqual(len(run.points), 0)
        count = run.db.execute('SELECT COUNT(*) FROM secondary_samples').fetchone()[0]
        self.assertEqual(count, 6)
        self.tick()
        self.assertEqual(run.metadata['state'], 'completed')
        with Path(run.metadata['secondary_csv']).open() as stream:
            self.assertEqual(len(list(csv.reader(stream))), 9)
        self.assertTrue(any(plot['data']=='secondary' for plot in run.metadata['presentation']))

    def test_continuous_temperature_check_can_stop_during_settling(self):
        definition = copy.deepcopy(BUCK_CONVERTER)
        definition['tests']['efficiency']['checks'].append(dict(stage='continuous', expression='dut_temperature_c < 20', message='DUT thermal limit reached'))
        self.engine.command('save_experiment', dict(experiment=definition))
        run = self.start()
        run.due = 1e20
        self.engine.cycle()
        self.assertEqual(run.metadata['state'], 'failed')
        self.assertEqual(run.metadata['error'], 'DUT thermal limit reached')
        self.assertEqual(len(run.points), 0)
        self.assertFalse(self.engine.instruments['demo:converter_supply'].enabled)

    def test_capture_limits_and_invalid_temperature_units_are_rejected(self):
        definition = copy.deepcopy(BUCK_CONVERTER)
        spec = definition['tests']['efficiency']
        spec['secondary_measurements']['dut_temperature_c']['unit'] = 'V'
        with self.assertRaises(ValueError):
            validate_test(spec, definition['roles'])
        definition = copy.deepcopy(BUCK_CONVERTER)
        definition['tests']['efficiency']['waveforms']['ripple']['points'] = 20000
        self.engine.command('save_experiment', dict(experiment=definition))
        with self.assertRaises(ValueError):
            self.start()
        self.assertFalse(self.engine.instruments['demo:converter_supply'].enabled)
