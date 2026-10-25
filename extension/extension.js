import St from 'gi://St';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

const BUS = 'io.github.MAWoodMain.GATE';
const PATH = '/io/github/MAWoodMain/GATE';

function deviceName(instrument) {
    if (instrument.simulated) {
        const category = {multimeter: 'Multimeter', power_supply: 'Power supply',
            oscilloscope: 'Oscilloscope'}[instrument.category] ?? instrument.category;
        return `DEMO · ${category}`;
    }
    const fields = instrument.name.split(',').map(field => field.trim());
    return fields.slice(0, 2).join(' ').replace(/ Technologies/i, '');
}

function channelName(channel) {
    const names = {PKPK: 'Peak-to-peak', RMS: 'RMS voltage', FREQ: 'Frequency',
        voltage_dc: 'DC voltage', set_voltage: 'Voltage setpoint',
        output_enabled: 'Output enabled', voltage: 'Voltage', current: 'Current',
        VDC: 'DC voltage', VAC: 'AC voltage', VACDC: 'AC + DC voltage',
        ADC: 'DC current', AAC: 'AC current', AACDC: 'AC + DC current',
        OHMS: 'Resistance', DIODE: 'Diode voltage', CONT: 'Continuity voltage',
        power: 'Power', decibels: 'Decibels', disabled: 'Display off'};
    const parts = channel.split('.');
    const display = {primary: 'Primary', secondary: 'Secondary'}[parts[0]] ?? parts[0];
    return parts.length >= 2 ? `${display} · ${names[parts[1]] ?? parts[1]}${parts.length > 2 ? ` (${parts.slice(2).join('.')})` : ''}`
        : names[channel] ?? channel;
}

function readingValue(reading) {
    if (reading.value === null || reading.quality !== 'ok')
        return `— (${reading.quality})`;
    return `${Number(reading.value).toPrecision(5)} ${reading.unit}`.trim();
}

