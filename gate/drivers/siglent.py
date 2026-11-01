"""SDS1202X-E channel-aware measurements and bounded native-timing captures."""
import math
import re
import time
from gate.model import Reading, MeasurementFunction
from gate.ranges import numeric_control, range_option

_NUMBER = re.compile(r"^([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?)([a-zA-Z%]*)$")

def parse_measurement(response, parameter, unit):
    parts = [p.strip() for p in response.split(",")]
    for index, part in enumerate(parts[:-1]):
        if part.upper().endswith(parameter):
            match = _NUMBER.fullmatch(parts[index + 1])
            if match:
                value = float(match[1])
                if match[2] and match[2].lower() != unit.lower():
                    raise ValueError("Unexpected measurement unit")
                if math.isfinite(value) and abs(value) < 1e30:
                    return value
                return None
    if any(s in response.upper() for s in ("UNDEF", "****", "OVER", "INVALID")):
        return None
    raise ValueError(f"Unexpected {parameter} response: {response[:160]}")

class SiglentSDS:
    device_type = 'SIGLENT:SDS1202X-E'
    category = "oscilloscope"
    experiment_capabilities = ('measure.scalar', 'capture.waveform')
    waveform_capabilities = {channel:dict(unit='V', phases=['settled', 'transition'], max_points=16384, configurable_window=False,
        verification='protocol-fixture tested; hardware waveform verification pending') for channel in ('C1', 'C2')}
    simulated = False
    @staticmethod
    def matches(identity):
        fields = identity.upper().split(",")
        return len(fields) >= 2 and "SIGLENT" in fields[0] and fields[1].strip() == "SDS1202X-E"

    def __init__(self, transport, identity):
        self.transport = transport
        self.id = transport.resource
        self.name = identity.strip()
        self._waveform_requests = {}
        self._experiment_channels = None
        self._saved_channels = None

    def functions(self):
        labels = {"PKPK": "Peak-to-peak", "RMS": "RMS voltage", "FREQ": "Frequency"}
        return [MeasurementFunction(f"{channel}.{parameter}", f"{channel} · {labels[parameter]}",
                                    f"{channel}.{parameter}", unit, instrument_channel=channel)
                for channel in ("C1", "C2")
                for parameter, unit in (("PKPK", "V"), ("RMS", "V"), ("FREQ", "Hz"))]

    def range_controls(self):
        # Input-referred scale; the existing probe factor is retained.
        scales = [.0005, .001, .002, .005, .01, .02, .05, .1, .2, .5, 1., 2., 5., 10.]
        return [numeric_control(channel, channel+' input vertical scale (before probe factor)',
                                'V/div', scales, automatic=False, instrument_channel=channel) for channel in ('C1', 'C2')]

    def select_range(self, control_id, option_id):
        control, option = range_option(self, control_id, option_id)
        if self._waveform_requests:
            raise ValueError('Finish waveform capture before changing vertical scale')
        command = control.id+':VDIV'
        def attenuation():
            value = float(self.transport.query(control.id+':ATTN?').strip().split()[-1])
            if not math.isfinite(value) or value <= 0:
                raise ValueError('Invalid probe attenuation')
            return value
        factor = attenuation()
        desired = option.value*factor
        self.transport.write(command+' '+format(desired, '.12g')+'V')
        actual = waveform_scalar(self.transport.query(command+'?'), 'voltage')
        if attenuation() != factor or not math.isclose(actual, desired, rel_tol=1e-6, abs_tol=0.):
            raise ValueError('Scope did not confirm the requested vertical scale')

    def channel_state(self, channel):
        state = self.transport.query(channel+':TRA?').strip().split()[-1].upper()
        if state not in ('ON', 'OFF'):
            raise ValueError('Invalid scope channel state')
        return state

    def begin_experiment_channels(self, channels):
        channels = {channel.split('.')[0] for channel in channels}
        if not channels <= {'C1', 'C2'} or self._saved_channels is not None:
            raise ValueError('Invalid or overlapping scope channel configuration')
        self._saved_channels = {channel:self.channel_state(channel) for channel in ('C1', 'C2')}
        self._experiment_channels = channels
        for channel, old in self._saved_channels.items():
            desired = 'ON' if channel in channels else 'OFF'
            if old != desired:
                self.transport.write(channel+':TRA '+desired)
            if self.channel_state(channel) != desired:
                raise ValueError('Scope did not apply channel selection')

    def end_experiment_channels(self):
        errors = []
        for channel, state in (self._saved_channels or {}).items():
            try:
                if self.channel_state(channel) != state:
                    self.transport.write(channel+':TRA '+state)
            except Exception as exc:
                errors.append(str(exc))
        self._saved_channels = self._experiment_channels = None
        if errors:
            raise RuntimeError('; '.join(errors))

    def read(self):
        readings = []
        # PAVA on an off channel can enable its trace. Never query it. While a
        # single shot is armed/stopped, also avoid disturbing its acquisition.
        armed = any(item['phase'] == 'transition' for item in self._waveform_requests.values())
        for channel in ('C1', 'C2'):
            if self._experiment_channels is not None:
                enabled = channel in self._experiment_channels
            elif armed:
                enabled = any(item['channel'] == channel for item in self._waveform_requests.values())
            else:
                enabled = self.channel_state(channel) == 'ON'
            for parameter, unit in (("PKPK", "V"), ("RMS", "V"), ("FREQ", "Hz")):
                if not enabled or armed:
                    readings.append(Reading(f"{channel}.{parameter}", None, unit, 'disabled' if not enabled else 'unavailable'))
                    continue
                response = self.transport.query(f"{channel}:PAVA? {parameter}")
                value = parse_measurement(response, parameter, unit)
                readings.append(Reading(f"{channel}.{parameter}", value, unit,
                                        "ok" if value is not None else "unavailable"))
        return readings

    @staticmethod
    def response_value(response, command):
        response = response.strip()
        header = command.rstrip('?')
        if response.upper().startswith(header.upper()+' '):
            return response[len(header):].strip()
        return response

    def begin_waveform(self, request):
        import uuid
        channel = request['channel']
        if request['phase'] not in ('settled', 'transition') or channel not in ('C1', 'C2'):
            raise ValueError('Unsupported scope waveform request')
        if any(request.get(key) is not None for key in ('window_s', 'pretrigger')):
            raise ValueError('Configure capture timing on the scope; leave window/pretrigger unset')
        if self._waveform_requests:
            raise ValueError('Only one capture can own the scope acquisition at a time')
        item = dict(request)
        item['deadline'] = time.monotonic()+request.get('timeout_s', 3.)
        token = uuid.uuid4().hex
        if request['phase'] == 'transition':
            if not request.get('trigger'):
                raise ValueError('Single shot requires an explicit trigger')
            # Save all changed settings before issuing the first write.
            commands = ['TRMD', 'TRSE', channel+':TRLV', channel+':TRSL', channel+':TRCP']
            item['restore'] = [(command, self.response_value(self.transport.query(command+'?'), command)) for command in commands]
            self._waveform_requests[token] = item
            try:
                self.transport.write('TRMD STOP')
                self.transport.write('TRSE EDGE,SR,'+channel+',HT,OFF')
                self.transport.write(channel+':TRCP DC')
                self.transport.write(channel+':TRLV '+str(request['trigger']['level']))
                self.transport.write(channel+':TRSL '+('NEG' if request['trigger']['slope'] == 'falling' else 'POS'))
                actual = waveform_scalar(self.transport.query(channel+':TRLV?'), 'voltage')
                if not math.isclose(actual, request['trigger']['level'], rel_tol=.01, abs_tol=1e-6):
                    raise ValueError('Scope clamped the trigger level; adjust vertical scale/offset')
                selected = re.split(r'[ ,]+', self.response_value(self.transport.query('TRSE?'), 'TRSE').upper())
                slope = 'NEG' if request['trigger']['slope'] == 'falling' else 'POS'
                if (not selected or selected[0] != 'EDGE' or 'SR' not in selected or
                    selected[selected.index('SR')+1:selected.index('SR')+2] != [channel] or
                    self.transport.query(channel+':TRSL?').strip().split()[-1].upper() != slope or
                    self.transport.query(channel+':TRCP?').strip().split()[-1].upper() != 'DC'):
                    raise ValueError('Scope did not apply the requested edge trigger')
                self.transport.query('INR?')  # clear the previous acquisition event
                self.transport.write('TRMD SINGLE')
                deadline = min(item['deadline'], time.monotonic()+1.)
                while True:
                    state = int(self.transport.query('INR?').strip().split()[-1])
                    if state & 1:
                        raise ValueError('Scope triggered before the experiment transition')
                    if state & 8192:
                        break
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Scope did not become trigger-ready')
                    time.sleep(.01)
            except Exception:
                self.cancel_waveform(token)
                raise
        elif request.get('trigger') is not None:
            raise ValueError('Settled snapshots use the native trigger settings')
        self._waveform_requests[token] = item
        return token

    def poll_waveform(self, token):
        from gate.waveforms import Waveform
        request = self._waveform_requests[token]
        transition = request['phase'] == 'transition'
        channel = request['channel']
        transport = self.transport
        deadline = request['deadline']
        def query(command, binary=False):
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Waveform transfer deadline exceeded')
            saved_timeout = transport.timeout
            transport.timeout = min(saved_timeout, remaining)
            try:
                return transport.query_binary(command, maximum=request['points']) if binary else transport.query(command)
            finally:
                transport.timeout = saved_timeout
        if transition:
            state = int(query('INR?').strip().split()[-1])
            request['acquired'] = request.get('acquired', False) or bool(state & 1)
            mode = query('TRMD?').strip().split()[-1].upper()
            if not request['acquired'] or mode != 'STOP':
                return None
        if query(channel+':UNIT?').strip().split()[-1].upper() != 'V':
            raise ValueError('Native waveform channel must report voltage units')
        if query(channel+':TRA?').strip().split()[-1].upper() != 'ON':
            raise ValueError('Enable the waveform channel on the scope before capture')
        setup = query('WFSU?')
        tokens = re.split(r'[ ,]+', setup.strip())
        values = {}
        for index, item in enumerate(tokens[:-1]):
            if item.upper() in ('SP', 'NP', 'FP'):
                values[item.upper()] = int(tokens[index+1])
        if set(values) != {'SP', 'NP', 'FP'} or any(value < 0 for value in values.values()):
            raise ValueError('Unsupported native waveform transfer setup')
        def scales():
            return tuple(waveform_scalar(query(command), quantity) for command, quantity in
                [(channel+':VDIV?', 'voltage'), (channel+':OFST?', 'voltage'), ('TDIV?', 'time'), ('TRDL?', 'time'), ('SARA?', 'rate')])
        before = scales()
        vdiv, offset, tdiv, delay, rate = before
        if min(vdiv, tdiv, rate) <= 0:
            raise ValueError('Invalid positive waveform scale or sample rate')
        count = waveform_scalar(query('SANU? '+channel), 'count')
        if count < 2 or count > 100000000:
            raise ValueError('Invalid scope acquisition point count')
        # A transient must span the record, not just its first pre-trigger samples.
        # Settled ripple keeps native sample spacing to avoid added decimation.
        stride = max(1, math.ceil(count/request['points'])) if transition else 1
        try:
            # Limit only transfer framing, not acquisition, trigger or front-panel scales.
            transport.write(f"WFSU SP,{stride},NP,{request['points']},FP,0")
            payload = query(channel+':WF? DAT2', binary=True)
            after = scales()
            if before != after or query(channel+':UNIT?').strip().split()[-1].upper() != 'V':
                raise ValueError('Scope scales or units changed during waveform transfer')
        finally:
            transport.write(f"WFSU SP,{values['SP']},NP,{values['NP']},FP,{values['FP']}")
        if len(payload) < 2:
            raise ValueError('Scope returned too few waveform samples')
        codes = [value if value < 128 else value-256 for value in payload]
        waveform = Waveform(channel, [value*(vdiv/25)-offset for value in codes], stride/rate, delay-7*tdiv, 'V',
            quality='clipped' if any(value in (-128, 127) for value in codes) else 'ok',
            metadata=dict(native_settings=True, verification='protocol-fixture tested; hardware waveform accuracy not yet verified',
                          vdiv=vdiv, offset_v=offset, time_div_s=tdiv, trigger_delay_s=delay, sample_rate_hz=rate,
                          acquisition=('Single shot; fresh acquisition event observed, scope stopped' if transition else 'Existing scope waveform; no new-trigger/freshness guarantee'),
                          transfer_stride=stride, acquired_points=count))
        if request.get('screenshot', transition):
            from dataclasses import replace
            from gate.waveforms import validate_screenshot
            try:
                saved_timeout = transport.timeout
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Screenshot transfer deadline exceeded')
                transport.timeout = remaining
                try:
                    image = transport.query_binary('SCDP', maximum=1048576, terminator=b'\n', framing='bmp')
                finally:
                    transport.timeout = saved_timeout
                validate_screenshot(image)
                waveform = replace(waveform, screenshot=image)
            except Exception as exc:
                waveform = replace(waveform, screenshot_error=str(exc))
        return waveform

    def cancel_waveform(self, token):
        item = self._waveform_requests.pop(token, None)
        if item and item.get('restore'):
            errors = []
            # Stop first; restore source/level/slope/coupling before acquisition mode.
            commands = [('TRMD', 'STOP')]+item['restore'][1:]+item['restore'][:1]
            for command, value in commands:
                try:
                    self.transport.write(command+' '+value)
                except Exception as exc:
                    errors.append(str(exc))
            if errors:
                raise RuntimeError('Scope trigger restoration failed: '+'; '.join(errors))

    def close(self):
        getattr(self, '_waveform_requests', {}).clear()
        self.transport.close()


def waveform_scalar(response, quantity):
    """Parse header-on/off vertical/time/rate query responses without changing CHDR."""
    token = response.strip().split()[-1]
    match = re.fullmatch(r'([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?)(.*)', token)
    if not match:
        raise ValueError('Invalid waveform scale response')
    value, suffix = float(match[1]), match[2].upper()
    scales = {'G':1e9, 'M':1e6, 'K':1e3} if quantity in ('rate', 'count') else {'M':1e-3, 'U':1e-6, 'N':1e-9, 'P':1e-12}
    expected = 'PTS' if quantity == 'count' else 'SA/S' if quantity == 'rate' else 'V' if quantity == 'voltage' else 'S'
    if suffix:
        if suffix == expected:
            pass
        elif suffix[:1] in scales and suffix[1:] == expected:
            value *= scales[suffix[0]]
        else:
            raise ValueError('Unexpected waveform scale units')
    if not math.isfinite(value):
        raise ValueError('Non-finite waveform scale')
    return value
