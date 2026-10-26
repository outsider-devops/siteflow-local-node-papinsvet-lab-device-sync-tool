'use strict';
let state;
async function post(path,body){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-SiteFlow-Token':state.csrf},body:JSON.stringify(body)});const data=await r.json();if(!r.ok)throw Error(data.error||'Request failed');return data;}
function toast(message){const e=document.querySelector('#toast');e.textContent=message;e.hidden=false;setTimeout(()=>e.hidden=true,4500);}
async function loadWorkspace(){try{const r=await fetch('/api/state');if(!r.ok)throw Error('Local server unavailable');state=await r.json();const requested=new URLSearchParams(location.search).get('device');const d=state.devices.find(d=>d.kind==='artnet'&&(!requested||d.id===requested));if(!d)throw Error('Art-Net device not found in the registry');document.querySelector('#monitor-device').textContent=`${d.name} · ${d.ip} · ${d.model || 'Art-Net node'} · ${d.firmware || 'Firmware unverified'}`;document.querySelector('#monitor-host').textContent=location.host;document.querySelector('#input-controls-link').href='/input-controls?device='+encodeURIComponent(d.id);document.querySelector('#dmx-fixtures').href='/fixtures?device='+encodeURIComponent(d.id);if(d.webUrl){const u=new URL(d.webUrl);if(u.protocol==='http:'&&u.hostname===d.ip){const link=document.querySelector('#monitor-interface');link.href=u.href;link.hidden=false;}}loadLEDOverview();document.querySelector('#blackout-tx').onclick=async()=>{const button=document.querySelector('#blackout-tx'),enable=!artnetState?.txMuted;button.disabled=true;try{await post('/api/artnet/blackout',{enabled:enable});toast(enable?'BLACKOUT pressed · all TOZ Art-Net TX stopped':'BLACKOUT released · TX enabled after SOFTWARE CONTROL verification');}catch(e){toast(e.message);}finally{await updateArtNet();}};document.querySelector('#monitor-workspace').innerHTML=artnetPanel(d);await updateArtNet();}catch(e){document.querySelector('#monitor-workspace').innerHTML='<div class="empty"></div>';document.querySelector('#monitor-workspace .empty').textContent=e.message;}}
loadWorkspace();

async function loadLEDOverview(){try{const r=await fetch('/api/led'),data=await r.json();const devices=state.devices;document.querySelector('#custom-led-devices').innerHTML='<h2>Custom Art-Net fixtures · Pixel LEDs</h2><div class="fixture-cards">'+data.fixtures.map(f=>{const controller=devices.find(d=>d.ip===f.controllerIp);return `<article class="fixture-card"><h2>${artEsc(f.name)}</h2><p><code>${artEsc(f.controllerIp)}</code> · P${f.port} · U${f.firstUniverse}–${f.firstUniverse+1}</p><p class="artnet-note">${f.count} RGB groups · ${f.order} · Virtual DMX ${f.mapping.length} CH</p><a class="button" href="/led-controls?device=${encodeURIComponent(controller?.id||'')}&fixture=${encodeURIComponent(f.id)}">Control &amp; Mapping</a></article>`;}).join('')+'</div><p class="artnet-note">Beam-230: historical address 145 at .80; channel map unresolved, control not imported. USB PAR/Coral notes are in the imported inventory report.</p>';}catch{}}

let switchingMode=false;
function paintControllerMode(){
 const m=artnetState?.controllerMode,box=document.querySelector('#controller-modes');if(!box)return;
 const status=document.querySelector('#controller-mode-status'),detail=document.querySelector('#controller-mode-detail');
 status.textContent=m?`${m.connection} · ${m.mode} · TX ${m.tx}`:'UNKNOWN · TX STOP';
 for(const [id,name] of [['#mode-software','SOFTWARE CONTROL'],['#mode-bypass','DIRECT BYPASS']]){
  const b=document.querySelector(id);b.classList.toggle('primary',m?.mode===name);b.setAttribute('aria-pressed',String(m?.mode===name));
  b.disabled=switchingMode||!m||m.busy||m.connection==='OFFLINE'||m.mode===name;
  b.onclick=async()=>{switchingMode=true;paintControllerMode();try{await post('/api/artnet/mode',{mode:name});toast('Controller configuration read back: '+name);}catch(e){toast(e.message);}finally{switchingMode=false;await updateArtNet();}};
 }
 detail.textContent=m?.error|| (m?.ports?.length?m.ports.map(p=>`DMX${p.port} ${p.direction} U${p.universe}`).join(' · '):'Waiting for verified configuration.');
 const resume=document.querySelector('#blackout-tx');if(resume&&artnetState?.txMuted)resume.disabled=!m||!m.fresh||m.mode!=='SOFTWARE CONTROL'||m.busy||switchingMode;
}
