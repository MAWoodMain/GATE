import St from 'gi://St';
import Atk from 'gi://Atk';
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

// Shell's standard expander allows only one open submenu across the entire
// top menu. Track siblings per parent so opening a function keeps its channel
// and instrument expanded, while closing an ancestor closes its descendants.
const NestedSubMenuItem = GObject.registerClass(class NestedSubMenuItem extends PopupMenu.PopupSubMenuMenuItem {
    _setParent(parent) {
        super._setParent(parent);
        parent.connectObject('open-state-changed', (_menu, open) => {
            if (!open) this.menu.close(false);
        }, this);
        this.connect('destroy', () => {
            if (parent._gateOpenedChild === this.menu) parent._gateOpenedChild = null;
        });
    }

    _subMenuOpenStateChanged(menu, open) {
        const parent = this._parent;
        if (open) {
            const previous = parent?._gateOpenedChild;
            if (previous && previous !== menu) previous.close(false);
            if (parent) parent._gateOpenedChild = menu;
            this.add_style_pseudo_class('open');
            this.add_style_pseudo_class('checked');
            this.add_accessible_state(Atk.StateType.EXPANDED);
        } else {
            if (parent?._gateOpenedChild === menu) parent._gateOpenedChild = null;
            this.remove_style_pseudo_class('open');
            this.remove_style_pseudo_class('checked');
            this.remove_accessible_state(Atk.StateType.EXPANDED);
        }
    }
});

function functionGroup(fn) {
    const group = fn.exclusive_group ?? (fn.configurable ? 'primary' : null);
    return group === null ? null : JSON.stringify([fn.instrument_channel ?? 'primary', group]);
}

