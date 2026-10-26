"""Inventory-only DMX patch; never sends lighting commands."""
import uuid
import copy

PRESETS=[dict(id='jellyfish-11ch',name='Jellyfish · 11 channels',channels=[dict(function=role,default=255 if n==0 else 0) for n,role in enumerate(['Master','Red','Green','Blue','White','Strobe']+['Unknown']*5)])]

PRESETS.append(dict(id='rgbwauv-16ch',name='Waterproof Parlight',channels=[dict(function=role,default=255 if n==0 else 0) for n,role in enumerate(['Master','Red','Green','Blue','White','Amber','UV','Strobe']+['Unknown']*8)]))

BEAM230_CHANNELS=[
    ('Pan',128),('Tilt',128),('Pan Fine',0),('Tilt Fine',0),('Pan/Tilt Speed',0),('Frost',0),
    ('Strobe / Shutter',0),('Dimmer',0),('Color Wheel',0),('Color Adjustment',0),('Gobo Wheel',0),
    ('Gobo Shake',0),('Focus',96),('Prism',0),('Prism Rotation',0),('Lamp Control / Reset',0)
]
PRESETS.append(dict(id='beam230-16ch',name='Beam 230 · Original · 16 channels',
                    channels=[dict(function=role,default=value) for role,value in BEAM230_CHANNELS]))

def ensure_beam230_patch(state,device_id,input_universe=0,output_universe=1):
    """Seed the Beam next to desk bank 10 while keeping source and output universes distinct."""
    presets=state.setdefault('fixturePresets',copy.deepcopy(PRESETS))
    for preset in PRESETS:
        if not any(p.get('id')==preset['id'] for p in presets):presets.append(copy.deepcopy(preset))
    maps=state.setdefault('fixtureMaps',{})
    items=maps.setdefault(device_id,initial_patch(device_id))
    beam=next((f for f in items if f.get('id')=='beam230-input10'),None)
    if beam is None:
        beam=dict(id='beam230-input10',name='Beam 230 · Input 10',universe=output_universe,start=145,
                  mappingPreset='beam230-16ch',channels=copy.deepcopy(PRESETS[-1]['channels']))
        items.append(beam)
    # Fixture patches always describe the physical output universe. Input banks
    # describe the controller input universe; matching DMX numbers are valid.
    for patch in maps.values():
        for fixture in patch:fixture['universe']=output_universe
    for bank in state.get('inputControls',{}).values():
        for control in bank:control['universe']=input_universe
    return beam

def initial_patch(device_id):
    if device_id!='00:00:c8:d8:a2:71':return []
    roles=['Master','Red','Green','Blue','White','Strobe']+['Unknown']*5
    return [dict(id='jellyfish-'+str(i+1),name='Jellyfish '+str(i+1),universe=1,start=193+i*11,
                 mappingPreset='jellyfish-11ch',channels=[dict(function=role,default=255 if n==0 else 0) for n,role in enumerate(roles)]) for i in range(8)]

def validate_fixture(data,existing,presets=None):
    presets=PRESETS if presets is None else presets
    if not isinstance(data,dict):raise ValueError('Invalid fixture')
    name=data.get('name','')
    if not isinstance(name,str) or not name.strip() or len(name)>80:raise ValueError('Fixture name required (maximum 80 characters)')
    universe,start=data.get('universe'),data.get('start')
    channels=data.get('channels')
    if type(universe) is not int or not 0<=universe<=255:raise ValueError('Universe must be 0–255 for this node')
    if type(start) is not int or not isinstance(channels,list) or not 1<=len(channels)<=512 or start<1 or start+len(channels)-1>512:raise ValueError('Fixture range must fit channels 1–512')
    clean=[]
    for ch in channels:
        if not isinstance(ch,dict) or not isinstance(ch.get('function'),str) or len(ch['function'])>80 or type(ch.get('default')) is not int or not 0<=ch['default']<=255:raise ValueError('Each channel needs a function and default value 0–255')
        clean.append(dict(function=ch['function'].strip() or 'Unknown',default=ch['default']))
    fid=data.get('id')
    if fid is not None and not any(f['id']==fid for f in existing):raise ValueError('Fixture not found')
    for f in existing:
        if f['id']!=fid and f['universe']==universe and start<=f['start']+len(f['channels'])-1 and f['start']<=start+len(clean)-1:raise ValueError('Range overlaps '+f['name'])
    old=next((f for f in existing if f['id']==fid),None)
    preset=data.get('mappingPreset',old.get('mappingPreset') if old else None)
    # Auto-detect only exact known channel maps; unknown profiles remain custom.
    if preset in (None,'auto'):preset=next((p['id'] for p in presets if p['channels']==clean),'custom')
    if preset!='custom' and not any(p['id']==preset for p in presets):raise ValueError('Mapping preset not found')
    return dict(id=fid or uuid.uuid4().hex,name=name.strip(),universe=universe,start=start,channels=clean,mappingPreset=preset)


