"""Bounded waveform records and capture validation shared by drivers and runs."""
import math
import json
from dataclasses import dataclass, field

STATISTICS = ('peak_to_peak', 'rms', 'ac_rms', 'mean', 'min', 'max')


@dataclass(frozen=True)
class Waveform:
    channel: str
    values: list[float]
    sample_interval_s: float
    start_time_s: float
    unit: str
    quality: str = 'ok'
    trigger_utc_ns: int | None = None
    metadata: dict = field(default_factory=dict)
    screenshot: bytes | None = None
    screenshot_error: str = ''


def validate_screenshot(data):
    """Accept a bounded, complete Windows bitmap from a scope screen dump."""
    if not isinstance(data, bytes) or not 54 <= len(data) <= 1048576 or data[:2] != b'BM':
        raise ValueError('Scope screenshot must be a bounded BMP image')
    if int.from_bytes(data[2:6], 'little') != len(data):
        raise ValueError('Incomplete scope screenshot')
    width, height = (int.from_bytes(data[start:start+4], 'little', signed=True) for start in (18, 22))
    if not 1 <= width <= 4096 or not 1 <= abs(height) <= 4096:
        raise ValueError('Invalid screenshot dimensions')


def validate_waveform(value, request):
    if not isinstance(value, Waveform) or value.channel != request['channel'] or value.unit != request['unit']:
        raise ValueError('Waveform channel or units do not match the request')
    if not 2 <= len(value.values) <= request['points']:
        raise ValueError('Waveform length is outside the requested bound')
    if not math.isfinite(value.sample_interval_s) or value.sample_interval_s <= 0 or not math.isfinite(value.start_time_s):
        raise ValueError('Waveform requires finite calibrated time coordinates')
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item) or abs(item) > 1e100 for item in value.values):
        raise ValueError('Waveform contains invalid samples')
    if not math.isfinite(value.start_time_s+(len(value.values)-1)*value.sample_interval_s):
        raise ValueError('Waveform time range is not finite')
    if not isinstance(value.quality, str) or len(value.quality) > 80:
        raise ValueError('Invalid waveform quality flag')
    try:
        encoded = json.dumps(value.metadata, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError('Waveform metadata must be finite JSON data') from exc
    if len(encoded) > 16384:
        raise ValueError('Waveform metadata is too large')
    if value.trigger_utc_ns is not None and (type(value.trigger_utc_ns) is not int or value.trigger_utc_ns < 0):
        raise ValueError('Trigger time must be a UTC nanosecond integer or null')
    mean = sum(value.values)/len(value.values)
    return dict(peak_to_peak=max(value.values)-min(value.values),
                rms=math.sqrt(sum(item*item for item in value.values)/len(value.values)),
                ac_rms=math.sqrt(sum((item-mean)**2 for item in value.values)/len(value.values)),
                mean=mean, min=min(value.values), max=max(value.values))


def preview_rows(waveform, maximum=1600):
    """Preserve extrema for preview; full calibrated samples remain in CSV/SQLite."""
    size = len(waveform.values)
    indices = set([0, size-1])
    stride = max(1, math.ceil(size/(maximum//2)))
    for start in range(0, size, stride):
        stop = min(size, start+stride)
        indices.add(min(range(start, stop), key=waveform.values.__getitem__))
        indices.add(max(range(start, stop), key=waveform.values.__getitem__))
    return [dict(time_s=waveform.start_time_s+index*waveform.sample_interval_s, value=waveform.values[index]) for index in sorted(indices)]