export default class GateExtension extends Extension {
    enable() {
        this._devices = new Map();
        this._lastState = null;
        this._pendingSelections = new Map();
        this._pendingRanges = new Map();
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
                channel: reading.channel, label: channelName(reading.channel), configurable: false,
                instrument_channel: /^(C\d+)\./.exec(reading.channel)?.[1] ?? 'primary'}));
    }

    _allInstruments(state) {
        const instruments = [...state.instruments];
        const ids = new Set(instruments.map(instrument => instrument.id));
        for (const [id, profile] of Object.entries(state.instrument_profiles ?? {})) {
            if (ids.has(id) || !profile.functions?.length) continue;
            instruments.push({id, name: profile.identity, custom_name: profile.name,
                serial: profile.serial, category: profile.category, simulated: id.startsWith('demo:'),
                functions: profile.functions, settings: profile.settings, readings: [], offline: true,
                channels: profile.channels, range_controls: profile.range_controls,
                range_unavailable_reason: profile.range_unavailable_reason,
                current_function: profile.settings?.function,
                current_functions: profile.settings?.channel_functions ?? {}});
        }
        return instruments;
    }

    _selectionFor(state, instrument) {
        const settings = state.instrument_profiles?.[instrument.id]?.settings ?? instrument.settings ?? {};
        // An explicit empty list means hidden, rather than falling back to a reading.
        if (Array.isArray(settings.display_functions)) return new Set(settings.display_functions);
        const functions = this._functions(instrument);
        const channelFunctions = {...(instrument.current_functions ?? {}), ...(settings.channel_functions ?? {})};
        const active = functions.filter(fn => channelFunctions[fn.instrument_channel ?? 'primary'] === fn.id);
        if (active.length) return new Set(active.map(fn => fn.id));
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
        for (const [instrument, pending] of this._pendingRanges) {
            const result = state.commands?.[pending.request];
            if (result?.state === 'succeeded' || result?.state === 'failed') {
                this._pendingRanges.delete(instrument);
                if (result.state === 'failed') this._selectionError = `Could not update range: ${result.error}`;
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
                device = {item: new NestedSubMenuItem(''), channels: new Map(), functions: new Map()};
                this._devices.set(instrument.id, device);
                this._deviceSection.addMenuItem(device.item, position);
            }
            if (instrument.output_control_supported && !device.outputItem) {
                device.outputItem = new PopupMenu.PopupMenuItem('Configure output…');
                device.item.menu.addMenuItem(device.outputItem, 0);
                device.outputItem.connect('activate', () => {
                    try {
                        const info = Gio.DesktopAppInfo.new('io.github.MAWoodMain.GATE.desktop');
                        const executable = info?.get_executable() ?? GLib.find_program_in_path('gate');
                        if (!executable) throw new Error('GATE launcher is not installed');
                        Gio.Subprocess.new([executable, 'gui', '--output', instrument.id], Gio.SubprocessFlags.NONE);
                    } catch (error) {
                        Main.notify('GATE', `Could not open output controls: ${error.message}`);
                    }
                });
            }
            if (device.outputItem) device.outputItem.sensitive = Boolean(instrument.output_control_supported && !instrument.offline);
            const title = deviceName(instrument);
            const serial = instrument.serial ?? (!instrument.custom_name ? instrument.name.split(',')[2]?.trim() : null);
            device.item.label.set_text((!instrument.simulated && serial ? `${title} · ${serial}` : title) + (instrument.offline ? ' (offline)' : ''));
            const functions = this._functions(instrument);
            const selected = this._selectionFor(state, instrument);
            const pending = this._pendingSelections.get(instrument.id);
            const busy = Boolean(pending || this._pendingRanges.has(instrument.id));
            const checked = pending ? new Set(pending.functions) : selected;
            const settings = state.instrument_profiles?.[instrument.id]?.settings ?? instrument.settings ?? {};
            const controls = instrument.range_controls ?? [];
            const channelDefinitions = new Map((instrument.channels ?? []).map(channel => [channel.id, channel]));
            for (const fn of functions) {
                const channel = fn.instrument_channel ?? 'primary';
                if (!channelDefinitions.has(channel)) channelDefinitions.set(channel, {id: channel, label: channelName(channel)});
            }
            for (const control of controls) {
                const channel = control.instrument_channel ?? 'primary';
                if (!channelDefinitions.has(channel)) channelDefinitions.set(channel, {id: channel, label: channelName(channel)});
            }
            for (const [id, row] of device.functions) {
                const fn = functions.find(item => item.id === id);
                if (!fn || (fn.instrument_channel ?? 'primary') !== row.channel) {
                    row.menuItem.destroy();
                    device.functions.delete(id);
                }
            }
            for (const [channel, row] of device.channels) {
                if (!channelDefinitions.has(channel)) {
                    row.item.destroy();
                    device.channels.delete(channel);
                }
            }
            for (const [id, definition] of channelDefinitions) {
                let channel = device.channels.get(id);
                if (!channel) {
                    channel = {item: new NestedSubMenuItem(''), rangeMenus: new Map()};
                    device.channels.set(id, channel);
                    device.item.menu.addMenuItem(channel.item);
                }
                channel.item.label.set_text(definition.label);
                this._renderRanges(channel, channel.item.menu, instrument,
                    controls.filter(control => (control.instrument_channel ?? 'primary') === id && !control.function), settings, busy);
            }
            const shown = [];
            functions.forEach(fn => {
                let row = device.functions.get(fn.id);
                if (!row) {
                    const channel = fn.instrument_channel ?? 'primary';
                    const menuItem = new NestedSubMenuItem('');
                    const item = new MeasurementSwitch('', false);
                    row = {item, menuItem, channel, rangeMenus: new Map()};
                    device.functions.set(fn.id, row);
                    device.channels.get(channel).item.menu.addMenuItem(menuItem);
                    menuItem.menu.addMenuItem(item);
                    item.connect('toggled', () => {
                        if (!this._syncingMenu) this._activateFunction(instrument.id, row.definition);
                    });
                }
                row.definition = fn;
                const current = instrument.current_functions?.[fn.instrument_channel ?? 'primary'] ??
                    ((fn.instrument_channel ?? 'primary') === 'primary' ? instrument.current_function : null);
                const reading = ((!fn.configurable || !current || current === fn.id)
                    ? instrument.readings.find(r => (r.channel === fn.channel && (!fn.unit || r.unit === fn.unit)) ||
                        r.channel.startsWith(`${fn.channel}.mod`)) : null) ??
                    (fn.configurable && current === fn.id
                        ? instrument.readings.find(r => r.channel.startsWith('primary.')) : null);
                const switching = pending && checked.has(fn.id) && !selected.has(fn.id);
                const value = switching ? (instrument.offline ? 'saving…' : 'switching…')
                    : reading ? readingValue(reading) : instrument.offline ? '— (offline)' : '— (not active)';
                const label = fn.label === fn.channel ? channelName(fn.channel) : fn.label;
                row.menuItem.label.set_text(`${label}   ${value}`);
                row.item.label.set_text('Show in top bar');
                row.item.sensitive = !busy;
                this._syncingMenu = true;
                row.item.setToggleState(checked.has(fn.id));
                this._syncingMenu = false;
                this._renderRanges(row, row.menuItem.menu, instrument,
                    controls.filter(control => control.function === fn.id && (control.instrument_channel ?? 'primary') === row.channel), settings, busy);
                if (!row.unavailable) {
                    row.unavailable = new PopupMenu.PopupMenuItem('Range selection unavailable', {reactive: false});
                    row.menuItem.menu.addMenuItem(row.unavailable);
                }
                row.unavailable.visible = !controls.some(control =>
                    (control.instrument_channel ?? 'primary') === row.channel && (!control.function || control.function === fn.id));
                // The panel uses confirmed preferences; a failed switch never
                // replaces an existing reading or labels old data with new units.
                if (selected.has(fn.id)) shown.push(`${label} ${reading ? readingValue(reading) : instrument.offline ? '— (offline)' : '— (not active)'}`);
            });
            if (shown.length) panel.push(`${title} · ${shown.join(' · ')}`);
        });
        this._empty.visible = instruments.length === 0;
        const prefix = `GATE${state.recording ? ' ●' : ''}`;
        this._label.set_text(panel.length ? `${prefix} · ${stale ? 'STALE · ' : ''}${panel.join(' | ')}` : prefix);
        this._details.label.set_text(this._selectionError ?? (this._pendingSelections.size || this._pendingRanges.size
            ? 'Updating instrument settings…'
            : `${stale ? 'Stale readings' : state.recording ? 'Recording' : 'Live'} · ${instruments.length} instruments · ${state.status}`));
    }

    _renderRanges(owner, menu, instrument, controls, settings, busy) {
        const ids = new Set(controls.map(control => control.id));
        for (const [id, row] of owner.rangeMenus) {
            if (!ids.has(id)) {
                row.item.destroy();
                owner.rangeMenus.delete(id);
            }
        }
        for (const control of controls) {
            let row = owner.rangeMenus.get(control.id);
            if (!row) {
                row = {item: new NestedSubMenuItem(''), options: new Map()};
                owner.rangeMenus.set(control.id, row);
                menu.addMenuItem(row.item);
                row.hint = new PopupMenu.PopupMenuItem('Select this function first', {reactive: false});
                row.item.menu.addMenuItem(row.hint);
            }
            const automatic = control.options.find(option => option.automatic);
            const saved = settings.ranges?.[control.id];
            const selected = saved ?? automatic?.id;
            const choice = control.options.find(option => option.id === selected);
            const pending = this._pendingRanges.get(instrument.id);
            const changing = pending?.control === control.id;
            row.item.label.set_text(`${control.function ? 'Range' : control.label}: ${changing ? 'setting…' :
                choice ? `${choice.automatic ? 'Auto' : choice.label}${saved === undefined ? ' (default)' : ''}` : 'Instrument setting'}`);
            const channel = control.instrument_channel ?? 'primary';
            const current = instrument.current_functions?.[channel] ??
                (channel === 'primary' ? instrument.current_function : null);
            const active = !control.function || current === control.function;
            row.hint.visible = !active && !instrument.offline;
            const ids = new Set(control.options.map(option => option.id));
            for (const [id, item] of row.options) {
                if (!ids.has(id)) {
                    item.destroy();
                    row.options.delete(id);
                }
            }
            for (const option of control.options) {
                let item = row.options.get(option.id);
                if (!item) {
                    item = new MeasurementSwitch('', false);
                    row.options.set(option.id, item);
                    row.item.menu.addMenuItem(item);
                    item.connect('toggled', () => {
                        if (!this._syncingMenu) this._activateRange(instrument.id, control.id, option.id);
                    });
                }
                item.label.set_text(option.automatic ? 'Auto' : option.label);
                item.sensitive = !busy && !instrument.offline && active;
                this._syncingMenu = true;
                item.setToggleState(option.id === selected);
                this._syncingMenu = false;
            }
        }
    }

    _activateRange(instrument, control, range) {
        if (!this._lastState || this._pendingSelections.has(instrument) || this._pendingRanges.has(instrument)) return;
        this._selectionError = null;
        const request = GLib.uuid_string_random();
        this._pendingRanges.set(instrument, {control, range, request});
        this._render(this._lastState);
        this._sendCommand({action: 'select_range', instrument, control, range, request}, error => {
            if (this._pendingRanges.get(instrument)?.request !== request) return;
            if (error) {
                this._pendingRanges.delete(instrument);
                this._selectionError = `Could not update range: ${error.message}`;
                if (this._lastState) this._render(this._lastState);
            } else {
                this._refresh();
            }
        });
    }

    _activateFunction(instrument, fn) {
        if (!this._lastState || this._pendingSelections.has(instrument) || this._pendingRanges.has(instrument)) return;
        this._selectionError = null;
        const device = this._allInstruments(this._lastState).find(item => item.id === instrument);
        if (!device) return;
        const functions = this._functions(device);
        const selected = this._selectionFor(this._lastState, device);
        const enabling = !selected.has(fn.id);
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
        const settings = this._lastState.instrument_profiles?.[instrument]?.settings ?? device.settings ?? {};
        const ranges = {};
        if (enabling) {
            for (const control of device.range_controls ?? []) {
                if ((control.instrument_channel ?? 'primary') !== (fn.instrument_channel ?? 'primary') ||
                    (control.function && control.function !== fn.id)) continue;
                const automatic = control.options.find(option => option.automatic);
                if (automatic && !Object.hasOwn(settings.ranges ?? {}, control.id)) ranges[control.id] = automatic.id;
            }
        }
        this._pendingSelections.set(instrument, {request, functions: chosen});
        this._render(this._lastState);
        this._sendCommand({action: 'display_measurements', instrument, functions: chosen, request,
            ...(Object.keys(ranges).length ? {ranges} : {})}, error => {
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
        this._pendingRanges.clear();
        this._label.set_text('GATE · offline');
        this._details.label.set_text('Start the GATE acquisition service');
        for (const device of this._devices.values()) {
            if (device.outputItem) device.outputItem.sensitive = false;
            for (const row of device.functions.values()) {
                row.menuItem.label.set_text(`${row.definition.label}   — (offline)`);
                row.item.sensitive = false;
            }
            for (const owner of [...device.channels.values(), ...device.functions.values()]) {
                for (const row of owner.rangeMenus.values()) {
                    for (const item of row.options.values()) item.sensitive = false;
                }
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
        this._pendingRanges = null;
    }
}
