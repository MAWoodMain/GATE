from dataclasses import dataclass
from typing import Protocol

DEFAULT_TEMPERATURE_UNIT = '°C'

@dataclass(frozen=True)
class Reading:
    channel: str
    value: float | None
    unit: str
    quality: str = "ok"

class Instrument(Protocol):
    id: str
    name: str
    category: str
    simulated: bool
    def read(self) -> list[Reading]: ...
    def close(self) -> None: ...

@dataclass(frozen=True)
class MeasurementFunction:
    """A measurement a driver can expose, including currently inactive modes."""
    id: str
    label: str
    channel: str
    unit: str
    configurable: bool = False
    exclusive_group: str | None = None
    instrument_channel: str = 'primary'

@dataclass(frozen=True)
class InstrumentChannel:
    """A physical or logical instrument channel, distinct from a reading key."""
    id: str
    label: str

@dataclass(frozen=True)
class RangeOption:
    """A driver-owned choice ID; value is nominal full scale (or units/div)."""
    id: str
    label: str
    value: float | None = None
    automatic: bool = False

@dataclass(frozen=True)
class RangeControl:
    """Independent range selector, optionally tied to a meter function."""
    id: str
    label: str
    unit: str
    options: tuple[RangeOption, ...]
    function: str | None = None
    instrument_channel: str = 'primary'

class RangedInstrument(Protocol):
    """Optional instrument extension. Catalogs are passive; selections confirm I/O."""
    def range_controls(self) -> list[RangeControl]: ...
    def select_range(self, control_id: str, option_id: str) -> None: ...
