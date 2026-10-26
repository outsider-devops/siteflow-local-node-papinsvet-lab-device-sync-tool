"""Read NI Controller Editor S2 MK2 templates without writing to the controller."""
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import xml.etree.ElementTree as ET

from midi_controls import same_identity


def surface_ids():
    ids = {'ButtonAssignC': 'mixer-remix-a', 'ButtonAssignD': 'mixer-remix-b',
           'ButtonBrowsep': 'mixer-browser-press', 'DialBrowset': 'mixer-browser',
           'PotiVolume': 'mixer-remix', 'PotiXfader': 'mixer-crossfader',
           'PotiCuemixK': 'front-cue-mix'}
    for deck, letter in [('a', 'A'), ('b', 'B')]:
        for native, suffix in [('ButtonGainp', 'gain-filter-press'), ('DialGaint', 'gain-filter'),
                               ('ButtonMcue', 'cue'), ('PotiFader', 'channel-fader'),
                               ('PotiEqhi', 'hi'), ('PotiEqmid', 'mid'), ('PotiEqlow', 'low')]:
            ids[native + letter] = f'mix-{deck}-{suffix}'
        ids['ButtonLoad' + letter] = f'mixer-load-{deck}'
        for unit in (1, 2):
            ids[f'ButtonFx{unit}{letter}'] = f'mix-{deck}-assign-{unit}'
        unit = 1 if deck == 'a' else 2
        ids[f'PotiFx{unit}Dw'] = f'deck-{deck}-fx-0'
        ids[f'ButtonFx{unit}On'] = f'deck-{deck}-fx-button-0'
        for number in (1, 2, 3):
            ids[f'PotiFx{unit}K{number}'] = f'deck-{deck}-fx-{number}'
            ids[f'ButtonFx{unit}B{number}'] = f'deck-{deck}-fx-button-{number}'
        for native, suffix in [('ButtonCue', 'cue'), ('ButtonJogpush', 'jog-top-press'),
                               ('ButtonLoopin', 'loop-in'), ('ButtonLoopout', 'loop-out'),
                               ('ButtonLoopmp', 'loop-move-press'), ('ButtonLoopsp', 'loop-size-press'),
                               ('ButtonPlay', 'play'), ('ButtonSync', 'sync'), ('ButtonTemporst', 'flux'),
                               ('DialLoopmt', 'loop-move'), ('DialLoopst', 'loop-size'),
                               ('JogwheelJogturn', 'jog-top'), ('PotiTempof', 'tempo')]:
            ids[native + letter] = f'deck-{deck}-{suffix}'
        for number in (1, 2, 3, 4):
            ids[f'ButtonCue{number}{letter}'] = f'deck-{deck}-remix-{number}'
    return ids


def load_template(path, source, labels):
    raw = Path(path).read_bytes()
    root = ET.fromstring(raw)
    maps = root.findall('midi-map')
    index = int(root.findtext('current_index', '0'))
    if root.tag != 'ni-controller-midi-map' or not 0 <= index < len(maps):
        raise ValueError('Invalid Controller Editor template')
    template = maps[index]
    if template.get('type') != 'TraktorKontrolS2MK2':
        raise ValueError('This template is not for Traktor Kontrol S2 MK2')
    lookup = surface_ids()
    groups = [('Common', template.find('controls'))]
    for section in ('pagesLeft', 'pagesRight'):
        container = template.find(section)
        if container is not None:
            groups.extend((page.get('name', 'Basic'), page.find('controls') or page)
                          for page in container.findall('page'))
    records = []
    skipped = []
    now = datetime.now(timezone.utc).isoformat()
    for layer, group in groups:
        if group is None:
            continue
        for control in group.iter():
            if control.find('controller') is None:
                continue
            native_id = control.get('id', '')
            if native_id.startswith('Ledchain') or control.tag == 'led':
                continue  # Controller feedback is output, never a physical MIDI input.
            surface_id = lookup.get(native_id)
            if surface_id is None or surface_id not in labels:
                skipped.append(native_id)
                continue
            channel = int(control.findtext('channel', '0')) + 1
            number = int(control.findtext('controller'))
            if not 1 <= channel <= 16 or not 0 <= number <= 127:
                raise ValueError('Unsupported MIDI identity in template: ' + native_id)
            if control.tag == 'button':
                off = int(control.findtext('off', '0')); on = int(control.findtext('on', '127'))
                minimum, maximum = sorted((off, on))
                interpretation = 'Button / CC press-release'
            else:
                minimum = int(control.findtext('min', '0')); maximum = int(control.findtext('max', '127'))
                interpretation = "Relative encoder / two's complement" if control.tag == 'wheel' else 'Fader or absolute knob'
            if not 0 <= minimum <= maximum <= 127:
                raise ValueError('Unsupported control range in template: ' + native_id)
            label = labels[surface_id]
            record = dict(name=label + (' · Shift' if layer == 'Shift' else ''), source=source,
                          type='Control Change', channel=channel, number=number, min=minimum, max=maximum,
                          surfaceControlId=surface_id, surfaceControlName=label, origin='controller-editor',
                          interpretation=interpretation, templateLayer=layer, templateControlId=native_id,
                          events=0, addedAt=now)
            if control.tag == 'button':
                record.update(onValue=on, offValue=off, behavior=control.findtext('behavior', 'gate'))
            if native_id.startswith('JogwheelJogturn'):
                record['surfaceAliases'] = [surface_id.replace('-jog-top', '-jog-rim')]
            records.append(record)
    if skipped:
        raise ValueError('Unmapped template controls: ' + ', '.join(skipped))
    identities = [(r['source'], r['type'], r['channel'], r['number']) for r in records]
    if len(identities) != len(set(identities)):
        raise ValueError('Template contains conflicting MIDI input identities')
    profile = dict(name=template.get('name', 'S2 MK2'), model='TraktorKontrolS2MK2',
                   filename=Path(path).name, sha256=hashlib.sha256(raw).hexdigest(),
                   source=source, count=len(records), layers=dict(Counter(r['templateLayer'] for r in records)),
                   importedAt=now)
    return records, profile


def merge_template(device, records, profile):
    old = device.get('midiControls', [])
    merged = []
    for record in records:
        new = deepcopy(record)
        previous = next((c for c in old if same_identity(c, new)), None)
        if previous:
            for field in ('functionRole', 'target', 'mappingName', 'assignedAt'):
                if field in previous:
                    new[field] = deepcopy(previous[field])
        merged.append(new)
    # Preserve custom inputs absent from this import; replace previous template records.
    merged.extend(deepcopy(c) for c in old if c.get('origin') != 'controller-editor'
                  and not any(same_identity(c, imported) for imported in records))
    device['midiControls'] = merged
    device['midiTemplate'] = deepcopy(profile)
