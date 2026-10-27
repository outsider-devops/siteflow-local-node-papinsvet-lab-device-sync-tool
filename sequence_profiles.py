"""Saved Art-Net sequence profiles and their virtual routing targets."""
from copy import deepcopy
import re
import uuid


def make_virtual_fixtures(sequences, universe):
    result = []
    for sequence in sequences:
        if not isinstance(sequence, dict) or sequence.get('kind') != 'fx-walk':
            continue
        result.append(dict(
            id='sequence:' + sequence['id'], sequenceId=sequence['id'],
            name=sequence['name'], universe=universe, start=1,
            mappingPreset='virtual-sequence-trigger', virtualSequence=True,
            sequenceArmed=bool(sequence.get('armed')),
            sequenceDefinition=deepcopy(sequence),
            channels=[dict(function='FX Activate', default=0),
                      dict(function='Sequence Trigger', default=0)],
        ))
    return result


def validate_sequence(data, existing, fixtures):
    if not isinstance(data, dict):
        raise ValueError('Invalid sequence')
    sid = data.get('id') or uuid.uuid4().hex
    if not isinstance(sid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', sid):
        raise ValueError('Invalid sequence ID')
    name = data.get('name', '')
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
        raise ValueError('Sequence name must contain 1–80 characters')
    kind = data.get('kind', 'fx-walk')
    if kind != 'fx-walk':
        raise ValueError('Unsupported sequence type')
    steps = data.get('steps')
    if not isinstance(steps, list) or not 1 <= len(steps) <= 32 or any(not isinstance(fid, str) for fid in steps):
        raise ValueError('Add 1–32 fixture steps')
    by_id = {fixture.get('id'): fixture for fixture in fixtures}
    if any(fid not in by_id or by_id[fid].get('mappingPreset') != 'fx-activate-effect-4ch' for fid in steps):
        raise ValueError('FX Walk steps must reference available FX fixtures')
    effect = data.get('effectSeconds', 1)
    interval = data.get('intervalSeconds', 1)
    if type(effect) not in (int, float) or not .1 <= effect <= 30:
        raise ValueError('Effect duration must be 0.1–30 seconds')
    if type(interval) not in (int, float) or not 0 <= interval <= 30:
        raise ValueError('Start interval must be 0–30 seconds')
    armed = data.get('armed', False)
    if type(armed) is not bool:
        raise ValueError('Invalid sequence trigger state')
    trigger_mode = data.get('triggerMode', 'oneshot')
    if trigger_mode not in ('oneshot', 'loop'):
        raise ValueError('Select one-shot or loop-while-held trigger mode')
    return dict(id=sid, name=name.strip(), kind=kind, steps=list(steps),
                effectSeconds=float(effect), intervalSeconds=float(interval), armed=armed,
                triggerMode=trigger_mode)


def validate_sample(data, fixtures):
    sample = validate_sequence({**data, 'id': 'sample-check', 'armed': False}, [], fixtures)
    sid = data.get('id') or uuid.uuid4().hex
    if not isinstance(sid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', sid):
        raise ValueError('Invalid sample ID')
    return dict(id=sid, name=sample['name'], kind=sample['kind'], steps=sample['steps'],
                effectSeconds=sample['effectSeconds'], intervalSeconds=sample['intervalSeconds'],
                triggerMode=sample['triggerMode'])
