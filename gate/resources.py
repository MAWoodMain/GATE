"""Locate documentation in both source checkouts and Debian installations."""
from pathlib import Path

def documentation(name):
    root=Path(__file__).resolve().parent
    for path in (root/'data'/name,root.parent/name):
        if path.is_file():return path
    raise FileNotFoundError(f'GATE documentation not installed: {name}')
