from dataclasses import dataclass
from typing import Protocol

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
