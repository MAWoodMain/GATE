"""Common range contract. Catalog inspection never performs instrument I/O."""
from dataclasses import asdict
from gate.model import RangeOption, RangeControl


def range_controls(instrument):
    return list(getattr(instrument, 'range_controls', lambda: ())())


def range_catalog(instrument):
    controls = range_controls(instrument)
    functions = {item.id: item for item in getattr(instrument, 'functions', lambda: ())()}
    if len({control.id for control in controls}) != len(controls):
        raise ValueError('Driver range control IDs must be unique within an instrument')
    for control in controls:
        if control.function is not None and (control.function not in functions or
                functions[control.function].instrument_channel != control.instrument_channel):
            raise ValueError('Driver range must belong to a function on the same instrument channel')
    return [asdict(control) for control in controls]


def range_option(instrument, control_id, option_id):
    for control in range_controls(instrument):
        if control.id == control_id:
            for option in control.options:
                if option.id == option_id:
                    return control, option
    raise ValueError('Unsupported instrument range control or choice')


def validate_ranges(catalog, selected):
    if not isinstance(selected, dict):
        raise ValueError('ranges must map range control IDs to choice IDs')
    choices = {control['id']: {option['id'] for option in control['options']} for control in catalog}
    for key, value in selected.items():
        if not isinstance(key, str) or not isinstance(value, str) or key not in choices or value not in choices[key]:
            raise ValueError('Unsupported instrument range control or choice')


def numeric_control(identity, label, unit, values, function=None, automatic=True, instrument_channel='primary'):
    options = ([RangeOption('auto', 'Autorange', automatic=True)] if automatic else [])
    for value in values:
        for factor, prefix in ((1e9, 'G'), (1e6, 'M'), (1e3, 'k'), (1., ''), (1e-3, 'm'), (1e-6, 'µ'), (1e-9, 'n'), (1e-12, 'p')):
            if value >= factor:
                options.append(RangeOption(format(value, 'g'), f'{value/factor:g} {prefix}{unit}', value))
                break
    return RangeControl(identity, label, unit, tuple(options), function, instrument_channel)


def apply_ranges(instrument, settings, function=None):
    """Restore independent controls and only the active meter function's range."""
    selected = settings.get('ranges', {})
    if not selected:
        return
    # Validate every choice before the first hardware write.
    choices = [range_option(instrument, key, value) for key, value in selected.items()]
    if function is not None:
        choices = [(control, option) for control, option in choices if control.function == function]
    current = instrument.range_functions() if any(control.function for control, _ in choices) else {}
    for control, option in choices:
        if control.function is None or control.function == current.get(control.instrument_channel):
            instrument.select_range(control.id, option.id)