def fixture_test_plan(fixtures, universe,presets=None):
    presets=PRESETS if presets is None else presets
    """Only recognized profiles have a defined white/red/off recipe."""
    # Beam 230 has lamp command and moving-head channels; the generic color
    # sequence would not be a safe test recipe for it.
    known=[f for f in fixtures if f['universe']==universe and f.get('mappingPreset')!='beam230-16ch'
           and any(f['channels']==p['channels'] for p in presets)]
    known.sort(key=lambda f:f['start'])
    if not known:raise ValueError('No fixtures with a supported test mapping in the output universe')
    for i,f in enumerate(known):
        if not 1<=f['start']<=512 or f['start']+len(f['channels'])-1>512:raise ValueError('Invalid fixture range')
        if i and known[i-1]['start']+len(known[i-1]['channels'])>f['start']:raise ValueError('Fixture ranges overlap')
    return known


def fixture_test_frame(plan, baseline, active=None, color='off'):
    end=max(len(baseline),max(f['start']+len(f['channels'])-1 for f in plan))
    end+=end%2
    frame=list(baseline)+[0]*(end-len(baseline))
    for f in plan:
        offset=f['start']-1
        frame[offset:offset+len(f['channels'])]=[0]*len(f['channels'])
    if active is not None and color!='off':
        offset=active['start']-1
        for i,ch in enumerate(active['channels']):
            role=ch['function']
            frame[offset+i]=255 if role=='Master' or (color=='red' and role=='Red') or (color=='white' and role=='White') else 0
    return bytes(frame)


def prepare_fixture_update(state,device_id,data,input_universe=None,output_universe=None):
    """Validate a shared preset edit and all affected patches before committing."""
    from input_controls import initial_controls,validate_control
    presets=copy.deepcopy(state.get('fixturePresets',PRESETS))
    maps=copy.deepcopy(state.get('fixtureMaps',{}))
    maps.setdefault(device_id,initial_patch(device_id))
    if output_universe is not None:
        for patch in maps.values():
            for item in patch:item['universe']=output_universe
    fixture=validate_fixture(data,maps[device_id],presets)
    items=maps[device_id]
    maps[device_id]=[fixture if f['id']==fixture['id'] else f for f in items] if any(f['id']==fixture['id'] for f in items) else items+[fixture]
    pid=fixture['mappingPreset']
    affected=1
    if pid!='custom':
        preset=next(p for p in presets if p['id']==pid)
        if preset['channels']!=fixture['channels']:
            preset['channels']=copy.deepcopy(fixture['channels'])
            for patch in maps.values():
                for f in patch:
                    if f.get('mappingPreset')==pid:f['channels']=copy.deepcopy(fixture['channels'])
        affected=sum(f.get('mappingPreset')==pid for patch in maps.values() for f in patch)
    controls=copy.deepcopy(state.get('inputControls',{}))
    if input_universe is not None:
        for bank in controls.values():
            for item in bank:item['universe']=input_universe
    for did,patch in maps.items():
        for f in patch:
            validate_fixture(f,patch,presets)
            for c in controls.get(did,initial_controls(did)):
                if c['universe']==f['universe'] and f['start']<=c['start']+c['count']-1 and c['start']<=f['start']+len(f['channels'])-1:raise ValueError('Fixture range is reserved by virtual input '+c['name'])
        if did in controls:
            from led_controls import virtual_fixture
            virtuals=[virtual_fixture(f) for f in state.get('ledFixtures',[])]
            controls[did]=[validate_control(c,controls[did],patch+virtuals) for c in controls[did]]
    return maps,presets,controls,affected
