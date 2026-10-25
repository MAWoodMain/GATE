"""Exercise the installed/extracted package independently of checkout imports."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

parser=argparse.ArgumentParser()
parser.add_argument('--root',type=Path,default=Path('/'))
args=parser.parse_args()
root=args.root.resolve()
sys.path.insert(0,str(root/'usr/lib/python3/dist-packages'))
import gate
from gate.client import GateClient
from gate.resources import documentation
assert Path(gate.__file__).resolve().is_relative_to(root/'usr/lib/python3/dist-packages')
assert documentation('AGENT_INSTRUMENT_GUIDE.md').is_file()
with tempfile.TemporaryDirectory(prefix='gate-installed-') as temporary:
    env=dict(os.environ,XDG_RUNTIME_DIR=temporary,PYTHONPATH=str(root/'usr/lib/python3/dist-packages'))
    process=subprocess.Popen([sys.executable,str(root/'usr/bin/gate'),'service','--demo','--ephemeral','--control-port','0'],env=env)
    try:
        manifest=Path(temporary)/'gate/control.json'
        for _ in range(80):
            try:
                api=GateClient(manifest)
                if len(api.request('/v1/instruments')['instruments'])==3:break
            except (OSError,ValueError):pass
            time.sleep(.1)
        else:raise AssertionError('Packaged service did not start')
        assert api.request('/v1/health')['version']==gate.__version__
        assert api.command('static',instrument='demo:power_supply',voltage=2)['state']=='succeeded'
        import gi
        gi.require_version('Gtk','4.0')
        from gi.repository import GLib,Gtk
        from gate.app import Window
        app=Gtk.Application(application_id='io.github.MAWoodMain.GATE.PackageSmoke')
        app.register(None)
        window=Window(app)
        window.present()
        loop=GLib.MainLoop()
        errors=[]
        def check():
            try:
                assert len(window.labels)==3,window.status.get_text()
                assert window.get_title()=='GATE · Devices & Settings'
            except Exception as error:errors.append(error)
            finally:window.close();loop.quit()
        GLib.timeout_add(1800,check)
        loop.run()
        if errors:raise errors[0]
        print('PASS: installed package service, authenticated API, runtime version, device manager and agent guide')
    finally:
        process.terminate()
        process.wait(timeout=15)
