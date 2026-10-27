import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen
from gate.control import ControlServer
from gate.engine import Engine
from gate.model import Reading

class ControlTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.engine = Engine(.1)
        self.engine.submit('demo')
        self.engine.start()
        self.control = ControlServer(self.engine, port=0, directory=self.folder.name)
        self.control.start()
        self.wait(lambda: len(self.engine.snapshot()['instruments']) == 3)
    def tearDown(self):
        self.control.close()
        self.engine.close()
        self.folder.cleanup()
    def wait(self, predicate):
        for _ in range(60):
            result = predicate()
            if result:
                return result
            time.sleep(.025)
        raise AssertionError('Timed out waiting for acquisition result')
    def request(self, path, body=None, token=True, headers=None, raw=False):
        request_headers = {'Authorization': 'Bearer '+self.control.token} if token else {}
        if body is not None:
            request_headers['Content-Type'] = 'application/json'
        request_headers.update(headers or {})
        payload = body if raw else json.dumps(body).encode() if body is not None else None
        request = Request(self.control.manifest['url']+path, data=payload, headers=request_headers)
        try:
            with urlopen(request, timeout=2) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)
    def completed(self, identifier):
        return self.wait(lambda: (result if result.get('state') in ('succeeded','failed') else None)
                         if (result := self.request('/v1/commands/'+identifier)[1]) else None)
    def test_authenticated_inspection_and_private_discovery(self):
        self.assertEqual(self.request('/v1/instruments', token=False)[0],401)
        self.assertEqual(self.request('/v1/health',headers={'Host':'attacker.example'})[0],403)
        self.assertEqual(self.request('/v1/health',headers={'Origin':'https://attacker.example'})[0],403)
        from gate.client import GateClient
        self.assertEqual(GateClient(self.control.path).request('/v1/health')['api_version'],1)
        status,body = self.request('/v1/instruments')
        self.assertEqual(status,200)
        self.assertEqual(len(body['instruments']),3)
        status,device = self.request('/v1/instruments/'+quote('demo:power_supply',safe=''))
        self.assertEqual(status,200)
        self.assertEqual(device['category'],'power_supply')
        self.assertEqual(os.stat(self.control.path).st_mode & 0o777,0o600)
        self.assertEqual(self.control.server.server_address[0],'127.0.0.1')
    def test_commands_complete_or_fail_and_update_live_state(self):
        status,accepted = self.request('/v1/commands',dict(action='static',instrument='demo:power_supply',voltage=5))
        self.assertEqual(status,202)
        self.assertEqual(self.completed(accepted['id'])['state'],'succeeded')
        self.wait(lambda: any(d['id']=='demo:power_supply' and d['readings'][0]['value']==5
                             for d in self.engine.snapshot()['instruments']))
        status,accepted = self.request('/v1/commands',dict(action='static',instrument='missing',voltage=5))
        self.assertEqual(self.completed(accepted['id'])['state'],'failed')
    def test_commands_stay_responsive_at_long_sampling_intervals(self):
        _,accepted=self.request('/v1/commands',dict(action='settings',interval=10))
        self.assertEqual(self.completed(accepted['id'])['state'],'succeeded')
        started=time.monotonic()
        _,accepted=self.request('/v1/commands',dict(action='static',instrument='demo:power_supply',voltage=2))
        self.assertEqual(self.completed(accepted['id'])['state'],'succeeded')
        self.assertLess(time.monotonic()-started,1)

    def test_validation_and_raw_commands_are_opt_in(self):
        self.assertEqual(self.request('/v1/commands',dict(action='unknown'))[0],400)
        self.assertEqual(self.request('/v1/commands',b'{oops',raw=True)[0],400)
        self.assertEqual(self.request('/v1/commands',b'{}',raw=True,headers={'Content-Type':'text/plain'})[0],415)
        self.assertEqual(self.request('/v1/commands',dict(action='query',instrument='fake',command='*IDN?'))[0],403)
        self.assertEqual(self.request('/v1/commands',b'x'*17000,raw=True)[0],413)
    def test_enabled_raw_operations_use_acquisition_owner_and_reject_batches(self):
        self.control.allow_raw=True
        calls=[]
        transport=SimpleNamespace(query=lambda cmd: (calls.append(('query',cmd)) or 'fixture'),
                                  write=lambda cmd: calls.append(('write',cmd)))
        device=SimpleNamespace(id='fake',name='Fake,scope',category='oscilloscope',simulated=False,
                               transport=transport,read=lambda:[Reading('voltage',1.,'V')],close=lambda:None)
        # Add before the worker sees the API operation; all actual I/O is still
        # performed by the worker thread, just as for real drivers.
        self.engine.instruments['fake']=device
        _,accepted=self.request('/v1/commands',dict(action='query',instrument='fake',command='*IDN?'))
        result=self.completed(accepted['id'])
        self.assertEqual(result['result']['response'],'fixture')
        _,accepted=self.request('/v1/commands',dict(action='write',instrument='fake',command='STOP'))
        self.assertEqual(self.completed(accepted['id'])['state'],'succeeded')
        for action,command in (('write','*IDN?'),('query','*IDN?;*RST'),('query','*IDN?\n*RST')):
            _,accepted=self.request('/v1/commands',dict(action=action,instrument='fake',command=command))
            self.assertEqual(self.completed(accepted['id'])['state'],'failed')
        self.assertEqual(calls,[('query','*IDN?'),('write','STOP')])
    def test_physical_profiles_and_scoped_forget_through_http(self):
        from unittest.mock import Mock, patch
        from gate.model import MeasurementFunction
        keys = []
        for index in (1, 2):
            resource = f'tcp://fixture{index}:5025'
            device = SimpleNamespace(id=resource, name=f'FLUKE,45,{index},1.7', device_type='FLUKE:45',
                category='multimeter', simulated=False, close=Mock(), select_function=Mock(),
                functions=lambda:[MeasurementFunction('FREQ', 'Frequency', 'primary.FREQ', 'Hz', True)],
                read=lambda:[Reading('primary.FREQ', 1000, 'Hz')])
            with patch('gate.discovery.connect', return_value=device):
                status, accepted = self.request('/v1/commands', dict(action='connect', resource=resource))
                self.assertEqual(status, 202)
                result = self.completed(accepted['id'])
                self.assertEqual(result['state'], 'succeeded')
                keys.append(result['result']['instrument'])
            self.assertEqual(self.request('/v1/instruments/'+quote(resource, safe=''))[1]['id'], keys[-1])
        _, accepted = self.request('/v1/commands', dict(action='instrument_settings', instrument=keys[0], name='Reference', settings={'function':'FREQ'}))
        self.assertEqual(self.completed(accepted['id'])['state'], 'succeeded')
        _, accepted = self.request('/v1/commands', dict(action='copy_settings', instrument=keys[1], source=keys[0]))
        self.assertEqual(self.completed(accepted['id'])['state'], 'succeeded')
        state = self.request('/v1/state')[1]
        self.assertEqual(state['instrument_profiles'][keys[1]]['settings'], {'function':'FREQ'})
        _, accepted = self.request('/v1/commands', dict(action='forget', instrument=keys[0], resource='tcp://fixture1:5025', scope='connection'))
        self.assertEqual(self.completed(accepted['id'])['state'], 'succeeded')
        self.assertIn(keys[0], self.request('/v1/state')[1]['instrument_profiles'])
        _, accepted = self.request('/v1/commands', dict(action='forget', instrument=keys[0], scope='instrument'))
        self.assertEqual(self.completed(accepted['id'])['state'], 'succeeded')
        self.assertNotIn(keys[0], self.request('/v1/state')[1]['instrument_profiles'])

    def test_top_bar_measurements_can_be_selected_and_cleared_through_http(self):
        for functions in (['voltage', 'current', 'set_voltage'], []):
            status, accepted = self.request('/v1/commands', dict(action='display_measurements', instrument='demo:power_supply', functions=functions))
            self.assertEqual(status, 202)
            self.assertEqual(self.completed(accepted['id'])['state'], 'succeeded')
            state = self.request('/v1/state')[1]
            self.assertEqual(state['instrument_profiles']['demo:power_supply']['settings']['display_functions'], functions)
            self.assertFalse(self.engine.instruments['demo:power_supply'].enabled)

    def test_manifest_is_removed_when_server_stops(self):
        path=self.control.path
        self.control.close()
        self.assertFalse(path.exists())
        # close() remains safe during tearDown.

if __name__=='__main__':
    unittest.main()
