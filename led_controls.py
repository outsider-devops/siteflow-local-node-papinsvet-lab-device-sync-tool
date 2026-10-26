"""Imported SP801E pixel fixtures and editable virtual-DMX adapters."""
import copy,math,time
FUNCTIONS=['Master','Red','Green','Blue','White','Amber','UV','Strobe']
def defaults():
    base=dict(count=144,order='GRB',limit=32,packing=512,mapping=FUNCTIONS,transport='artnet',controllerIp='192.168.18.3')
    items=[dict(copy.deepcopy(base),id='led:strip'+str(i),name='LED Strip P'+str(i),port=i,baseUniverse=1,firstUniverse=2*i-1,mirror=False) for i in range(1,5)]
    items.append(dict(id='led:matrix',name='Vertical LED Matrix',controllerIp='192.168.18.157',port=1,count=200,order='BRG',limit=64,packing=512,mapping=list(FUNCTIONS),transport='artnet',baseUniverse=16,firstUniverse=16,mirror=True))
    return items

def virtual_fixture(f):
    return dict(id=f['id'],name=f['name'],universe=1,start=1,channels=[dict(function='Unknown' if k=='UV' else k,default=0) for k in f['mapping']],mappingPreset='custom',virtualLED=True,controllerIp=f['controllerIp'])

def validate_mapping(data,old):
    mapping=data.get('mapping');limit=data.get('limit',old['limit'])
    if not isinstance(mapping,list) or not 1<=len(mapping)<=16 or any(k not in FUNCTIONS+['Unknown'] for k in mapping):raise ValueError('Use 1–16 virtual channels with known functions')
    known=[k for k in mapping if k!='Unknown']
    if len(known)!=len(set(known)):raise ValueError('Each function may appear once')
    if type(limit) is not int or not 0<=limit<=255:raise ValueError('Brightness limit must be 0–255')
    return dict(old,mapping=list(mapping),limit=limit)

def rgb_value(f,values,now):
    roles=dict(zip(f['mapping'],values));master=roles.get('Master',255)/255;white=roles.get('White',0);amber=roles.get('Amber',0)
    rgb=[min(255,roles.get('Red',0)+white+amber),min(255,roles.get('Green',0)+white+round(amber*96/255)),min(255,roles.get('Blue',0)+white)]
    strobe=roles.get('Strobe',0)
    if strobe and (now*(.5+9.5*strobe/255))%1>=.5:master=0
    return dict(zip('RGB',[round(v*master*f['limit']/255) for v in rgb]))

def controller_frames(fixtures,values,now):
    frames={}
    for f in fixtures:
        if f['id'] not in values:continue
        rgb=rgb_value(f,values[f['id']],now);payload=[rgb[k] for k in f['order']]*f['count'];ip=f['controllerIp'];base=f['baseUniverse']
        for u in range(base,base+8):frames.setdefault((ip,u),[0]*512)
        pairs=[f['firstUniverse']] if not f['mirror'] else [base+2*i for i in range(4)]
        for start in pairs:
            for i,value in enumerate(payload):frames[(ip,start+i//512)][i%512]=value
    return frames

class LEDRuntime:
    def __init__(self):self.manual={};self.last_active=set();self.status={};self.last_values={}
    def set_values(self,f,values,enabled):
        if type(enabled) is not bool or not isinstance(values,list) or len(values)!=len(f['mapping']) or any(type(v) is not int or not 0<=v<=255 for v in values):raise ValueError('Invalid virtual DMX values')
        self.manual[f['id']]=dict(values=list(values),enabled=enabled)
    def values(self,fixtures,controls,monitor,ip):
        from input_controls import semantic_rules
        virtual=[virtual_fixture(f) for f in fixtures];ids={f['id'] for f in fixtures}
        result={k:list(v['values']) for k,v in self.manual.items() if v['enabled'] and k in ids}
        with monitor.lock:
            for c in controls:
                targets=[fid for fid in c.get('targetFixtures',[]) if fid in ids]
                if not c.get('enabled') or not targets:continue
                try:baseline=monitor._fixture_baseline(ip,c['universe'])
                except ValueError:
                    if not any(o['controlId']==c['id'] for o in monitor.manual_overrides.values()):continue
                    baseline=monitor._optional_baseline(ip,c['universe'])
                rules=semantic_rules(targets,virtual,c.get('faderFunctions'))
                for fid in targets:result.setdefault(fid,[0]*len(next(f for f in fixtures if f['id']==fid)['mapping']))
                for r in rules:
                    index=c['start']+r['input']-2
                    if index>=len(baseline):continue
                    override=monitor.manual_overrides.get((c['id'],r['input']))
                    value=override['value'] if override else baseline[index]
                    result[r['fixtureId']][r['channel']-1]=max(result[r['fixtureId']][r['channel']-1],value)
        self.last_values=result
        return result
