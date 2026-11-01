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
    extension._refresh = () => {};
    const snapshot = {
        status: 'Ready', recording: false, updated_utc_ns: Date.now() * 1e6, commands: {},
        instrument_profiles: {
            'demo:multimeter': {settings: {display_functions: []}},
            scope: {settings: {display_functions: ['C1.PKPK']}},
        },
        instruments: [
            {id: 'demo:multimeter', name: 'SIMULATED · Multimeter', category: 'multimeter', simulated: true,
                readings: [{channel: 'voltage_dc', value: 3.3, unit: 'V', quality: 'ok'}]},
            {id: 'scope', resource: '/dev/usbtmc3', name: 'Siglent Technologies,SDS1202X-E,serial,1.3.26', simulated: false,
                readings: [{channel: 'C1.PKPK', value: 0.08, unit: 'V', quality: 'ok'},
                    {channel: 'C1.FREQ', value: null, unit: 'Hz', quality: 'unavailable'}]},
        ],
    };
    let sent;
    extension._sendCommand = (payload, done) => { sent = payload; done(null); };
    function complete(error = null) {
        check(['display_measurements', 'select_range'].includes(sent?.action), 'Wrong control command');
        if (!error) {
            const settings = snapshot.instrument_profiles[sent.instrument].settings;
            if (sent.action === 'display_measurements') settings.display_functions = [...sent.functions];
            if (sent.ranges) settings.ranges = {...(settings.ranges ?? {}), ...sent.ranges};
            if (sent.action === 'select_range') settings.ranges = {...(settings.ranges ?? {}), [sent.control]: sent.range};
        }
        snapshot.commands[sent.request] = error ? {state: 'failed', error} : {state: 'succeeded'};
        extension._render(snapshot);
    }
    extension._render(snapshot);
    snapshot.instruments.push({id: 'psu', name: 'Fixture PSU', category: 'power_supply', simulated: true,
        output_control_supported: true, readings: []});
    extension._render(snapshot);
    const outputItem = extension._devices.get('psu').outputItem;
    check(outputItem?.sensitive, 'Output-capable instrument has no output launcher');
    extension._render(snapshot);
    check(extension._devices.get('psu').outputItem === outputItem, 'Refresh replaced output launcher');
    snapshot.instruments.pop();
    extension._render(snapshot);
    const selector = extension._deviceSection;
    const scope = extension._devices.get('scope');
    const meter = extension._devices.get('demo:multimeter');
    const voltage = scope.functions.get('C1.PKPK').item;
    const frequency = scope.functions.get('C1.FREQ').item;
    check(scope.item !== meter.item, 'Devices must have separate submenus');
    check(selector.firstMenuItem === scope.item, 'Real hardware should precede demos');
    check(voltage.state && !frequency.state, 'Saved checkbox selection did not load');
    extension._button.menu.open(false);
    scope.item.menu.open(false);
    check(scope.channels.size === 1 && scope.channels.has('C1'), 'Scope inputs are not channel submenus');
    const scopeChannel = scope.channels.get('C1');
    scopeChannel.item.menu.open(false);
    scope.functions.get('C1.PKPK').menuItem.menu.open(false);
    check(scope.item.menu.firstMenuItem === scopeChannel.item, 'Instrument does not open to channels');
    check(scopeChannel.item.menu.firstMenuItem === scope.functions.get('C1.PKPK').menuItem,
        'Channel does not open to functions');
    voltage.grab_key_focus();
    await Scripting.sleep(250);
    for (let index = 0; index < 3; index++) {
        snapshot.instruments[1].readings[0].value += 0.001;
        extension._render(snapshot);
        await Scripting.sleep(150);
        check(extension._deviceSection === selector, 'Refresh replaced measurement submenu');
        check(extension._devices.get('scope') === scope, 'Refresh replaced device submenu');
        check(scope.functions.get('C1.PKPK').item === voltage, 'Refresh replaced measurement row');
        check(extension._button.menu.isOpen && scope.item.menu.isOpen, 'Refresh closed an open submenu');
        check(scopeChannel.item.menu.isOpen && scope.functions.get('C1.PKPK').menuItem.menu.isOpen,
            'Refresh closed channel or function submenu');
        check(global.stage.get_key_focus() === voltage, 'Refresh lost keyboard focus');
    }
    check(scope.functions.get('C1.PKPK').menuItem.label.text.includes('0.083000'), 'Live value did not update');
    frequency.activate(null);
    check(sent.functions.includes('C1.PKPK') && sent.functions.includes('C1.FREQ'), 'Second channel replaced the first');
    check(extension._button.menu.isOpen && scope.item.menu.isOpen, 'Toggling closed the measurement menu');
    complete();
    check(voltage.state && frequency.state, 'Multiple checkboxes cannot stay checked');
    check(extension._label.text.includes('0.083000') && extension._label.text.includes('unavailable'), 'Multiple scope readings are not shown');
    snapshot.instruments[1].readings[1] = {channel: 'C1.FREQ', value: 1000, unit: 'Hz', quality: 'ok'};
    extension._render(snapshot);
    check(scope.functions.get('C1.FREQ').item === frequency, 'Quality change replaced measurement row');
    check(extension._label.text.includes('1000.0 Hz'), 'Selected channel did not recover');
    snapshot.instruments[1].range_controls = [{id: 'C1.scale', label: 'Input scale', instrument_channel: 'C1',
        options: [{id: '0.1', label: '100 mV/div'}, {id: '1', label: '1 V/div'}]}];
    extension._render(snapshot);
    const sharedRange = scopeChannel.rangeMenus.get('C1.scale');
    check(sharedRange && !sharedRange.options.get('0.1').state && sharedRange.item.label.text.includes('Instrument setting'),
        'Channel-wide range without Auto does not preserve the instrument setting');
    sharedRange.options.get('0.1').activate(null);
    complete();
    check(sharedRange.options.get('0.1').state && voltage.state && frequency.state,
        'Channel-wide range changed the function selections');
    meter.functions.get('voltage_dc').item.activate(null);
    complete();
    check(extension._label.text.includes('3.3000 V') && extension._label.text.includes('1000.0 Hz'), 'Selections from different instruments do not coexist');
    voltage.activate(null);
    complete();
    check(!voltage.state && frequency.state && !extension._label.text.includes('0.083000'), 'Unchecking one channel hides other channels');
    frequency.activate(null);
    complete();
    check(!extension._label.text.includes('SDS1202X-E') && extension._label.text.includes('3.3000 V'), 'Hiding an instrument hides unrelated measurements');
    meter.functions.get('voltage_dc').item.activate(null);
    complete();
    check(extension._label.text === 'GATE', 'No selection should leave a compact GATE indicator');
    extension._render(snapshot);
    check(extension._label.text === 'GATE', 'Refresh auto-selected a hidden reading');
    // No arbitrary channel-count cap, including readings from the same device.
    for (let index = 0; index < 12; index++) snapshot.instruments[1].readings.push({channel: `extra${index}`, value: index, unit: 'V', quality: 'ok'});
    extension._render(snapshot);
    for (const row of scope.functions.values()) {
        row.item.activate(null);
        complete();
    }
    check(snapshot.instrument_profiles.scope.settings.display_functions.length === 14, 'Channel selection has an arbitrary limit');
    check(extension._label.text.includes('extra11'), 'Last selected channel is absent');
    extension._offline();
    check(scope.functions.get('C1.FREQ').menuItem.label.text.includes('offline') && !frequency.sensitive, 'Offline readings look live');
    extension._render(snapshot);
    check(frequency.sensitive && frequency.state, 'Recovery lost the selections');
    extension._button.menu.open(false);
    scope.item.menu.open(false);
    snapshot.instruments = snapshot.instruments.filter(device => !device.simulated);
    extension._render(snapshot);
    check(extension._devices.size === 1 && scope.item.menu.isOpen, 'Unrelated disconnect rebuilt or closed the scope menu');
    // Mutually exclusive DMM functions replace one another, but never other devices.
    const fluke = {id: 'fluke', resource: 'serial:///dev/fixture', name: 'FLUKE,45,fixture,1.7',
        simulated: false, category: 'multimeter', current_function: 'VDC',
        readings: [{channel: 'primary.VDC', value: 0.17, unit: 'V', quality: 'ok'}],
        functions: [
            {id: 'VDC', channel: 'primary.VDC', label: 'DC voltage', unit: 'V', configurable: true, exclusive_group: 'primary'},
            {id: 'OHMS', channel: 'primary.OHMS', label: 'Resistance', unit: 'Ω', configurable: true, exclusive_group: 'primary'},
        ], range_controls: ['VDC', 'OHMS'].map(id => ({id, function: id, instrument_channel: 'primary',
            label: id, options: [{id: 'auto', label: 'Autorange', automatic: true},
                {id: '1', label: 'Range 1'}, {id: '2', label: 'Range 2'}]}))};
    snapshot.instruments.push(fluke);
    snapshot.instrument_profiles.fluke = {settings: {display_functions: ['VDC']}};
    extension._render(snapshot);
    const meterDevice = extension._devices.get('fluke');
    const voltageRange = meterDevice.functions.get('VDC').rangeMenus.get('VDC');
    const primary = meterDevice.channels.get('primary');
    meterDevice.item.menu.open(false);
    primary.item.menu.open(false);
    meterDevice.functions.get('VDC').menuItem.menu.open(false);
    voltageRange.item.menu.open(false);
    check(meterDevice.item.menu.isOpen && primary.item.menu.isOpen && meterDevice.functions.get('VDC').menuItem.menu.isOpen &&
        voltageRange.item.menu.isOpen, 'Range submenu closed one of its ancestors');
    check(voltageRange.options.get('auto').state && voltageRange.item.label.text.includes('default'),
        'Auto is not the default range');
    check(!meterDevice.functions.get('OHMS').rangeMenus.get('OHMS').options.get('1').sensitive,
        'Inactive function range is enabled');
    voltageRange.options.get('2').activate(null);
    check(sent.action === 'select_range' && sent.control === 'VDC' && sent.range === '2', 'Manual range command is wrong');
    check(voltageRange.options.get('auto').state && !voltageRange.options.get('2').state,
        'Range changed before hardware confirmation');
    complete();
    check(voltageRange.options.get('2').state && !voltageRange.options.get('auto').state, 'Confirmed range did not load');
    check(voltageRange.item.menu.isOpen && primary.item.menu.isOpen, 'Range selection closed the nested menus');
    voltageRange.options.get('1').activate(null);
    complete('Meter refused range');
    check(voltageRange.options.get('2').state && extension._details.label.text.includes('Meter refused range'),
        'Failed range did not roll back or report its error');
    primary.item.menu.close(false);
    check(!meterDevice.functions.get('VDC').menuItem.menu.isOpen && !voltageRange.item.menu.isOpen,
        'Closing a channel left its function or range expanded');
    const ohms = meterDevice.functions.get('OHMS').item;
    check(meterDevice.functions.get('OHMS').menuItem.label.text.includes('not active'), 'Inactive resistance mode is missing');
    check(meterDevice.functions.size === 2, 'Unexpected secondary-display controls');
    ohms.activate(null);
    check(sent.ranges?.OHMS === 'auto', 'Enabling an unset function does not apply Auto');
    check(sent.functions.length === 1 && sent.functions[0] === 'OHMS', 'Mutually exclusive functions were combined');
    check(extension._label.text.includes('DC voltage') && !extension._label.text.includes('Resistance'), 'Panel changed before hardware confirmed');
    check(meterDevice.functions.get('OHMS').menuItem.label.text.includes('switching'), 'Pending change is not indicated');
    fluke.current_function = 'OHMS';
    fluke.readings = [{channel: 'primary.OHMS', value: 1000, unit: 'Ω', quality: 'ok'}];
    complete();
    check(ohms.state && !meterDevice.functions.get('VDC').item.state, 'Confirmed mode did not replace the old checkbox');
    check(extension._label.text.includes('Resistance') && extension._label.text.includes('Ω') && extension._label.text.includes('extra11'), 'Switching a DMM replaced other instruments');
    meterDevice.functions.get('VDC').item.activate(null);
    check(!sent.ranges?.VDC, 'Showing a function overwrote its saved manual range with Auto');
    complete('Meter refused voltage mode');
    check(ohms.state && extension._label.text.includes('Resistance'), 'Failed switch changed displayed function');
    check(extension._details.label.text.includes('Meter refused'), 'Failed switch is hidden');
    fluke.resource = 'tcp://meter:5025';
    extension._render(snapshot);
    check(extension._devices.get('fluke') === meterDevice && ohms.state, 'Connection handover lost stable selections');
    ohms.activate(null);
    complete();
    check(!extension._label.text.includes('FLUKE') && extension._label.text.includes('extra11'), 'DMM cannot hide all readings');
    // Missing instruments remain configurable so an unavailable selection can be hidden.
    snapshot.instrument_profiles.fluke = {identity: fluke.name, name: '', serial: 'fixture',
        category: 'multimeter', functions: fluke.functions, range_controls: fluke.range_controls,
        settings: {display_functions: ['OHMS'], function: 'OHMS'}};
    snapshot.instruments = snapshot.instruments.filter(item => item.id !== 'fluke');
    extension._render(snapshot);
    check(extension._label.text.includes('offline'), 'Selected disconnected instrument disappeared silently');
    check(extension._devices.get('fluke').functions.get('OHMS').item.state, 'Missing instrument lost its checkbox');
    check(!extension._devices.get('fluke').functions.get('OHMS').rangeMenus.get('OHMS').options.get('auto').sensitive,
        'Disconnected instrument range is enabled');
    extension._devices.get('fluke').functions.get('OHMS').item.activate(null);
    complete();
    check(!extension._label.text.includes('FLUKE'), 'A missing instrument cannot be hidden');
    const smu = {id: 'smu', name: 'Fixture,SMU,serial,1', simulated: true, category: 'smu',
        current_functions: {source: 'source.voltage', measure: 'measure.resistance'},
        readings: [{channel: 'source.value', value: 1, unit: 'V', quality: 'ok'},
            {channel: 'measure.value', value: 100, unit: 'Ω', quality: 'ok'}],
        functions: [
            {id: 'source.voltage', instrument_channel: 'source', channel: 'source.value', label: 'Source voltage', unit: 'V', configurable: true, exclusive_group: 'mode'},
            {id: 'source.current', instrument_channel: 'source', channel: 'source.value', label: 'Source current', unit: 'A', configurable: true, exclusive_group: 'mode'},
            {id: 'measure.resistance', instrument_channel: 'measure', channel: 'measure.value', label: 'Resistance', unit: 'Ω', configurable: true, exclusive_group: 'mode'},
            {id: 'measure.voltage', instrument_channel: 'measure', channel: 'measure.value', label: 'Measured voltage', unit: 'V', configurable: true, exclusive_group: 'mode'},
            {id: 'measure.current', instrument_channel: 'measure', channel: 'measure.value', label: 'Measured current', unit: 'A', configurable: true, exclusive_group: 'mode'},
        ]};
    snapshot.instruments.push(smu);
    snapshot.instrument_profiles.smu = {settings: {display_functions: ['source.voltage', 'measure.resistance']}};
    extension._render(snapshot);
    const smuDevice = extension._devices.get('smu');
    check(smuDevice.channels.size === 2 && smuDevice.channels.has('source') && smuDevice.channels.has('measure'),
        'SMU source and measurement are not separate channel submenus');
    check(smuDevice.functions.size === 5, 'Functions sharing reading keys collapsed into one row');
    smuDevice.functions.get('source.current').item.activate(null);
    check(sent.functions.includes('source.current') && sent.functions.includes('measure.resistance') &&
        !sent.functions.includes('source.voltage'), 'Source selection replaced the measurement channel');
    complete();
    smuDevice.functions.get('measure.voltage').item.activate(null);
    check(sent.functions.includes('source.current') && sent.functions.includes('measure.voltage') &&
        !sent.functions.includes('measure.resistance'), 'Measurement selection replaced the source channel');
    complete();
    scope.item.menu.close(false);
    extension._button.menu.close(false);
    await Scripting.sleep(300);
    print('PASS: instrument/channel/function nesting, shared and function ranges, Auto defaults, stable menus/focus, confirmation rollback, independent channels and transport handover');
}
