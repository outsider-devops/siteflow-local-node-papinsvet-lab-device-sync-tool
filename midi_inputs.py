"""Node-side MIDI sections: sixteen semantic faders, independent of browser tabs."""
from copy import deepcopy
import threading
import time
import uuid

from input_controls import FADER_FUNCTIONS, semantic_rules
from midi_controls import same_identity


def identity(control):
    return {key:control.get(key) for key in ('source','type','channel','number')}


def key(control):
    return tuple(control.get(field) for field in ('source','type','channel','number'))


def decode(event):
    b=event.get('bytes',[])
    if not b:return None
    kind=b[0]&240;channel=(b[0]&15)+1
    if kind==176 and len(b)==3:return dict(source=event['source'],type='Control Change',channel=channel,number=b[1],value=b[2])
    if kind==224 and len(b)==3:return dict(source=event['source'],type='Pitch Bend',channel=channel,number=None,value=((b[2]<<7)|b[1])-8192)
    if kind in (128,144) and len(b)==3:return dict(source=event['source'],type='Note',channel=channel,number=b[1],value=b[2] if kind==144 else 0,pressed=kind==144 and b[2]>0)
    return None


def validate_section(data,existing,bindings,fixtures,source_id):
    if not isinstance(data,dict):raise ValueError('Invalid MIDI section')
    name=data.get('name','').strip();sid=data.get('id')
    if not name or len(name)>80:raise ValueError('Section name must contain 1–80 characters')
    if sid is not None and not any(s['id']==sid and s['sourceId']==source_id for s in existing):raise ValueError('MIDI section not found')
    enabled=data.get('enabled',True)
    if type(enabled) is not bool:raise ValueError('Invalid section state')
    functions=data.get('faderFunctions',FADER_FUNCTIONS+[None]*8)
    if not isinstance(functions,list) or len(functions)!=16:raise ValueError('Use exactly 16 fader functions')
    known={v.casefold() for v in FADER_FUNCTIONS}
    known.update(ch['function'].strip().casefold() for f in fixtures for ch in f['channels'] if ch['function'].strip().casefold() not in ('unknown','unassigned'))
    if any(f is not None and (not isinstance(f,str) or f.casefold() not in known) for f in functions):raise ValueError('Select known fixture functions')
    assigned=[f.casefold() for f in functions if f]
    if len(assigned)!=len(set(assigned)):raise ValueError('Assign each function only once in a section')
    targets=data.get('targetFixtures',[])
    semantic_rules(targets,fixtures,functions)
    modes=data.get('targetModes',{})
    if not isinstance(modes,dict):raise ValueError('Invalid fixture control presets')
    for fid,mode in modes.items():
        f=next((f for f in fixtures if f['id']==fid),None)
        if fid not in targets or mode not in ('default','original','parled','pixel-fill') or mode=='parled' and f.get('mappingPreset')!='beam230-16ch' and fid!='led:matrix' or mode=='pixel-fill' and fid!='led:matrix':raise ValueError('Invalid target control preset')
    slots=data.get('slots',[None]*16)
    if not isinstance(slots,list) or len(slots)!=16:raise ValueError('Use exactly 16 MIDI slots')
    clean=[]
    for slot in slots:
        if slot is None:clean.append(None);continue
        if not isinstance(slot,dict) or not isinstance(slot.get('binding'),dict):raise ValueError('Select a MIDI input for each assigned slot')
        binding=next((c for c in bindings if same_identity(c,slot['binding'])),None)
        if binding is None:raise ValueError('MIDI mapping is no longer available; select it again')
        mode=slot.get('buttonMode','momentary');step=slot.get('step',2)
        if mode not in ('momentary','toggle') or type(step) is not int or not 1<=step<=32:raise ValueError('Invalid MIDI control behavior')
        clean.append(dict(binding=identity(binding),buttonMode=mode,step=step))
    return dict(id=sid or uuid.uuid4().hex,sourceId=source_id,name=name,enabled=enabled,slots=clean,
                count=16,routingMode='mapped',faderFunctions=functions,targetFixtures=targets,targetModes=dict(modes))


