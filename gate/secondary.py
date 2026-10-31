"""Optional secondary measurement examples, expressed entirely as test JSON."""

def add_converter_secondary(spec, test_id):
    spec['secondary_measurements'] = {
        'dut_temperature_c': dict(label='DUT temperature', unit='°C', role='thermal', quantity='dut_temperature', required=False, kind='temperature', location='DUT'),
        'ambient_temperature_c': dict(label='Ambient temperature', unit='°C', role='thermal', quantity='ambient_temperature', required=False, kind='temperature', location='Ambient'),
    }
    spec['derived']['temperature_rise_c'] = dict(label='Temperature rise above ambient', unit='°C', expression='dut_temperature_c - ambient_temperature_c', aggregate='point')
    spec['plots'] += [dict(kind='line', data='secondary' if key != 'temperature_rise_c' else 'points', x='elapsed_s', y=key, title=label+' versus time') for key, label in
                      [('dut_temperature_c', 'DUT temperature'), ('ambient_temperature_c', 'Ambient temperature'), ('temperature_rise_c', 'Temperature rise')]]
    spec['waveforms'] = {
        'ripple':dict(label='Output ripple under load', unit='V', role='scope', channel='C1', required=False,
            phase='settled', when='output_current_a > 0', points=2048, window_s=None, pretrigger=None,
            timeout_s=3, max_captures=8, trigger=None),
    }
    spec['plots'].append(dict(kind='line', data='points', x='output_current_a', y='ripple_peak_to_peak',
                             series='input_voltage_set_v', title='Ripple peak-to-peak versus load'))
    if test_id == 'maximum_output_current':
        spec['waveforms']['overcurrent'] = dict(label='Overcurrent protection transient', unit='V', role='scope', channel='C1', required=False,
            phase='transition', when='load_current_set_a >= 3.5', points=4096, window_s=None, pretrigger=None,
            timeout_s=10, max_captures=3, trigger=dict(level='nominal_output_v * (1 - tolerance_pct / 100)', slope='falling'))


def add_observations(spec, thermal_role=None, scope_role=None, dut=True, ambient=True, ripple=True,
                     protection=False, trigger_level=4.75, condition='True'):
    """UI starting definitions for optional observations; execution stays generic."""
    automatic_plots = spec['acquisition']['capture_all'] and not spec['plots']
    scalars = spec.setdefault('secondary_measurements', {})
    waves = spec.setdefault('waveforms', {})
    for enabled, key, label, quantity, location in [
        (dut, 'dut_temperature_c', 'DUT temperature', 'dut_temperature', 'DUT'),
        (ambient, 'ambient_temperature_c', 'Ambient temperature', 'ambient_temperature', 'Ambient')]:
        if enabled and thermal_role:
            scalars.setdefault(key, dict(label=label, unit='°C', role=thermal_role, quantity=quantity, required=False, kind='temperature', location=location))
            plot = dict(kind='line', data='secondary' if key != 'temperature_rise_c' else 'points', x='elapsed_s', y=key, title=label+' versus time')
            if not automatic_plots and plot not in spec['plots']:
                spec['plots'].append(plot)
    if dut and ambient and thermal_role:
        spec['derived'].setdefault('temperature_rise_c', dict(label='Temperature rise above ambient', unit='°C', expression='dut_temperature_c - ambient_temperature_c', aggregate='point'))
    if scope_role and ripple:
        waves.setdefault('ripple', dict(label='Ripple under load', unit='V', role=scope_role, channel='C1', required=False,
            phase='settled', when=True, points=2048, window_s=None, pretrigger=None, timeout_s=3, max_captures=8, trigger=None))
        axis = spec['axes'][-1] if spec['axes'] else dict(id='elapsed_s', label='time')
        plot = dict(kind='line', data='points', x=axis['id'], y='ripple_peak_to_peak', title='Ripple peak-to-peak versus '+axis['label'].lower())
        if not automatic_plots and plot not in spec['plots']:
            spec['plots'].append(plot)
    if scope_role and protection:
        waves.setdefault('overcurrent', dict(label='Protection transition', unit='V', role=scope_role, channel='C1', required=False,
            phase='transition', when=condition, points=4096, window_s=None, pretrigger=None, timeout_s=10, max_captures=3,
            trigger=dict(level=trigger_level, slope='falling')))
    return spec
