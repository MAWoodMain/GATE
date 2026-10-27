"""Transport-independent instrument identities and versioned profile migration."""
import hashlib


def identify(identity, device_type=None):
    fields = [field.strip() for field in identity.split(',')]
    if len(fields) < 3 or fields[2].upper() in ('', '0', 'UNKNOWN', 'NONE', 'N/A'):
        return None
    maker, model, serial = fields[:3]
    maker_key = maker.upper().replace(' TECHNOLOGIES', '')
    kind = device_type or f'{maker_key}:{model.upper()}'
    key = hashlib.sha256(f'{kind}\0{serial.upper()}'.encode()).hexdigest()[:24]
    return dict(id='instrument:'+key, type=kind, manufacturer=maker, model=model,
                serial=serial, identity=identity, name='', settings={}, auto_connect=True)


def migrate(profile):
    """Keep legacy endpoints; only merge identities containing a real serial number."""
    instruments = profile.setdefault('instrument_profiles', {})
    connections = profile.setdefault('connections', {})
    for resource in profile.get('resources', []):
        if resource in connections:
            continue
        old = profile.get('devices', {}).get(resource, {})
        record = identify(old.get('name', ''))
        key = None
        if record:
            key = record.pop('id')
            record['category'] = old.get('category', '')
            instruments.setdefault(key, record)
        connections[resource] = dict(instrument=key, enabled=True)
    profile['schema_version'] = 2
    return profile
