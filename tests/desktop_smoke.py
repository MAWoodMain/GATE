"""Run from repository root under a private dbus-run-session."""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gate.service import proxy, call
process = subprocess.Popen([sys.executable, '-m', 'gate', 'service', '--demo', '--interval', '0.1', '--no-control-api', '--ephemeral'])
try:
    for attempt in range(50):
        try:
            state=call(proxy(),'Snapshot')
            if len(state['instruments'])==3:
                break
        except Exception:
            pass
        time.sleep(.1)
    else:
        raise AssertionError('Service did not start')
    with tempfile.TemporaryDirectory() as folder:
        path=str(Path(folder)/'smoke.sqlite')
        call(proxy(),'Command',dict(action='record',path=path))
        time.sleep(.3)
        assert call(proxy(),'Snapshot')['recording']
        call(proxy(),'Command',dict(action='stop_recording'))
        time.sleep(.2)
        assert not call(proxy(),'Snapshot')['recording']
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import GLib, Gtk
    from gate.app import Window
    app=Gtk.Application(application_id='io.github.MAWoodMain.GATE.Smoke')
    app.register(None)
    window=Window(app)
    window.present()
    loop=GLib.MainLoop()
    errors = []
    dialogs = []  # Retain wrappers until Wayland has dispatched pending configure events.
    def check():
        try:
            assert len(window.labels)==3, window.status.get_text()
            assert window.get_title() == 'GATE · Devices & Settings'
            from gate.experiments import BUCK_CONVERTER
            import copy
            assert window.pages.get_visible_child_name() == 'devices'
            experiment_row = window.experiment_rows['buck_converter']
            assert experiment_row.get_header().get_text() == 'Experiments'
            window.navigation.select_row(experiment_row)
            assert window.pages.get_visible_child_name() == 'experiment:buck_converter'
            assert window.page_title.get_text() == BUCK_CONVERTER['name']
            page = window.experiment_pages['buck_converter']
            window.render_experiments({'buck_converter': BUCK_CONVERTER})
            assert window.experiment_pages['buck_converter'] is page
            assert window.navigation.get_selected_row() is experiment_row
            updated = copy.deepcopy(BUCK_CONVERTER)
            updated['name'] = 'Updated converter'
            second = copy.deepcopy(BUCK_CONVERTER)
            second['id'], second['name'] = 'second', 'Second experiment'
            window.render_experiments({'buck_converter': updated, 'second': second})
            assert len(window.experiment_rows) == 2
            assert window.page_title.get_text() == 'Updated converter'
            assert window.pages.get_visible_child_name() == 'experiment:buck_converter'
            window.render_experiments({'second': second})
            assert window.pages.get_visible_child_name() == 'devices'
            window.render_experiments({'buck_converter': BUCK_CONVERTER})
            window.navigation.select_row(window.devices_navigation)
            assert all('Connected' in label.get_text() or 'Demo' in label.get_text() for label in window.labels.values())
            assert not hasattr(window, 'voltage'), 'Measurement/output controls belong outside device manager'
            # Exercise missing-device sections, structured editor and service actions without touching hardware.
            fixture = dict(instruments=[], configured_resources=['/dev/usbtmc9', 'hantek://usb/3-1', 'serial:///dev/ttyUSB9?baud=9600', 'tcp://missing:5025'],
                           saved_devices={'/dev/usbtmc9': {'name': 'Remembered scope'}})
            window.render_devices(fixture)
            assert window.titles['/dev/usbtmc9'].get_text() == 'Remembered scope'
            assert all('Disconnected' in label.get_text() for label in window.labels.values())
            assert all(window.sections[kind].get_first_child() for kind in ('TMC', 'USB', 'Serial', 'Network'))
            usb_fields = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            read_usb = window.connection_fields('USB', 'hantek://usb/3-1', usb_fields)
            assert read_usb() == 'hantek://usb/3-1'
            aoip_fields = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            aoip_address = 'aoip:///dev/serial/by-id/fixture?baud=19200&bits=7&parity=even&stop=2&flow=xonxoff&idle=0.4'
            read_aoip = window.connection_fields('Serial', aoip_address, aoip_fields)
            assert read_aoip() == aoip_address, 'AOIP connection edits must preserve protocol and framing'
            dialog = window.edit_connection('Serial', 'serial:///dev/ttyUSB9?baud=9600')
            dialogs.append(dialog)
            entry = dialog.get_content_area().get_first_child().get_next_sibling()
            assert isinstance(entry, Gtk.Entry)
            entry.set_text('/dev/ttyUSB8')
            window.render_devices(fixture)
            assert dialog.get_visible() and entry.get_text() == '/dev/ttyUSB8', 'Refresh must preserve editor state'
            from unittest.mock import Mock
            original_command, original_systemctl, original_refresh = window.command, window.run_systemctl, window.refresh
            window.command = Mock()
            dialog.response(Gtk.ResponseType.ACCEPT)
            window.command.assert_called_once_with('edit_device', instrument='serial:///dev/ttyUSB9?baud=9600', resource='serial:///dev/ttyUSB8?baud=9600')
            window.refresh = Mock()
            for action in ('start', 'stop', 'restart'):
                window.run_systemctl = Mock(side_effect=lambda arguments, callback: callback(True, '', ''))
                window.service_action(action)
                assert window.run_systemctl.call_args.args[0] == [action, 'gate.service']
                assert not window.service_busy
            window.run_systemctl = Mock(side_effect=lambda arguments, callback: callback(False, '', 'Unit not found'))
            window.service_action('start')
            assert 'Unit not found' in window.message.get_text()
            window.online = False
            window.service_state = 'inactive'
            window.update_sensitivity()
            assert window.service_buttons['start'].get_sensitive()
            assert not window.service_buttons['stop'].get_sensitive()
            window.command, window.run_systemctl, window.refresh = original_command, original_systemctl, original_refresh
            window.online = True
            # Known physical identities share settings while addresses remain per-connection.
            profile = dict(type='FLUKE:45', manufacturer='FLUKE', model='45', serial='A', identity='FLUKE,45,A,1.7', name='Reference',
                           settings={'function':'FREQ', 'display_functions':['FREQ']}, functions=[dict(id='FREQ', label='Frequency', channel='primary.FREQ', unit='Hz', configurable=True)])
            fixture['instrument_profiles'] = {'instrument:A': profile, 'instrument:B':dict(profile, serial='B', name='Donor')}
            address = 'serial:///dev/ttyUSB9?baud=9600'
            fixture['connections'] = {address:dict(instrument='instrument:A', enabled=True)}
            fixture['instruments'] = [dict(id='instrument:A', resource=address, name='Reference', category='multimeter', simulated=False, error='')]
            window.render_devices(fixture)
            assert window.titles[address].get_text() == 'Reference'
            buttons = window.device_actions[address]
            toggle = buttons.get_first_child()
            settings_button = toggle.get_next_sibling()
            trash = settings_button.get_next_sibling()
            assert toggle.get_icon_name() == 'network-disconnect-symbolic'
            assert settings_button.get_icon_name() == 'emblem-system-symbolic'
            assert trash.get_icon_name() == 'user-trash-symbolic'
            assert trash.get_next_sibling() is None
            window.command = Mock()
            toggle.emit('clicked')
            window.command.assert_called_once_with('disconnect', instrument=address, forget=False)
            fixture['instruments'] = []
            window.render_devices(fixture)
            assert toggle.get_icon_name() == 'network-connect-symbolic'
            window.command.reset_mock()
            toggle.emit('clicked')
            window.command.assert_called_once_with('connect', resource=address)
            settings = window.device_settings(address)
            dialogs.append(settings)
            tabs = settings.get_content_area().get_first_child()
            assert tabs.get_n_pages() == 2
            assert tabs.get_tab_label_text(tabs.get_nth_page(0)) == 'Connection'
            assert tabs.get_tab_label_text(tabs.get_nth_page(1)) == 'Instrument'
            page = tabs.get_nth_page(1)
            children = []
            child = page.get_first_child()
            while child:
                children.append(child)
                child = child.get_next_sibling()
            name = next(child for child in children if isinstance(child, Gtk.Entry))
            name.set_text('Bench reference')
            window.command.reset_mock()
            next(child for child in children if isinstance(child, Gtk.Button) and child.get_label() == 'Save Instrument Settings').emit('clicked')
            window.command.assert_called_once_with('instrument_settings', instrument='instrument:A', name='Bench reference', settings={'function':'FREQ', 'display_functions':['FREQ']})
            # Generic selectors use driver catalogs, track the chosen function,
            # preserve saved overrides and distinguish keep-current from auto.
            from gate.ranges import range_catalog
            from gate.drivers.fluke45 import Fluke45
            from types import SimpleNamespace
            profile['range_controls'] = range_catalog(Fluke45(SimpleNamespace(resource=address), profile['identity']))
            profile['settings']['ranges'] = {'FREQ': '2'}
            range_dialog = window.device_settings(address)
            dialogs.append(range_dialog)
            page = range_dialog.get_content_area().get_first_child().get_nth_page(1)
            rows, direct = [], []
            child = page.get_first_child()
            while child:
                direct.append(child)
                if isinstance(child, Gtk.Box):
                    label = child.get_first_child()
                    selector = label.get_next_sibling() if isinstance(label, Gtk.Label) else None
                    if isinstance(selector, Gtk.DropDown):
                        rows.append((label.get_text(), child, selector))
                child = child.get_next_sibling()
            frequency = next(item for item in rows if item[0].startswith('Frequency'))
            voltage = next(item for item in rows if item[0].startswith('DC voltage'))
            assert frequency[1].get_visible() and not voltage[1].get_visible()
            assert frequency[2].get_selected() == 3
            frequency[2].set_selected(1)  # Autorange, distinct from keep-current.
            window.command.reset_mock()
            next(child for child in direct if isinstance(child, Gtk.Button) and child.get_label() == 'Save Instrument Settings').emit('clicked')
            window.command.assert_called_once_with('instrument_settings', instrument='instrument:A', name='Reference',
                settings={'function':'FREQ', 'display_functions':['FREQ'], 'ranges':{'FREQ':'auto'}})
            from dataclasses import asdict
            from gate.channels import channel_catalog
            from test_ranges import MultiChannelInstrument
            multi = MultiChannelInstrument()
            multi_settings = dict(channel_functions={'source': 'source.current', 'measure': 'measure.voltage'},
                                  ranges={'source.current.range': '10', 'measure.voltage.range': '1000'})
            multi_profile = dict(profile, type='Fixture:SMU', settings=multi_settings,
                functions=[asdict(item) for item in multi.functions()], channels=channel_catalog(multi),
                range_controls=range_catalog(multi))
            window.view_state['instrument_profiles']['instrument:A'] = multi_profile
            multi_dialog = window.device_settings(address)
            dialogs.append(multi_dialog)
            page = multi_dialog.get_content_area().get_first_child().get_nth_page(1)
            children = []
            child = page.get_first_child()
            while child:
                children.append(child)
                child = child.get_next_sibling()
            function_selectors = [child for child in children if isinstance(child, Gtk.DropDown)]
            assert len(function_selectors) == 2
            assert [selector.get_selected() for selector in function_selectors] == [2, 1]
            function_selectors[1].set_selected(2)  # Measurement resistance; source stays current.
            assert function_selectors[0].get_selected() == 2
            window.command.reset_mock()
            next(child for child in children if isinstance(child, Gtk.Button) and child.get_label() == 'Save Instrument Settings').emit('clicked')
            window.command.assert_called_once_with('instrument_settings', instrument='instrument:A', name='Reference',
                settings=dict(channel_functions={'source':'source.current', 'measure':'measure.resistance'}, ranges=multi_settings['ranges']))
            window.view_state['instrument_profiles']['instrument:A'] = profile
            copy_dialog = window.device_settings(address)
            dialogs.append(copy_dialog)
            page = copy_dialog.get_content_area().get_first_child().get_nth_page(1)
            child = page.get_first_child()
            while child and not (isinstance(child, Gtk.Button) and child.get_label() == 'Copy Instrument Settings'):
                child = child.get_next_sibling()
            assert child is not None, 'Compatible instruments must offer copy settings'
            window.command.reset_mock()
            child.emit('clicked')
            window.command.assert_called_once_with('copy_settings', instrument='instrument:A', source='instrument:B')
            for response, scope in ((1, 'connection'), (2, 'instrument')):
                forget = window.forget_device(address)
                dialogs.append(forget)
                assert forget.get_widget_for_response(1).get_label() == 'Remove Connection'
                assert forget.get_widget_for_response(2).get_label() == 'Forget Instrument'
                window.command.reset_mock()
                forget.response(response)
                expected = dict(instrument='instrument:A', scope=scope)
                if scope == 'connection': expected['resource'] = address
                window.command.assert_called_once_with('forget', **expected)
            window.command = original_command
            window.render_devices(fixture)
            window.update_sensitivity()
            window.notify('')
            print('PASS: sidebar navigation and experiment updates, D-Bus recording, device icons/toggle, settings tabs/copy, scoped forget, service controls')
        except Exception as exc:
            errors.append(exc)
        finally:
            # Let layout/paint settle after replacing rows before taking a snapshot.
            window.pending = True
            GLib.timeout_add(300, finish)
    def finish():
        try:
            if os.environ.get('GATE_SCREENSHOT'):
                from gi.repository import Gdk, Graphene
                paintable=Gtk.WidgetPaintable.new(window)
                snapshot=Gtk.Snapshot.new()
                paintable.snapshot(snapshot, window.get_width(), window.get_height())
                node=snapshot.to_node()
                rect=Graphene.Rect()
                rect.init(0,0,window.get_width(),window.get_height())
                texture=window.get_native().get_renderer().render_texture(node,rect)
                texture.save_to_png(os.environ['GATE_SCREENSHOT'])
        except Exception as exc:
            errors.append(exc)
        finally:
            window.close()
            loop.quit()
        return False
    GLib.timeout_add(1800,check)
    loop.run()
    if errors:
        raise errors[0]
finally:
    process.terminate()
    process.wait(timeout=10)
