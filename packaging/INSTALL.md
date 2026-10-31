# Installing GATE on Ubuntu

These packages target Ubuntu 24.04 and newer with GNOME Shell 46–50. They are architecture-independent Python/JavaScript packages; Ubuntu supplies native GTK and Python bindings through apt dependencies. Other desktop environments can use the agent service and device manager, but the top bar indicator requires supported GNOME Shell.

## Install a downloaded release

```bash
# From the directory containing the downloaded .deb and checksum:
sha256sum -c gate-ate_*.deb.sha256
sudo apt install ./gate-ate_*_all.deb
```

Use apt rather than `dpkg -i` so dependencies are resolved. The package installs:

- `/usr/bin/gate` and public Python modules for the agent client.
- `/usr/lib/systemd/user/gate.service`.
- The desktop entry and GNOME extension under `/usr/share`.
- Siglent and Hantek USB access rules under `/usr/lib/udev/rules.d`.
- This guide and the full agent guide under `/usr/share/doc/gate-ate`.

Start GATE for your user:

```bash
systemctl --user daemon-reload
systemctl --user enable --now gate
/usr/bin/gate gui
```

The service runs as your user and starts at graphical login once enabled. Package installation does not start instruments as root or enable a service for every account on the machine. It does not modify personal settings, saved connections, or recordings.

For the top bar, log out and in after installing the extension for the first time, then run:

```bash
gnome-extensions enable gate@mawoodmain.github.io
```

GNOME caches extension code, so updates also require a new login to load changed JavaScript. The service itself can be restarted without a logout.

For USB access, unplug/reconnect the scope after installation. For a USB serial meter, your account must be permitted to open the serial port, normally through `dialout`. Configure the Fluke 45 as 9600 baud, no parity and add its endpoint in the device manager. The included agent guide explains the connection and API.

## Agent API

The packaged service enables typed agent controls on localhost port 8765. Its private bearer token is in `$XDG_RUNTIME_DIR/gate/control.json`; agents should follow `/usr/share/doc/gate-ate/AGENT_INSTRUMENT_GUIDE.md`.

To enable advanced raw instrument queries/writes explicitly:

```bash
systemctl --user edit gate
```

Enter:

```ini
[Service]
ExecStart=
ExecStart=/usr/bin/gate service --allow-raw
```

Then apply it:

```bash
systemctl --user daemon-reload
systemctl --user restart gate
```

Do not restart during recording. Agents must reread the runtime manifest after a restart because its token rotates. Enabled connection methods reconnect and identified instruments restore their saved measurement functions. Deliberately disconnected devices remain disconnected. Instrument profiles and connections migrate automatically to schema version 2; no output commands are replayed.

## Migrating from the development checkout

A per-user service, launcher or extension overrides the corresponding packaged file. The development setup used in this project creates all three. Stop any recording, then back these up before switching to the package:

```bash
systemctl --user disable --now gate
gnome-extensions disable gate@mawoodmain.github.io
mkdir -p ~/.local/share/gate/development-backup
# Run only for files/directories that exist. Keep existing backups instead of overwriting them.
mv -n ~/.config/systemd/user/gate.service ~/.local/share/gate/development-backup/gate.service
mv -n ~/.local/bin/gate ~/.local/share/gate/development-backup/gate
mv -n ~/.local/share/gnome-shell/extensions/gate@mawoodmain.github.io \
  ~/.local/share/gate/development-backup/gate-extension
systemctl --user daemon-reload
systemctl --user enable --now gate
```

Start `/usr/bin/gate gui`, then log out/in and enable the packaged extension. Use `systemctl --user cat gate` to verify `ExecStart=/usr/bin/gate service`; `type -a gate` helps identify a remaining local launcher. Preserve an intentional service drop-in, such as `--allow-raw`, by reviewing it before migration. The earlier manual `/etc/udev/rules.d/70-gate-siglent.rules` overrides a same-named packaged rule; remove it only after confirming it contains no local customizations.

Saved settings in `~/.config/gate`, recordings in `~/.local/share/gate/recordings`, and the private runtime token are not part of the package and are not removed by an upgrade.

## Upgrade or uninstall

Install a newer `.deb` with apt, then restart the user service after stopping recording. Log out/in to reload changed extension code.

Before removal:

```bash
systemctl --user disable --now gate
gnome-extensions disable gate@mawoodmain.github.io
sudo apt remove gate-ate
systemctl --user daemon-reload
```

Recordings and personal configuration are retained even on purge.

## Build locally

Building requires Python 3.11+ and `dpkg-deb`; it does not require root, a virtual environment, pip downloads or debhelper.

```bash
python3 scripts/build_deb.py --output dist
# Or build a specific stable version:
python3 scripts/build_deb.py --version 0.1.0 --output dist
```

A SHA-256 sidecar is generated with every package. Without an explicit version, the builder uses the source baseline/latest stable tag plus commit count and SHA, such as `0.1.0+git7.g1234abcd`.

## GitHub Actions and automatic releases

The build workflow tests code and GTK, builds the `.deb`, installs it on a clean Ubuntu 24.04 runner, and uploads the package, checksum and installation/agent guides as an Actions artifact for each branch push and pull request.

On `main`, successful builds also publish a GitHub Release with an annotated `vX.Y.Z` tag. The first release uses the baseline in `pyproject.toml` (currently 0.1.0); later pushes increment patch automatically. A higher source baseline takes precedence. Manual workflow runs on `main` can select minor/major bumps. Repeating the same workflow run reuses its release tag rather than bumping again.

Version choice and publication run in a serialized workflow per ref. Preview packages contain a commit-qualified version and are never published as stable releases. Built runtime metadata, `gate --version`, the Debian package and the agent API health version use the same selected version; release automation does not create version-bump commits or force-push branches.

Publishing uses the repository's built-in `GITHUB_TOKEN` with `contents: write` only in the trusted release job; no personal access token is required for the workflow. The repository must permit Actions to create release tags/releases. The workflow must be merged into `main` before stable publishing is activated.
