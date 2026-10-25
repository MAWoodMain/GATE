#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
# Prerequisites: python3-venv, python3-gi, gir1.2-gtk-4.0
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install --no-deps -e .
mkdir -p "$HOME/.local/bin" "$HOME/.local/share/applications" "$HOME/.config/systemd/user"
repo=$(pwd)
# shlex quoting preserves repository paths containing spaces.
.venv/bin/python - "$repo" "$HOME/.local/bin/gate" <<'SCRIPT'
import pathlib, shlex, sys
repo, target = sys.argv[1:]
path = pathlib.Path(target)
path.write_text('#!/bin/sh\nexec ' + shlex.quote(repo+'/.venv/bin/python') + ' -m gate "$@"\n')
path.chmod(0o755)
SCRIPT
cp data/io.github.MAWoodMain.GATE.desktop "$HOME/.local/share/applications/"
.venv/bin/python - "$HOME/.local/bin/gate" "$HOME/.local/share/applications/io.github.MAWoodMain.GATE.desktop" <<'SCRIPT'
import pathlib, sys
path = pathlib.Path(sys.argv[2])
launcher = sys.argv[1].replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$').replace('%', '%%')
path.write_text(path.read_text().replace('Exec=gate gui', 'Exec="'+launcher+'" gui'))
SCRIPT
cp data/gate.service "$HOME/.config/systemd/user/"
mkdir -p "$HOME/.local/share/gnome-shell/extensions/gate@mawoodmain.github.io"
cp extension/* "$HOME/.local/share/gnome-shell/extensions/gate@mawoodmain.github.io/"
systemctl --user daemon-reload
printf '%s\n' 'Installed. Start with: systemctl --user enable --now gate' 'Log out/in, then: gnome-extensions enable gate@mawoodmain.github.io' 'For USB access, follow README.md (udev rule needs sudo).'
