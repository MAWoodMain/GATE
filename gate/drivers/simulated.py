import math
import time
from gate.model import Reading

class Simulated:
    simulated = True
    def __init__(self, category):
        self.category = category
        self.id = "demo:" + category
        self.name = "SIMULATED · " + category.replace("_", " ").title()
        self.voltage = 0.0
        self.enabled = False
    def read(self):
        wobble = math.sin(time.monotonic()) * 0.003
        if self.category == "power_supply":
            return [Reading("set_voltage", self.voltage, "V"),
                    Reading("output_enabled", float(self.enabled), ""),
                    Reading("voltage", self.voltage + wobble if self.enabled else 0., "V"),
                    Reading("current", self.voltage / 100 if self.enabled else 0., "A")]
        if self.category == "multimeter":
            return [Reading("voltage_dc", 3.3 + wobble, "V")]
        return [Reading("C1.PKPK", 2.0 + wobble, "V"), Reading("C1.FREQ", 1000., "Hz")]
    def set_voltage(self, voltage):
        if self.category != "power_supply" or not math.isfinite(voltage) or not 0 <= voltage <= 30:
            raise ValueError("Demo PSU range: 0–30 V")
        self.voltage = voltage
    def set_enabled(self, enabled):
        self.enabled = bool(enabled)
    def close(self):
        self.enabled = False
