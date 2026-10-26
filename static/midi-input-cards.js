(()=>{
  'use strict';
  const host=document.querySelector('#midi-source-cards'),controller=new URLSearchParams(location.search).get('device');
  if(!host||!controller)return;
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let busy=false,signature='',available=[];
  async function update(){
    if(busy||document.hidden)return;busy=true;
    try{
      const response=await fetch('/api/midi-inputs?device='+encodeURIComponent(controller));
      if(!response.ok)return;
      const data=await response.json();available=data.devices||[];const devices=available.filter(d=>d.sections>0);
      const next=JSON.stringify(devices.map(d=>[d.id,d.name,d.sections]));
      if(next!==signature){
        signature=next;
        host.innerHTML=devices.map(d=>`<a class="fixture-card midi-source-card" data-midi-source="${esc(d.id)}" href="/midi-input?device=${encodeURIComponent(controller)}&source=${encodeURIComponent(d.id)}"><img src="/traktor-icon.svg" alt="MIDI controller"><div class="midi-source-content"><div class="midi-source-heading"><span class="input-mode-badge">MIDI</span><h2>${esc(d.name)}</h2><span class="signal-badge waiting" data-midi-state></span></div><p>${d.sections} ${d.sections===1?'section':'sections'} · 16 faders per section · Function remap</p><div class="midi-source-activity" data-midi-activity></div><span class="midi-source-link">Open MIDI inputs →</span></div></a>`).join('');
      }
      for(const d of devices){
        const card=[...host.querySelectorAll('[data-midi-source]')].find(c=>c.dataset.midiSource===d.id);
        const active=(d.activity||[]).filter(a=>a.age<1.5).slice(0,3),state=card.querySelector('[data-midi-state]');
        state.textContent=!d.online?'Offline':active.length?'Live MIDI':'Ready';state.className='signal-badge '+(!d.online?'offline':active.length?'waiting':'stale');
        card.querySelector('[data-midi-activity]').innerHTML=active.length?active.map(a=>`<span>${esc(a.name)} <strong>${a.value}</strong></span>`).join(''):'<span class="muted">No current MIDI activity</span>';
      }
    }catch{}finally{busy=false;}
  }
  document.querySelector('#add-midi-input').onclick=async()=>{
    await update();const picker=document.querySelector('#midi-source-picker');
    picker.innerHTML=available.map(d=>`<option value="${esc(d.id)}">${esc(d.name)}${d.online?'':' · offline'}</option>`).join('');
    document.querySelector('#midi-input-picker').hidden=false;
    document.querySelector('#create-midi-input').disabled=!available.length;
    document.querySelector('#midi-add-error').textContent=available.length?'':'No MIDI controllers found';
  };
  document.querySelector('#create-midi-input').onclick=async()=>{
    const button=document.querySelector('#create-midi-input');button.disabled=true;
    try{
      const sourceId=document.querySelector('#midi-source-picker').value;
      if(!sourceId)throw Error('Select a MIDI controller');
      if(!available.find(d=>d.id===sourceId)?.sections){
        const state=await (await fetch('/api/state')).json();
        const response=await fetch('/api/midi-inputs/section',{method:'POST',headers:{'Content-Type':'application/json','X-SiteFlow-Token':state.csrf},body:JSON.stringify({deviceId:controller,sourceId,section:{name:'MIDI section 1',enabled:true,slots:Array(16).fill(null),faderFunctions:['Master','Red','Green','Blue','White','Amber','UV','Strobe',...Array(8).fill(null)],targetFixtures:[],targetModes:{}}})});
        const data=await response.json();if(!response.ok)throw Error(data.error);
      }
      location.href='/midi-input?device='+encodeURIComponent(controller)+'&source='+encodeURIComponent(sourceId);
    }catch(e){document.querySelector('#midi-add-error').textContent=e.message;button.disabled=false;}
  };
  document.addEventListener('click',event=>{if(!event.target.closest('.input-add-picker')){document.querySelector('#input-add-menu').hidden=true;document.querySelector('#add-control').setAttribute('aria-expanded','false');}});
  update();setInterval(update,250);
})();
