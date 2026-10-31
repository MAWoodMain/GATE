#!/usr/bin/env python3
"""Build an architecture-independent Ubuntu .deb using Python 3.11+ and dpkg-deb."""
import argparse
import gzip
from datetime import datetime, timezone
from email.utils import format_datetime
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import tomllib
from release_version import choose_version, git

ROOT=Path(__file__).resolve().parents[1]
VERSION=re.compile(r'^\d+\.\d+\.\d+(?:\+git\d+\.g[0-9a-f]{7,40})?$')

def build(version,output,work=None):
    if not VERSION.fullmatch(version):
        raise ValueError('Use X.Y.Z or X.Y.Z+gitN.gSHA')
    if not shutil.which('dpkg-deb'):
        raise SystemExit('Install dpkg to build the Debian package')
    project=tomllib.loads((ROOT/'pyproject.toml').read_text())['project']
    epoch=int(os.environ.get('SOURCE_DATE_EPOCH') or git(ROOT,'log','-1','--format=%ct'))
    output=Path(output).resolve();output.mkdir(parents=True,exist_ok=True)
    if work:Path(work).mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='gate-deb-',dir=work) as temporary:
        stage=Path(temporary)
        def write(name,text,mode=0o644):
            path=stage/name;path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text(text);path.chmod(mode)
        def copy(source,destination):
            target=stage/destination;target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(ROOT/source,target);target.chmod(0o644)
        package=stage/'usr/lib/python3/dist-packages/gate'
        shutil.copytree(ROOT/'gate',package,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        init=package/'__init__.py'
        init.write_text(re.sub(r'__version__ = "[^"]+"',f'__version__ = "{version}"',init.read_text()))
        metadata=f'usr/lib/python3/dist-packages/gnome_automatic_test_equipment-{version}.dist-info'
        write(metadata+'/METADATA',f'Metadata-Version: 2.1\nName: {project["name"]}\nVersion: {version}\nSummary: {project["description"]}\nLicense: MIT\n')
        entries=['[console_scripts]','gate = gate.__main__:main','']
        for group,items in project.get('entry-points',{}).items():
            entries.extend([f'[{group}]',*[f'{key} = {value}' for key,value in items.items()],''])
        write(metadata+'/entry_points.txt','\n'.join(entries))
        write(metadata+'/INSTALLER','deb\n')
        write('usr/bin/gate','#!/usr/bin/python3\nfrom gate.__main__ import main\nmain()\n',0o755)
        doc='usr/share/doc/gate-ate'
        for name in ('README.md','AGENT_INSTRUMENT_GUIDE.md'):
            copy(name,doc+'/'+name)
        installed_readme=stage/doc/'README.md'
        installed_readme.write_text(installed_readme.read_text().replace('(packaging/INSTALL.md)','(INSTALL.md)'))
        for source in sorted((ROOT/'docs').glob('*.md')):
            copy(str(source.relative_to(ROOT)),doc+'/docs/'+source.name)
        for source in sorted((ROOT/'gate/templates').glob('*.json')):
            copy(str(source.relative_to(ROOT)),doc+'/gate/templates/'+source.name)
        copy('packaging/INSTALL.md',doc+'/INSTALL.md')
        copyright_text=('Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/\n'
            'Upstream-Name: GATE\nSource: https://github.com/MAWoodMain/GATE\n\n'
            'Files: *\nCopyright: 2026 Matthew Wood\nLicense: Expat\n '+
            '\n'.join(line if line else '.' for line in (ROOT/'LICENSE').read_text().splitlines()).replace('\n','\n ')+'\n')
        write(doc+'/copyright',copyright_text)
        change=(f'gate-ate ({version}) unstable; urgency=medium\n\n'
                '  * Package GATE instrument service, device manager, top bar extension and agent guide.\n\n'
                ' -- Matthew Wood <mawoodmain@googlemail.com>  '+
                format_datetime(datetime.fromtimestamp(epoch,timezone.utc))+'\n')
        changelog=stage/doc/'changelog.Debian.gz'
        with open(changelog,'wb') as stream:
            with gzip.GzipFile(filename='',mode='wb',fileobj=stream,mtime=epoch) as compressed:
                compressed.write(change.encode())
        # A relative link keeps packaged resources usable when extracted for tests.
        (package/'data').symlink_to('../../../../share/doc/gate-ate',target_is_directory=True)
        for source in sorted((ROOT/'extension').iterdir()):
            if source.is_file():copy(str(source.relative_to(ROOT)),'usr/share/gnome-shell/extensions/gate@mawoodmain.github.io/'+source.name)
        copy('data/70-gate-siglent.rules','usr/lib/udev/rules.d/70-gate-siglent.rules')
        copy('data/70-gate-hantek.rules','usr/lib/udev/rules.d/70-gate-hantek.rules')
        unit=(ROOT/'data/gate.service').read_text().replace('ExecStart=%h/.local/bin/gate service','ExecStart=/usr/bin/gate service')
        write('usr/lib/systemd/user/gate.service',unit)
        desktop=(ROOT/'data/io.github.MAWoodMain.GATE.desktop').read_text().replace('Exec=gate gui','Exec=/usr/bin/gate gui')
        write('usr/share/applications/io.github.MAWoodMain.GATE.desktop',desktop)
        for name in ('postinst','prerm','postrm'):
            write('DEBIAN/'+name,(ROOT/'packaging'/name).read_text(),0o755)
        files=sorted(p for p in stage.rglob('*') if p.is_file() and not p.is_symlink() and 'DEBIAN' not in p.relative_to(stage).parts)
        size=(sum(path.stat().st_size for path in files)+1023)//1024
        control=(ROOT/'packaging/control.in').read_text().replace('@VERSION@',version).replace('@SIZE@',str(size))
        write('DEBIAN/control',control)
        write('DEBIAN/md5sums',''.join(hashlib.md5(p.read_bytes(),usedforsecurity=False).hexdigest()+'  '+str(p.relative_to(stage))+'\n' for p in files))
        for path in sorted(stage.rglob('*'),reverse=True):
            if not path.is_symlink():
                path.chmod(0o755 if path.is_dir() or path.relative_to(stage).as_posix() in ('usr/bin/gate','DEBIAN/postinst','DEBIAN/prerm','DEBIAN/postrm') else 0o644)
            os.utime(path,(epoch,epoch),follow_symlinks=False)
        stage.chmod(0o755);os.utime(stage,(epoch,epoch))
        target=output/f'gate-ate_{version}_all.deb'
        env=dict(os.environ,SOURCE_DATE_EPOCH=str(epoch))
        subprocess.run(['dpkg-deb','--root-owner-group','--threads-max=1','-Zxz','--build',str(stage),str(target)],check=True,env=env)
        target.chmod(0o644)
        checksum=target.with_suffix('.deb.sha256')
        checksum.write_text(hashlib.sha256(target.read_bytes()).hexdigest()+'  '+target.name+'\n')
        print(target)
        return target

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version')
    parser.add_argument('--output',type=Path,default=ROOT/'dist')
    parser.add_argument('--work-dir',type=Path)
    args=parser.parse_args()
    version=args.version
    if not version:
        base=tomllib.loads((ROOT/'pyproject.toml').read_text())['project']['version']
        version=choose_version(base,git(ROOT,'tag','--list').splitlines(),[],git(ROOT,'rev-list','--count','HEAD'),git(ROOT,'rev-parse','HEAD'))
    build(version,args.output,args.work_dir)

if __name__=='__main__':main()
