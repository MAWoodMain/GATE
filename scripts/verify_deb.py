#!/usr/bin/env python3
"""Validate package metadata, ownership, payload imports and documentation links."""
import argparse
import io
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

def verify(archive):
    archive=Path(archive).resolve()
    def field(name):
        return subprocess.check_output(['dpkg-deb','--field',str(archive),name],text=True).strip()
    assert field('Package')=='gate-ate'
    assert field('Architecture')=='all'
    version=field('Version')
    data=subprocess.check_output(['dpkg-deb','--fsys-tarfile',str(archive)])
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        members=tar.getmembers()
        assert all(member.uid==0 and member.gid==0 for member in members),'Non-root archive ownership'
        names={member.name.removeprefix('./') for member in members}
        required={'usr/bin/gate','usr/lib/python3/dist-packages/gate/__main__.py',
            'usr/lib/systemd/user/gate.service','usr/lib/udev/rules.d/70-gate-siglent.rules',
            'usr/lib/udev/rules.d/70-gate-hantek.rules',
            'usr/share/applications/io.github.MAWoodMain.GATE.desktop',
            'usr/share/gnome-shell/extensions/gate@mawoodmain.github.io/extension.js',
            'usr/share/doc/gate-ate/AGENT_INSTRUMENT_GUIDE.md','usr/share/doc/gate-ate/INSTALL.md'}
        assert required<=names,required-names
        assert not any('__pycache__' in name or name.endswith('.pyc') or '/.git/' in name for name in names)
    with tempfile.TemporaryDirectory(prefix='gate-verify-') as temporary:
        root=Path(temporary)/'root'
        subprocess.run(['dpkg-deb','--extract',str(archive),str(root)],check=True)
        control=Path(temporary)/'control'
        subprocess.run(['dpkg-deb','--control',str(archive),str(control)],check=True)
        for script in ('postinst','prerm','postrm'):
            subprocess.run(['sh','-n',str(control/script)],check=True)
        env=dict(os.environ,PYTHONPATH=str(root/'usr/lib/python3/dist-packages'))
        result=subprocess.check_output([sys.executable,str(root/'usr/bin/gate'),'--version'],cwd=temporary,env=env,text=True)
        assert result.strip()=='GATE '+version,result
        code='''
import importlib.metadata, pathlib, sys, gate
from gate.resources import documentation
from gate.discovery import driver_types
root=pathlib.Path(sys.argv[1])
assert pathlib.Path(gate.__file__).resolve().is_relative_to(root)
assert gate.__version__==sys.argv[2]
assert importlib.metadata.version('gnome-automatic-test-equipment')==sys.argv[2]
assert documentation('AGENT_INSTRUMENT_GUIDE.md').resolve()==root/'usr/share/doc/gate-ate/AGENT_INSTRUMENT_GUIDE.md'
assert {driver.__name__ for driver in driver_types()}>={'SiglentSDS','Fluke45'}
'''
        subprocess.run([sys.executable,'-c',code,str(root),version],cwd=temporary,env=env,check=True)
        unit=(root/'usr/lib/systemd/user/gate.service').read_text()
        assert 'ExecStart=/usr/bin/gate service' in unit and '.local/bin' not in unit
    print('PASS: Debian metadata, root ownership, clean payload, installed CLI version, plugins and agent guide')
    return version

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive',type=Path)
    verify(parser.parse_args().archive)
