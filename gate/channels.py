"""Channel/function catalogs, independent of current readings or transport."""
from dataclasses import asdict
from gate.model import InstrumentChannel


def channel_catalog(instrument, functions=None):
    functions = list(instrument.functions()) if functions is None else list(functions)
    provider = getattr(instrument, 'channels', None)
    channels = list(provider()) if provider else [InstrumentChannel(key, key.replace('_', ' ').title())
        for key in dict.fromkeys(function.instrument_channel for function in functions)]
    if len({item.id for item in functions}) != len(functions):
        raise ValueError('Driver function IDs must be unique within an instrument')
    if len({item.id for item in channels}) != len(channels):
        raise ValueError('Driver channel IDs must be unique within an instrument')
    if any(function.instrument_channel not in {channel.id for channel in channels} for function in functions):
        raise ValueError('Every driver function must belong to a declared instrument channel')
    return [dict(asdict(channel), functions=[asdict(function) for function in functions
                                            if function.instrument_channel == channel.id]) for channel in channels]


def current_functions(instrument):
    """Cached state for snapshots; querying hardware belongs to the engine thread."""
    return dict(getattr(instrument, 'current_functions', {'primary': getattr(instrument, 'current_function', None)}))


def function_group(function):
    group = function.exclusive_group or ('primary' if function.configurable else None)
    return (function.instrument_channel, group) if group else None
