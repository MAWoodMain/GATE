import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from gate.engine import Engine
from gate.experiments import validate_definition
from gate.expressions import Expression
from gate.model import Reading
from gate.procedures import load_template
from gate.test_spec import validate_test, sweep_values
from gate.experiment_runs import plot_svg


def definition(spec, controlled=False):
    return dict(id='custom_lab', name='Custom lab', description='A test without converter-specific procedures.',
        dut=dict(name='DUT', terminals=['signal']),
        roles={'bias':dict(name='Bias source' if controlled else 'Probe',
            capabilities=['source.dc_voltage'] if controlled else ['measure.scalar'], terminals=['signal'])},
        connections=[['bias.signal', 'dut.signal']], tests={'custom_test':spec})


def metric(expression, aggregate='point'):
    return dict(label='Calculated value', unit='V', expression=expression, aggregate=aggregate)


class DeclarativeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.engine = Engine(.1, experiment_directory=self.directory.name)
        self.read = Mock(return_value=[Reading('sense', 3.3, 'V')])
        self.sensor = SimpleNamespace(id='sensor', name='Unrelated sensor', category='meter', simulated=False,
            experiment_capabilities=['measure.scalar'], read=self.read, close=lambda:None)
        self.engine.instruments['sensor'] = self.sensor

    def tearDown(self):
        self.engine.command('cancel_experiment', {})
        self.directory.cleanup()

    def run_test(self, spec, instrument='sensor', enable=False):
        item = definition(spec, enable)
        self.engine.command('save_experiment', dict(experiment=item))
        self.engine.command('start_experiment', dict(experiment=item['id'], test='custom_test',
            bindings={'bias':instrument}, enable_outputs=enable, wiring_confirmed=enable))
        run = self.engine.experiment_run
        for _ in range(50):
            if not run.active:
                break
            run.due = 0.
            self.engine.cycle()
        self.assertFalse(run.active, 'Run did not complete')
        return run

    def spec(self):
        spec = load_template('capture')
        spec['parameters']['capture_points']['default'] = 1
        spec['acquisition']['capture_all'] = False
        spec['measurements'] = {'signal_v':dict(label='Signal voltage', unit='V', role='bias', channel='sense', required=True)}
        return spec

    def test_passive_calculations_dependency_order_averaging_and_scatter_plot(self):
        spec = self.spec()
        spec['acquisition']['samples'] = 2
        spec['derived'] = {
            'doubled':metric('offset_v * 2'), 'offset_v':metric('signal_v + 1'),
            'mean_square':metric('signal_v ** 2', 'sample_mean'), 'square_mean':metric('signal_v ** 2')}
        spec['plots'] = [dict(kind='scatter', data='points', x='square_mean', y='mean_square', title='Averaging comparison', x_scale='log', y_scale='linear')]
        spec['table'] = dict(data='points', columns=['mean_square', 'square_mean', 'doubled'])
        self.read.side_effect = [[Reading('sense', 1., 'V')], [Reading('sense', 3., 'V')]]
        run = self.run_test(spec)
        self.assertEqual(run.metadata['state'], 'completed', run.metadata['error'])
        self.assertEqual(run.points[0]['mean_square'], 5.)
        self.assertEqual(run.points[0]['square_mean'], 4.)
        self.assertEqual(run.points[0]['doubled'], 6.)
        self.assertEqual(run.metadata['table']['columns'], ['mean_square', 'square_mean', 'doubled'])
        svg = (run.directory/'plot-0.svg').read_text()
        self.assertIn('Averaging comparison', svg)
        self.assertNotIn('stroke-width="2"', svg, 'Scatter must not connect points')
        self.assertEqual(run.metadata['test_spec'], spec)

    def test_generic_descending_output_sweep_with_one_role_and_custom_parameters(self):
        self.engine.command('demo_experiment', {})
        spec = self.spec()
        spec['parameters'] = {
            'from_v':dict(label='From', unit='V', default=3., min=0., max=10., step=.1, type='number'),
            'to_v':dict(label='To', unit='V', default=1., min=0., max=10., step=.1, type='number')}
        spec['axes'] = [dict(id='drive_v', label='Applied voltage', unit='V', role='bias', control='voltage', start='from_v', stop='to_v', step=-.7)]
        spec['outputs'] = ['bias']
        spec['limits'] = {'bias':dict(voltage='max(from_v, to_v)', current=1.)}
        spec['setup'] = [dict(role='bias', control='current_limit', value=1.)]
        spec['acquisition'].update(repeat=1, settle_s=.1, on_invalid='fail')
        spec['measurements']['signal_v'].pop('channel')
        spec['measurements']['signal_v']['quantity'] = 'voltage'
        spec['derived'] = {'error_v':metric('signal_v - drive_v')}
        spec['plots'] = [dict(kind='line', data='points', x='drive_v', y='signal_v', title='Voltage tracking')]
        run = self.run_test(spec, 'demo:converter_supply', True)
        self.assertEqual(run.metadata['state'], 'completed', run.metadata['error'])
        self.assertEqual([point['drive_v'] for point in run.points], [3., 2.3, 1.6, 1.])
        self.assertTrue(all(point['error_v'] == 0 for point in run.points))
        self.assertFalse(self.engine.instruments['demo:converter_supply'].enabled)

    def test_nested_value_axes_generic_group_stop_reducers_and_custom_markers(self):
        spec = self.spec()
        spec['acquisition']['repeat'] = 1
        spec['axes'] = [dict(id='batch', label='Batch', unit='', values=[1, 2]),
                        dict(id='gain', label='Gain setting', unit='', values=[3, 2, 1])]
        spec['derived'] = {'scaled':metric('signal_v / gain')}
        spec['stop_when'] = dict(expression='gain <= 2', scope='group')
        spec['reduce'] = dict(group_by=['batch'], where=True,
            metrics={'average':dict(label='Average', unit='V', expression='scaled', reducer='mean')},
            status=dict(field='ending', stopped='cut', exhausted='full'))
        spec['plots'] = [dict(kind='scatter', data='summary', x='batch', y='average', title='Grouped averages',
            markers=dict(field='ending', styles={'cut':dict(shape='square', label='Cut short')}))]
        spec['table']['data'] = 'summary'
        run = self.run_test(spec)
        self.assertEqual(run.metadata['state'], 'completed', run.metadata['error'])
        self.assertEqual(len(run.points), 4)
        self.assertEqual([point['gain'] for point in run.points], [3, 2, 3, 2])
        self.assertEqual([row['ending'] for row in run.summary], ['cut', 'cut'])
        self.assertAlmostEqual(run.summary[0]['average'], 1.375)
        self.assertIn('Cut short', (run.directory/'plot-0.svg').read_text())

    def test_division_by_zero_and_unavailable_inputs_propagate_null(self):
        spec = self.spec()
        spec['derived'] = {'undefined':metric('signal_v / 0')}
        run = self.run_test(spec)
        self.assertEqual(run.metadata['state'], 'completed')
        self.assertIsNone(run.points[0]['undefined'])
        self.assertEqual(run.points[0]['quality'], 'unavailable')
        self.assertIsNone(Expression('missing + 1', {'missing'}).evaluate({'missing':None}))
        self.assertIsNone(Expression('not missing', {'missing'}).evaluate({'missing':None}))
        self.assertEqual(Expression('coalesce(missing, 2)', {'missing'}).evaluate({'missing':None}), 2)

    def test_unknown_references_cycles_and_unsafe_syntax_are_rejected_on_save(self):
        invalid = ['signal_v.__class__', 'signal_v[0]', '__import__(1)', '[x for x in y]',
                   '(lambda: 1)()', 'open(1)', 'sum(1, 2)', 'missing + 1', 'abs', '"text"']
        for expression in invalid:
            spec = self.spec()
            spec['derived'] = {'bad':metric(expression)}
            with self.subTest(expression=expression), self.assertRaises(ValueError):
                validate_definition(definition(spec))
        spec = self.spec()
        spec['derived'] = {'a':metric('b + 1'), 'b':metric('a + 1')}
        with self.assertRaisesRegex(ValueError, 'cycle'):
            validate_definition(definition(spec))
        spec['derived'] = {'a':metric('b + 1', 'sample_mean'), 'b':metric('signal_v')}
        with self.assertRaisesRegex(ValueError, 'point calculations'):
            validate_definition(definition(spec))

    def test_invalid_schema_plot_refs_controls_and_grid_are_rejected(self):
        for change in ('version', 'axis_role', 'plot_column', 'unknown_control', 'unsafe_recording'):
            spec = self.spec()
            if change == 'version':
                spec['schema_version'] = 2
            elif change == 'plot_column':
                spec['plots'] = [dict(kind='line', data='points', x='missing', y='signal_v', title='Wrong')]
            elif change == 'axis_role':
                spec['axes'] = [dict(id='level', label='Level', unit='V', role='missing', control='voltage', values=[1])]
            elif change == 'unknown_control':
                spec['outputs'] = ['bias']
                spec['limits'] = {'bias':dict(voltage=3, current=1)}
                spec['setup'] = [dict(role='bias', control='raw_scpi', value=1)]
            else:
                spec['outputs'] = ['bias']
                spec['limits'] = {'bias':dict(voltage=3, current=1)}
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_definition(definition(spec))
        spec = self.spec()
        spec['parameters']['capture_points']['default'] = 1000
        spec['axes'] = [dict(id='gain', label='Gain', unit='', values=[1, 2])]
        self.engine.command('save_experiment', dict(experiment=definition(spec)))
        with self.assertRaises(ValueError):
            self.engine.command('start_experiment', dict(experiment='custom_lab', test='custom_test', bindings={'bias':'sensor'}))
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    def test_missing_readback_fails_and_custom_check_stops(self):
        spec = self.spec()
        spec['acquisition']['on_invalid'] = 'fail'
        self.read.return_value = [Reading('sense', None, 'V', 'unavailable')]
        run = self.run_test(spec)
        self.assertEqual(run.metadata['state'], 'failed')
        self.assertIsNone(run.points[0]['signal_v'])
        self.read.return_value = [Reading('sense', 3.3, 'V')]
        spec['checks'] = [dict(stage='point', expression='signal_v < 2', message='Custom threshold exceeded')]
        run = self.run_test(spec)
        self.assertEqual(run.metadata['state'], 'failed')
        self.assertEqual(run.metadata['error'], 'Custom threshold exceeded')

    def test_record_only_missing_data_keeps_requested_average_window(self):
        spec = self.spec()
        spec['acquisition']['samples'] = 2
        self.read.side_effect = [[Reading('sense', None, 'V', 'unavailable')], [Reading('sense', 3., 'V')]]
        run = self.run_test(spec)
        self.assertEqual(run.metadata['state'], 'completed')
        self.assertEqual(self.read.call_count, 2)
        self.assertIsNone(run.points[0]['signal_v'])
        self.assertEqual(run.points[0]['quality'], 'invalid')

    def test_legacy_procedure_migrates_to_json_with_role_aliases(self):
        from gate.experiments import BUCK_CONVERTER
        item = copy.deepcopy(BUCK_CONVERTER)
        item['id'] = 'legacy_example'
        item['roles'] = {'source':item['roles']['supply'], 'sink':item['roles']['load']}
        item['connections'] = [[endpoint.replace('supply.', 'source.').replace('load.', 'sink.') for endpoint in wire] for wire in item['connections']]
        item['tests'] = {'efficiency':dict(description='Legacy efficiency', procedure='efficiency')}
        migrated = validate_definition(item)
        self.assertEqual(migrated['tests']['efficiency']['schema_version'], 1)
        self.assertEqual(migrated['tests']['efficiency']['outputs'], ['source', 'sink'])
        self.engine.command('save_experiment', dict(experiment=item))
        self.engine.command('demo_experiment', {})
        self.engine.command('start_experiment', dict(experiment=item['id'], test='efficiency',
            bindings={'source':'demo:converter_supply', 'sink':'demo:electronic_load'},
            parameters={'input_start_v':8., 'input_stop_v':8., 'load_stop_a':.1, 'settle_s':.1}, enable_outputs=True, wiring_confirmed=True))
        run = self.engine.experiment_run
        run.due = 0.
        self.engine.cycle()
        self.assertEqual(run.metadata['state'], 'completed', run.metadata['error'])
        self.assertAlmostEqual(run.points[0]['efficiency_pct'], 88.3)

    def test_grid_roundoff_and_extreme_counts_are_handled(self):
        values = sweep_values(0., .1+.2, .1)
        self.assertEqual(len(values), 4)
        self.assertEqual(values[-1], .1+.2)
        self.assertEqual(len(set(values)), 4)
        with self.assertRaises(ValueError):
            sweep_values(-1e300, 1e300, 1e-300)

    def test_log_scale_filters_nonpositive_points_and_line_breaks_missing_data(self):
        spec = dict(kind='line', x='x', y='y', series=None, title='Plot', x_label='x', y_label='y', x_scale='log', y_scale='linear')
        svg = plot_svg(spec, [dict(x=0, y=2), dict(x=1, y=2), dict(x=2, y=None), dict(x=10, y=4)])
        self.assertEqual(svg.count('<circle'), 2)
        self.assertIn(' M', svg, 'Line must restart after unavailable values')
        self.assertNotIn(' V input', svg)