class MIDIInputs:
    def __init__(self):
        self.lock=threading.RLock();self.sections={};self.bindings={};self.values={};self.last={};self.activity={};self.revision=0

    def configure(self,banks,devices):
        with self.lock:
            self.sections={sid:(controller,deepcopy(s)) for controller,sections in banks.items() for s in sections for sid in [s['id']]}
            self.bindings={d['id']:{key(c):deepcopy(c) for c in d.get('midiControls',[])} for d in devices}
            for sid,(_,section) in self.sections.items():
                old=self.values.get(sid);fresh=[]
                for index,slot in enumerate(section['slots']):
                    binding=self.bindings.get(section['sourceId'],{}).get(key(slot['binding'])) if slot else None
                    stamp=(key(slot['binding']),slot.get('buttonMode'),slot.get('step')) if binding else None
                    previous=old[index] if old and old[index]['binding']==stamp else None
                    initial=128 if section['faderFunctions'][index] in ('Pan','Tilt') else 0
                    state=previous or dict(value=initial,changed=0,touched=False,pressed=False,manual=False,binding=stamp)
                    latest=self.last.get(key(binding)) if binding else None
                    if not previous and latest and binding['type']!='Note' and not binding.get('interpretation','').startswith('Button') and 'relative' not in binding.get('interpretation','').lower():
                        low=-8192 if binding['type']=='Pitch Bend' else binding.get('min',0);high=8191 if binding['type']=='Pitch Bend' else binding.get('max',127)
                        state.update(value=max(0,min(255,round((latest[0]['value']-low)*255/max(1,high-low)))),changed=latest[1],touched=True)
                    fresh.append(state)
                self.values[sid]=fresh
            self.values={sid:v for sid,v in self.values.items() if sid in self.sections}
            self.revision+=1

    def ingest(self,events):
        changed=False
        with self.lock:
            for event in events:
                message=decode(event)
                if message is None:continue
                now=event.get('monotonic') or time.monotonic();k=key(message);self.last[k]=(message,now)
                for source_id,bindings in self.bindings.items():
                    binding=bindings.get(k)
                    if binding is not None:
                        entry=dict(name=binding['name'],value=message['value'],last=now,layer=binding.get('templateLayer'))
                        self.activity.setdefault(source_id,{})[k]=entry
                for sid,(_,section) in self.sections.items():
                    binding=self.bindings.get(section['sourceId'],{}).get(k)
                    if binding is None:continue
                    for index,slot in enumerate(section['slots']):
                        if slot is None or key(slot['binding'])!=k:continue
                        state=self.values[sid][index];raw=message['value'];movement=False
                        button=binding['type']=='Note' or binding.get('interpretation','').startswith('Button')
                        if button:
                            pressed=message.get('pressed',raw==binding.get('onValue',127))
                            if slot['buttonMode']=='toggle':
                                value=(0 if state['value'] else 255) if pressed and not state['pressed'] else state['value']
                            else:value=255 if pressed else 0
                            state['pressed']=pressed
                        elif 'relative' in binding.get('interpretation','').lower():
                            mode=binding.get('interpretation','')
                            delta=raw-64 if 'binary offset' in mode else (-(raw&63) if raw&64 else raw) if 'signed bit' in mode else raw-128 if raw>63 else raw
                            value=max(0,min(255,state['value']+delta*slot['step']))
                            movement=delta!=0
                        else:
                            low=-8192 if binding['type']=='Pitch Bend' else binding.get('min',0)
                            high=8191 if binding['type']=='Pitch Bend' else binding.get('max',127)
                            value=max(0,min(255,round((raw-low)*255/max(1,high-low))))
                        if value!=state['value'] or not state['touched'] or state['manual'] or movement:
                            state.update(value=value,changed=now,touched=True,manual=False);changed=True
            if changed:self.revision+=1
        return changed

    def manual(self,section_id,index,value):
        if type(index) is not int or not 1<=index<=16 or type(value) is not int or not 0<=value<=255:raise ValueError('Use fader 1–16 and a value 0–255')
        with self.lock:
            if section_id not in self.values:raise ValueError('MIDI section not found')
            state=self.values[section_id][index-1];state.update(value=value,changed=time.monotonic(),touched=True,manual=True)
            self.revision+=1

    def routing_controls(self,controller):
        with self.lock:
            result=[]
            for sid,(parent,section) in self.sections.items():
                if parent!=controller or not section['enabled']:continue
                states=self.values[sid]
                result.append(dict(section,sourceType='midi',values=[s['value'] for s in states],
                                   changed=[s['changed'] if s['touched'] else 0 for s in states]))
            return result

    def snapshot(self,source_id=None):
        with self.lock:
            now=time.monotonic()
            activity={sid:[dict(name=e['name'],value=e['value'],age=max(0,now-e['last']),layer=e.get('layer'))
                           for e in sorted(items.values(),key=lambda e:e['last'],reverse=True)[:5]] for sid,items in self.activity.items() if source_id is None or sid==source_id}
            sections={sid:dict(values=[v['value'] for v in states],active=[v['touched'] for v in states],manual=[v['manual'] for v in states])
                      for sid,states in self.values.items() if source_id is None or self.sections[sid][1]['sourceId']==source_id}
            return dict(revision=self.revision,activity=activity,sections=sections)


