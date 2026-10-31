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
