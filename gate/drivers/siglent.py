"""SDS1202X-E scalar measurements; no acquisition-setting changes."""
import math
import re
from gate.model import Reading, MeasurementFunction

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
    experiment_capabilities = ('measure.scalar',)
    simulated = False
    @staticmethod
    def matches(identity):
        fields = identity.upper().split(",")
        return len(fields) >= 2 and "SIGLENT" in fields[0] and fields[1].strip() == "SDS1202X-E"

    def __init__(self, transport, identity):
        self.transport = transport
        self.id = transport.resource
        self.name = identity.strip()

    def functions(self):
        labels = {"PKPK": "Peak-to-peak", "RMS": "RMS voltage", "FREQ": "Frequency"}
        return [MeasurementFunction(f"{channel}.{parameter}", f"{channel} · {labels[parameter]}",
                                    f"{channel}.{parameter}", unit)
                for channel in ("C1", "C2")
                for parameter, unit in (("PKPK", "V"), ("RMS", "V"), ("FREQ", "Hz"))]

    def read(self):
        readings = []
        for channel in ("C1", "C2"):
            for parameter, unit in (("PKPK", "V"), ("RMS", "V"), ("FREQ", "Hz")):
                response = self.transport.query(f"{channel}:PAVA? {parameter}")
                value = parse_measurement(response, parameter, unit)
                readings.append(Reading(f"{channel}.{parameter}", value, unit,
                                        "ok" if value is not None else "unavailable"))
        return readings

    def close(self):
        self.transport.close()
