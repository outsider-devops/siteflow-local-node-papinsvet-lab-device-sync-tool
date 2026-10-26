"""Recoverable learned-control removal, scoped to one MIDI device."""
from copy import deepcopy
from datetime import datetime, timezone
import secrets

IDENTITY_FIELDS = ('source', 'type', 'channel', 'number')


def same_identity(control, identity):
    return all(control.get(key) == identity.get(key) for key in IDENTITY_FIELDS)


def remove_control(device, identity):
    controls = device.setdefault('midiControls', [])
    control = next((c for c in controls if same_identity(c, identity)), None)
    if control is None:
        raise ValueError('Saved MIDI control not found')
    removed = dict(id=secrets.token_hex(12), removedAt=datetime.now(timezone.utc).isoformat(),
                   control=deepcopy(control))
    controls.remove(control)
    device.setdefault('midiRemovedControls', []).append(removed)
    return removed


def restore_control(device, removal_id):
    removed = device.setdefault('midiRemovedControls', [])
    entry = next((c for c in removed if c.get('id') == removal_id), None)
    if entry is None:
        raise ValueError('Removed MIDI control not found')
    controls = device.setdefault('midiControls', [])
    if any(same_identity(c, entry['control']) for c in controls):
        raise ValueError('This MIDI input has already been learned again; remove the new mapping first')
    control = deepcopy(entry['control'])
    controls.append(control)
    removed.remove(entry)
    return control
