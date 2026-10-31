import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from gate.engine import Engine
from gate.experiments import BUCK_CONVERTER, validate_definition


class ExperimentTests(unittest.TestCase):
    def test_definition_is_model_independent_and_wires_through_dut(self):
        definition = validate_definition(BUCK_CONVERTER)
        self.assertEqual({key for key, role in definition['roles'].items() if not role.get('optional', False)}, {'supply', 'load'})
        self.assertTrue(all(any(endpoint.startswith('dut.') for endpoint in wire)
                            for wire in definition['connections']))
        definition['roles']['supply']['name'] = 'Changed'
        self.assertNotEqual(definition, BUCK_CONVERTER)

    def test_invalid_topology_and_roles(self):
        for mutate in (
            lambda d: d['connections'][0].__setitem__(1, 'dut.unknown'),
            lambda d: d['connections'].pop(),
            lambda d: d['roles']['load'].__setitem__('capabilities', []),
            lambda d: d['roles'].__setitem__('dut', d['roles']['load']),
            lambda d: d['tests'].__setitem__('bad id', 'A test'),
        ):
            definition = copy.deepcopy(BUCK_CONVERTER)
            mutate(definition)
            with self.assertRaises(ValueError):
                validate_definition(definition)

    def test_persistence_update_delete_and_snapshot_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'profile.json'
            engine = Engine(profile_path=path)
            definition = copy.deepcopy(BUCK_CONVERTER)
            definition['name'] = 'My converter fixture'
            engine.command('save_experiment', {'experiment': definition})
            definition['name'] = 'Mutation'
            snapshot = engine.snapshot()
            snapshot['experiments'].clear()
            restored = Engine(profile_path=path)
            self.assertEqual(restored.experiments['buck_converter']['name'], 'My converter fixture')
            restored.command('delete_experiment', {'experiment': 'buck_converter'})
            self.assertEqual(Engine(profile_path=path).experiments, {})

    def test_bindings_require_live_explicit_capabilities_and_distinct_devices(self):
        engine = Engine()
        args = dict(experiment='buck_converter', bindings={})
        self.assertFalse(engine.command('check_experiment', args)['compatible'])
        for role, requirement in BUCK_CONVERTER['roles'].items():
            engine.instruments[role] = SimpleNamespace(
                name='Any make or model', experiment_capabilities=requirement['capabilities'])
        engine.connections['tcp://fixture:5025'] = {'instrument': 'supply'}
        args['bindings'] = {'supply': 'tcp://fixture:5025', 'load': 'load'}
        self.assertTrue(engine.command('check_experiment', args)['compatible'])
        del engine.instruments['load'].experiment_capabilities
        result = engine.command('check_experiment', args)
        self.assertFalse(result['compatible'])
        self.assertIn('missing capabilities', result['errors'][0])
        args['bindings']['load'] = 'supply'
        self.assertIn('Equipment roles require distinct instruments', engine.command('check_experiment', args)['errors'])
        args['bindings']['unknown'] = 'supply'
        with self.assertRaises(ValueError):
            engine.command('check_experiment', args)
