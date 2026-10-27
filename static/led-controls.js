'use strict';
let ledFixtures=[],ledState,ledDevice,csrf;
const funcs=['Master','Red','Green','Blue','White','Amber','UV','Strobe','Unknown'];
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function post(path,body){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-SiteFlow-Token':csrf},body:JSON.stringify(body)});const d=await r.json();if(!r.ok)throw Error(d.error);return d;}
function layoutOf(f){return f.matrixLayout||{pixelsPerRow:f.count,rows:1,serpentine:false,axis:'horizontal'};}
function normalizedRGB(f,values,index){
 const roles=Object.fromEntries(f.mapping.map((name,i)=>[name,Number(values[i]||0)]));
 let master=(roles.Master??255)/255*f.limit/255;
 const strobe=roles.Strobe||0;if(strobe>0&&strobe<255&&Math.floor(Date.now()/1000*(.5+9.5*strobe/254)*2)%2)master=0;
 if(f.renderMode==='parled'){const rgb=[Math.min(255,(roles.Red||0)+(roles.White||0)+(roles.Amber||0)),Math.min(255,(roles.Green||0)+(roles.White||0)+Math.round((roles.Amber||0)*96/255)),Math.min(255,(roles.Blue||0)+(roles.White||0))].map(x=>Math.round(x*master));return `rgb(${rgb.join(',')})`;}
 let cover={};const layout=layoutOf(f),mode=f.renderMode;
 if(mode==='screen'){
  const band=Math.floor(index/layout.pixelsPerRow),offset=index%layout.pixelsPerRow,reverse=layout.serpentine&&band%2===1;
  const position=reverse?layout.pixelsPerRow-1-offset:offset;
  const progress=layout.axis==='vertical'?position:layout.rows-1-band;
  const extent=layout.axis==='vertical'?layout.pixelsPerRow:layout.rows;
  for(const role of ['Red','Green','Blue','White','Amber','UV'])cover[role]=Math.max(0,Math.min(1,(roles[role]||0)*extent/255-progress));
 }else{
  const physical=f.reverse?f.count-1-index:index;
  for(const role of ['Red','Green','Blue','White','Amber','UV'])cover[role]=Math.max(0,Math.min(1,(roles[role]||0)*f.count/255-physical));
 }
 const rgb=[Math.min(1,(cover.Red||0)+(cover.White||0)+(cover.Amber||0)+.55*(cover.UV||0)),Math.min(1,(cover.Green||0)+(cover.White||0)+.376*(cover.Amber||0)),Math.min(1,(cover.Blue||0)+(cover.White||0)+(cover.UV||0))].map(x=>Math.round(x*255*master));
 return `rgb(${rgb.join(',')})`;
}
function drawScreen(card,f,values){
 const canvas=card.querySelector('[data-screen-preview]');if(!canvas)return;
 const layout=layoutOf(f),ctx=canvas.getContext('2d'),width=layout.axis==='vertical'?layout.rows:layout.pixelsPerRow,height=layout.axis==='vertical'?layout.pixelsPerRow:layout.rows;
 canvas.width=Math.max(1,width);canvas.height=Math.max(1,height);ctx.clearRect(0,0,width,height);
 for(let i=0;i<f.count;i++){
  const band=Math.floor(i/layout.pixelsPerRow),offset=i%layout.pixelsPerRow,reverse=layout.serpentine&&band%2===1,position=reverse?layout.pixelsPerRow-1-offset:offset;
  let x,y;
  if(layout.axis==='vertical'){x=band;y=height-1-position;}else{x=position;y=height-1-band;}
  if(x>=width||y<0||y>=height)continue;
  const color=normalizedRGB(f,values,i);ctx.fillStyle=color==='rgb(0,0,0)'?'#26282c':color;ctx.fillRect(x,y,1,1);ctx.strokeStyle='#101113';ctx.lineWidth=.12;ctx.strokeRect(x,y,1,1);
 }
 const rows=layout.rows,length=layout.pixelsPerRow;canvas.style.aspectRatio=`${layout.axis==='vertical'?rows:length} / ${layout.axis==='vertical'?length:rows}`;
  card.querySelector('[data-screen-caption]').textContent=`${length} pixels × ${rows} rows · ${layout.axis==='vertical'?'vertical':'horizontal'} scan · ${layout.serpentine?'serpentine':'same direction'} · ${layout.serpentine&&rows>1?'alternate rows reverse':'same direction'} · ${f.count} pixels`;
}
function render(){
 document.querySelector('#led-cards').innerHTML=ledFixtures.map(f=>{
  const matrix=f.id==='led:matrix',layout=layoutOf(f),controlValues=ledState.runtime[f.id]?.values||f.mapping.map(()=>0),activeValues=ledState.effectiveValues[f.id]||controlValues;
  return `<article class="fixture-card led-profile-card" data-led="${esc(f.id)}"><div class="fixture-heading"><div><h2>${esc(f.name)}</h2><p class="artnet-note">${f.controllerIp} · P${f.port} · U${f.firstUniverse}–${f.firstUniverse+1} · ${f.count} pixels · ${f.order}</p></div><button data-edit="${esc(f.id)}" aria-label="Edit ${esc(f.name)} profile"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="m16 3 5 5-12 12-6 1 1-6Z M14 5l5 5"/></svg></button></div>
   ${matrix?`<div class="matrix-screen-wrap"><canvas data-screen-preview aria-label="LED matrix pixel canvas"></canvas><small data-screen-caption></small></div>`:''}
   <label class="forwarding-toggle"><input type="checkbox" data-enabled ${ledState.runtime[f.id]?.enabled?'checked':''}> Enable manual output</label><div class="led-virtual-faders">${f.mapping.map((role,i)=>`<label>CH ${i+1} · ${esc(role)}<input type="range" min="0" max="255" value="${controlValues[i]||0}" data-channel="${i}" aria-label="${esc(f.name)} CH ${i+1} ${esc(role)}"><output>${controlValues[i]||0}</output></label>`).join('')}</div>
   <p class="artnet-note">${matrix?`${f.renderMode==='screen'?'Screen':'Pixel Fill'} · ${layout.pixelsPerRow} × ${layout.rows} · `:''}Brightness cap ${f.limit}/255 · ${f.mirror?'All four ports mirrored':'Independent port'}</p><p data-led-effective></p>
   <form data-mapping hidden><h3>Control preset and profile</h3>${matrix?`<label class="led-map-row">Control preset<select name="renderMode"><option value="screen" ${f.renderMode==='screen'?'selected':''}>Screen · Pixel canvas</option><option value="pixel-fill" ${f.renderMode==='pixel-fill'?'selected':''}>Pixel Fill · Color height</option><option value="parled" ${f.renderMode==='parled'?'selected':''}>PAR LED · Color intensity</option></select></label><fieldset class="matrix-layout-fields"><legend>Screen layout</legend><label>Pixels per row<input name="pixelsPerRow" type="number" min="1" max="${f.count}" value="${layout.pixelsPerRow}" required></label><label>Rows<input name="rows" type="number" min="1" max="${f.count}" value="${layout.rows}" required></label><label>Row orientation<select name="axis"><option value="vertical" ${layout.axis==='vertical'?'selected':''}>Vertical · bottom to top</option><option value="horizontal" ${layout.axis==='horizontal'?'selected':''}>Horizontal · left to right</option></select></label><label class="forwarding-toggle"><input type="checkbox" name="serpentine" ${layout.serpentine?'checked':''}> Snake wiring · reverse every other row</label><p class="artnet-note" data-layout-total></p></fieldset><p class="artnet-note">Screen mode fills upward from the bottom edge. With vertical strips, each strip runs bottom → top, then the next runs top → bottom. The canvas shows the logical pixels; it does not add video playback.</p>`:''}<h3>Virtual DMX channel functions</h3>${f.mapping.map((role,i)=>`<label class="led-map-row">Virtual CH ${i+1}<select data-map>${funcs.map(k=>`<option ${k===role?'selected':''}>${k}</option>`).join('')}</select></label>`).join('')}<label class="led-map-row">Brightness cap<input name="limit" type="number" min="0" max="255" value="${f.limit}" required></label><p class="artnet-note">Mapping is virtual. Each output pixel is RGB; UV is rendered as a visible violet approximation.</p><button type="submit" class="primary">Save profile</button><button type="button" data-cancel>Cancel</button><p data-form-error class="signal-error" role="alert"></p></form><p data-error class="signal-error" role="alert"></p></article>`;
 }).join('');
 for(const card of document.querySelectorAll('[data-led]')){
  const f=ledFixtures.find(x=>x.id===card.dataset.led);let busy=false,pending=false;
  const send=async()=>{if(busy){pending=true;return;}busy=true;do{pending=false;try{await post('/api/led/control',{id:f.id,values:[...card.querySelectorAll('[data-channel]')].map(e=>Number(e.value)),enabled:card.querySelector('[data-enabled]').checked});card.querySelector('[data-error]').textContent='';}catch(e){card.querySelector('[data-error]').textContent=e.message;}}while(pending);busy=false;};
  card.querySelectorAll('[data-channel]').forEach(input=>input.oninput=()=>{input.nextElementSibling.textContent=input.value;drawScreen(card,f,[...card.querySelectorAll('[data-channel]')].map(e=>Number(e.value)));send();});
  card.querySelector('[data-enabled]').onchange=send;
  const form=card.querySelector('[data-mapping]');
  card.querySelector('[data-edit]').onclick=()=>{form.hidden=false;};card.querySelector('[data-cancel]').onclick=()=>{form.hidden=true;};
  if(f.id==='led:matrix'){
   const updateTotal=()=>{const length=Number(form.querySelector('[name=pixelsPerRow]').value),rows=Number(form.querySelector('[name=rows]').value),valid=Number.isInteger(length)&&Number.isInteger(rows)&&length*rows===f.count;form.querySelector('[data-layout-total]').textContent=`${length*rows||0} / ${f.count} configured pixels${valid?' · valid layout':' · dimensions must multiply to '+f.count}`;form.querySelector('[type=submit]').disabled=!valid;};
   form.querySelectorAll('[name=pixelsPerRow],[name=rows]').forEach(input=>input.oninput=updateTotal);
  }
  form.onsubmit=async e=>{e.preventDefault();try{const matrixLayout=f.id==='led:matrix'?{pixelsPerRow:Number(form.querySelector('[name=pixelsPerRow]').value),rows:Number(form.querySelector('[name=rows]').value),axis:form.querySelector('[name=axis]').value,serpentine:form.querySelector('[name=serpentine]').checked}:undefined;await post('/api/led/mapping',{id:f.id,mapping:[...card.querySelectorAll('[data-map]')].map(e=>e.value),limit:Number(form.querySelector('[name=limit]').value),renderMode:form.querySelector('[name=renderMode]')?.value||'parled',reverse:f.reverse,matrixLayout});await init();}catch(err){form.querySelector('[data-form-error]').textContent=err.message;}};
  drawScreen(card,f,ledState.effectiveValues[f.id]||ledState.runtime[f.id]?.values||f.mapping.map((_,i)=>i===0?255:0));
 }
}
async function update(){try{const r=await fetch('/api/led');ledState=await r.json();document.querySelector('#led-status').textContent=ledState.txMuted?'Blackout · TX stopped':ledState.status.error||ledState.status.message||(ledState.status.active?'Sending LED Art-Net frames':'Output disabled');for(const card of document.querySelectorAll('[data-led]')){const f=ledFixtures.find(x=>x.id===card.dataset.led),values=ledState.effectiveValues[card.dataset.id];card.querySelector('[data-led-effective]').textContent=values?'Effective virtual values: '+values.join(' · '):'No active manual output or input rule';if(f)drawScreen(card,f,values||ledState.runtime[f.id]?.values||f.mapping.map((_,i)=>i===0?255:0));}}catch{document.querySelector('#led-status').textContent='Local server unavailable';}}
async function init(){try{const s=await (await fetch('/api/state')).json();csrf=s.csrf;const params=new URLSearchParams(location.search);ledDevice=s.devices.find(d=>d.id===params.get('device'));ledState=await (await fetch('/api/led')).json();ledFixtures=ledState.fixtures.filter(f=>!ledDevice||f.controllerIp===ledDevice.ip);if(params.get('fixture'))ledFixtures=ledFixtures.filter(f=>f.id===params.get('fixture'));document.querySelector('#led-cards').classList.toggle('single-fixture',Boolean(params.get('fixture')));document.querySelector('#led-controller').textContent=ledDevice?`${ledDevice.name} · ${ledDevice.ip} · ${ledDevice.reachability}`:'All imported Art-Net pixel fixtures';render();await update();}catch(e){document.querySelector('#led-controller').textContent=e.message;}}
init();setInterval(update,500);
