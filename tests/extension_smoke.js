// GNOME 50 integration regression: run in a private, headless test Shell.
// gnome-shell-test-tool --headless --disable-animations --extension ZIP THIS_FILE
import GLib from 'gi://GLib';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as Scripting from 'resource:///org/gnome/shell/ui/scripting.js';

export const METRICS = {};
function check(condition, message) {
    if (!condition) throw new Error(message);
}

let started = false;
export function init() {
    // Some headless environments never complete the startup animation.
    GLib.timeout_add_once(GLib.PRIORITY_DEFAULT, 1500, () => {
        if (!started) Scripting.runPerfScript({run, METRICS}, GLib.getenv('SHELL_PERF_OUTPUT'));
    });
}

export async function run() {
    if (started) return;
    started = true;
    await Scripting.sleep(1000);
    const extension = Main.extensionManager.lookup('gate@mawoodmain.github.io')?.stateObj;
    check(extension?._button, 'GATE failed to enable');
    GLib.source_remove(extension._timer);
    extension._timer = null;
    extension._cancellable.cancel();
    extension._cancellable = null;
    const snapshot = {
        status: 'Ready', recording: false, updated_utc_ns: Date.now() * 1e6,
        instruments: [
            {id: 'demo:multimeter', name: 'SIMULATED · Multimeter', category: 'multimeter', simulated: true,
                readings: [{channel: 'voltage_dc', value: 3.3, unit: 'V', quality: 'ok'}]},
            {id: '/dev/usbtmc3', name: 'Siglent Technologies,SDS1202X-E,serial,1.3.26', simulated: false,
                readings: [{channel: 'C1.PKPK', value: 0.08, unit: 'V', quality: 'ok'},
                    {channel: 'C1.FREQ', value: null, unit: 'Hz', quality: 'unavailable'}]},
        ],
    };
    extension._render(snapshot);
    const selector = extension._deviceSection;
    const scope = extension._devices.get('/dev/usbtmc3');
    const meter = extension._devices.get('demo:multimeter');
    const voltage = scope.channels.get('C1.PKPK').item;
    const frequency = scope.channels.get('C1.FREQ').item;
    check(scope.item !== meter.item, 'Devices must have separate submenus');
    check(selector.firstMenuItem === scope.item, 'Real hardware should precede demos');
    extension._button.menu.open(false);
    scope.item.menu.open(false);
    voltage.grab_key_focus();
    await Scripting.sleep(250);
    for (let index = 0; index < 3; index++) {
        snapshot.instruments[1].readings[0].value += 0.001;
        extension._render(snapshot);
        await Scripting.sleep(150);
        check(extension._deviceSection === selector, 'Refresh replaced measurement submenu');
        check(extension._devices.get('/dev/usbtmc3') === scope, 'Refresh replaced device submenu');
        check(scope.channels.get('C1.PKPK').item === voltage, 'Refresh replaced measurement row');
        check(extension._button.menu.isOpen && scope.item.menu.isOpen, 'Refresh closed an open submenu');
        check(global.stage.get_key_focus() === voltage, 'Refresh lost keyboard focus');
    }
    check(voltage.label.text.includes('0.083000'), 'Live value did not update');
    frequency.emit('activate', null);
    check(extension._label.text.includes('unavailable'), 'Unavailable channel cannot be selected');
    snapshot.instruments[1].readings[1] = {channel: 'C1.FREQ', value: 1000, unit: 'Hz', quality: 'ok'};
    extension._render(snapshot);
    check(scope.channels.get('C1.FREQ').item === frequency, 'Quality change replaced measurement row');
    check(extension._label.text.includes('1000.0 Hz'), 'Selected channel did not recover');
    extension._offline();
    check(frequency.label.text.includes('offline') && !frequency.sensitive, 'Offline readings look live');
    extension._render(snapshot);
    check(frequency.sensitive, 'Recovered reading remains disabled');
    extension._button.menu.open(false);
    scope.item.menu.open(false);
    snapshot.instruments = snapshot.instruments.filter(device => !device.simulated);
    extension._render(snapshot);
    check(extension._devices.size === 1, 'Disconnected device was not removed');
    check(extension._devices.get('/dev/usbtmc3') === scope, 'Disconnect rebuilt remaining device');
    check(scope.item.menu.isOpen, 'Unrelated disconnect closed scope submenu');
    scope.item.menu.close(false);
    extension._button.menu.close(false);
    await Scripting.sleep(300);
    print('PASS: device grouping, stable open menus and keyboard focus across live updates, quality recovery and disconnects');
}
