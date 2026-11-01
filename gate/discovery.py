from importlib.metadata import entry_points
from pathlib import Path
from gate.drivers.siglent import SiglentSDS
from gate.drivers.fluke45 import Fluke45
from gate.drivers.keysight344xxa import Keysight344xxA
from gate.riden_usb import RidenUSB
from gate.drivers.riden import RidenRD6006
from gate.serial import FlukeSerial
from gate.aoip_serial import AOIPSerial
from gate.drivers.aoip import AOIPSN8310, AOIPPJ6301
from gate.transport import SCPI
from gate.hantek_usb import HantekUSB, resources as hantek_resources
from gate.drivers.hantek365 import Hantek365

def driver_types():
    drivers = [SiglentSDS, Fluke45, Hantek365, Keysight344xxA, RidenRD6006, AOIPSN8310, AOIPPJ6301]
    for entry in entry_points(group="gate.drivers"):
        cls = entry.load()
        if cls not in drivers:
            drivers.append(cls)
    return drivers

def usb_resources():
    return sorted([str(p) for p in Path("/dev").glob("usbtmc[0-9]*")]+hantek_resources())

def usb_inventory():
    result = []
    for product in Path("/sys/bus/usb/devices").glob("*/product"):
        try:
            if "SDS1202X-E" in product.read_text():
                result.append("USB detected: " + product.read_text().strip())
        except OSError:
            pass
    result += ['USB detected: Hantek 365 series · '+resource for resource in hantek_resources()]
    return result

def connect(resource):
    if resource.startswith('riden://'):
        transport = RidenUSB(resource)
        try:
            return RidenRD6006(transport)
        except Exception:
            transport.close()
            raise
    if resource.startswith('hantek://'):
        transport = HantekUSB(resource)
        try:
            return Hantek365(transport)
        except Exception:
            transport.close()
            raise
    transport = (AOIPSerial(resource) if resource.startswith("aoip://") else
                 FlukeSerial(resource) if resource.startswith("serial://") else SCPI(resource))
    try:
        identity = transport.query("*IDN?")
        for cls in driver_types():
            if cls.matches(identity):
                return cls(transport, identity)
        raise ValueError(f"No driver for {identity}")
    except Exception:
        transport.close()
        raise
