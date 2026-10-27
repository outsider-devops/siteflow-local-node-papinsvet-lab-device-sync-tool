"""Inventory-only DMX patch; never sends lighting commands."""
import uuid
import copy

PRESETS=[dict(id='jellyfish-11ch',name='Jellyfish · 11 channels',channels=[dict(function=role,default=255 if n==0 else 0) for n,role in enumerate(['Master','Red','Green','Blue','White','Strobe']+['Unknown']*5)])]

PRESETS.append(dict(id='rgbwauv-16ch',name='Waterproof Parlight',channels=[dict(function=role,default=255 if n==0 else 0) for n,role in enumerate(['Master','Red','Green','Blue','White','Amber','UV','Strobe']+['Unknown']*8)]))

FX_CHANNELS=[dict(function='Activate · 102–153',default=0),dict(function='Effect · 0/255',default=0),
             dict(function='Unknown',default=0),dict(function='Unknown',default=0)]
PRESETS.append(dict(id='fx-activate-effect-4ch',name='FX · Activate / Effect · 4 channels',channels=copy.deepcopy(FX_CHANNELS)))

def fx_channel_slots(fixture):
    """Return fixture-local offsets for the FX roles in its saved channel map."""
    if fixture.get('mappingPreset')!='fx-activate-effect-4ch':raise ValueError('FX 4-channel activation profile required')
    channels=fixture.get('channels',[])
    if len(channels)!=4:raise ValueError('FX fixture must have four channels')
    activate=[i for i,ch in enumerate(channels) if ch.get('function','').startswith('Activate')]
    effect=[i for i,ch in enumerate(channels) if ch.get('function','').startswith('Effect')]
    if len(activate)!=1 or len(effect)!=1 or activate[0]==effect[0]:
        raise ValueError('FX profile needs one Activate channel and one Effect channel')
    return activate[0],effect[0]

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

def ensure_fx_profile(state,device_id):
    """Add the verified FX activation profile and migrate untouched FX placeholders once."""
    presets=state.setdefault('fixturePresets',copy.deepcopy(PRESETS))
    if not any(p.get('id')=='fx-activate-effect-4ch' for p in presets):
        presets.append(copy.deepcopy(next(p for p in PRESETS if p['id']=='fx-activate-effect-4ch')))
    items=state.setdefault('fixtureMaps',{}).get(device_id,[])
    changed=False
    for fixture in items:
        if not re_full_fx(fixture.get('name','')) or len(fixture.get('channels',[]))!=4:continue
        if fixture.get('mappingPreset')=='custom' and all(ch.get('function')=='Unknown' for ch in fixture['channels']):
            fixture['mappingPreset']='fx-activate-effect-4ch'
            fixture['channels']=copy.deepcopy(FX_CHANNELS)
            changed=True
    return changed

def re_full_fx(name):
    import re
    return bool(re.fullmatch(r'FX\s*[1-6]',str(name),re.IGNORECASE))

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
    known=[f for f in fixtures if f['universe']==universe and f.get('mappingPreset') not in ('beam230-16ch','fx-activate-effect-4ch')
           and any(f['channels']==p['channels'] for p in presets)]
    known.sort(key=lambda f:f['start'])
    if not known:raise ValueError('No fixtures with a supported test mapping in the output universe')
    for i,f in enumerate(known):
        if not 1<=f['start']<=512 or f['start']+len(f['channels'])-1>512:raise ValueError('Invalid fixture range')
        if i and known[i-1]['start']+len(known[i-1]['channels'])>f['start']:raise ValueError('Fixture ranges overlap')
    return known


def fixture_test_frame(plan, baseline, active=None, color='off', completed=()):
    end=max(len(baseline),max(f['start']+len(f['channels'])-1 for f in plan))
    end+=end%2
    frame=list(baseline)+[0]*(end-len(baseline))
    # Preserve the already routed output for every unrelated fixture. Only
    # the current and previously tested fixtures belong to this test.
    muted_starts={f['start'] for f in completed}
    if active is None:muted_starts.update(f['start'] for f in plan)
    else:muted_starts.add(active['start'])
    for f in plan:
        if f['start'] not in muted_starts:continue
        offset=f['start']-1
        frame[offset:offset+len(f['channels'])]=[0]*len(f['channels'])
    if active is not None and color!='off':
        offset=active['start']-1
        for i,ch in enumerate(active['channels']):
            role=ch['function']
            frame[offset+i]=255 if role=='Master' or (color=='red' and role=='Red') or (color=='white' and role=='White') else 0
    return bytes(frame)


def prepare_fixture_update(state,device_id,data,input_universe=None,output_universe=None,extra_fixtures=None):
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
            controls[did]=[validate_control(c,controls[did],patch+virtuals+(extra_fixtures or [])) for c in controls[did]]
    return maps,presets,controls,affected


