import St from 'gi://St';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

const BUS = 'io.github.MAWoodMain.GATE';
const PATH = '/io/github/MAWoodMain/GATE';

function deviceName(instrument) {
    if (instrument.custom_name) return instrument.custom_name;
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

// Measurement toggles stay open for mouse, Space and Enter so several choices
// can be made without reopening the instrument menu.
const MeasurementSwitch = GObject.registerClass(class MeasurementSwitch extends PopupMenu.PopupSwitchMenuItem {
    activate(_event) {
        if (this.sensitive) this.toggle();
    }
});

function functionGroup(fn) {
    return fn.exclusive_group ?? (fn.configurable ? 'primary' : null);
}

export default class GateExtension extends Extension {
    enable() {
        this._devices = new Map();
        this._lastState = null;
        this._pendingSelections = new Map();
        this._selectionError = null;
        this._button = new PanelMenu.Button(0.0, 'GATE');
        this._label = new St.Label({text: 'GATE · connecting', y_align: 2});
        this._button.add_child(this._label);
        this._details = new PopupMenu.PopupMenuItem('Waiting for acquisition service', {reactive: false});
        this._button.menu.addMenuItem(this._details);
        this._button.menu.addMenuItem(new PopupMenu.PopupMenuItem('Measurements in top bar', {reactive: false}));
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

    _functions(instrument) {
        return instrument.functions?.length ? instrument.functions
            : instrument.readings.map(reading => ({id: reading.channel,
                channel: reading.channel, label: channelName(reading.channel), configurable: false}));
    }

    _allInstruments(state) {
        const instruments = [...state.instruments];
        const ids = new Set(instruments.map(instrument => instrument.id));
        for (const [id, profile] of Object.entries(state.instrument_profiles ?? {})) {
            if (ids.has(id) || !profile.functions?.length) continue;
            instruments.push({id, name: profile.identity, custom_name: profile.name,
                serial: profile.serial, category: profile.category, simulated: id.startsWith('demo:'),
                functions: profile.functions, settings: profile.settings, readings: [], offline: true,
                current_function: profile.settings?.function});
        }
        return instruments;
    }

    _selectionFor(state, instrument) {
        const settings = state.instrument_profiles?.[instrument.id]?.settings ?? instrument.settings ?? {};
        // An explicit empty list means hidden, rather than falling back to a reading.
        if (Array.isArray(settings.display_functions)) return new Set(settings.display_functions);
        const functions = this._functions(instrument);
        const current = functions.find(fn => fn.id === settings.function) ??
            functions.find(fn => fn.id === instrument.current_function) ??
            functions.find(fn => instrument.readings.some(reading => reading.channel === fn.channel)) ?? functions[0];
        return new Set(current ? [current.id] : []);
    }

    _render(state) {
        this._lastState = state;
        for (const [instrument, pending] of this._pendingSelections) {
            const result = state.commands?.[pending.request];
            if (result?.state === 'succeeded' || result?.state === 'failed') {
                this._pendingSelections.delete(instrument);
                if (result.state === 'failed') this._selectionError = `Could not update measurements: ${result.error}`;
            }
        }
        const stale = Date.now() - state.updated_utc_ns / 1e6 > Math.max(5000, (state.interval_s ?? 1) * 2000);
        // Keep actors and their open/focus state. Only topology changes add/remove
        // items; values and quality changes update existing labels in place.
        const instruments = this._allInstruments(state).sort((a, b) =>
            Number(a.simulated) - Number(b.simulated) ||
            deviceName(a).localeCompare(deviceName(b)) || a.id.localeCompare(b.id));
        const ids = new Set(instruments.map(instrument => instrument.id));
        for (const [id, device] of this._devices) {
            if (!ids.has(id)) {
                device.item.destroy();
                this._devices.delete(id);
            }
        }
        const panel = [];
        instruments.forEach((instrument, position) => {
            let device = this._devices.get(instrument.id);
            if (!device) {
                device = {item: new PopupMenu.PopupSubMenuMenuItem(''), channels: new Map()};
                this._devices.set(instrument.id, device);
                this._deviceSection.addMenuItem(device.item, position);
            }
            const title = deviceName(instrument);
            const serial = instrument.serial ?? (!instrument.custom_name ? instrument.name.split(',')[2]?.trim() : null);
            device.item.label.set_text((!instrument.simulated && serial ? `${title} · ${serial}` : title) + (instrument.offline ? ' (offline)' : ''));
            const functions = this._functions(instrument);
            const selected = this._selectionFor(state, instrument);
            const pending = this._pendingSelections.get(instrument.id);
            const checked = pending ? new Set(pending.functions) : selected;
            const channels = new Set(functions.map(fn => fn.channel));
            for (const [channel, row] of device.channels) {
                if (!channels.has(channel)) {
                    row.item.destroy();
                    device.channels.delete(channel);
                }
            }
            const shown = [];
            functions.forEach((fn, index) => {
                let row = device.channels.get(fn.channel);
                if (!row) {
                    const item = new MeasurementSwitch('', false);
                    row = {item};
                    device.channels.set(fn.channel, row);
                    device.item.menu.addMenuItem(item, index);
                    item.connect('toggled', () => {
                        if (!this._syncingMenu) this._activateFunction(instrument.id, row.definition);
                    });
                }
                row.definition = fn;
                const reading = instrument.readings.find(r => r.channel === fn.channel ||
                    r.channel.startsWith(`${fn.channel}.mod`)) ??
                    (fn.configurable && instrument.current_function === fn.id
                        ? instrument.readings.find(r => r.channel.startsWith('primary.')) : null);
                const switching = pending && checked.has(fn.id) && !selected.has(fn.id);
                const value = switching ? (instrument.offline ? 'saving…' : 'switching…')
                    : reading ? readingValue(reading) : instrument.offline ? '— (offline)' : '— (not active)';
                const label = fn.label === fn.channel ? channelName(fn.channel) : fn.label;
                row.item.label.set_text(`${label}   ${value}`);
                row.item.sensitive = !pending;
                this._syncingMenu = true;
                row.item.setToggleState(checked.has(fn.id));
                this._syncingMenu = false;
                // The panel uses confirmed preferences; a failed switch never
                // replaces an existing reading or labels old data with new units.
                if (selected.has(fn.id)) shown.push(`${label} ${reading ? readingValue(reading) : instrument.offline ? '— (offline)' : '— (not active)'}`);
            });
            if (shown.length) panel.push(`${title} · ${shown.join(' · ')}`);
        });
        this._empty.visible = instruments.length === 0;
        const prefix = `GATE${state.recording ? ' ●' : ''}`;
        this._label.set_text(panel.length ? `${prefix} · ${stale ? 'STALE · ' : ''}${panel.join(' | ')}` : prefix);
        this._details.label.set_text(this._selectionError ?? (this._pendingSelections.size
            ? 'Updating displayed measurements…'
            : `${stale ? 'Stale readings' : state.recording ? 'Recording' : 'Live'} · ${instruments.length} instruments · ${state.status}`));
    }

    _activateFunction(instrument, fn) {
        if (!this._lastState || this._pendingSelections.has(instrument)) return;
        this._selectionError = null;
        const device = this._allInstruments(this._lastState).find(item => item.id === instrument);
        if (!device) return;
        const functions = this._functions(device);
        const selected = this._selectionFor(this._lastState, device);
        if (selected.has(fn.id)) {
            selected.delete(fn.id);
        } else {
            const group = functionGroup(fn);
            if (group) {
                for (const other of functions) {
                    if (functionGroup(other) === group) selected.delete(other.id);
                }
            }
            selected.add(fn.id);
        }
        const request = GLib.uuid_string_random();
        const chosen = functions.filter(item => selected.has(item.id)).map(item => item.id);
        this._pendingSelections.set(instrument, {request, functions: chosen});
        this._render(this._lastState);
        this._sendCommand({action: 'display_measurements', instrument, functions: chosen, request}, error => {
            if (this._pendingSelections.get(instrument)?.request !== request) return;
            if (error) {
                this._pendingSelections.delete(instrument);
                this._selectionError = `Could not update measurements: ${error.message}`;
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
        this._pendingSelections.clear();
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
        this._pendingSelections = null;
    }
}