export default class GateExtension extends Extension {
    enable() {
        this._devices = new Map();
        this._lastState = null;
        this._pendingSelection = null;
        this._selectionError = null;
        this._button = new PanelMenu.Button(0.0, 'GATE');
        this._label = new St.Label({text: 'GATE · connecting', y_align: 2});
        this._button.add_child(this._label);
        this._details = new PopupMenu.PopupMenuItem('Waiting for acquisition service', {reactive: false});
        this._button.menu.addMenuItem(this._details);
        this._button.menu.addMenuItem(new PopupMenu.PopupMenuItem('Display measurement', {reactive: false}));
        this._deviceSection = new PopupMenu.PopupMenuSection();
        this._button.menu.addMenuItem(this._deviceSection);
        this._empty = new PopupMenu.PopupMenuItem('No instruments connected', {reactive: false});
        this._deviceSection.addMenuItem(this._empty);
        const open = new PopupMenu.PopupMenuItem('Open GATE');
        open.connect('activate', () => {
            const info = Gio.DesktopAppInfo.new('io.github.MAWoodMain.GATE.desktop');
            if (info) info.launch([], null);
        });
        this._button.menu.addMenuItem(open);
        Main.panel.addToStatusArea(this.uuid, this._button);
        this._cancellable = new Gio.Cancellable();
        this._pending = false;
        this._timer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 1, () => {
            this._refresh();
            return GLib.SOURCE_CONTINUE;
        });
        this._refresh();
    }

    _render(state) {
        this._lastState = state;
        const pending = this._pendingSelection;
        const result = pending && state.controls?.[pending.request];
        if (result?.state === 'applied') {
            this._selected = JSON.stringify([pending.instrument, result.channel]);
            this._pendingSelection = null;
        } else if (result?.state === 'failed') {
            this._selectionError = `Could not switch function: ${result.error}`;
            this._pendingSelection = null;
        }
        const stale = Date.now() - state.updated_utc_ns / 1e6 > Math.max(5000, (state.interval_s ?? 1) * 2000);
        // Keep actors and their open/focus state. Only topology changes add/remove
        // items; values and quality changes update existing labels in place.
        const instruments = [...state.instruments].sort((a, b) =>
            Number(a.simulated) - Number(b.simulated) ||
            deviceName(a).localeCompare(deviceName(b)) || a.id.localeCompare(b.id));
        const ids = new Set(instruments.map(instrument => instrument.id));
        for (const [id, device] of this._devices) {
            if (!ids.has(id)) {
                device.item.destroy();
                this._devices.delete(id);
            }
        }
        const choices = [];
        instruments.forEach((instrument, position) => {
            let device = this._devices.get(instrument.id);
            if (!device) {
                device = {item: new PopupMenu.PopupSubMenuMenuItem(''), channels: new Map()};
                this._devices.set(instrument.id, device);
                this._deviceSection.addMenuItem(device.item, position);
            }
            const title = deviceName(instrument);
            const serial = instrument.name.split(',')[2]?.trim();
            device.item.label.set_text(!instrument.simulated && serial ? `${title} · ${serial}` : title);
            const functions = instrument.functions?.length ? instrument.functions
                : instrument.readings.map(reading => ({id: reading.channel,
                    channel: reading.channel, label: channelName(reading.channel), configurable: false}));
            const channels = new Set(functions.map(fn => fn.channel));
            for (const [channel, row] of device.channels) {
                if (!channels.has(channel)) {
                    row.item.destroy();
                    device.channels.delete(channel);
                }
            }
            functions.forEach((fn, index) => {
                const key = JSON.stringify([instrument.id, fn.channel]);
                let row = device.channels.get(fn.channel);
                if (!row) {
                    const item = new PopupMenu.PopupMenuItem('');
                    row = {item};
                    device.channels.set(fn.channel, row);
                    device.item.menu.addMenuItem(item, index);
                    item.connect('activate', () => this._activateFunction(instrument.id, row.definition));
                }
                row.definition = fn;
                const reading = instrument.readings.find(r => r.channel === fn.channel ||
                    r.channel.startsWith(`${fn.channel}.mod`)) ??
                    (fn.configurable && instrument.current_function === fn.id
                        ? instrument.readings.find(r => r.channel.startsWith('primary.')) : null);
                const switching = this._pendingSelection?.instrument === instrument.id &&
                    this._pendingSelection?.function === fn.id;
                const value = switching ? 'switching…' : reading ? readingValue(reading) : '— (not active)';
                row.item.label.set_text(`${fn.label === fn.channel ? channelName(fn.channel) : fn.label}   ${value}`);
                row.item.sensitive = !switching;
                row.item.setOrnament(this._selected === key ? PopupMenu.Ornament.DOT : PopupMenu.Ornament.NONE);
                choices.push({key, live: Boolean(reading),
                    text: `${title} · ${fn.label} ${value}`});
            });
        });
        this._empty.visible = instruments.length === 0;
        const selected = this._selected ? choices.find(choice => choice.key === this._selected)
            : choices.find(choice => choice.live) ?? choices[0];
        const text = selected?.text ?? (this._selected ? 'selected device unavailable' : 'no readings');
        this._label.set_text(`GATE${state.recording ? ' ●' : ''} · ${stale ? 'STALE' : text}`);
        this._details.label.set_text(this._selectionError ?? (this._pendingSelection
            ? `Switching to ${this._pendingSelection.label}…`
            : `${stale ? 'Stale readings' : state.recording ? 'Recording' : 'Live'} · ${instruments.length} instruments · ${state.status}`));
    }

    _activateFunction(instrument, fn) {
        this._selectionError = null;
        if (!fn.configurable) {
            this._pendingSelection = null;
            this._selected = JSON.stringify([instrument, fn.channel]);
            if (this._lastState) this._render(this._lastState);
            return;
        }
        const request = GLib.uuid_string_random();
        this._pendingSelection = {request, instrument, function: fn.id, label: fn.label};
        if (this._lastState) this._render(this._lastState);
        this._sendCommand({action: 'select_function', instrument, function: fn.id, request}, error => {
            if (this._pendingSelection?.request !== request) return;
            if (error) {
                this._pendingSelection = null;
                this._selectionError = `Could not switch function: ${error.message}`;
                if (this._lastState) this._render(this._lastState);
            } else {
                this._refresh();
            }
        });
    }

    _sendCommand(payload, done) {
        const cancellable = this._cancellable;
        Gio.DBus.session.call(BUS, PATH, BUS, 'Command',
            new GLib.Variant('(s)', [JSON.stringify(payload)]), new GLib.VariantType('(s)'),
            Gio.DBusCallFlags.NO_AUTO_START, 1500, cancellable, (connection, result) => {
                if (this._cancellable !== cancellable || !this._button) return;
                try {
                    connection.call_finish(result);
                    done(null);
                } catch (error) {
                    done(error);
                }
            });
    }

    _offline() {
        this._lastState = null;
        this._pendingSelection = null;
        this._label.set_text('GATE · offline');
        this._details.label.set_text('Start the GATE acquisition service');
        for (const device of this._devices.values()) {
            for (const [channel, row] of device.channels) {
                row.item.label.set_text(`${channelName(channel)}   — (offline)`);
                row.item.sensitive = false;
            }
        }
    }

    _refresh() {
        if (this._pending) return;
        this._pending = true;
        const cancellable = this._cancellable;
        Gio.DBus.session.call(BUS, PATH, BUS, 'Snapshot', null,
            new GLib.VariantType('(s)'), Gio.DBusCallFlags.NO_AUTO_START,
            1500, cancellable, (connection, result) => {
                // A cancelled request from an earlier enable() must not mutate
                // a subsequently enabled instance's menu or pending state.
                if (this._cancellable !== cancellable) return;
                this._pending = false;
                if (!this._button) return;
                try {
                    this._render(JSON.parse(connection.call_finish(result).deepUnpack()[0]));
                } catch (error) {
                    this._offline();
                }
            });
    }

    disable() {
        if (this._timer) GLib.source_remove(this._timer);
        this._timer = null;
        this._cancellable?.cancel();
        this._cancellable = null;
        this._button?.destroy();
        this._button = null;
        this._deviceSection = null;
        this._devices = null;
        this._lastState = null;
    }
}
