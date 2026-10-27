"""Imported SP801E pixel fixtures and editable virtual-DMX adapters."""
import copy,math,time
from midi_inputs import mapped_values as midi_mapped_values, LastChange
FUNCTIONS=['Master','Red','Green','Blue','White','Amber','UV','Strobe']
def defaults():
    base=dict(count=144,order='GRB',limit=32,packing=512,mapping=FUNCTIONS,transport='artnet',controllerIp='192.168.18.3')
    items=[dict(copy.deepcopy(base),id='led:strip'+str(i),name='LED Strip P'+str(i),port=i,baseUniverse=1,firstUniverse=2*i-1,mirror=False) for i in range(1,5)]
    items.append(dict(id='led:matrix',name='Vertical LED Matrix',controllerIp='192.168.18.157',port=1,count=200,order='BRG',limit=64,packing=512,mapping=list(FUNCTIONS),transport='artnet',baseUniverse=16,firstUniverse=16,mirror=True,renderMode='pixel-fill',reverse=False,matrixLayout=dict(pixelsPerRow=100,rows=2,serpentine=True,axis='vertical')))
    return items

def virtual_fixture(f,universe=1):
    return dict(id=f['id'],name=f['name'],universe=universe,start=1,channels=[dict(function='Unknown' if k=='UV' else k,default=0) for k in f['mapping']],mappingPreset='custom',virtualLED=True,controllerIp=f['controllerIp'])

def validate_mapping(data,old):
    mapping=data.get('mapping');limit=data.get('limit',old['limit'])
    if not isinstance(mapping,list) or not 1<=len(mapping)<=16 or any(k not in FUNCTIONS+['Unknown'] for k in mapping):raise ValueError('Use 1–16 virtual channels with known functions')
    known=[k for k in mapping if k!='Unknown']
    if len(known)!=len(set(known)):raise ValueError('Each function may appear once')
    if type(limit) is not int or not 0<=limit<=255:raise ValueError('Brightness limit must be 0–255')
    mode=data.get('renderMode',old.get('renderMode','parled'));reverse=data.get('reverse',old.get('reverse',False))
    if mode not in ('parled','pixel-fill','screen') or mode in ('pixel-fill','screen') and old['id']!='led:matrix':raise ValueError('Pixel rendering presets are available for LED Matrix only')
    if type(reverse) is not bool:raise ValueError('Invalid pixel direction')
    layout=data.get('matrixLayout',old.get('matrixLayout',dict(pixelsPerRow=old['count'],rows=1,serpentine=False,axis='horizontal')))
    if old['id']=='led:matrix':
        if not isinstance(layout,dict):raise ValueError('Invalid screen layout')
        length=layout.get('pixelsPerRow');rows=layout.get('rows');serpentine=layout.get('serpentine');axis=layout.get('axis','vertical')
        if type(length) is not int or not 1<=length<=4096 or type(rows) is not int or not 1<=rows<=4096 or length*rows!=old['count']:
            raise ValueError(f'Screen layout must contain exactly {old["count"]} pixels ({old["count"]} configured groups)')
        if type(serpentine) is not bool or axis not in ('vertical','horizontal'):raise ValueError('Choose a valid row direction and wiring mode')
        layout=dict(pixelsPerRow=length,rows=rows,serpentine=serpentine,axis=axis)
    return dict(old,mapping=list(mapping),limit=limit,renderMode=mode,reverse=reverse,matrixLayout=layout if old['id']=='led:matrix' else old.get('matrixLayout'))

def strobe_open(value,now):
    """LED emulation: 0 and 255 are steady; intermediate values pulse."""
    return not (0<value<255) or (now*(.5+9.5*value/254))%1<.5

def rgb_value(f,values,now):
    roles=dict(zip(f['mapping'],values));master=roles.get('Master',255)/255;white=roles.get('White',0);amber=roles.get('Amber',0)
    rgb=[min(255,roles.get('Red',0)+white+amber),min(255,roles.get('Green',0)+white+round(amber*96/255)),min(255,roles.get('Blue',0)+white)]
    strobe=roles.get('Strobe',0)
    if not strobe_open(strobe,now):master=0
    return dict(zip('RGB',[round(v*master*f['limit']/255) for v in rgb]))

def pixel_payload(f,values,now):
    mode=f.get('renderMode')
    if mode not in ('pixel-fill','screen'):
        rgb=rgb_value(f,values,now)
        return [rgb[k] for k in f['order']]*f['count']
    roles=dict(zip(f['mapping'],values));count=f['count'];master=roles.get('Master',255)/255*f['limit']/255
    strobe=roles.get('Strobe',0)
    if not strobe_open(strobe,now):master=0
    layout=f.get('matrixLayout') or dict(pixelsPerRow=count,rows=1,serpentine=False,axis='horizontal')
    if mode=='screen':
        length=layout['pixelsPerRow'];rows=layout['rows'];axis=layout.get('axis','vertical');serpentine=layout.get('serpentine',False)
        payload=[]
        for physical in range(count):
            band,offset=divmod(physical,length)
            reverse=serpentine and band%2==1
            position=length-1-offset if reverse else offset
            if axis=='vertical':
                row_from_bottom=position
            else:
                # A horizontal scan fills complete screen rows from the bottom.
                row_from_bottom=rows-1-band
            coverage={role:max(0,min(1,roles.get(role,0)*({ 'vertical':length, 'horizontal':rows }[axis])/255-row_from_bottom)) for role in ('Red','Green','Blue','White','Amber','UV')}
            rgb=[coverage['Red']+coverage['White']+coverage['Amber']+.55*coverage['UV'],coverage['Green']+coverage['White']+.376*coverage['Amber'],coverage['Blue']+coverage['White']+coverage['UV']]
            color=dict(zip('RGB',[round(min(1,v)*255*master) for v in rgb]))
            payload.extend(color[k] for k in f['order'])
        return payload
    payload=[]
    for physical in range(count):
        i=count-1-physical if f.get('reverse') else physical
        coverage={role:max(0,min(1,roles.get(role,0)*count/255-i)) for role in ('Red','Green','Blue','White','Amber','UV')}
        rgb=[coverage['Red']+coverage['White']+coverage['Amber']+.55*coverage['UV'],
             coverage['Green']+coverage['White']+.376*coverage['Amber'],
             coverage['Blue']+coverage['White']+coverage['UV']]
        color=dict(zip('RGB',[round(min(1,v)*255*master) for v in rgb]))
        payload.extend(color[k] for k in f['order'])
    return payload


