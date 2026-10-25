"""GATE device management and settings; monitoring lives in GNOME Shell."""
import json
import subprocess
from pathlib import Path
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gio, GLib, Gtk
from gate.service import BUS, PATH
from gate.resources import documentation

CSS = b'''
window { background: #111820; color: #edf4fa; }
headerbar { background: #18232e; }
.hero { font-size: 28px; font-weight: 800; }
.muted { color: #9eafbd; }
.card { background: #1c2935; border-radius: 14px; padding: 18px; }
.tag { color: #67e8bc; font-weight: bold; }
'''

class Window(Gtk.ApplicationWindow):
    def __init__(self, application):
        super().__init__(application=application, title='GATE · Devices & Settings',
                         default_width=900, default_height=760)
        self.pending = False
        self.closed = False
        self.ids = None
        self.labels = {}
        self.titles = {}
        self.interval_dirty = False
        self.syncing = False
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(self.get_display(), provider, 600)
        header = Gtk.HeaderBar()
        self.set_titlebar(header)
        header.pack_start(self.button('Scan USB', lambda: self.command('scan')))
        scroll = Gtk.ScrolledWindow()
        self.set_child(scroll)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                        margin_top=26, margin_bottom=26, margin_start=26, margin_end=26)
        scroll.set_child(outer)
        outer.append(self.label('Devices & Settings', 'hero'))
        outer.append(self.label('Manage your bench here. Monitor measurements in the top bar; agents control the service.', 'muted'))
        self.status = self.label('Connecting to the GATE service…', 'muted')
        outer.append(self.status)
        row = Gtk.Box(spacing=8)
        self.resource = Gtk.Entry(placeholder_text='USB, TCP or serial instrument address', hexpand=True)
        row.append(self.resource)
        row.append(self.button('Add device', lambda: self.command('connect',resource=self.resource.get_text().strip())))
        outer.append(row)
        outer.append(self.label('Examples: /dev/usbtmc3  ·  tcp://192.168.1.100:5025  ·  serial:///dev/ttyUSB0?baud=9600', 'muted'))
        self.cards = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        outer.append(self.cards)
        settings = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=10)
        settings.add_css_class('card')
        settings.append(self.label('Service settings', 'tag'))
        row = Gtk.Box(spacing=10)
        row.append(self.label('Acquisition interval (seconds)'))
        self.interval = Gtk.SpinButton.new_with_range(.1,60,.1)
        self.interval.set_value(1)
        self.interval.connect('value-changed',self.interval_changed)
        row.append(self.interval)
        row.append(self.button('Apply',lambda:self.command('settings',interval=self.interval.get_value())))
        settings.append(row)
        settings.append(self.label('Devices share a host timebase. Actual timing depends on instrument response time.', 'muted'))
        outer.append(settings)
        agent = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=10)
        agent.add_css_class('card')
        agent.append(self.label('Agent access', 'tag'))
        self.api = self.label('Waiting for service…', 'muted')
        agent.append(self.api)
        guide = str(documentation('AGENT_INSTRUMENT_GUIDE.md'))
        agent.append(self.button('Open agent guide',lambda:Gio.AppInfo.launch_default_for_uri(Path(guide).as_uri(),None)))
        outer.append(agent)
        row = Gtk.Box(spacing=10)
        row.append(self.button('Add demo devices',lambda:self.command('demo')))
        row.append(self.button('Start background service',self.start_service))
        outer.append(row)
        outer.append(self.label('Closing this window leaves acquisition and agent access running.', 'muted'))
        self.connect('close-request',self.on_close)
        self.set_focus(self.resource)
        self.timer = GLib.timeout_add(1000,self.refresh)
        self.refresh()

    def label(self,text,css=None):
        label=Gtk.Label(label=text,xalign=0,wrap=True,selectable=css!='hero')
        if css:label.add_css_class(css)
        return label
    def button(self,text,callback):
        button=Gtk.Button(label=text)
        button.connect('clicked',lambda *_:callback())
        return button
    def interval_changed(self,*args):
        if not self.syncing:self.interval_dirty=True
    def start_service(self):
        subprocess.Popen(['systemctl','--user','start','gate'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        self.status.set_text('Starting background service…')
    def on_close(self,*args):
        self.closed=True
        GLib.source_remove(self.timer)
        return False
    def bus_call(self,method,payload,callback):
        connection=Gio.bus_get_sync(Gio.BusType.SESSION,None)
        params=GLib.Variant('(s)',(json.dumps(payload),)) if payload is not None else None
        def finished(conn,result):
            try:
                callback(json.loads(conn.call_finish(result).unpack()[0]),None)
            except Exception as exc:
                callback(None,exc)
        connection.call(BUS,PATH,BUS,method,params,GLib.VariantType.new('(s)'),
                        Gio.DBusCallFlags.NONE,2000,None,finished)
    def command(self,action,**args):
        if action=='settings':self.interval_dirty=False
        self.bus_call('Command',dict(action=action,**args),
                      lambda value,error:self.status.set_text(str(error) if error else 'Change queued'))
    def refresh(self):
        if self.pending or self.closed:return not self.closed
        self.pending=True
        def updated(state,error):
            self.pending=False
            if self.closed:return
            if error:
                self.status.set_text('Background service offline. Start it to manage devices.')
                self.api.set_text('Agent control API is offline')
                for label in self.labels.values():label.set_text('Service offline')
                return
            self.status.set_text(state['status'] + (' · Recording active' if state['recording'] else ''))
            if not self.interval_dirty:
                self.syncing=True
                self.interval.set_value(state.get('interval_s',1))
                self.syncing=False
            control=state.get('control_api')
            self.api.set_text((f"Control API: {control['url']}\nAuthentication: private local token\nAdvanced instrument commands: {'enabled' if control['allow_raw'] else 'disabled'}")
                              if control else 'Agent control API is disabled')
            live={device['id']:device for device in state['instruments']}
            ids=sorted(set(live)|set(state.get('configured_resources',[])))
            if ids!=self.ids:
                while self.cards.get_first_child():self.cards.remove(self.cards.get_first_child())
                self.labels={}
                self.titles={}
                for identity in ids:
                    device=live.get(identity)
                    box=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=8)
                    box.add_css_class('card')
                    title=device['name'] if device else 'Saved instrument'
                    title_label=self.label(title,'tag')
                    self.titles[identity]=title_label
                    box.append(title_label)
                    box.append(self.label(identity,'muted'))
                    label=self.label('','muted')
                    self.labels[identity]=label
                    box.append(label)
                    actions=Gtk.Box(spacing=8)
                    actions.append(self.button('Reconnect',lambda resource=identity:self.command('connect',resource=resource)))
                    actions.append(self.button('Remove device',lambda resource=identity:self.command('disconnect',instrument=resource)))
                    box.append(actions)
                    self.cards.append(box)
                if not ids:self.cards.append(self.label('No devices configured. Scan USB or add an instrument by address.','muted'))
                self.ids=ids
            for identity in ids:
                device=live.get(identity)
                self.titles[identity].set_text(device['name'] if device else 'Saved instrument')
                if not device:
                    text='Saved · disconnected'
                elif device['error']:
                    text='Connection error: '+device['error']
                else:
                    count=len(device.get('functions',[]))
                    text=f"{'Demo' if device['simulated'] else 'Connected'} · {device['category'].replace('_',' ')} · {count} measurement {'function' if count == 1 else 'functions'}"
                self.labels[identity].set_text(text)
        self.bus_call('Snapshot',None,updated)
        return True

def launch():
    app=Gtk.Application(application_id=BUS+'.App')
    app.connect('activate',lambda app:Window(app).present())
    app.run(None)
