(()=>{
  'use strict';
  const $=s=>document.querySelector(s),q=new URLSearchParams(location.search),deviceId=q.get('device'),sourceId=q.get('source');
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const defaults=['Master','Red','Green','Blue','White','Amber','UV','Strobe',...Array(8).fill(null)];
  const identity=c=>({source:c.source,type:c.type,channel:c.channel,number:c.number});
  const key=c=>c?JSON.stringify([c.source,c.type,c.channel,c.number]):'';
  let csrf='',sections=[],bindings=[],fixtures=[],presets=[],selectedId=null,editing=null,slotIndex=0,revision='',runtime={sections:{}},busy=false,source=null,lastRemoved=null,elementHeld=false;
  const current=()=>editing||sections.find(s=>s.id===selectedId);
  const bindingFor=slot=>slot&&bindings.find(b=>key(b)===key(slot.binding));
  function targetFunctions(section=current()){
    const roles=[];
    for(const fid of section?.targetFixtures||[]){
      const fixture=fixtures.find(f=>f.id===fid);if(!fixture)continue;
      const names=fixture.mappingPreset==='beam230-16ch'&&section.targetModes[fid]==='parled'?defaults:fixture.channels.map(c=>c.function);
      for(const role of names)if(role&&!['unknown','unassigned'].includes(role.toLowerCase())&&!roles.some(r=>r.toLowerCase()===role.toLowerCase()))roles.push(role);
    }
    return roles;
  }
  function showElement(binding,value=null){
    if(elementHeld||!binding)return;
    const address=binding.type==='Control Change'?'CC '+binding.number:binding.type==='Pitch Bend'?'Pitch Bend':'Note '+binding.number;
    $('#midi-last-element-name').textContent=binding.name;
    $('#midi-last-element-detail').textContent=`${binding.templateLayer||'Learned'} · CH ${binding.channel} / ${address}${value===null?'':' · '+value}`;
    $('#hold-midi-element').disabled=false;
  }
  function message(text){$('#midi-section-message').textContent=text;}
  function tabs(){
    $('#midi-section-tabs').innerHTML=sections.map(s=>`<button data-section="${esc(s.id)}" class="${s.id===selectedId?'primary':''}" ${editing?'disabled':''}>${esc(s.name)}</button>`).join('');
    $('#midi-section-tabs').querySelectorAll('[data-section]').forEach(b=>b.onclick=()=>{selectedId=b.dataset.section;slotIndex=0;render();});
    $('#new-midi-section').disabled=!!editing;
  }
  function render(){
    tabs();const section=current(),exists=!!section;
    $('#midi-section-empty').hidden=exists;$('#midi-section-controls').hidden=!exists;
    $('#edit-midi-section').hidden=!exists||!!editing;$('#midi-section-edit-actions').hidden=!editing;
    $('#midi-section-name').disabled=!editing;$('#midi-section-name').value=section?.name||'No section selected';
    $('#enable-midi-section').disabled=!editing;$('#enable-midi-section').checked=!!section?.enabled;
    $('#remove-midi-section').hidden=!editing?.id;
    for(const id of ['#midi-slot-binding','#midi-slot-function','#midi-slot-button','#midi-slot-step','#midi-function-preset','#midi-target-picker'])$(id).disabled=!editing;
    const allowed=targetFunctions(section),supports=role=>allowed.some(r=>r.toLowerCase()===role.toLowerCase());
    const matching=presets.filter(p=>p.functions.some(Boolean)&&p.functions.filter(Boolean).every(supports));
    $('#midi-function-preset').innerHTML='<option value="">Custom roles</option>'+(defaults.filter(Boolean).every(supports)?'<option value="parled">PAR LED · Master / RGBWAUV / Strobe</option>':'')+matching.map(p=>`<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('');
    if(!section){$('#midi-section-faders').innerHTML='';return;}
    $('#midi-section-faders').innerHTML=[0,8].map(offset=>`<section class="midi-section-bank"><h3>Page ${offset?'B · F9–F16':'A · F1–F8'}</h3><div class="midi-section-bank-grid">${Array.from({length:8},(_,j)=>{
      const i=offset+j,b=bindingFor(section.slots[i]);
      return `<div class="midi-remap-fader ${i===slotIndex?'selected':''}" data-midi-slot="${i}"><button class="midi-slot-pick" data-select-slot="${i}" title="Select F${i+1}">F${i+1}</button><div class="midi-remap-track"><input type="range" min="0" max="255" value="0" orient="vertical" data-slot-value="${i}" aria-label="F${i+1} manual value"><svg viewBox="0 0 12 84" preserveAspectRatio="none" aria-hidden="true"><rect class="fader-track" x="1" y="0" width="10" height="84" rx="3"/><rect class="fader-fill" x="1" y="84" width="10" height="0" rx="3"/><line class="fader-cap" x1="0" x2="12" y1="84" y2="84"/></svg></div><output data-slot-output="${i}">0</output><button class="midi-slot-label" data-select-slot="${i}" title="${esc(b?.name||'Unassigned MIDI element')}"><strong>${esc(section.faderFunctions[i]||'Unassigned')}</strong><span>${esc(b?.name||'No MIDI')}</span></button></div>`;
    }).join('')}</div></section>`).join('');
    $('#midi-section-faders').querySelectorAll('[data-select-slot]').forEach(b=>b.onclick=()=>selectSlot(Number(b.dataset.selectSlot)));
    $('#midi-section-faders').querySelectorAll('[data-slot-value]').forEach(slider=>{
      slider.oninput=()=>manual(Number(slider.dataset.slotValue),Number(slider.value));
      slider.onpointerdown=()=>slider.dataset.dragging='1';slider.onpointerup=()=>delete slider.dataset.dragging;slider.onpointercancel=()=>delete slider.dataset.dragging;
    });
    selectSlot(slotIndex);targets();paint();
  }
  function selectSlot(index){
    slotIndex=index;const section=current();if(!section)return;
    document.querySelectorAll('[data-midi-slot]').forEach(el=>el.classList.toggle('selected',Number(el.dataset.midiSlot)===index));
    $('#midi-slot-title').textContent=`F${index+1} · ${index<8?'Page A':'Page B'}`;
    const selected=key(section.slots[index]?.binding);
    $('#midi-slot-binding').innerHTML='<option value="">Unassigned MIDI element</option>'+['Common','Basic','Shift','Learned'].map(layer=>{
      const entries=bindings.filter(b=>(b.templateLayer||'Learned')===layer);
      return entries.length?`<optgroup label="${layer}">${entries.map(b=>`<option value="${esc(key(b))}" ${key(b)===selected?'selected':''}>${esc(b.name)} · CH ${b.channel} / ${b.type==='Control Change'?'CC '+b.number:b.type==='Pitch Bend'?'Pitch Bend':'Note '+b.number}</option>`).join('')}</optgroup>`:'';
    }).join('');
    $('#midi-slot-function').innerHTML='<option value="">Unassigned function</option>'+targetFunctions(section).map(f=>`<option ${section.faderFunctions[index]===f?'selected':''}>${esc(f)}</option>`).join('');
    $('#midi-slot-function').disabled=!editing||!targetFunctions(section).length;
    $('#midi-slot-button').value=section.slots[index]?.buttonMode||'momentary';$('#midi-slot-step').value=section.slots[index]?.step||2;
    const binding=bindingFor(section.slots[index]);
    $('#midi-slot-button').parentElement.hidden=!(binding?.type==='Note'||binding?.interpretation?.startsWith('Button'));
    $('#midi-slot-step').parentElement.hidden=!binding?.interpretation?.toLowerCase().includes('relative');
  }
  function targets(){
    const section=current();if(!section)return;
    $('#midi-target-picker').innerHTML='<option value="">Select fixture</option>'+fixtures.filter(f=>!section.targetFixtures.includes(f.id)).map(f=>`<option value="${esc(f.id)}">${esc(f.name)}${f.virtualWiz?' · WiZ':f.virtualLED?' · LED':''}</option>`).join('');
    $('#add-midi-target').disabled=true;
    $('#midi-target-list').innerHTML=section.targetFixtures.map(fid=>{
      const fixture=fixtures.find(f=>f.id===fid),mode=section.targetModes[fid]||'original';
      return `<div class="midi-target-row"><strong>${esc(fixture?.name||'Unavailable fixture')}</strong>${fixture?.id==='led:matrix'?`<select data-midi-target-mode="${esc(fid)}" ${editing?'':'disabled'} aria-label="LED Matrix control preset"><option value="parled" ${mode==='parled'?'selected':''}>PAR LED · Color intensity</option><option value="pixel-fill" ${mode!=='parled'?'selected':''}>Pixel Fill · Color height</option></select>`:''}${fixture?.mappingPreset==='beam230-16ch'?`<select data-midi-target-mode="${esc(fid)}" ${editing?'':'disabled'} aria-label="${esc(fixture.name)} control preset"><option value="original" ${mode!=='parled'?'selected':''}>Original</option><option value="parled" ${mode==='parled'?'selected':''}>ParLed Driver Emulation</option></select>`:''}<button data-remove-midi-target="${esc(fid)}" ${editing?'':'disabled'} aria-label="Remove ${esc(fixture?.name||'fixture')}">×</button></div>`;
    }).join('')||'<p class="artnet-note">No target fixtures selected.</p>';
    $('#midi-target-list').querySelectorAll('[data-remove-midi-target]').forEach(b=>b.onclick=()=>{editing.targetFixtures=editing.targetFixtures.filter(id=>id!==b.dataset.removeMidiTarget);delete editing.targetModes[b.dataset.removeMidiTarget];render();});
    $('#midi-target-list').querySelectorAll('[data-midi-target-mode]').forEach(s=>s.onchange=()=>{editing.targetModes[s.dataset.midiTargetMode]=s.value;render();});
  }
  function paint(){
    const section=current();if(!section)return;
    const state=runtime.sections?.[section.id];
    for(let i=0;i<16;i++){
      const cell=$(`[data-midi-slot="${i}"]`);if(!cell)continue;
      const v=state?.values[i]??0,h=v/255*84,y=84-h;
      cell.classList.toggle('midi-slot-idle',!state?.active[i]);cell.classList.toggle('manual-active',!!state?.manual[i]);
      cell.querySelector('.fader-fill').setAttribute('height',h);cell.querySelector('.fader-fill').setAttribute('y',y);
      cell.querySelector('.fader-cap').setAttribute('y1',y);cell.querySelector('.fader-cap').setAttribute('y2',y);
      cell.querySelector('[data-slot-output]').textContent=v;
      const slider=cell.querySelector('input');slider.disabled=!section.id;if(!slider.dataset.dragging)slider.value=v;
    }
  }
  const manualQueue=new Map();
  function manual(index,value){
    const section=current();if(!section?.id)return;
    const k=section.id+'/'+index;let item=manualQueue.get(k);if(!item){item={pending:null,busy:false};manualQueue.set(k,item);}item.pending=value;
    if(item.busy)return;item.busy=true;
    (async()=>{try{while(item.pending!==null){const v=item.pending;item.pending=null;const response=await fetch('/api/midi-inputs/fader',{method:'POST',headers:{'Content-Type':'application/json','X-SiteFlow-Token':csrf},body:JSON.stringify({deviceId,sourceId,sectionId:section.id,fader:index+1,value:v})});const data=await response.json();if(!response.ok)throw Error(data.error);runtime=data.runtime;paint();}}catch(e){message(e.message);}finally{item.busy=false;}})();
  }
  function begin(section){editing=section?JSON.parse(JSON.stringify(section)):{name:'MIDI section '+(sections.length+1),sourceId,enabled:true,slots:Array(16).fill(null),faderFunctions:[...defaults],targetFixtures:[],targetModes:{}};selectedId=section?.id||null;slotIndex=0;render();message('Choose a fader, select its MIDI element and fixture function, then Save section.');}
  async function save(action='save'){
    if(editing){editing.name=$('#midi-section-name').value;editing.enabled=$('#enable-midi-section').checked;}
    $('#save-midi-section').disabled=true;
    try{
      const response=await fetch('/api/midi-inputs/section',{method:'POST',headers:{'Content-Type':'application/json','X-SiteFlow-Token':csrf},body:JSON.stringify({deviceId,sourceId,action,section:editing,sectionId:editing?.id||lastRemoved})});
      const data=await response.json();if(!response.ok)throw Error(data.error||'Could not save section');
      if(action==='remove')lastRemoved=data.section.id;
      sections=data.sections;selectedId=action==='remove'?sections[0]?.id||null:data.section.id;editing=null;revision='';render();
      message(action==='remove'?'Section removed.':'Section saved · live routing follows MIDI and controller mode.');
    }catch(e){message(e.message);}finally{$('#save-midi-section').disabled=false;}
  }
  async function update(){
    if(busy||document.hidden)return;busy=true;
    try{
      const response=await fetch('/api/midi-inputs?device='+encodeURIComponent(deviceId)+'&source='+encodeURIComponent(sourceId)+'&revision='+encodeURIComponent(revision));
      const data=await response.json();if(!response.ok)throw Error(data.error||'MIDI inputs unavailable');
      runtime=data.runtime||{sections:{}};
      if(data.source){source=data.source;bindings=source.midiControls||[];fixtures=data.fixtures;presets=data.presets;sections=data.sections;revision=data.configRevision;
        $('#midi-input-title').textContent='MIDI · '+source.name;
        if(!selectedId)selectedId=sections[0]?.id||null;
        if(!editing)render();
      }
      const card=(data.devices||[]).find(d=>d.id===sourceId),active=(card?.activity||[]).filter(a=>a.age<1.5);
      const latest=card?.activity?.[0];if(latest&&latest.age<1.5)showElement(bindings.find(b=>b.name===latest.name),latest.value);
      $('#midi-input-status').textContent=`${card?.online?'MIDI connected':'MIDI offline'} · ${data.txAllowed?'TX enabled':'TX stopped · controller mode / Blackout'} · ${active.map(a=>a.name+' '+a.value).join(' · ')||'Ready'} · Last movement takes control`;
      paint();
    }catch(e){message(e.message);}finally{busy=false;}
  }
  window.addEventListener('message',event=>{
    if(event.origin!==location.origin||event.source!==$('#midi-input-surface').contentWindow||event.data?.type!=='siteflow-midi-selection')return;
    const touched=bindings.find(b=>key(b)===key(event.data.control));showElement(touched);
    if(!editing){message('Press Edit to assign a MIDI element to the selected fader.');return;}
    const binding=bindings.find(b=>key(b)===key(event.data.control));if(!binding){message('This element has no MIDI mapping. Load its template or use MIDI Learn first.');return;}
    if(key(editing.slots[slotIndex]?.binding)===key(binding))return;
    editing.slots[slotIndex]={binding:identity(binding),buttonMode:editing.slots[slotIndex]?.buttonMode||'momentary',step:editing.slots[slotIndex]?.step||2};render();
    message(`F${slotIndex+1} ← ${binding.name}. Choose its fixture function, then Save section.`);
  });
  $('#hold-midi-element').onclick=()=>{elementHeld=!elementHeld;$('#hold-midi-element').textContent=elementHeld?'Resume':'Hold';$('#hold-midi-element').setAttribute('aria-pressed',String(elementHeld));};
  $('#back-inputs').href='/input-controls?device='+encodeURIComponent(deviceId);
  $('#midi-device-link').href='/midi?device='+encodeURIComponent(sourceId);
  $('#midi-input-surface').src='/midi?device='+encodeURIComponent(sourceId)+'&embed=1';
  $('#midi-section-name').oninput=e=>{if(editing)editing.name=e.target.value;};
  $('#enable-midi-section').onchange=e=>{if(editing)editing.enabled=e.target.checked;};
  $('#new-midi-section').onclick=()=>begin();$('#edit-midi-section').onclick=()=>begin(current());
  $('#cancel-midi-section').onclick=()=>{editing=null;selectedId=sections.find(s=>s.id===selectedId)?.id||sections[0]?.id||null;render();message('Changes cancelled.');};
  $('#save-midi-section').onclick=()=>save();$('#remove-midi-section').onclick=()=>save('remove');
  $('#midi-slot-binding').onchange=e=>{if(!editing)return;const b=bindings.find(b=>key(b)===e.target.value);editing.slots[slotIndex]=b?{binding:identity(b),buttonMode:'momentary',step:2}:null;render();};
  $('#midi-slot-function').onchange=e=>{if(editing){editing.faderFunctions[slotIndex]=e.target.value||null;render();}};
  $('#midi-slot-button').onchange=e=>{if(editing?.slots[slotIndex])editing.slots[slotIndex].buttonMode=e.target.value;};
  $('#midi-slot-step').onchange=e=>{if(editing?.slots[slotIndex])editing.slots[slotIndex].step=Number(e.target.value);};
  $('#midi-function-preset').onchange=e=>{if(editing&&e.target.value){editing.faderFunctions=e.target.value==='parled'?[...defaults]:[...presets.find(p=>p.id===e.target.value).functions];render();}};
  $('#midi-target-picker').onchange=e=>$('#add-midi-target').disabled=!editing||!e.target.value;
  $('#add-midi-target').onclick=()=>{const fid=$('#midi-target-picker').value;if(editing&&fid){editing.targetFixtures.push(fid);editing.targetModes[fid]=fid==='led:matrix'?'pixel-fill':'original';render();}};
  fetch('/api/state').then(r=>r.json()).then(s=>{csrf=s.csrf;return update();}).catch(e=>message(e.message));
  setInterval(update,150);
})();