class LastChange:
    """Compare value changes, never periodic repetitions of the same DMX frame."""
    def __init__(self):self.previous={};self.changed={}

    def mix(self,desk,midi,received=None):
        for k,v in desk.items():
            stamp=received.get(k,0) if isinstance(received,dict) else received or 0
            if k not in self.previous:self.changed[k]=stamp if isinstance(received,dict) else 0
            elif self.previous[k]!=v:self.changed[k]=stamp or time.monotonic()
            elif isinstance(received,dict):self.changed[k]=max(self.changed.get(k,0),stamp)
        self.previous=dict(desk);self.changed={k:v for k,v in self.changed.items() if k in desk}
        result=dict(desk)
        for k,(value,stamp,owner) in midi.items():
            if k not in desk or stamp>=self.changed.get(k,0):result[k]=value
        return result


def mapped_values(controls,fixtures,beam_converter=None):
    """Return only functions whose MIDI slot has actually received a value."""
    result={};known={f['id']:f for f in fixtures}
    def put(k,value,stamp,owner):
        if k not in result or stamp>=result[k][1]:result[k]=(value,stamp,owner)
    for c in controls:
        if not c.get('enabled'):continue
        targets=[fid for fid in c.get('targetFixtures',[]) if fid in known]
        parled={fid for fid in targets if c.get('targetModes',{}).get(fid)=='parled' and known[fid].get('mappingPreset')=='beam230-16ch'}
        rules=semantic_rules([fid for fid in targets if fid not in parled],fixtures,c['faderFunctions'])
        for rule in rules:
            index=rule['input']-1;stamp=c['changed'][index]
            if not stamp:continue
            f=known[rule['fixtureId']];role=f['channels'][rule['channel']-1]['function']
            k=('virtual',f['id'],role) if f.get('virtualWiz') or f.get('virtualLED') else ('dmx',f['start']+rule['channel']-2)
            put(k,c['values'][index],stamp,c['id'])
        if beam_converter:
            for fid in parled:
                f=known[fid];values=[0]*8;stamp=0
                for index,role in enumerate(c['faderFunctions']):
                    if role in FADER_FUNCTIONS and c['changed'][index]:values[FADER_FUNCTIONS.index(role)]=c['values'][index];stamp=max(stamp,c['changed'][index])
                if stamp:
                    for index,value in enumerate(beam_converter(fid,values)):put(('dmx',f['start']-1+index),value,stamp,c['id'])
    return result
