import copy
import csv
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from gate.engine import Engine
from gate.experiments import BUCK_CONVERTER
from gate.experiment_runs import history
from gate.model import Reading
from gate.procedures import parameters, sweep_values


class ExperimentRunTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.engine = Engine(.1, experiment_directory=self.folder.name)
        self.engine.command('demo_experiment', {})
        self.supply = self.engine.instruments['demo:converter_supply']
        self.load = self.engine.instruments['demo:electronic_load']
        self.args = dict(experiment='buck_converter', test='efficiency',
            bindings={'supply':self.supply.id, 'load':self.load.id},
            parameters=dict(input_start_v=8., input_stop_v=10., input_step_v=2.,
                            load_start_a=.1, load_stop_a=1., load_step_a=.6, settle_s=.1),
            enable_outputs=True, wiring_confirmed=True)

    def tearDown(self):
        self.engine.command('cancel_experiment', {})
        self.folder.cleanup()

    def start(self):
        self.engine.command('start_experiment', self.args)
        return self.engine.experiment_run

    def tick(self):
        # Advance to the scheduled sample window without real sleeps.
        run = self.engine.experiment_run
        run.due = 0.
        self.engine.cycle()

    def finish(self):
        for _ in range(50):
            if not self.engine.experiment_run.active:
                return self.engine.experiment_run
            self.tick()
        self.fail('Run did not finish')

    def test_efficiency_grid_averaging_artifacts_and_restart_history(self):
        self.args['parameters']['samples_per_point'] = 2
        run = self.start()
        self.assertTrue(self.supply.enabled and self.load.enabled)
        self.tick()
        self.assertEqual(len(run.points), 0)
        self.finish()
        self.assertEqual(run.metadata['state'], 'completed', run.metadata['error'])
        self.assertEqual(len(run.points), 6)
        self.assertEqual([point['load_current_set_a'] for point in run.points[:3]], [.1, .7, 1.])
        for point in run.points:
            self.assertAlmostEqual(point['efficiency_pct'], 100*(.88+.03*min(point['output_current_a'], 1.)))
        self.assertFalse(self.supply.enabled or self.load.enabled)
        with sqlite3.connect(run.directory/'run.sqlite') as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM samples').fetchone()[0], 6*2*6)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM points').fetchone()[0], 6)
            self.assertTrue(all(start <= end for start, end in db.execute('SELECT read_start_ns,read_end_ns FROM samples')))
            stored = json.loads(db.execute('SELECT metadata FROM run').fetchone()[0])
            self.assertEqual(stored['definition'], BUCK_CONVERTER)
        with (run.directory/'points.csv').open() as stream:
            self.assertEqual(len(list(csv.DictReader(stream))), 6)
        report = (run.directory/'report.html').read_text()
        self.assertIn('SIMULATED', report)
        self.assertIn('Efficiency versus output current', report)
        self.assertTrue((run.directory/'plot-0.svg').is_file())
        self.assertEqual(history(self.folder.name)[0]['state'], 'completed')
        restored = Engine(.1, experiment_directory=self.folder.name)
        self.assertEqual(restored.snapshot()['experiment_runs'][0]['id'], run.metadata['id'])
        self.assertIsNone(restored.experiment_run)

    def test_invalid_parameters_bindings_limits_and_unarmed_runs_never_enable_outputs(self):
        for change in (
            {'enable_outputs':False}, {'wiring_confirmed':False},
            {'parameters':{'load_stop_a':6.}}, {'parameters':{'input_stop_v':31.}},
            {'parameters':{'settle_s':.01}}, {'parameters':{'samples_per_point':1.5}},
            {'parameters':{'input_stop_v':float('nan')}}, {'parameters':{'load_step_a':0}},
            {'parameters':{'input_start_v':20., 'input_stop_v':10.}},
            {'bindings':{'supply':self.supply.id, 'load':self.supply.id}},
        ):
            with self.subTest(change=change):
                original = self.args
                self.args = dict(self.args, **change)
                with self.assertRaises(ValueError):
                    self.start()
                self.assertFalse(self.supply.enabled or self.load.enabled)
                self.args = original
        self.assertEqual(list(Path(self.folder.name).iterdir()), [])
        self.supply.experiment_control_supported = False
        with self.assertRaisesRegex(ValueError, 'verified'):
            self.start()

    def test_max_current_threshold_and_sweep_limit_are_distinguished(self):
        self.args['test'] = 'maximum_output_current'
        self.args['parameters'].update(load_start_a=0., load_stop_a=4., load_step_a=.5)
        run = self.start()
        self.finish()
        self.assertEqual(run.metadata['state'], 'completed', run.metadata['error'])
        self.assertEqual(len(run.summary), 2)
        for row in run.summary:
            self.assertEqual(row['boundary'], 'threshold_reached')
            self.assertEqual(row['maximum_current_a'], 3.)
        self.assertEqual(run.metadata['presentation'][0]['y'], 'maximum_current_a')
        self.args['parameters']['load_stop_a'] = 1.
        run = self.start()
        self.finish()
        self.assertTrue(all(row['boundary'] == 'at_least_sweep_limit' for row in run.summary))
        self.assertTrue(all(row['maximum_current_a'] == 1. for row in run.summary))

    def test_first_point_out_of_regulation_has_no_fabricated_current(self):
        self.args['test'] = 'maximum_output_current'
        self.args['parameters'].update(input_start_v=4., input_stop_v=4.)
        run = self.start()
        self.finish()
        self.assertEqual(run.metadata['state'], 'completed')
        self.assertIsNone(run.summary[0]['maximum_current_a'])
        self.assertEqual(run.summary[0]['boundary'], 'threshold_reached')

    def test_cancel_configuration_change_and_shutdown_disable_both_outputs(self):
        run = self.start()
        with self.assertRaisesRegex(ValueError, 'Stop the active'):
            self.engine.command('static', dict(instrument=self.supply.id, voltage=5.))
        self.engine.command('cancel_experiment', {})
        self.assertEqual(run.metadata['state'], 'cancelled')
        self.assertFalse(self.supply.enabled or self.load.enabled)
        run = self.start()
        self.engine.command('disconnect', dict(instrument=self.load.id))
        self.assertEqual(run.metadata['state'], 'cancelled')
        self.assertFalse(self.supply.enabled or self.load.enabled)
        self.engine.command('demo_experiment', {})
        run = self.start()
        self.engine.stop_event.set()
        self.engine.run()
        self.assertEqual(run.metadata['state'], 'cancelled')
        self.assertFalse(any(device.enabled for device in run.devices.values()))

    def test_invalid_reading_is_null_and_stops_outputs(self):
        self.args['parameters']['samples_per_point'] = 100
        run = self.start()
        self.load.read = lambda: [Reading('voltage', None, 'V', 'unavailable'), Reading('current', .1, 'A')]
        self.tick()
        self.assertEqual(run.metadata['state'], 'failed')
        self.assertIsNone(run.points[0]['efficiency_pct'])
        self.assertIsNone(run.points[0]['output_voltage_v'])
        self.assertFalse(self.supply.enabled or self.load.enabled)

    def test_read_failure_and_disable_failure_are_reported(self):
        run = self.start()
        self.load.read = Mock(side_effect=OSError('Disconnected'))
        self.tick()
        self.assertEqual(run.metadata['state'], 'failed')
        self.assertFalse(self.supply.enabled or self.load.enabled)
        self.engine.command('demo_experiment', {})
        self.supply = self.engine.instruments['demo:converter_supply']
        self.load = self.engine.instruments['demo:electronic_load']
        run = self.start()
        self.load.set_enabled = Mock(side_effect=OSError('Disable failed'))
        self.engine.command('cancel_experiment', {})
        self.assertFalse(self.supply.enabled)
        self.assertEqual(run.metadata['state'], 'failed')
        self.assertFalse(run.metadata['output_disable_confirmed'])
        self.assertIn('Disable failed', run.metadata['error'])

    def test_capture_auto_metrics_and_missing_values_without_output_changes(self):
        definition = copy.deepcopy(BUCK_CONVERTER)
        definition['tests'] = {'capture':dict(description='Record current readings', procedure='capture')}
        self.engine.command('save_experiment', dict(experiment=definition))
        self.args.update(test='capture', parameters={'capture_points':2}, enable_outputs=False, wiring_confirmed=False)
        self.load.read = lambda: [Reading('voltage', None, 'V', 'unavailable'), Reading('current', .2, 'A')]
        run = self.start()
        self.finish()
        self.assertEqual(run.metadata['state'], 'completed')
        self.assertFalse(self.supply.enabled or self.load.enabled)
        self.assertIn('load.voltage', run.metadata['metrics'])
        self.assertIsNone(run.points[0]['load.voltage'])
        self.assertEqual(run.points[0]['quality'], 'unavailable')
        self.assertGreater(len(run.metadata['plots']), 1)

    def test_settling_window_and_single_sample_overcurrent_stop(self):
        self.args['parameters']['samples_per_point'] = 100
        run = self.start()
        run.due = 1e20
        self.engine.cycle()
        self.assertEqual(len(run.points), 0)
        self.assertTrue(run.active)
        self.supply.read = lambda: [Reading('voltage', 8., 'V'), Reading('current', 4., 'A')]
        self.tick()
        self.assertFalse(run.active)
        self.assertIn('current exceeded', run.metadata['error'])
        self.assertFalse(self.supply.enabled or self.load.enabled)

    def test_parameter_bounds_and_endpoint_clamping(self):
        self.assertEqual(sweep_values(1., 2., .6), [1., 1.6, 2.])
        with self.assertRaises(ValueError):
            parameters('capture', {'capture_points':True}, .1)
        with self.assertRaises(ValueError):
            parameters('efficiency', {'input_step_v':.001, 'input_stop_v':1000.}, .1)
