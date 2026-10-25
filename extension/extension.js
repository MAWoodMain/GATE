import St from 'gi://St';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

const BUS = 'io.github.MAWoodMain.GATE';
const PATH = '/io/github/MAWoodMain/GATE';
export default class GateExtension extends Extension {
    enable() {
        this._button = new PanelMenu.Button(0.0, 'GATE');
        this._label = new St.Label({text: 'GATE · connecting', y_align: 2});
        this._button.add_child(this._label);
        this._details = new PopupMenu.PopupMenuItem('Waiting for acquisition service', {reactive: false});
        this._button.menu.addMenuItem(this._details);
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
    _refresh() {
        if (this._pending) return;
        this._pending = true;
        Gio.DBus.session.call(BUS, PATH, BUS, 'Snapshot', null,
            new GLib.VariantType('(s)'), Gio.DBusCallFlags.NO_AUTO_START,
            1500, this._cancellable, (connection, result) => {
                this._pending = false;
                if (!this._button) return;
                try {
                    const state = JSON.parse(connection.call_finish(result).deepUnpack()[0]);
                    const stale = Date.now() - state.updated_utc_ns / 1e6 > 5000;
                    const rows = [];
                    const choices = [];
                    for (const instrument of state.instruments) {
                        for (const reading of instrument.readings) {
                            const value = reading.value === null ? '—' : `${Number(reading.value).toPrecision(5)} ${reading.unit}`;
                            const prefix = instrument.simulated ? 'DEMO ' : '';
                            rows.push(`${prefix}${instrument.id} · ${reading.channel}: ${value}`);
                            if (reading.quality === 'ok') choices.push({key: `${instrument.id}/${reading.channel}`, text: `${prefix}${reading.channel} ${value}`});
                        }
                    }
                    this._choices = choices;
                    const selected = choices.find(c => c.key === this._selected) ?? choices[0];
                    this._label.set_text(`GATE${state.recording ? ' ●' : ''} · ${stale ? 'STALE' : (selected?.text ?? 'no readings')}`);
                    this._details.label.set_text(rows.join('\n') || state.status);
                    if (this._selector) this._selector.destroy();
                    this._selector = new PopupMenu.PopupSubMenuMenuItem('Display measurement');
                    for (const choice of choices) {
                        const item = new PopupMenu.PopupMenuItem(choice.text);
                        item.connect('activate', () => { this._selected = choice.key; this._refresh(); });
                        this._selector.menu.addMenuItem(item);
                    }
                    this._button.menu.addMenuItem(this._selector);
                } catch (error) {
                    this._label.set_text('GATE · offline');
                    this._details.label.set_text('Start the GATE acquisition service');
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
        this._selector = null;
    }
}
