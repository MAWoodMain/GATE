from importlib.metadata import entry_points
from pathlib import Path
from gate.drivers.siglent import SiglentSDS
from gate.drivers.fluke45 import Fluke45
from gate.serial import FlukeSerial
from gate.transport import SCPI

def driver_types():
    drivers = [SiglentSDS, Fluke45]
    for entry in entry_points(group="gate.drivers"):
        cls = entry.load()
        if cls not in drivers:
            drivers.append(cls)
    return drivers

def usb_resources():
    return sorted(str(p) for p in Path("/dev").glob("usbtmc[0-9]*"))

def usb_inventory():
    result = []
    for product in Path("/sys/bus/usb/devices").glob("*/product"):
        try:
            if "SDS1202X-E" in product.read_text():
                result.append("USB detected: " + product.read_text().strip())
        except OSError:
            pass
    return result

def connect(resource):
    transport = FlukeSerial(resource) if resource.startswith("serial://") else SCPI(resource)
    try:
        identity = transport.query("*IDN?")
        for cls in driver_types():
            if cls.matches(identity):
                return cls(transport, identity)
        raise ValueError(f"No driver for {identity}")
    except Exception:
        transport.close()
        raise
