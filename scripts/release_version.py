#!/usr/bin/env python3
"""Choose stable release or commit-qualified Debian versions without editing sources."""
import argparse
import os
from pathlib import Path
import re
import subprocess
import tomllib

STABLE = re.compile(r'^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$')

def parse_version(value):
    match = STABLE.fullmatch(value)
    if not match:
        raise ValueError(f'Expected X.Y.Z version, got {value!r}')
    return tuple(int(part) for part in match.groups())

def choose_version(base, tags, head_tags, count, sha, release=False, bump='patch', force_bump=False):
    if bump not in ('patch','minor','major'):
        raise ValueError('Bump must be patch, minor or major')
    base_tuple=parse_version(base)
    stable=[parse_version(tag) for tag in tags if tag.startswith('v') and STABLE.fullmatch(tag)]
    current=[parse_version(tag) for tag in head_tags if tag.startswith('v') and STABLE.fullmatch(tag)]
    latest=max(stable,default=base_tuple)
    if not release:
        if not re.fullmatch(r'[0-9a-f]{7,40}',sha) or int(count)<1:
            raise ValueError('Invalid commit identity')
        return '.'.join(map(str,max(base_tuple,latest)))+f'+git{count}.g{sha[:8]}'
    # An existing stable tag on this commit is reused on reruns. Manual major
    # and minor bumps are intended for a new main commit, before its release.
    if current and not force_bump:
        return '.'.join(map(str,max(current)))
    version=max(base_tuple,latest)
    if (stable and base_tuple<=latest) or bump!='patch':
        major,minor,patch=version
        version=(major+1,0,0) if bump=='major' else (major,minor+1,0) if bump=='minor' else (major,minor,patch+1)
    return '.'.join(map(str,version))

def git(root,*args):
    return subprocess.check_output(['git','-C',str(root),*args],text=True).strip()

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release',action='store_true')
    parser.add_argument('--bump',choices=('patch','minor','major'),default='patch')
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    args=parser.parse_args()
    project=tomllib.loads((args.root/'pyproject.toml').read_text())['project']
    head_tags=git(args.root,'tag','--points-at','HEAD').splitlines()
    force_bump=args.release and args.bump!='patch'
    if force_bump and os.environ.get('GITHUB_RUN_ID'):
        marker='GATE-Actions-Run: '+os.environ['GITHUB_RUN_ID']
        prior=[tag for tag in head_tags if marker in git(args.root,'tag','--list',tag,'--format=%(contents)').splitlines()]
        if prior:
            head_tags=prior
            force_bump=False
    version=choose_version(project['version'],git(args.root,'tag','--list').splitlines(),
        head_tags,git(args.root,'rev-list','--count','HEAD'),
        git(args.root,'rev-parse','HEAD'),args.release,args.bump,force_bump)
    print(version)
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'],'a') as stream:
            stream.write(f'version={version}\ntag=v{version}\n')

if __name__=='__main__':main()