def prepare_fixture_import(state,device_id,raw_fixtures,input_universe=None,output_universe=None,extra_fixtures=None):
    """Classify an import without overwriting existing fixtures; return safe, atomic candidates."""
    if not isinstance(raw_fixtures,list) or not 1<=len(raw_fixtures)<=256:
        raise ValueError('Import must contain 1–256 fixtures')
    current=copy.deepcopy(state.get('fixtureMaps',{}).get(device_id,initial_patch(device_id)))
    presets=copy.deepcopy(state.get('fixturePresets',PRESETS))
    rows=[]; additions=[]; seen_in_file=[]

    def identity(f):
        return (f['name'].strip().casefold(),f['universe'],f['start'],tuple((c['function'].strip().casefold(),c['default']) for c in f['channels']),f['mappingPreset'])
    def overlaps(a,b):
        return a['universe']==b['universe'] and a['start']<=b['start']+len(b['channels'])-1 and b['start']<=a['start']+len(a['channels'])-1

    for index,raw in enumerate(raw_fixtures,1):
        try:
            candidate_data=copy.deepcopy(raw)
            if not isinstance(candidate_data,dict):raise ValueError('Fixture entry must be an object')
            candidate_data.pop('id',None)
            candidate=validate_fixture(candidate_data,[],presets)
            exact=next((f for f in current if identity(f)==identity(candidate)),None)
            if exact:
                rows.append(dict(index=index,name=candidate['name'],universe=candidate['universe'],start=candidate['start'],end=candidate['start']+len(candidate['channels'])-1,status='existing',message='Already in this patch; skipped'))
                continue
            duplicate=next((f for f in seen_in_file if identity(f)==identity(candidate)),None)
            if duplicate:
                rows.append(dict(index=index,name=candidate['name'],universe=candidate['universe'],start=candidate['start'],end=candidate['start']+len(candidate['channels'])-1,status='duplicate',message='Duplicate entry in this file; skipped'))
                continue
            conflict=next((f for f in current+additions if overlaps(f,candidate)),None)
            if conflict:
                rows.append(dict(index=index,name=candidate['name'],universe=candidate['universe'],start=candidate['start'],end=candidate['start']+len(candidate['channels'])-1,status='conflict',message='Overlaps '+conflict['name']+f' (DMX {conflict["start"]}–{conflict["start"]+len(conflict["channels"])-1})'))
                seen_in_file.append(candidate)
                continue
            seen_in_file.append(candidate)
            try:
                new_candidate=copy.deepcopy(candidate);new_candidate.pop('id',None)
                maps,updated_presets,controls,_=prepare_fixture_update(
                    {**state,'fixtureMaps':{**state.get('fixtureMaps',{}),device_id:current},'fixturePresets':presets},
                    device_id,new_candidate,input_universe,output_universe,extra_fixtures)
                current=maps[device_id];presets=updated_presets
                additions.append(current[-1])
                rows.append(dict(index=index,name=candidate['name'],universe=candidate['universe'],start=candidate['start'],end=candidate['start']+len(candidate['channels'])-1,status='ready',message='Ready to add'))
            except (ValueError,TypeError,KeyError) as exc:
                rows.append(dict(index=index,name=candidate.get('name','(unnamed)'),universe=candidate.get('universe'),start=candidate.get('start'),end=None,status='invalid',message=str(exc)))
        except (ValueError,TypeError,KeyError) as exc:
            rows.append(dict(index=index,name=raw.get('name','(unnamed)') if isinstance(raw,dict) else '(invalid)',universe=raw.get('universe') if isinstance(raw,dict) else None,start=raw.get('start') if isinstance(raw,dict) else None,end=None,status='invalid',message=str(exc)))
    if additions:
        # Rebuild from original on preview; apply returns the complete validated state snapshot.
        maps=copy.deepcopy(state.get('fixtureMaps',{}));maps.setdefault(device_id,initial_patch(device_id))
        for candidate in additions:
            new_candidate=copy.deepcopy(candidate);new_candidate.pop('id',None)
            maps,presets,controls,_=prepare_fixture_update(
                {**state,'fixtureMaps':maps,'fixturePresets':presets,'inputControls':state.get('inputControls',{})},
                device_id,new_candidate,input_universe,output_universe,extra_fixtures)
        result=(maps,presets,controls)
    else:result=(None,None,None)
    return dict(rows=rows,ready=sum(r['status']=='ready' for r in rows),existing=sum(r['status'] in ('existing','duplicate') for r in rows),blocked=sum(r['status'] in ('conflict','invalid') for r in rows),state=result)
