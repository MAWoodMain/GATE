"""Connection classification and validation shared by the service and device editor."""
import re
from urllib.parse import parse_qs, urlsplit


def communication_type(resource):
    if resource.startswith(('hantek://', 'riden://')):
        return 'USB'
    if resource.startswith(('serial://', 'aoip://', 'plp://')):
        return 'Serial'
    if resource.startswith('tcp://'):
        return 'Network'
    if resource.startswith('demo:'):
        return 'Demo'
    return 'TMC'


def validate_resource(resource):
    if not isinstance(resource, str) or any(c.isspace() for c in resource):
        raise ValueError('Enter a connection address without spaces')
    if re.fullmatch(r'/dev/usbtmc[0-9]+', resource):
        return resource
    if resource.startswith('plp://'):
        from gate.plp_serial import connection
        return connection(resource)[0]
    if resource.startswith('aoip://'):
        from gate.aoip_serial import connection
        return connection(resource)[0]
    if resource.startswith('riden://'):
        from gate.riden_usb import connection
        path, baud, address = connection(resource)
        return f'riden://{path}?baud={baud}&address={address}'
    url = urlsplit(resource)
    if url.scheme == 'hantek' and url.netloc == 'usb' and re.fullmatch(r'/[0-9]+-[0-9]+(?:\.[0-9]+)*', url.path) and not (url.query or url.fragment):
        return resource
    if url.scheme == 'serial' and not url.netloc and url.path.startswith('/dev/'):
        options = parse_qs(url.query, strict_parsing=True)
        if url.fragment or set(options) - {'baud'} or any(len(v) != 1 for v in options.values()):
            raise ValueError('Serial connections support baud rate only, with 8N1 framing')
        baud = int(options.get('baud', ['9600'])[0])
        if baud not in (300, 600, 1200, 2400, 4800, 9600):
            raise ValueError('Choose a supported serial baud rate (300–9600)')
        return f'serial://{url.path}?baud={baud}'
    if url.scheme == 'tcp' and url.hostname and url.username is None and url.password is None:
        if url.path or url.query or url.fragment or not 1 <= (url.port if url.port is not None else 5025) <= 65535:
            raise ValueError('Use tcp://host:port for a raw SCPI connection')
        return resource
    raise ValueError('Use plp:///dev/ttyACM0, aoip:///dev/ttyUSB0, /dev/usbtmcN, hantek://usb/PORT, riden:///dev/ttyUSB1, serial:///dev/ttyUSB0?baud=9600 or tcp://host:5025')
