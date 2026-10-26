'use strict';
const $=s=>document.querySelector(s),q=new URLSearchParams(location.search),deviceId=q.get('device');
let lastSeen=0,events=[],sourceFilter='',timer=null,csrf='',learning=false,learnStartedAt=0,learned=new Map(),saved=new Set(),existingControls=[],selectedControl=null,captureTarget=null,surfaceSkin='papinsvet',removedControls=[],discardedCandidates=new Set(),receivedMIDIMessages=0,midiTemplate=null,bindingLayer='Basic',bindingIndex=new Map(),midiRevision=-1;
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function chooseSurface(id) {
  if (learning) return;
  selectedControl = SURFACE.find(c => c.id === id) || null;
  for (const node of $('#midi-surface').querySelectorAll('[data-surface-id]')) {
    node.classList.toggle('selected', !!selectedControl && physicalSurface(selectedControl).id === node.dataset.surfaceId);
  }
  const actions = selectedControl ? surfaceActions(selectedControl) : [];
  const select = $('#midi-control-action');
  select.innerHTML = actions.map(c => `<option value="${c.id}">${esc(surfaceActionLabel(c))}</option>`).join('');
  select.hidden = actions.length < 2;
  if (selectedControl) select.value = selectedControl.id;
  $('#midi-selected-control').textContent = selectedControl
    ? `${selectedControl.label}${selectedControl.reserved ? ' · Shift layer, not MIDI assignable' : ''}`
    : 'Select a control on the surface.';
  $('#front-cue-select').classList.toggle('selected',id==='front-cue-mix');
  const mapped=existingControls.find(c=>matchesSurface(c,id)&&(c.templateLayer===bindingLayer||!c.templateLayer||c.templateLayer==='Common'));
  setAssignmentControl(mapped||null);
  if(q.get('embed')==='1'&&window.parent!==window)window.parent.postMessage({type:'siteflow-midi-selection',control:mapped?{source:mapped.source,type:mapped.type,channel:mapped.channel,number:mapped.number}:null,surfaceId:id},location.origin);
}
function decode(bytes){if(!bytes?.length)return {name:'—',channel:'—',data:'—'};const status=bytes[0],kind=status&0xf0,channel=(status&15)+1,a=bytes[1]??0,b=bytes[2]??0,note=n=>({60:'C3',62:'D3',64:'E3',65:'F3',67:'G3',69:'A3',71:'B3',72:'C4'}[n]||`Note ${n}`);if(kind===0x80)return{name:'Note Off',channel,data:`${note(a)} · vel ${b}`};if(kind===0x90)return{name:b?'Note On':'Note Off',channel,data:`${note(a)} · vel ${b}`};if(kind===0xb0)return{name:'Control Change',channel,data:`CC ${a} · ${b}`};if(kind===0xc0)return{name:'Program Change',channel,data:String(a)};if(kind===0xe0)return{name:'Pitch Bend',channel,data:String((b<<7|a)-8192)};if(kind===0xd0)return{name:'Channel Pressure',channel,data:String(a)};if(kind===0xa0)return{name:'Poly Pressure',channel,data:`${note(a)} · ${b}`};return{name:`System · 0x${status.toString(16).padStart(2,'0')}`,channel:'—',data:bytes.slice(1).join(' ')};}
function controlEvent(e){const b=e.bytes||[],status=b[0]||0,kind=status&0xf0,ch=(status&15)+1;if(kind===0xb0)return {type:'Control Change',channel:ch,number:b[1]??0,value:b[2]??0,key:`${e.source}|cc|${ch}|${b[1]??0}`};if(kind===0xe0){const v=(((b[2]??0)<<7)|(b[1]??0))-8192;return {type:'Pitch Bend',channel:ch,number:null,value:v,key:`${e.source}|pitch|${ch}|`};}if(kind===0x80||kind===0x90){const on=kind===0x90&&(b[2]??0)>0;return {type:'Note',channel:ch,number:b[1]??0,value:b[2]??0,key:`${e.source}|note|${ch}|${b[1]??0}`,pressed:on};}return null;}
function classify(c) {
  const surface = SURFACE.find(x => x.id === c.surfaceControlId);
  if (c.type === 'Note') return 'Button / transport';
  if (surface?.action === 'press') return 'Button / CC press-release';
  if (c.type === 'Pitch Bend') return '14-bit absolute / Pitch Bend';
  const values = c.values || [];
  if (values.length >= 4 && (surface?.encoder || surface?.type === 'jog' || surface?.type === 'jog-rim')) {
    const both = values.some(v => v > 64) && values.some(v => v < 64);
    if (both && values.every(v => v <= 15 || v >= 113)) return "Relative encoder / two's complement";
    if (both && values.every(v => v >= 49 && v <= 79)) return 'Relative encoder / binary offset';
    if (values.some(v => v >= 65) && values.some(v => v > 0 && v < 16)
        && values.every(v => v <= 15 || v >= 64 && v <= 79)) return 'Relative encoder / signed bit';
  }
  return 'Fader or absolute knob';
}
function relativeDelta(value, interpretation) {
  if (interpretation.includes('binary offset')) return value - 64;
  if (interpretation.includes('signed bit')) return value & 64 ? -(value & 63) : value;
  return value > 63 ? value - 128 : value;
}
function controlLabel(c){return c.type==='Control Change'?`CC ${c.number}`:c.type==='Pitch Bend'?'Pitch Bend':`Note ${c.number}`;}
function controlKey(c) {
  const kind = c.type === 'Control Change' ? 'cc' : c.type === 'Pitch Bend' ? 'pitch' : 'note';
  return `${c.source}|${kind}|${c.channel}|${c.number ?? ''}`;
}
const trashIcon = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h18M9 6V4h6v2M5 6l1 14h12l1-14M10 10v6M14 10v6"/></svg>';
function identityMatches(control,event){return control.type===event.type&&control.channel===event.channel&&control.number===event.number&&control.source===event.source;}
let activeLearnKey=null,mouseControl=null,mouseStart=null,lastMidiValues=new Map(),surfaceAngles=new Map(),surfaceValues=new Map(),surfacePressed=new Map();
function setAssignmentControl(c){activeLearnKey=c?(c.key||controlKey(c)):null;const savedControl=c&&existingControls.find(x=>identityMatches(x,c));$('#midi-assignment-identity').textContent=c?`${controlLabel(c)} · CH ${c.channel}${c.surfaceControlName?' · '+c.surfaceControlName:''}`:'No control captured';$('#midi-assignment-range').textContent=c?`${c.min}–${c.max} · ${c.origin==='controller-editor'?'Template · '+c.templateLayer:(c.count||c.events||0)+' events'}${savedControl?.interpretation?' · '+savedControl.interpretation:''}`:'—';$('#midi-function').value=savedControl?.functionRole||'';$('#midi-target').value=savedControl?.target||'artnet';$('#midi-mapping-name').value=savedControl?.mappingName||c?.name||'';$('#save-assignment').disabled=!savedControl;$('#midi-assignment-status').textContent=savedControl?.functionRole?`Saved · ${savedControl.functionRole} → ${savedControl.target==='global'?'Global / transport':'Art-Net routing layer'}. MIDI output routing is not enabled yet.`:savedControl?'Choose a function and save this assignment. MIDI output routing is not enabled yet.':'Select a mapped control or save a learned control to assign its function.';}
function matchesSurface(control, id) {
  return control.surfaceControlId === id || (control.surfaceAliases || []).includes(id);
}
function indexBindings() {
  bindingIndex = new Map(existingControls.map(c=>[controlKey(c),c]));
}
function renderTemplateStatus() {
  $('#midi-template-status').textContent = midiTemplate
    ? `${midiTemplate.name} · ${existingControls.length} mapped · Basic + Shift`
    : 'No template loaded · Learn available';
  $('#import-midi-template').textContent = midiTemplate ? 'Reload template' : 'Load template';
  if(midiTemplate&&!events.length&&!learning)$('#midi-current-message').innerHTML='<span>MIDI template loaded</span><strong>Move a control on the Traktor</strong><small>Mapped inputs animate automatically · Learn optional</small>';
  $('#midi-import-note').textContent = midiTemplate ? 'Template loaded · select a control to assign its function. Learn can override it.' : 'Import the Controller Editor template or learn controls individually.';
}
async function importTemplate() {
  const button=$('#import-midi-template');button.disabled=true;
  try {
    const response=await fetch('/api/midi/template/import',{method:'POST',headers:{'Content-Type':'application/json','X-SiteFlow-Token':csrf},body:JSON.stringify({deviceId,source:$('#midi-source').value})});
    const data=await response.json();if(!response.ok)throw Error(data.error||'Could not load template');
    existingControls=data.controls;removedControls=data.removedControls||[];midiTemplate=data.template;
    renderKnown();renderTemplateStatus();if(selectedControl)chooseSurface(selectedControl.id);
    controlFeedback(`Loaded ${data.template.count} inputs from ${data.template.name}. Previous mappings were backed up.`);
  } catch(e) {controlFeedback(e.message);} finally {button.disabled=false;}
}
function renderKnown() {
  indexBindings();
  const host = $('#midi-known-controls');
  const mappedOpen=!!host.querySelector?.('.midi-saved-controls')?.open;
  const removedOpen=!!host.querySelector?.('.midi-removed-list')?.open;
  host.hidden = !existingControls.length && !removedControls.length;
  host.innerHTML = `<details class="midi-saved-controls"${mappedOpen?' open':''}><summary>Mapped controls · ${existingControls.length}</summary><div class="midi-saved-list">${existingControls.map(c => {
    const key = esc(controlKey(c));
    return `<div class="midi-known-control"><button type="button" class="midi-known-select" data-known-control="${key}"><strong>${esc(c.name)}</strong><small>${esc(controlLabel(c))} · CH ${c.channel}${c.templateLayer?' · '+esc(c.templateLayer):''}${c.functionRole ? ' · ' + esc(c.functionRole) : ''}</small></button><button type="button" class="midi-remove-control" data-remove-control="${key}" aria-label="Remove ${esc(c.name)}" title="Remove control and assignment">${trashIcon}</button></div>`;
  }).join('')}</div></details>` + (removedControls.length ? `<details class="midi-removed-list"${removedOpen?' open':''}><summary>Recently removed · ${removedControls.length}</summary>${removedControls.slice().reverse().map(entry => `<div><span>${esc(entry.control.name)} <small>${esc(controlLabel(entry.control))} · CH ${entry.control.channel}</small></span><button type="button" data-restore-control="${esc(entry.id)}">Restore</button></div>`).join('')}</details>` : '');
}
function controlFeedback(message) {
  const node = $('#midi-control-feedback'); node.hidden = false; node.textContent = message;
}
async function removeControl(key, button) {
  const control = existingControls.find(c => controlKey(c) === key);
  if (!control) return;
  button.disabled = true;
  try {
    const response = await fetch('/api/midi/controls/remove', {
      method:'POST',headers:{'Content-Type':'application/json','X-SiteFlow-Token':csrf},
      body:JSON.stringify({deviceId,control:{type:control.type,channel:control.channel,number:control.number,source:control.source}})
    });
    const data = await response.json(); if (!response.ok) throw Error(data.error || 'Could not remove control');
    existingControls = data.controls; removedControls = data.removedControls;
    saved.delete(key); learned.delete(key); discardedCandidates.add(key);
    const surface = SURFACE.find(c => c.id === control.surfaceControlId);
    if (surface) pressSurface(surface, false);
    lastMidiValues.delete(key);
    if (activeLearnKey === key) setAssignmentControl(null);
    renderKnown(); renderLearn(); controlFeedback(`Removed ${control.name}. Restore is available in Recently removed.`);
  } catch (e) { button.disabled = false; controlFeedback(e.message); }
}
async function restoreControl(id, button) {
  button.disabled = true;
  try {
    const response = await fetch('/api/midi/controls/restore', {
      method:'POST',headers:{'Content-Type':'application/json','X-SiteFlow-Token':csrf},
      body:JSON.stringify({deviceId,removalId:id})
    });
    const data = await response.json(); if (!response.ok) throw Error(data.error || 'Could not restore control');
    existingControls = data.controls; removedControls = data.removedControls;
    const key = controlKey(data.changed); discardedCandidates.delete(key);
    renderKnown(); renderLearn(); setAssignmentControl(data.changed); controlFeedback(`Restored ${data.changed.name}.`);
  } catch (e) { button.disabled = false; controlFeedback(e.message); }
}
function discardCapture(key) {
  learned.delete(key); saved.delete(key); discardedCandidates.add(key);
  if (activeLearnKey === key) setAssignmentControl(null);
  renderLearn(); controlFeedback('Capture discarded. Start a new capture to learn this input again.');
}
function renderLearn(){const rows=[...learned.values()].sort((a,b)=>b.count-a.count);$('#learn-results').innerHTML=rows.length?rows.map(c=>{const label=controlLabel(c),mode=classify(c),range=c.type==='Note'?`pressed ${c.presses}× · released ${c.releases}× · velocity ${c.presses?`${c.min}–${c.max}`:'—'}`:`range ${c.min}–${c.max} · ${c.count} events`,id=esc(c.key),already=saved.has(c.key)||existingControls.some(x=>identityMatches(x,c)),incomplete=c.type==='Note'&&!c.presses;return `<article class="midi-learn-row ${activeLearnKey===c.key?'is-active':''}" data-learn-row="${id}"><div class="midi-learn-summary"><strong>${esc(label)}</strong><span>CH ${c.channel}</span><span>${esc(mode)}</span><code>${esc(range)}</code><small>${esc(c.source)}</small></div><div class="midi-learn-save"><input aria-label="Control name" value="${esc(c.name||captureTarget?.label||label)}" data-name="${id}"><button ${already?'data-select-capture':'data-save'}="${id}" ${incomplete?'disabled':''}>${already?'Saved · select': 'Save control'}</button><button class="midi-discard-capture" data-discard-capture="${id}" title="Discard this capture" aria-label="Discard ${esc(label)} capture">${trashIcon}</button></div></article>`;}).join(''):'<div class="empty">Move the selected fader, knob, jog or button.</div>';}
function chooseLearned(key) {
  const c = learned.get(key) || existingControls.find(x => controlKey(x) === key);
  if (!c) return;
  if(c.templateLayer==='Basic'||c.templateLayer==='Shift'){bindingLayer=c.templateLayer;$('#midi-binding-layer').value=bindingLayer;}
  if(SURFACE.some(x=>x.id===c.surfaceControlId))chooseSurface(c.surfaceControlId);
  setAssignmentControl(c); renderLearn();
}
function capture(e){if(!learning||e.time<learnStartedAt||sourceFilter&&e.source!==sourceFilter)return false;const m=controlEvent(e);if(!m||discardedCandidates.has(m.key))return false;let c=learned.get(m.key);if(!c){c={key:m.key,source:e.source,type:m.type,channel:m.channel,number:m.number,min:m.type==='Note'?127:m.value,max:m.type==='Note'?0:m.value,count:0,presses:0,releases:0,values:[],name:'',surfaceControlId:captureTarget.id,surfaceControlName:captureTarget.label};learned.set(m.key,c);}c.count++;if(m.type==='Note'){if(m.pressed){c.presses++;c.min=Math.min(c.min,m.value);c.max=Math.max(c.max,m.value);}else c.releases++;}else{c.min=Math.min(c.min,m.value);c.max=Math.max(c.max,m.value);if(c.values.length<200)c.values.push(m.value);}activeLearnKey=c.key;setAssignmentControl(c);const info=$('#midi-current-message');info.innerHTML=`<span>Latest input · ${esc(captureTarget.label)}</span><strong>${esc(controlLabel(m))} · CH ${m.channel} · ${m.value}</strong><small>${esc(classify(c))} · range ${c.min}–${c.max}</small>`;return true;}
function visualSurface(c) {
  const physical = physicalSurface(c);
  return SURFACE.find(x => x.id === physical.animationTarget) || physical;
}
function setSurfaceValue(c, value) {
  c = visualSurface(c);
  const node = $(`#midi-surface [data-surface-id="${CSS.escape(c.id)}"]`);
  const face = node?.querySelector('[data-control-face]');
  const norm = Math.max(0, Math.min(1, value));
  surfaceValues.set(c.id, norm);
  if(c.id==='front-cue-mix'){$('#front-cue-value').textContent=String(Math.round(norm*127));$('#front-cue-range').value=String(Math.round(norm*127));return;}
  if (!face) return;
  if (c.type === 'knob') {
    face.querySelector('.surface-knob-rotor')?.setAttribute('transform', `rotate(${-135 + 270 * norm})`);
  } else if (c.type === 'fader' || c.type === 'crossfader') {
    const position = c.travel * (norm - .5);
    face.querySelector('.surface-fader-cap')?.setAttribute('transform',
      c.type === 'fader' ? `translate(0 ${-position})` : `translate(${position} 0)`);
  }
}
function rotateSurface(c, angle) {
  c = visualSurface(c);
  surfaceAngles.set(c.id, angle);
  $(`#midi-surface [data-surface-id="${CSS.escape(c.id)}"] [data-control-face]`)
    ?.setAttribute('transform', `rotate(${angle})`);
}
function pressSurface(c, pressed) {
  const physical = physicalSurface(c);
  surfacePressed.set(physical.id, pressed);
  $(`#midi-surface [data-surface-id="${CSS.escape(physical.id)}"]`)
    ?.classList.toggle('midi-active', pressed);
}
function restoreSurfaceVisuals() {
  for (const [id, value] of surfaceValues) {
    const c = SURFACE.find(c => c.id === id); if (c) setSurfaceValue(c, value);
  }
  for (const [id, angle] of surfaceAngles) {
    const c = SURFACE.find(c => c.id === id); if (c) rotateSurface(c, angle);
  }
  for (const [id, pressed] of surfacePressed) {
    const c = SURFACE.find(c => c.id === id); if (c) pressSurface(c, pressed);
  }
}
function visualValue(control, event) {
  const c = SURFACE.find(x => x.id === control.surfaceControlId);
  if (!c) return;
  if (c.action === 'press' || control.type === 'Note') {
    pressSurface(c, event.type === 'Note' ? !!event.pressed : control.onValue!==undefined ? event.value===control.onValue : event.value > 0);
    return;
  }
  if (c.type === 'jog' || c.type === 'jog-rim') {
    const target = visualSurface(c), prior = lastMidiValues.get(event.key);
    let angle = surfaceAngles.get(target.id) || 0;
    if (event.type === 'Pitch Bend') angle = event.value / 8192 * 180;
    else if (control.interpretation?.toLowerCase().includes('relative')) {
      angle += relativeDelta(event.value, control.interpretation) * 5;
    } else if (prior !== undefined) {
      let delta = event.value - prior;
      if (delta > 64) delta -= 128;
      if (delta < -64) delta += 128;
      angle += delta * 2.5;
    }
    rotateSurface(c, angle);
  } else {
    const min = event.type === 'Pitch Bend' ? -8192 : 0;
    const max = event.type === 'Pitch Bend' ? 8191 : 127;
    const relative = c.encoder && control.interpretation?.toLowerCase().includes('relative');
    setSurfaceValue(c, relative ? (surfaceValues.get(c.id) ?? .5) + relativeDelta(event.value, control.interpretation) / 127 : (event.value - min) / (max - min));
  }
  const node = $(`#midi-surface [data-surface-id="${CSS.escape(physicalSurface(c).id)}"]`);
  node?.classList.add('midi-active');
  if (node) {
    clearTimeout(node._activeTimer);
    node._activeTimer = setTimeout(() => node.classList.remove('midi-active'), 160);
  }
  lastMidiValues.set(event.key, event.value);
}
function applyIncoming(e) {
  const m=controlEvent(e);if(!m)return;
  const control=bindingIndex.get(m.key);
  if(control){
    visualValue(control,{...m,source:e.source});
    const c=SURFACE.find(x=>x.id===control.surfaceControlId);
    if(c?.deck){
      const shift=SURFACE.find(x=>x.id===`deck-${c.deck.toLowerCase()}-shift`);
      if(shift){clearTimeout(shift.activityTimer);pressSurface(shift,control.templateLayer==='Shift');if(control.templateLayer==='Shift')shift.activityTimer=setTimeout(()=>pressSurface(shift,false),180);}
    }
  }
  if(learning&&captureTarget){const c=learned.get(m.key);if(c&&(!control||!matchesSurface(control,captureTarget.id)))visualValue({...c,surfaceControlId:captureTarget.id,interpretation:classify(c)},{...m,source:e.source});}
}
function render(){const filtered=events.filter(e=>(!sourceFilter||e.source===sourceFilter)&&Array.isArray(e.bytes)&&e.bytes.length>0&&Number(e.bytes[0])>=0x80).slice(-300).reverse();$('#midi-events').innerHTML=filtered.length?filtered.map(e=>{const d=decode(e.bytes),date=new Date(e.time*1000);return `<div class="midi-event-row"><time>${date.toLocaleTimeString()}</time><span title="${esc(e.source)}">${esc(e.source)}</span><strong>${esc(d.name)}</strong><span>${d.channel==='—'?'—':'CH '+d.channel}</span><code>${esc(d.data)}</code></div>`;}).join(''):'<div class="empty">No MIDI messages yet. Move a control on the connected controller.</div>';$('#midi-event-count').textContent=`${receivedMIDIMessages} received`;$('#midi-event-count-inline').textContent=`${filtered.length} shown · ${receivedMIDIMessages} received`;}
let updatingMIDI=false;
async function update(){if(updatingMIDI)return;updatingMIDI=true;try{const r=await fetch('/api/midi?device='+encodeURIComponent(deviceId)+'&after='+lastSeen+'&revision='+midiRevision),d=await r.json();if(!r.ok)throw Error(d.error||'MIDI monitor unavailable');$('#midi-title').textContent=d.device.name||'MIDI Monitor';$('#midi-subtitle').textContent=[d.device.manufacturer,d.device.model,d.device.interface?.toUpperCase()].filter(Boolean).join(' · ');if(d.device.midiControls){existingControls=d.device.midiControls;removedControls=d.device.midiRemovedControls||[];midiTemplate=d.device.midiTemplate||null;renderKnown();renderTemplateStatus();}midiRevision=d.device.midiRevision??midiRevision;const status=d.status||{},select=$('#midi-source'),old=select.value,ports=status.ports||[],listening=!!status.available&&ports.length>0;$('#midi-status').textContent=listening?'Listening':status.available?'No source':'Unavailable';$('#midi-status').className='signal-badge '+(listening?'online':status.available?'waiting':'offline');$('#midi-port-status').textContent=status.message||'No CoreMIDI source is available.';if(JSON.stringify([...select.options].slice(1).map(o=>o.value))!==JSON.stringify(ports)){select.innerHTML='<option value="">All MIDI inputs</option>'+ports.map(p=>`<option value="${esc(p)}">${esc(p)}</option>`).join('');if(ports.includes(old))select.value=old;else if(ports.length===1){select.value=ports[0];sourceFilter=ports[0];}}const incoming=d.events||[];let learnChanged=false;for(const e of incoming){if(capture(e))learnChanged=true;applyIncoming(e);lastSeen=Math.max(lastSeen,e.receivedAt||0);}events.push(...incoming);receivedMIDIMessages+=incoming.length;if(events.length>500)events=events.slice(-500);if(incoming.length)render();if(!learning&&incoming.length){const latest=incoming[incoming.length-1],m=controlEvent(latest);if(m)$('#midi-current-message').innerHTML=`<span>Live MIDI · ${esc(latest.source)}</span><strong>${esc(controlLabel(m))} · CH ${m.channel} · ${m.value}</strong><small>${esc(bindingIndex.get(m.key)?.name||'Unmapped input · select a control to learn')}</small>`;}if(learning&&learnChanged)renderLearn();}catch(e){$('#midi-status').textContent='Offline';$('#midi-status').className='signal-badge offline';$('#midi-port-status').textContent=e.message;}finally{updatingMIDI=false;}}
async function saveControl(key){const c=learned.get(key);if(!c)return;const name=$(`[data-name="${CSS.escape(key)}"]`)?.value.trim();if(!name){$('#learn-state').textContent='Enter a control name';return;}try{const response=await fetch('/api/midi/controls',{method:'POST',headers:{'Content-Type':'application/json','X-SiteFlow-Token':csrf},body:JSON.stringify({deviceId,control:{name,type:c.type,channel:c.channel,number:c.number,min:c.min,max:c.max,source:c.source,interpretation:classify(c),events:c.count,surfaceControlId:c.surfaceControlId,surfaceControlName:c.surfaceControlName}})}),data=await response.json();if(!response.ok)throw Error(data.error||'Could not save MIDI control');saved.add(key);existingControls=data.controls||existingControls;$('#learn-state').textContent='MIDI identity saved';$('#learn-state').className='signal-badge waiting';setAssignmentControl(c);renderKnown();renderLearn();}catch(e){$('#learn-state').textContent=e.message;}}
async function saveAssignment(){const c=learned.get(activeLearnKey)||existingControls.find(x=>controlKey(x)===activeLearnKey);if(!c)return;const identity={type:c.type,channel:c.channel,number:c.number,source:c.source};try{const response=await fetch('/api/midi/assignment',{method:'POST',headers:{'Content-Type':'application/json','X-SiteFlow-Token':csrf},body:JSON.stringify({deviceId,control:identity,functionRole:$('#midi-function').value,target:$('#midi-target').value,mappingName:$('#midi-mapping-name').value})}),data=await response.json();if(!response.ok)throw Error(data.error||'Could not save function assignment');existingControls=existingControls.map(x=>identityMatches(x,{...identity})?data.control:x);$('#midi-assignment-status').textContent=`Saved · ${data.control.functionRole} → ${data.control.target==='global'?'Global / transport':'Art-Net routing layer'}. MIDI output routing is not enabled yet.`;renderKnown();}catch(e){$('#midi-assignment-status').textContent=e.message;}}
function svgPoint(e){const svg=$('#midi-surface'),point=svg.createSVGPoint();point.x=e.clientX;point.y=e.clientY;return point.matrixTransform(svg.getScreenCTM().inverse());}
function mousePreview(c, e) {
  const point = svgPoint(e), physical = visualSurface(c);
  if (c.action === 'press') return;
  if (c.type === 'fader') {
    setSurfaceValue(c, .5 + (c.y * .6 - point.y) / (c.travel * .6));
  } else if (c.type === 'crossfader') {
    setSurfaceValue(c, .5 + (point.x - c.x * .6) / (c.travel * .6));
  } else if (c.type === 'knob') {
    setSurfaceValue(c, mouseStart.value + (mouseStart.point.y - point.y) / 90);
  } else if (c.type === 'jog' || c.type === 'jog-rim') {
    const pointerAngle = Math.atan2(point.y - c.y * .6, point.x - c.x * .6) * 180 / Math.PI;
    let delta = pointerAngle - mouseStart.pointerAngle;
    if (delta > 180) delta -= 360;
    if (delta < -180) delta += 360;
    rotateSurface(c, (surfaceAngles.get(physical.id) || 0) + delta);
    mouseStart.pointerAngle = pointerAngle;
  }
}
function bindMouseControls() {
  const svg = $('#midi-surface');
  svg.addEventListener('pointerdown', e => {
    const node = e.target.closest('[data-surface-id]');
    if (!node) return;
    let c = SURFACE.find(x => x.id === node.dataset.surfaceId);
    if (!c) return;
    if (selectedControl && physicalSurface(selectedControl).id === c.id) c = selectedControl;
    else chooseSurface(c.id);
    const point = svgPoint(e), physical = visualSurface(c);
    mouseControl = { c, node };
    mouseStart = { point, value: surfaceValues.get(physical.id) ?? .5,
      pointerAngle: Math.atan2(point.y - c.y * .6, point.x - c.x * .6) * 180 / Math.PI };
    svg.setPointerCapture(e.pointerId);
    if (c.action === 'press') pressSurface(c, true);
    else if (c.type === 'fader' || c.type === 'crossfader') mousePreview(c, e);
    e.preventDefault();
  });
  svg.addEventListener('pointermove', e => { if (mouseControl) mousePreview(mouseControl.c, e); });
  const end = e => {
    if (mouseControl?.c.action === 'press') pressSurface(mouseControl.c, false);
    mouseControl = null; mouseStart = null;
    if (svg.hasPointerCapture?.(e.pointerId)) svg.releasePointerCapture(e.pointerId);
  };
  svg.addEventListener('pointerup', end);
  svg.addEventListener('pointercancel', end);
  $('#midi-control-action').onchange = e => chooseSurface(e.target.value);
  $('#midi-binding-layer').onchange=e=>{bindingLayer=e.target.value;if(selectedControl)chooseSurface(selectedControl.id);};
  $('#import-midi-template').onclick=importTemplate;
  $('#front-cue-select').onclick=()=>chooseSurface('front-cue-mix');
  $('#front-cue-range').oninput=e=>{chooseSurface('front-cue-mix');setSurfaceValue(SURFACE.find(c=>c.id==='front-cue-mix'),Number(e.target.value)/127);};
}
async function init(){if(q.get('embed')==='1')document.body.classList.add('midi-embedded');renderSurface();bindMouseControls();$('#skin-toggle').onclick=()=>{surfaceSkin=surfaceSkin==='papinsvet'?'factory':'papinsvet';const isPap=surfaceSkin==='papinsvet';$('#skin-toggle').textContent=`${isPap?'Papinsvet':'Factory'} Skin`;$('#skin-toggle').setAttribute('aria-pressed',String(isPap));renderSurface();};$('#midi-surface').addEventListener('click',e=>{const node=e.target.closest('[data-surface-id]');if(node&&(!selectedControl||physicalSurface(selectedControl).id!==node.dataset.surfaceId))chooseSurface(node.dataset.surfaceId);});$('#midi-surface').addEventListener('keydown',e=>{if((e.key==='Enter'||e.key===' ')&&e.target.matches('[data-surface-id]')){e.preventDefault();chooseSurface(e.target.dataset.surfaceId);}});try{const sr=await fetch('/api/state'),s=await sr.json();csrf=s.csrf;const d=s.devices.find(x=>x.id===deviceId&&(x.kind==='midi'||x.deviceClass==='midi'));if(!d)throw Error('MIDI device is not in the local inventory');$('#back').href='/';existingControls=d.midiControls||[];removedControls=d.midiRemovedControls||[];midiTemplate=d.midiTemplate||null;midiRevision=d.midiRevision??0;renderKnown();renderTemplateStatus();setAssignmentControl(null);}catch(e){$('#midi-status').textContent='Unavailable';$('#midi-status').className='signal-badge offline';$('#midi-port-status').textContent=e.message;return;}await update();timer=setInterval(()=>{if(!document.hidden)update();},100);}
$('#midi-source').onchange=e=>{sourceFilter=e.target.value;if(learning){learned.clear();discardedCandidates.clear();learnStartedAt=Date.now()/1000;$('#learn-state').textContent='Capture restarted for selected input';}render();};$('#clear-midi').onclick=()=>{events=[];receivedMIDIMessages=0;render();};$('#learn-start').onclick=()=>{if(!sourceFilter){$('#learn-state').textContent='Select the Traktor input first';return;}if(!selectedControl){$('#learn-state').textContent='Select a control on the diagram first';return;}if(selectedControl.reserved){$('#learn-state').textContent='This control is reserved for controller mode switching';return;}learned.clear();discardedCandidates.clear();activeLearnKey=null;captureTarget={...selectedControl};learning=true;learnStartedAt=Date.now()/1000;$('#learn-start').disabled=true;$('#learn-stop').disabled=false;$('#midi-control-action').disabled=true;$('#midi-binding-layer').disabled=true;$('#import-midi-template').disabled=true;$('#learn-state').textContent=`Capturing · ${captureTarget.label}`;$('#learn-state').className='signal-badge online';$('#midi-selected-control').textContent=`Learning: ${captureTarget.label} · move only this control`;$('#midi-current-message').innerHTML=`<span>Listening for ${esc(captureTarget.label)}</span><strong>Move only this control now</strong><small>Capture starts when MIDI changes</small>`;renderLearn();};$('#learn-stop').onclick=()=>{learning=false;$('#learn-start').disabled=false;$('#learn-stop').disabled=true;$('#midi-control-action').disabled=false;$('#midi-binding-layer').disabled=false;$('#import-midi-template').disabled=false;$('#learn-state').textContent=`Stopped · ${learned.size} messages`;$('#learn-state').className='signal-badge waiting';renderLearn();};$('#learn-results').addEventListener('click',e=>{
  const discard=e.target.closest('[data-discard-capture]');
  if(discard){discardCapture(discard.dataset.discardCapture);return;}
  const save=e.target.closest('[data-save]');
  if(save){saveControl(save.dataset.save);return;}
  const row=e.target.closest('[data-learn-row]');if(row)chooseLearned(row.dataset.learnRow);
});
$('#learn-results').addEventListener('input',e=>{const key=e.target.dataset.name,c=learned.get(key);if(c)c.name=e.target.value;});
$('#midi-known-controls').addEventListener('click',e=>{
  const remove=e.target.closest('[data-remove-control]');if(remove){removeControl(remove.dataset.removeControl,remove);return;}
  const restore=e.target.closest('[data-restore-control]');if(restore){restoreControl(restore.dataset.restoreControl,restore);return;}
  const select=e.target.closest('[data-known-control]');if(select)chooseLearned(select.dataset.knownControl);
});
$('#save-assignment').onclick=saveAssignment;document.addEventListener('visibilitychange',()=>{if(!document.hidden)update();});init();