def controller_frames(fixtures,values,now,render_modes=None):
    frames={}
    for f in fixtures:
        if f['id'] not in values:continue
        f=dict(f,renderMode=(render_modes or {}).get(f['id'],f.get('renderMode','parled')))
        payload=pixel_payload(f,values[f['id']],now);ip=f['controllerIp'];base=f['baseUniverse']
        for u in range(base,base+8):frames.setdefault((ip,u),[0]*512)
        pairs=[f['firstUniverse']] if not f['mirror'] else [base+2*i for i in range(4)]
        for start in pairs:
            for i,value in enumerate(payload):frames[(ip,start+i//512)][i%512]=value
    return frames

class LEDRuntime:
    def __init__(self):self.takeover=LastChange();self.render_modes={};self.manual={};self.last_active=set();self.status={};self.last_values={}
    def set_values(self,f,values,enabled):
        if type(enabled) is not bool or not isinstance(values,list) or len(values)!=len(f['mapping']) or any(type(v) is not int or not 0<=v<=255 for v in values):raise ValueError('Invalid virtual DMX values')
        self.manual[f['id']]=dict(values=list(values),enabled=enabled,changed=time.monotonic())
    def values(self,fixtures,controls,monitor,ip,midi_controls=None):
        from input_controls import semantic_rules
        virtual=[virtual_fixture(f) for f in fixtures];ids={f['id'] for f in fixtures}
        result={k:list(v['values']) for k,v in self.manual.items() if v['enabled'] and k in ids}
        desk_times={('virtual',f['id'],role):self.manual.get(f['id'],{}).get('changed',0) for f in fixtures for role in f['mapping'] if f['id'] in result}
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
                    streams=[s for s in monitor.streams.values() if s['sourceIp']==ip and s['universe']==c['universe']]
                    stamp=override.get('changed',0) if override else streams[0].get('_changed',{}).get(index+1,0) if len(streams)==1 else 0
                    role=next(f for f in fixtures if f['id']==r['fixtureId'])['mapping'][r['channel']-1]
                    k=('virtual',r['fixtureId'],role);desk_times[k]=max(desk_times.get(k,0),stamp)
                    result[r['fixtureId']][r['channel']-1]=max(result[r['fixtureId']][r['channel']-1],value)
        desk={('virtual',f['id'],role):result[f['id']][i] for f in fixtures if f['id'] in result for i,role in enumerate(f['mapping'])}
        incoming=midi_mapped_values(midi_controls or [],virtual)
        mixed=self.takeover.mix(desk,incoming,desk_times)
        for (kind,fid,role),value in mixed.items():
            f=next(f for f in fixtures if f['id']==fid);result.setdefault(fid,[0]*len(f['mapping']))[f['mapping'].index(role)]=value
        # A matrix has one renderer. Select the mode belonging to the most
        # recently moved source; idle repeated DMX frames never change it.
        self.render_modes={}
        for f in fixtures:
            if f['id']!='led:matrix':continue
            choices=[]
            manual=self.manual.get(f['id'],{})
            if manual.get('enabled'):choices.append((manual['changed'],f.get('renderMode','pixel-fill')))
            for c in controls:
                if not c.get('enabled') or f['id'] not in c.get('targetFixtures',[]):continue
                with monitor.lock:
                    streams=[s for s in monitor.streams.values() if s['sourceIp']==ip and s['universe']==c['universe'] and time.monotonic()-s['_last']<10]
                    overrides=[o.get('changed',0) for (cid,_),o in monitor.manual_overrides.items() if cid==c['id']]
                    if not streams and not overrides:continue
                    stamps=[stamp for s in streams for channel,stamp in s.get('_changed',{}).items() if c['start']<=channel<c['start']+c.get('count',16)]
                mode=c.get('targetModes',{}).get(f['id'])
                choices.append((max(stamps+overrides,default=0),mode if mode in ('parled','pixel-fill','screen') else f.get('renderMode','pixel-fill')))
            for c in midi_controls or []:
                if not c.get('enabled') or f['id'] not in c.get('targetFixtures',[]) or not any(c['changed']):continue
                mode=c.get('targetModes',{}).get(f['id'])
                choices.append((max(c['changed']),mode if mode in ('parled','pixel-fill','screen') else f.get('renderMode','pixel-fill')))
            self.render_modes[f['id']]=max(choices,key=lambda choice:choice[0])[1] if choices else f.get('renderMode','pixel-fill')
        self.last_values=result
        return result
