import unittest
from unittest.mock import Mock
from gate.engine import Engine
from gate.drivers.simulated import Simulated
from gate.drivers.riden import RidenRD6006
from gate.outputs import output_catalog, configure_output
from test_riden import Registers


class OutputTests(unittest.TestCase):
    def test_catalog_is_passive_and_does_not_invent_controls(self):
        transport = Registers()
        driver = RidenRD6006(transport)
        catalog = output_catalog(driver)
        self.assertEqual(set(catalog['controls']), {'voltage', 'current_limit'})
        catalog['controls']['voltage']['max'] = 999
        self.assertEqual(output_catalog(driver)['controls']['voltage']['max'], 60)
        self.assertFalse(transport.writes)
        for key, value in [('priority', 'current'), ('ocp', 1), ('ovp', 10)]:
            with self.assertRaisesRegex(ValueError, 'verified support'):
                configure_output(driver, key, value)
        self.assertFalse(transport.writes)

    def test_validation_and_confirmed_writes(self):
        transport = Registers()
        driver = RidenRD6006(transport)
        for value in (True, None, '5', float('nan'), float('inf'), -1, 61):
            with self.assertRaises(ValueError): configure_output(driver, 'voltage', value)
        self.assertFalse(transport.writes)
        configure_output(driver, 'voltage', 5)
        self.assertEqual(transport.r[8], 500)
        self.assertEqual(transport.r[18], 0)
        transport.write_register = Mock()
        with self.assertRaisesRegex(ValueError, 'readback'):
            configure_output(driver, 'current_limit', .2)

    def test_optional_priority_and_protection_contract(self):
        driver = Mock(output_configuration={'controls': {
            'priority': {'choices': ['voltage', 'current']},
            'ovp': {'label': 'OVP', 'unit': 'V', 'min': 1, 'max': 30}}})
        configure_output(driver, 'priority', 'current')
        driver.set_priority.assert_called_once_with('current')
        with self.assertRaises(ValueError): configure_output(driver, 'priority', 'auto')
        configure_output(driver, 'ovp', 20)
        driver.set_ovp.assert_called_once_with(20)

    def test_manual_output_confirmation_does_not_require_wiring_checkbox(self):
        transport = Registers()
        driver = RidenRD6006(transport)
        engine = Engine(.1)
        engine.instruments[driver.id] = driver
        args = dict(instrument=driver.id, enabled=True)
        for confirmation in (None, False, 'true', 1):
            with self.assertRaisesRegex(ValueError, 'operator_confirmed'):
                engine.command('output', dict(args, operator_confirmed=confirmation))
        self.assertFalse(transport.writes)
        engine.command('output', dict(args, operator_confirmed=True))
        self.assertEqual(transport.r[18], 1)
        engine.command('output', dict(args, enabled=False))
        self.assertEqual(transport.r[18], 0)
        engine.experiment_run = Mock(active=True)
        with self.assertRaisesRegex(ValueError, 'Stop the active experiment'):
            engine.command('output', dict(args, operator_confirmed=True))
        self.assertEqual(transport.r[18], 0)

    def test_demo_priority_and_protection_trip(self):
        driver = Simulated('power_supply')
        configure_output(driver, 'priority', 'current')
        self.assertEqual(driver.output_settings['priority'], 'current')
        configure_output(driver, 'voltage', 5.)
        configure_output(driver, 'ovp', 4.)
        with self.assertRaisesRegex(ValueError, 'OVP/OCP'): driver.set_enabled(True)
        configure_output(driver, 'ovp', 6.)
        driver.set_enabled(True)
        configure_output(driver, 'ocp', .01)
        driver.read()
        self.assertFalse(driver.enabled)

    def test_owner_command_blocks_during_runs_and_never_enables(self):
        engine = Engine(.1)
        driver = Simulated('power_supply')
        engine.instruments[driver.id] = driver
        args = dict(instrument=driver.id, control='current_limit', value=.01)
        engine.command('configure_output', args)
        self.assertEqual(driver.current_limit, .01)
        self.assertFalse(driver.enabled)
        engine.experiment_run = Mock(active=True)
        with self.assertRaisesRegex(ValueError, 'Stop the active experiment'):
            engine.command('configure_output', dict(args, value=.1))
        self.assertEqual(driver.current_limit, .01)
        engine.experiment_run = None
        engine.experiment_sequence = {'state': 'running'}
        with self.assertRaises(ValueError): engine.command('configure_output', args)
