"""Cached output capabilities; catalog inspection never touches a transport.

Drivers opt in to each typed control and confirm writes in their setters.
Reading keys refer to output measurements, never source setpoints or inputs.
"""
import copy
import math

SETTERS = {'voltage': 'set_voltage', 'current_limit': 'set_current_limit',
           'priority': 'set_priority', 'ovp': 'set_ovp', 'ocp': 'set_ocp'}


def output_catalog(instrument):
    return copy.deepcopy(getattr(instrument, 'output_configuration', {}))


def configure_output(instrument, control, value):
    spec = output_catalog(instrument).get('controls', {}).get(control)
    setter = getattr(instrument, SETTERS.get(control, ''), None)
    if not spec or not callable(setter):
        raise ValueError('This driver has no verified support for this output control')
    if 'choices' in spec:
        if not isinstance(value, str) or value not in spec['choices']:
            raise ValueError('Select a driver-declared output choice')
    elif (isinstance(value, bool) or not isinstance(value, (int, float)) or
          not math.isfinite(value) or not spec['min'] <= value <= spec['max']):
        raise ValueError(f"{spec['label']} must be finite, {spec['min']}–{spec['max']} {spec['unit']}")
    setter(value)
