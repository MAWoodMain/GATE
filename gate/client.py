"""Standard-library client for local agents. Credentials are never printed."""
import json
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from gate.control import runtime_directory

class GateClient:
    def __init__(self, manifest=None):
        config=json.loads(Path(manifest or runtime_directory()/'control.json').read_text())
        url=urlsplit(config['url'])
        if url.scheme!='http' or url.hostname not in ('127.0.0.1','localhost') or not url.port:
            raise ValueError('GATE manifest must refer to a local HTTP service')
        self.url=config['url']
        self._token=config['token']
    def request(self,path,payload=None):
        if not path.startswith('/v1/') or '?' in path or '#' in path:
            raise ValueError('Use a /v1/ API path')
        data=json.dumps(payload).encode() if payload is not None else None
        headers={'Authorization':'Bearer '+self._token}
        if data is not None:headers['Content-Type']='application/json'
        with urlopen(Request(self.url+path,data=data,headers=headers),timeout=5) as response:
            return json.load(response)
    def submit(self,action,**args):
        return self.request('/v1/commands',dict(action=action,**args))
    def command(self,action,timeout=30,**args):
        accepted=self.submit(action,**args)
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            status=self.request(accepted['status_url'])
            if status['state']=='succeeded':return status
            if status['state']=='failed':raise RuntimeError(status['error'])
            time.sleep(.1)
        raise TimeoutError(f"Command {accepted['id']} has not completed; it may still run")
