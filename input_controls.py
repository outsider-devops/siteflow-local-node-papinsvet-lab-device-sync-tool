"""Virtual DMX inputs with direct 1:1 and semantic function-remap modes."""
import uuid

FADER_FUNCTIONS=['Master','Red','Green','Blue','White','Amber','UV','Strobe']

def routing_mode(control):
    """Infer a safe mode for saved controls created before routingMode existed."""
    mode=control.get('routingMode') if isinstance(control,dict) else None
    if mode in ('direct','mapped'):return mode
    return 'mapped' if control.get('targetFixtures') else 'direct'

def semantic_rules(target_ids,fixtures,functions=None):
    if not isinstance(target_ids,list) or len(target_ids)>128 or any(not isinstance(fid,str) for fid in target_ids) or len(set(target_ids))!=len(target_ids):raise ValueError('Select unique target fixtures')
    functions=FADER_FUNCTIONS if functions is None else functions
    if not isinstance(functions,list) or len(functions) not in (8,16) or any(function is not None and (not isinstance(function,str) or not function.strip() or len(function)>80) for function in functions):raise ValueError('Invalid fader functions')
    rules=[]
    for fid in target_ids:
        f=next((f for f in fixtures if f['id']==fid),None)
        if not f:raise ValueError('Target fixture not found')
        for i,function in enumerate(functions):
            if function is None:continue
            matches=[n+1 for n,ch in enumerate(f['channels']) if ch['function'].strip().casefold()==function.casefold()]
            if len(matches)>1:raise ValueError('Ambiguous function in '+f['name']+': '+function)
            if matches:rules.append(dict(input=i+1,fixtureId=fid,channel=matches[0],min=0,max=255))
    return rules


def initial_controls(device_id):
    if device_id!='00:00:c8:d8:a2:71':return []
    return [dict(id='desk-spare-81',name='Desk controls 81–96',universe=1,start=81,count=16,rules=[])]

def validate_control(data,existing,fixtures):
    if not isinstance(data,dict):raise ValueError('Invalid input control')
    name=data.get('name');start=data.get('start');count=data.get('count');universe=data.get('universe');cid=data.get('id')
    if not isinstance(name,str) or not name.strip() or len(name)>80:raise ValueError('Name required (maximum 80 characters)')
    if type(universe) is not int or not 0<=universe<=255:raise ValueError('Invalid universe')
    if type(start) is not int or type(count) is not int or count<1 or start<1 or start+count-1>192:raise ValueError('Desk input must fit channels 1–192')
    if cid is not None and not any(c['id']==cid for c in existing):raise ValueError('Input control not found')
    for f in fixtures:
        if not f.get('virtualLED') and not f.get('virtualWiz') and f['universe']==universe and start<=f['start']+len(f['channels'])-1 and f['start']<=start+count-1:raise ValueError('Input range is occupied by '+f['name'])
    for c in existing:
        if c['id']!=cid and c['universe']==universe and start<=c['start']+c['count']-1 and c['start']<=start+count-1:raise ValueError('Input range overlaps '+c['name'])
    requested_mode=data.get('routingMode')
    if requested_mode is not None and requested_mode not in ('direct','mapped'):raise ValueError('Select direct or function-remap mode')
    legacy=requested_mode is None and 'targetFixtures' not in data and bool(data.get('rules'))
    mode=('legacy' if legacy else routing_mode(data))
    semantic=mode in ('direct','mapped')
    if semantic and count not in (8,16):raise ValueError('Fader banks must contain 8 or 16 input channels')
    functions=data.get('faderFunctions',FADER_FUNCTIONS+[None]*(count-8))
    if semantic:semantic_rules([],fixtures,functions)  # Validate function labels in both modes.
    targets=data.get('targetFixtures',[])
    if not isinstance(targets,list) or any(not isinstance(fid,str) for fid in targets):raise ValueError('Select valid target fixtures')
    target_modes=data.get('targetModes',{})
    if not isinstance(target_modes,dict) or any(fid not in targets or target_mode not in ('original','parled','pixel-fill','default') for fid,target_mode in target_modes.items()):raise ValueError('Invalid target control preset')
    for fid,target_mode in target_modes.items():
        target=next((f for f in fixtures if f['id']==fid),None)
        if target_mode=='parled' and (not target or target.get('mappingPreset')!='beam230-16ch' and target.get('id')!='led:matrix'):raise ValueError('PAR LED mode requires Beam 230 or LED Matrix')
        if target_mode=='pixel-fill' and (not target or target.get('id')!='led:matrix'):raise ValueError('Pixel Fill requires LED Matrix')
    rules=semantic_rules(targets,fixtures,functions) if mode=='mapped' else data.get('rules',[]) if mode=='legacy' else []
    if not isinstance(rules,list) or len(rules)>1024:raise ValueError('Invalid rules')
    clean=[];used=set()
    for r in rules:
        if not isinstance(r,dict):raise ValueError('Invalid rule')
        offset=r.get('input');channel=r.get('channel');low=r.get('min');high=r.get('max');fid=r.get('fixtureId')
        target=next((f for f in fixtures if f['id']==fid),None)
        if type(offset) is not int or not 1<=offset<=count:raise ValueError('Invalid input channel')
        if not target or type(channel) is not int or not 1<=channel<=len(target['channels']):raise ValueError('Select an existing fixture and relative channel')
        if type(low) is not int or type(high) is not int or not 0<=low<=255 or not 0<=high<=255:raise ValueError('Output range must be 0–255')
        key=(fid,channel)
        if key in used:raise ValueError('A target fixture channel can only be assigned once per virtual input')
        used.add(key);clean.append(dict(input=offset,fixtureId=fid,channel=channel,min=low,max=high))
    result=dict(id=cid or uuid.uuid4().hex,name=name.strip(),universe=universe,start=start,count=count,rules=clean)
    if semantic:
        result.update(routingMode=mode,mappingMode='direct-1to1' if mode=='direct' else 'semantic-pages',
                      targetFixtures=targets,faderFunctions=functions,targetModes=target_modes)
        direct_fixture=data.get('directFixtureId')
        if direct_fixture is not None and (not isinstance(direct_fixture,str) or not any(f['id']==direct_fixture for f in fixtures)):
            raise ValueError('Direct fixture profile not found')
        if mode=='direct':result['directFixtureId']=direct_fixture
    enabled=data.get('enabled',True)
    if type(enabled) is not bool:raise ValueError('Invalid forwarding state')
    result['enabled']=enabled
    return result
