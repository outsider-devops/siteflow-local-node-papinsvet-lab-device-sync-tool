'use strict';
let ledFixtures=[],ledCanvases=[],allLedCanvases=[],ledState,ledDevice,csrf,editingCanvasId=null,locatorPreview=null,editorCanvasInferred=null,activeLocatorStop=null,activeLocatorPause=null;
const funcs=['Master','Red','Green','Blue','White','Amber','UV','Strobe','Unknown'];
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function controllerIdentity(device){return String(device?.mac||device?.id||'').replace(/-/g,':').toLowerCase();}
async function post(path,body){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-SiteFlow-Token':csrf},body:JSON.stringify(body)});const d=await r.json();if(!r.ok)throw Error(d.error);return d;}
function layoutOf(f){return f.matrixLayout||{pixelsPerRow:f.count,rows:1,serpentine:false,axis:'horizontal'};}
function canvasCoords(index,c){
 if(c.shape==='irregular'&&Array.isArray(c.lines))return irregularPixelCoords(index,c);
 const w=c.width,h=c.height,snake=c.order==='serpentine',corner=c.startCorner||'bottom-left',axis=c.scanAxis||'vertical';
 if(axis==='horizontal'){
  const band=Math.floor(index/w),offset=index%w,fromRight=corner.endsWith('right'),fromTop=corner.startsWith('top');
  const rev=fromRight^(snake&&band%2===1),x=rev?w-1-offset:offset,y=fromTop?band:h-1-band;
  return {x,y};
 }
 const band=Math.floor(index/h),offset=index%h,fromRight=corner.endsWith('right'),fromTop=corner.startsWith('top');
 const rev=fromTop^(snake&&band%2===1),y=rev?offset:h-1-offset,x=fromRight?w-1-band:band;
 return {x,y};
}
function irregularPixelCoords(index,c){
 const width=Number(c.width),height=Number(c.height),axis=c.scanAxis||'vertical';let lineIndex=0,offset=index;
 for(;lineIndex<c.lines.length;lineIndex++){const line=c.lines[lineIndex],count=Math.abs(line.endPixel-line.startPixel)+1;if(offset<count)break;offset-=count;}
 if(lineIndex>=c.lines.length)return {x:0,y:0};
 const line=c.lines[lineIndex],count=Math.abs(line.endPixel-line.startPixel)+1;
 // Index zero is always the configured first address, even when wiring runs downward.
 const fraction=count<=1?0:offset/(count-1),laneX=Math.round(lineIndex*Math.max(0,width-1)/Math.max(1,c.lines.length-1)),laneY=Math.round(lineIndex*Math.max(0,height-1)/Math.max(1,c.lines.length-1));
 const point=edge=>edge==='top'?{x:laneX,y:0}:edge==='bottom'?{x:laneX,y:height-1}:edge==='left'?{x:0,y:laneY}:{x:width-1,y:laneY};
 const a=point(line.startEdge),b=point(line.endEdge);
 return {x:Math.max(0,Math.min(width-1,Math.round(a.x+(b.x-a.x)*fraction))),y:Math.max(0,Math.min(height-1,Math.round(a.y+(b.y-a.y)*fraction)))};
}
function canvasIndexForAddress(address,c){
 if(c?.shape!=='irregular'||!Array.isArray(c.lines))return Number(address)-Number(c?.startPixel||1);
 let index=0;for(const line of c.lines){const count=Math.abs(line.endPixel-line.startPixel)+1,lo=Math.min(line.startPixel,line.endPixel),hi=Math.max(line.startPixel,line.endPixel);if(address>=lo&&address<=hi)return index+(line.endPixel>=line.startPixel?address-line.startPixel:line.startPixel-address);index+=count;}return -1;
}
function canvasAddressForIndex(index,c){if(c?.shape!=='irregular'||!Array.isArray(c.lines))return Number(c?.startPixel||1)+index;let offset=index;for(const line of c.lines){const count=Math.abs(line.endPixel-line.startPixel)+1;if(offset<count)return line.endPixel>=line.startPixel?line.startPixel+offset:line.startPixel-offset;offset-=count;}return Number(c.startPixel||1)+index;}
function drawCanvasMap(canvas,c){
 if(!canvas||!c)return;const w=c.width,h=c.height;if(!Number.isInteger(w)||!Number.isInteger(h)||w<1||h<1)return;
 const ctx=canvas.getContext('2d'),pad=5,cell=Math.max(1,Math.min(12,980/Math.max(w,h))),gap=Math.max(.2,cell*.14);
 canvas.width=Math.ceil(w*cell+Math.max(0,w-1)*gap+pad*2);canvas.height=Math.ceil(h*cell+Math.max(0,h-1)*gap+pad*2);
 const cw=cell,cy=cell,size=Math.max(.6,cell-gap);
 ctx.clearRect(0,0,canvas.width,canvas.height);ctx.font='9px system-ui';ctx.textAlign='center';ctx.textBaseline='middle';
 const count=c.shape==='irregular'&&Array.isArray(c.lines)?c.lines.reduce((sum,line)=>sum+Math.abs(line.endPixel-line.startPixel)+1,0):w*h;
 const occupied=new Set();if(c.shape==='irregular'&&Array.isArray(c.lines))for(let i=0;i<count;i++){const p=canvasCoords(i,c);occupied.add(`${p.x},${p.y}`);}
 const selectedAddresses=new Set(JSON.parse(canvas.dataset.selectedAddresses||'[]').map(Number));
 for(let i=0;i<Math.min(count,1024);i++){
  const {x,y}=canvasCoords(i,c);if(c.shape==='irregular'&&!occupied.has(`${x},${y}`))continue;const px=pad+x*(cw+gap),py=pad+y*(cy+gap),selected=i===Number(canvas.dataset.selected??'-1')||selectedAddresses.has(canvasAddressForIndex(i,c));
  ctx.beginPath();ctx.roundRect(px,py,size,size,Math.min(3,size/4));ctx.fillStyle=selected?(canvas.dataset.testColor||'#FFFFFF'):(typeof c.pixelColor==='function'?c.pixelColor(i):'#414146');if(selected){ctx.shadowColor=canvas.dataset.testColor||'#FFFFFF';ctx.shadowBlur=16;}ctx.fill();ctx.shadowBlur=0;
  if(size>12){ctx.fillStyle=selected?'#17181A':'#A6A6AD';ctx.fillText(String(canvasAddressForIndex(i,c)),px+size/2,py+size/2);}
 }
 const selected=Number(canvas.dataset.selected??'-1');if(selected>=0&&selected<w*h){const point=canvasCoords(selected,c),px=pad+point.x*(cw+gap),py=pad+point.y*(cy+gap);ctx.strokeStyle='#FFFFFF';ctx.lineWidth=Math.max(1,cell*.12);ctx.shadowColor='#FFFFFF';ctx.shadowBlur=cell*.8;ctx.strokeRect(px-1,py-1,size+2,size+2);ctx.shadowBlur=0;}
}
function drawCanvasShape(canvas,c){
 if(!canvas||!c)return;
 if(c.shape!=='circle'){drawCanvasMap(canvas,c);return;}
 const count=Number(c.pixelCount||c.width||0);if(!Number.isInteger(count)||count<3)return;
 const ctx=canvas.getContext('2d'),size=240,pad=24,cx=size/2,cy=size/2,radius=93,selected=Number(canvas.dataset.selected??'-1'),selectedAddresses=new Set(JSON.parse(canvas.dataset.selectedAddresses||'[]').map(Number));
 canvas.width=size;canvas.height=size;ctx.clearRect(0,0,size,size);
 ctx.beginPath();ctx.arc(cx,cy,radius,0,Math.PI*2);ctx.strokeStyle='#414146';ctx.lineWidth=1;ctx.stroke();
 const dotRadius=Math.max(.9,Math.min(5,320/count));
 for(let i=0;i<count;i++){const angle=-Math.PI/2+i/count*Math.PI*2,x=cx+Math.cos(angle)*radius,y=cy+Math.sin(angle)*radius,active=i===selected||selectedAddresses.has(canvasAddressForIndex(i,c));ctx.beginPath();ctx.arc(x,y,dotRadius,0,Math.PI*2);ctx.fillStyle=active?(canvas.dataset.testColor||'#fff'):(typeof c.pixelColor==='function'?c.pixelColor(i):'#414146');if(active){ctx.shadowColor=canvas.dataset.testColor||'#fff';ctx.shadowBlur=15;}ctx.fill();ctx.shadowBlur=0;}
 ctx.fillStyle='#a6a6ad';ctx.font='10px system-ui';ctx.textAlign='center';ctx.textBaseline='middle';ctx.fillText(`${count} pixels`,cx,cy-5);ctx.fillText('address ring',cx,cy+9);
}
function canvasPortCapacity(port){const universes=ledDevice?.artnetOutputRanges?.[port-1];return Array.isArray(universes)?Math.min(1024,Math.floor(universes.length*512/3)):0;}
function portAddressLabel(value){const n=Number(value);if(!Number.isInteger(n)||n<0||n>32767)return '';return `${Math.floor(n/256)}:${Math.floor((n%256)/16)}:${n%16}`;}
function parsePortAddressRange(value){const parts=String(value||'').trim().replace(/[–—]/g,'-').split('-');if(parts.length!==2)throw Error('Use Net:Sub-Net:Universe–Net:Sub-Net:Universe');const parse=part=>{const m=part.trim().match(/^(\d{1,3})\s*:\s*(\d{1,2})\s*:\s*(\d{1,2})$/);if(!m)throw Error('Each endpoint must be Net:Sub-Net:Universe');const [net,subnet,universe]=m.slice(1).map(Number);if(net>127||subnet>15||universe>15)throw Error('Net 0–127, Sub-Net 0–15, Universe 0–15');return net*256+subnet*16+universe;};const start=parse(parts[0]),end=parse(parts[1]);if(end<start)throw Error('End address must be at or after start');if(end-start+1>8)throw Error('A port can have at most 8 Universes');return Array.from({length:end-start+1},(_,i)=>start+i);}
let selectedCanvasId=null;
function drawControllerCoverage(canvas){
 if(!canvas)return;const ctx=canvas.getContext('2d'),pixels=1024,w=pixels*3,rowH=15,gap=6;canvas.width=w;canvas.height=4*rowH+3*gap;ctx.clearRect(0,0,canvas.width,canvas.height);
 for(let port=1;port<=4;port++){const y=(port-1)*(rowH+gap);ctx.fillStyle='#202124';ctx.fillRect(0,y,w,rowH);ctx.strokeStyle='#414146';ctx.strokeRect(.5,y+.5,w-1,rowH-1);
  const map=ledState?.outputMap?.ports?.[port-1]||[],occupied=new Map();for(const c of ledCanvases.filter(item=>Number(item.port)===port)){const first=Math.max(1,Number(c.startPixel)||1),last=Math.min(pixels,first+(Number(c.pixelCount)||0)-1);for(let pixel=first;pixel<=last;pixel++)occupied.set(pixel-1,c);}
  for(let i=0;i<pixels;i++){const x=i*3,c=occupied.get(i),signal=map[i];ctx.fillStyle=c?(c.id===selectedCanvasId?'#a83a49':c.enabled?'#542b33':'#393a3f'):'#292a2d';ctx.fillRect(x,y+2,2,rowH-4);if(signal&&signal!=='#000000'){ctx.fillStyle=signal;ctx.fillRect(x,y+2,2,rowH-4);}}
 }
 if(locatorPreview){const x=Math.max(0,Math.min(w-1,(Number(locatorPreview.pixel)-1)*3+1)),y=(Number(locatorPreview.port)-1)*(rowH+gap);ctx.strokeStyle='#fff';ctx.lineWidth=2;ctx.shadowColor='#fff';ctx.shadowBlur=10;ctx.beginPath();ctx.moveTo(x,y-2);ctx.lineTo(x,y+rowH+2);ctx.stroke();ctx.shadowBlur=0;ctx.fillStyle='#fff';ctx.beginPath();ctx.arc(x,y+rowH/2,3,0,Math.PI*2);ctx.fill();}
}
function fixturePreviewValues(f){return ledState?.runtime?.[f.id]?.values||ledState?.effectiveValues?.[f.id]||f.mapping.map(()=>0);}
function drawSavedCanvasPreviews(root=document,changedFixtureId=null,changedValues=null){
 root.querySelectorAll('[data-canvas-card-preview]').forEach(canvas=>{const c=ledCanvases.find(item=>item.id===canvas.dataset.canvasCardPreview);if(!c)return;const pixels=ledState?.outputMap?.ports?.[Number(c.port)-1]||[],preview={...c,shape:c.shape||'rectangle'},test=ledState?.status?.canvasTest?.canvasId===c.id?ledState.status.canvasTest:null;preview.pixelColor=index=>pixels[canvasAddressForIndex(index,c)-1]||'#414146';canvas.dataset.selected=test?String(canvasIndexForAddress(test.address,c)):'-1';canvas.dataset.selectedAddresses=JSON.stringify(test?.addresses||[]);canvas.dataset.testColor=test?(test.color==='red'?'#ff2028':'#ffffff'):'';drawCanvasShape(canvas,preview);});
}
function parledPreviewColor(f,values){return normalizedRGB({...f,renderMode:'parled'},values,0);}
function updateParledPreview(card,f,values){const preview=card.querySelector('[data-parled-preview]');if(!preview)return;const color=parledPreviewColor({...f,renderMode:'parled'},values),masterIndex=f.mapping.indexOf('Master'),master=masterIndex<0?255:Number(values[masterIndex]||0),swatch=preview.querySelector('[data-parled-lens]');preview.style.setProperty('--parled-color',color);swatch?.setAttribute('fill',color);preview.querySelector('[data-parled-master]').textContent=`Master ${master}/255`;preview.querySelector('[data-parled-color-label]').textContent=color;}
function canvasTargetsMarkup(f){
 const targets=Array.isArray(f.canvasTargets)?f.canvasTargets:[];
 const rows=targets.map((target,index)=>{const c=ledCanvases.find(item=>item.id===target.id);if(!c)return '';const options=ledCanvases.filter(item=>item.id===c.id||!targets.some((other,j)=>j!==index&&other.id===item.id)).map(item=>`<option value="${esc(item.id)}" ${item.id===c.id?'selected':''}>${esc(item.name)} · P${item.port} · ${canvasDimensionsLabel(item)}</option>`).join('');return `<div class="led-canvas-target" data-canvas-target><label>Canvas<select data-target-id>${options}</select></label><button type="button" data-target-remove aria-label="Remove ${esc(c.name)}">Remove</button></div>`;}).join('');
 return `<fieldset class="led-canvas-targets"><legend>Canvas targets</legend><div data-canvas-target-list>${rows}</div><button type="button" data-target-add ${ledCanvases.every(c=>targets.some(t=>t.id===c.id))?'disabled':''}>Add existing Canvas</button>${ledCanvases.length?'':'<p class="artnet-note">Create a Canvas above before assigning it to the emulator.</p>'}</fieldset>`;
}
function readCanvasTargetRows(form){return [...form.querySelectorAll('[data-canvas-target]')].map(row=>({id:row.querySelector('[data-target-id]').value}));}
function refreshCanvasTargetRows(form,targets){
 const list=form.querySelector('[data-canvas-target-list]');if(!list)return;
 list.innerHTML=targets.map((target,index)=>{const current=ledCanvases.find(c=>c.id===target.id);if(!current)return '';const options=ledCanvases.filter(c=>c.id===current.id||!targets.some((other,j)=>j!==index&&other.id===c.id)).map(c=>`<option value="${esc(c.id)}" ${c.id===current.id?'selected':''}>${esc(c.name)} · P${c.port} · ${canvasDimensionsLabel(c)}</option>`).join('');return `<div class="led-canvas-target" data-canvas-target><label>Canvas<select data-target-id>${options}</select></label><button type="button" data-target-remove>Remove</button></div>`;}).join('');
 const add=form.querySelector('[data-target-add]');if(add)add.disabled=targets.length>=ledCanvases.length;
 list.querySelectorAll('[data-target-id]').forEach(select=>select.onchange=()=>refreshCanvasTargetRows(form,readCanvasTargetRows(form)));
 list.querySelectorAll('[data-target-remove]').forEach(button=>button.onclick=()=>refreshCanvasTargetRows(form,readCanvasTargetRows(form).filter((_,i)=>i!==[...list.querySelectorAll('[data-target-remove]')].indexOf(button))));
}
function paintLedFader(card,input){const wrap=input.closest('[data-led-fader]'),meter=wrap?.querySelector('[data-led-fader-meter]');if(!meter)return;const value=Number(input.value),height=value/255*72,y=72-height;meter.querySelector('.fader-fill').setAttribute('height',height);meter.querySelector('.fader-fill').setAttribute('y',y);meter.querySelector('.fader-cap').setAttribute('y1',y);meter.querySelector('.fader-cap').setAttribute('y2',y);wrap.querySelector('[data-fader-value]').textContent=value;wrap.classList.toggle('manual-active',value>0);}
function drawAddressLocator(canvas,address){if(!canvas)return;const ctx=canvas.getContext('2d'),width=Math.max(300,canvas.clientWidth||600),height=24;canvas.width=width;canvas.height=height;ctx.clearRect(0,0,width,height);ctx.fillStyle='#414146';ctx.fillRect(0,9,width,6);const x=Math.max(0,Math.min(width-1,Math.round((address-1)/340*(width-1))));ctx.beginPath();ctx.arc(x,height/2,4,0,Math.PI*2);ctx.fillStyle='#fff';ctx.shadowColor='#fff';ctx.shadowBlur=14;ctx.fill();ctx.shadowBlur=0;}
function canvasPortUsage(port,ignoreId=''){
 const ranges=ledCanvases.filter(c=>c.port===port&&c.id!==ignoreId).map(c=>[c.startPixel,c.startPixel+c.pixelCount-1]).sort((a,b)=>a[0]-b[0]);
 let used=ranges.reduce((n,[a,b])=>n+b-a+1,0),next=1;for(const [a,b] of ranges){if(a<=next)next=b+1;else break;}
 const capacity=canvasPortCapacity(port);return {ranges,used,next,capacity,free:Math.max(0,capacity-used)};
}
function canvasUniverseUsage(start,count,ignoreId='',port=1){
 const peers=ledCanvases.filter(c=>c.id!==ignoreId&&c.controllerId===controllerIdentity(ledDevice));
 const ranges=peers.filter(c=>c.port!==port).map(c=>[c.firstUniverse,c.firstUniverse+c.universeCount-1]);
 for(let other=1;other<=4;other++){if(other===port||peers.some(c=>c.port===other))continue;const reported=ledDevice?.artnetOutputRanges?.[other-1];if(Array.isArray(reported)&&reported.length)ranges.push([reported[0],reported.at(-1)]);}
 const samePortMismatch=peers.some(c=>c.port===port&&c.firstUniverse!==start),end=start+count-1;
 return {ranges,collision:ranges.some(([a,b])=>start<=b&&end>=a),samePortMismatch,end};
}
function nextCanvasUniverse(port){const reported=ledDevice?.artnetOutputUniverses?.[port-1];return Number.isInteger(reported)?reported:null;}
function canvasDimensionsLabel(c){if(c.shape==='single')return '1 × 1 pixel';if(c.shape==='circle')return `${c.pixelCount} pixel ring`;if(c.shape==='line')return `Line · ${c.pixelCount} pixels`;if(c.shape==='irregular')return `Irregular · ${c.width} × ${c.height} · ${c.lines?.length||0} lines`;return c.scanAxis==='vertical'?`${c.height} LEDs × ${c.width} vertical strips`:`${c.width} × ${c.height} pixels`;}
function cornerAddressMap(c){
 const result={'top-left':null,'top-right':null,'bottom-left':null,'bottom-right':null};
 for(let i=0;i<c.width*c.height;i++){const {x,y}=canvasCoords(i,c);if(x===0&&y===0)result['top-left']=c.startPixel+i;if(x===c.width-1&&y===0)result['top-right']=c.startPixel+i;if(x===0&&y===c.height-1)result['bottom-left']=c.startPixel+i;if(x===c.width-1&&y===c.height-1)result['bottom-right']=c.startPixel+i;}
 return result;
}
function inferCanvasFromCorners(corners,maxPixels=1024){
 const labels=['top-left','top-right','bottom-left','bottom-right'],addresses=labels.map(k=>Number(corners[k]));
 if(addresses.some(v=>!Number.isInteger(v)||v<1||v>maxPixels)||new Set(addresses).size!==4)return {error:`Set four different pixel addresses within this port's ${maxPixels}-pixel capacity.`};
 const startPixel=Math.min(...addresses),last=Math.max(...addresses),count=last-startPixel+1;if(count>maxPixels)return {error:`The marked pixel range exceeds this port's ${maxPixels}-pixel capacity.`};
 const matches=[];
 for(let width=2;width<=count;width++){if(count%width)continue;const height=count/width;if(height<2)continue;
  for(const scanAxis of ['vertical','horizontal'])for(const startCorner of labels){const candidate={width,height,order:'serpentine',startCorner,scanAxis,startPixel};const found={};
   for(let i=0;i<count;i++){const {x,y}=canvasCoords(i,candidate);if(x===0&&y===0)found['top-left']=startPixel+i;if(x===width-1&&y===0)found['top-right']=startPixel+i;if(x===0&&y===height-1)found['bottom-left']=startPixel+i;if(x===width-1&&y===height-1)found['bottom-right']=startPixel+i;}
   if(labels.every(k=>found[k]===Number(corners[k])))matches.push(candidate);
  }
 }
 const unique=[...new Map(matches.map(m=>[`${m.width}x${m.height}:${m.scanAxis}:${m.startCorner}`,m])).values()];
 if(unique.length===1)return {...unique[0],pixelCount:count};
 return {error:unique.length?'Corner addresses fit more than one layout. Recheck the four marks.':'These addresses do not describe one contiguous serpentine rectangle. Recheck the marked corners.'};
}
function inferCanvasFromLines(lines,axis,maxPixels=1024){
 if(!Array.isArray(lines)||lines.length<2)return {error:'Add at least two mapped lines.'};
 const edges=['top','left','right','bottom'],ranges=[],counts=[];
 for(const [index,line] of lines.entries()){
  if(!Number.isInteger(line.startPixel)||!Number.isInteger(line.endPixel))return {error:`Set both address points for line ${index+1}.`};
  if(!edges.includes(line.startEdge)||!edges.includes(line.endEdge)||line.startEdge===line.endEdge)return {error:`Choose two different canvas edges for line ${index+1}.`};
  if(line.startPixel<1||line.endPixel<1||line.startPixel>maxPixels||line.endPixel>maxPixels||line.startPixel===line.endPixel)return {error:`Line ${index+1} must use two different addresses within 1–${maxPixels}.`};
  const lo=Math.min(line.startPixel,line.endPixel),hi=Math.max(line.startPixel,line.endPixel);ranges.push([lo,hi,index]);counts.push(hi-lo+1);
 }
 if(new Set(counts).size<2)return {error:'Use different pixel counts on at least two lines to define an irregular canvas.'};
 ranges.sort((a,b)=>a[0]-b[0]);let next=ranges[0][0];for(const [lo,hi] of ranges){if(lo!==next)return {error:'Mapped line ranges must be continuous and must not overlap.'};next=hi+1;}
 const startPixel=ranges[0][0],pixelCount=counts.reduce((a,b)=>a+b,0);if(pixelCount>maxPixels||next-startPixel!==pixelCount)return {error:`Mapped canvas exceeds the ${maxPixels}-pixel port limit or has address gaps.`};
 const width=axis==='vertical'?lines.length:Math.max(...counts),height=axis==='vertical'?Math.max(...counts):lines.length;
 return {shape:'irregular',width,height,pixelCount,startPixel,scanAxis:axis,order:'mapped',startCorner:'top-left',lines:lines.map(line=>({...line}))};
}
function inferCanvasFromEdgeMarks(corners,edgePoints,axis,maxPixels=1024){
 const labels=['top-left','top-right','bottom-left','bottom-right'],cornerAddresses=labels.map(key=>Number(corners[key]));
 if(cornerAddresses.some(value=>!Number.isInteger(value)||value<1||value>maxPixels)||new Set(cornerAddresses).size!==4)return {error:`Set four different corner addresses within this port's ${maxPixels}-pixel capacity.`};
 const edgeForCorner=key=>key.startsWith('top-')?'top':'bottom';
 const marks=[...labels.map(key=>({address:Number(corners[key]),edge:axis==='vertical'?edgeForCorner(key):(key.endsWith('left')?'left':'right')})),...edgePoints.map(point=>({address:Number(point.address),edge:point.edge}))];
 if(marks.some(mark=>!Number.isInteger(mark.address)||mark.address<1||mark.address>maxPixels||!['top','left','right','bottom'].includes(mark.edge)))return {error:'Every edge point needs a valid pixel address and edge.'};
 if(new Set(marks.map(mark=>mark.address)).size!==marks.length)return {error:'Each corner or edge point must use a different pixel address.'};
 if(edgePoints.length===0){
  const rectangle=inferCanvasFromCorners(corners,maxPixels);
  if(!rectangle.error){
   const groups=new Map();for(let i=0;i<rectangle.pixelCount;i++){const point=canvasCoords(i,rectangle),lane=axis==='vertical'?point.x:point.y;if(!groups.has(lane))groups.set(lane,[]);groups.get(lane).push({address:rectangle.startPixel+i,...point});}
   const lines=[...groups.values()].map(group=>{const first=group[0],last=group[group.length-1],edge=point=>axis==='vertical'?(point.y===0?'top':'bottom'):(point.x===0?'left':'right');return {startPixel:first.address,endPixel:last.address,startEdge:edge(first),endEdge:edge(last)};});
   return {...rectangle,shape:'irregular',lines,pixelCount:rectangle.pixelCount,scanAxis:axis,order:'mapped'};
  }
 }
 marks.sort((a,b)=>a.address-b.address);
 if(marks.length%2)return {error:'Add edge points in pairs so each mapped line has both ends.'};
 const lines=[];for(let i=0;i<marks.length;i+=2){const first=marks[i],last=marks[i+1];if(first.edge===last.edge)return {error:`Addresses ${first.address} and ${last.address} must mark opposite edges of a line.`};lines.push({startPixel:first.address,endPixel:last.address,startEdge:first.edge,endEdge:last.edge});}
 const intervals=lines.map(line=>[line.startPixel,line.endPixel]).sort((a,b)=>a[0]-b[0]);let cursor=intervals[0][0];for(const [start,end] of intervals){if(start!==cursor)return {error:'Corner and edge-point ranges must form one continuous address span without overlap or gaps.'};cursor=end+1;}
 const pixelCount=lines.reduce((sum,line)=>sum+line.endPixel-line.startPixel+1,0),startPixel=intervals[0][0];if(pixelCount>maxPixels)return {error:`Mapped canvas exceeds the ${maxPixels}-pixel port limit.`};
 const width=axis==='vertical'?lines.length:Math.max(...lines.map(line=>line.endPixel-line.startPixel+1)),height=axis==='vertical'?Math.max(...lines.map(line=>line.endPixel-line.startPixel+1)):lines.length;
 return {shape:'irregular',width,height,pixelCount,startPixel,scanAxis:axis,order:'mapped',startCorner:'top-left',lines};
}
function edgeCrossMarkup(lineIndex,pointName,selected){
 const labels=[['top','Top Edge','edge-top'],['left','Left Edge','edge-left'],['right','Right Edge','edge-right'],['bottom','Bottom Edge','edge-bottom']];
 return `<div class="edge-cross" role="group" aria-label="${pointName} edge">${labels.map(([edge,label,cls])=>`<button type="button" class="${cls} ${selected===edge?'selected':''}" data-line-edge="${lineIndex}:${pointName}:${edge}" aria-pressed="${selected===edge}">${label}</button>`).join('')}</div>`;
}
function irregularCalibrationFromCanvas(canvas){
 const axis=canvas.scanAxis||'vertical',corners={'top-left':null,'top-right':null,'bottom-left':null,'bottom-right':null},edgePoints=[];
 for(const [index,line] of (canvas.lines||[]).entries())for(const [addressKey,edgeKey] of [['startPixel','startEdge'],['endPixel','endEdge']]){
  const address=line[addressKey],edge=line[edgeKey];let cornerKey=null;
  if(axis==='vertical'&&(index===0||index===canvas.lines.length-1)&&(edge==='top'||edge==='bottom'))cornerKey=`${edge}-${index===0?'left':'right'}`;
  if(axis==='horizontal'&&(index===0||index===canvas.lines.length-1)&&(edge==='left'||edge==='right'))cornerKey=`${index===0?'top':'bottom'}-${edge}`;
  if(cornerKey&&!corners[cornerKey])corners[cornerKey]=address;else edgePoints.push({address,edge});
 }
 return {corners,edgePoints};
}
function canvasEditorMarkup(c,isNew=false){
 const shape=c.shape||'rectangle';
 return `<form id="canvas-editor" class="canvas-editor">
  <input name="id" type="hidden" value="${esc(c.id)}"><input name="shape" type="hidden" value="${shape}"><input name="width" type="hidden"><input name="height" type="hidden"><input name="startPixel" type="hidden"><input name="scanAxis" type="hidden"><input name="order" type="hidden"><input name="startCorner" type="hidden"><input name="controllerId" type="hidden" value="${esc(c.controllerId||'')}">
  <div class="canvas-editor-head"><div><h3>${isNew?'New canvas':'Edit canvas'}</h3><p>Select the physical layout, then locate its pixel addresses.</p></div><button type="button" data-canvas-cancel>Cancel</button></div>
  <div class="canvas-calibration">
   <div class="canvas-shape-picker" role="group" aria-label="Canvas shape">
    <button type="button" data-canvas-shape="rectangle" aria-pressed="${shape==='rectangle'}"><svg viewBox="0 0 32 24" aria-hidden="true"><rect x="6" y="4" width="20" height="16" rx="1"/><circle cx="6" cy="4" r="2"/><circle cx="26" cy="4" r="2"/><circle cx="6" cy="20" r="2"/><circle cx="26" cy="20" r="2"/></svg><span>Rectangle · 4 points</span></button>
    <button type="button" data-canvas-shape="irregular" aria-pressed="${shape==='irregular'}"><svg viewBox="0 0 32 24" aria-hidden="true"><path d="M7 20V7l7 5 5-8 6 7v9M7 7h18M7 20h18"/><circle cx="7" cy="7" r="2"/><circle cx="14" cy="12" r="2"/><circle cx="19" cy="4" r="2"/><circle cx="25" cy="11" r="2"/></svg><span>Irregular · mapped lines</span></button>
    <button type="button" data-canvas-shape="single" aria-pressed="${shape==='single'}"><svg viewBox="0 0 32 24" aria-hidden="true"><rect x="11" y="4" width="10" height="16" rx="2"/><circle cx="16" cy="12" r="3"/></svg><span>Single · 1×1</span></button>
    <button type="button" data-canvas-shape="line" aria-pressed="${shape==='line'}"><svg viewBox="0 0 32 24" aria-hidden="true"><path d="M5 12h22"/><circle cx="5" cy="12" r="2.5"/><circle cx="27" cy="12" r="2.5"/></svg><span>Line · 2 points</span></button>
    <button type="button" data-canvas-shape="circle" aria-pressed="${shape==='circle'}"><svg viewBox="0 0 32 24" aria-hidden="true"><circle cx="16" cy="12" r="8" style="fill:none;stroke:#fff;stroke-width:1.5"/><circle cx="24" cy="12" r="2" style="fill:#fff;stroke:#fff"/></svg><span>Circle · 2 points</span></button>
   </div>
   <div class="canvas-quick-fields"><label>Output port<select name="port">${[1,2,3,4].map(p=>`<option value="${p}" ${c.port===p?'selected':''}>Port P${p} · ${canvasPortCapacity(p)} pixels · ${canvasPortUsage(p,c.id).free} free</option>`).join('')}</select></label><input name="firstUniverse" type="hidden" value="${ledDevice?.artnetOutputUniverses?.[c.port-1]??''}"><label>Pixel color order<select name="colorOrder">${['RGB','RBG','GRB','GBR','BRG','BGR'].map(order=>`<option value="${order}" ${(c.colorOrder||'BRG')===order?'selected':''}>${order}</option>`).join('')}</select></label></div>
   <p class="artnet-note">Art-Net Universe ranges are read from the controller. Select the physical output port.</p>
   <div class="canvas-address-locator"><div class="canvas-locator-row"><label for="canvas-point">Address locator</label><input id="canvas-point" type="range" min="1" max="1024" value="${c.startPixel||1}"><span class="locator-live"><i id="locator-live-dot"></i><output id="canvas-point-label">Pixel ${c.startPixel||1}</output></span></div><canvas class="canvas-address-track" id="canvas-address-track" aria-label="Selected address lights white"></canvas></div>
   <div class="canvas-editor-workspace"><section class="canvas-editor-left"><div class="canvas-inferred" data-canvas-inferred hidden><canvas id="canvas-map-preview" data-selected="-1" aria-label="Inferred LED canvas address map"></canvas><p class="canvas-inferred-label" data-inferred-label></p><button type="button" data-canvas-sweep-editor>Sweep Test</button></div></section>
   <section class="canvas-editor-right"><div class="canvas-corner-grid" data-corner-grid>${[['top-left','Top left'],['top-right','Top right'],['bottom-left','Bottom left'],['bottom-right','Bottom right']].map(([key,label])=>`<div class="canvas-corner-mark"><span>${label}</span><output data-corner-value="${key}">Not set</output><button type="button" data-set-corner="${key}">Set current address</button></div>`).join('')}</div>
   <div class="irregular-fields" data-irregular-fields hidden><label>Line direction<select data-irregular-axis><option value="vertical">Vertical lines · left to right</option><option value="horizontal">Horizontal lines · top to bottom</option></select></label><p class="artnet-note">Set four corners, then optionally add address points to mark line edges.</p><div class="irregular-lines" data-irregular-points></div><button type="button" data-add-irregular-point>Add edge point</button></div>
   <div class="canvas-point-grid" data-point-grid><div class="canvas-corner-mark"><span data-point-one-label>First pixel</span><output data-point-value="first">Not set</output><button type="button" data-set-point="first">Set current address</button></div><div class="canvas-corner-mark" data-second-point><span data-point-two-label>Last pixel</span><output data-point-value="last">Not set</output><button type="button" data-set-point="last">Set current address</button></div></div><p class="artnet-note" data-infer-status></p></section></div>
  </div>
  <div class="canvas-fields canvas-config-fields" data-canvas-config hidden><label>Canvas name<input name="name" maxlength="80" required value="${esc(c.name||'New canvas')}"></label><div class="canvas-range-summary" id="canvas-range-summary"></div><label class="forwarding-toggle"><input name="enabled" type="checkbox" ${c.enabled?'checked':''}> Enable canvas</label><p class="artnet-note">Color order applies only to this canvas. Universe count is calculated from the marked range; match the start Universe on the SP801E port.</p></div>
  <div class="canvas-editor-actions"><button class="primary" type="submit" data-canvas-save disabled>${isNew?'Create canvas':'Save canvas'}</button><span class="signal-error" data-canvas-error role="alert"></span></div>
 </form>`;
}
function renderCanvases(){
 const root=document.querySelector('[data-canvas-controller]');if(!root)return;
 root.innerHTML=`<section class="controller-pixel-overview"><div class="controller-overview-head"><strong>Output address map</strong><div class="output-map-actions"><span id="output-map-state">4 ports · up to 1024 RGB pixels per port</span><button type="button" id="edit-port-map">Edit port Universes</button><button type="button" id="output-address-test" title="One second white, one second red, then off">Test · white → red</button></div></div><div class="controller-coverage"><div class="controller-port-labels">${[1,2,3,4].map(p=>`<span>P${p}</span>`).join('')}</div><canvas data-controller-coverage aria-label="Live Art-Net RGB pixel values across four controller outputs, up to 1024 pixels per output" title="Click an occupied Canvas range to select it"></canvas></div><div class="controller-coverage-scale"><span>1</span><span>512</span><span>1024 RGB pixels / port</span></div><div class="controller-coverage-legend"><span><i></i>Configured Canvas · no current TX</span><span><b></b>Pixel color from last Art-Net frame</span></div></section><div class="canvas-manager"><div class="canvas-section-heading"><div><h2 id="canvas-title">Canvas</h2><p>Controller MAC ${esc(controllerIdentity(ledDevice)||'unknown')} · 4 ports · up to 1024 RGB pixels per port</p></div><button class="primary" id="add-canvas">Add canvas</button></div><div class="canvas-list">${ledCanvases.map(c=>`<article class="canvas-card ${c.enabled?'':'disabled'} ${c.id===selectedCanvasId?'selected':''}" data-canvas-card="${esc(c.id)}"><canvas class="canvas-card-preview" data-canvas-card-preview="${esc(c.id)}" aria-label="${esc(c.name)} ${esc(canvasDimensionsLabel(c))} pixel layout"></canvas><div class="canvas-card-main"><span class="canvas-port-badge">P${c.port}</span><div><strong>${esc(c.name)}</strong><p>${canvasDimensionsLabel(c)} · Pixels ${c.startPixel}–${c.startPixel+c.pixelCount-1} · U${c.firstUniverse}–${c.firstUniverse+c.universeCount-1} · ${c.colorOrder||'BRG'} · ${c.order==='serpentine'?'Serpentine':'Linear'}</p></div></div><div class="canvas-card-capacity">${canvasPortUsage(c.port).used}/${canvasPortCapacity(c.port)}<br><small>port capacity</small></div><div class="canvas-card-actions"><button data-canvas-sweep="${esc(c.id)}" title="Run the white/red pixel sweep">Sweep Test</button><button data-canvas-edit="${esc(c.id)}">Edit</button><button data-canvas-delete="${esc(c.id)}" aria-label="Delete ${esc(c.name)}">Delete</button></div></article>`).join('')||'<p class="artnet-note">No canvases yet. Add one to map pixels to a controller port.</p>'}</div><p class="canvas-routing-note"><strong>Patch safety:</strong> Canvas output is assigned by controller MAC and saved Universe range. Same-port canvases share a Universe start; pixel ranges cannot overlap.</p><div id="canvas-editor-host"></div></div>`;
 const currentRanges=ledDevice?.artnetOutputRanges;
 const defaultRanges=[[16,17,18,19,20,21,22],[23,24,25,26,27,28,29],[30,31,32,33,34,35,36],[37,38,39,40,41,42,43]];
 const shownRanges=Array.isArray(currentRanges)&&currentRanges.length===4?currentRanges:defaultRanges;
 const mapForm=document.createElement('form');mapForm.id='port-map-editor';mapForm.className='port-map-editor';mapForm.hidden=true;
 mapForm.innerHTML=`<div class="port-map-editor-head"><div><strong>Edit port Universes</strong><p>Enter each inclusive range as Net:Sub-Net:Universe.</p></div><button type="button" id="port-map-cancel">Cancel</button></div><div class="port-map-table"><div class="port-map-row port-map-header"><span>Port</span><span>Диапазон Port-Address</span><span>Номера Universe</span></div>${[1,2,3,4].map(p=>{const row=shownRanges[p-1]||defaultRanges[p-1];return `<label class="port-map-row"><strong>P${p}</strong><input name="range${p}" aria-label="P${p} Port-Address range" autocomplete="off" spellcheck="false" value="${portAddressLabel(row[0])}–${portAddressLabel(row.at(-1))}"><output data-universe-range="${p}">U${row[0]}–U${row.at(-1)}</output></label>`;}).join('')}</div><p class="artnet-note">Ranges must be ordered, non-overlapping, and contain 1–8 Universes per port. Existing canvases must fit inside their selected port range.</p><div class="port-map-actions"><span id="port-map-error" class="signal-error" role="alert"></span><button class="primary" type="submit" id="port-map-save">Save ranges</button></div>`;
 root.querySelector('.controller-overview-head').after(mapForm);
 drawControllerCoverage(root.querySelector('[data-controller-coverage]'));
 drawSavedCanvasPreviews(root);
 const stableMac=/^(?:[0-9a-f]{2}:){5}[0-9a-f]{2}$/i.test(controllerIdentity(ledDevice));root.querySelector('#add-canvas').disabled=!stableMac;if(!stableMac)root.querySelector('#add-canvas').title='A confirmed controller MAC is required to avoid mixing Canvas patches.';
 const coverage=root.querySelector('[data-controller-coverage]');coverage.onclick=event=>{const rect=coverage.getBoundingClientRect(),x=Math.floor((event.clientX-rect.left)/rect.width*1024),y=Math.floor((event.clientY-rect.top)/rect.height*78),rowOffset=y%21,port=Math.floor(y/21)+1;if(x<0||x>=1024||port<1||port>4||rowOffset>=15)return;const canvas=ledCanvases.find(c=>Number(c.port)===port&&x+1>=Number(c.startPixel)&&x+1<Number(c.startPixel)+Number(c.pixelCount));if(!canvas)return;selectedCanvasId=canvas.id;root.querySelectorAll('[data-canvas-card]').forEach(card=>card.classList.toggle('selected',card.dataset.canvasCard===canvas.id));drawControllerCoverage(coverage);root.querySelector(`[data-canvas-card="${CSS.escape(canvas.id)}"]`)?.scrollIntoView({behavior:'smooth',block:'nearest'});};
 const testButton=root.querySelector('#output-address-test');
 const testUnavailable=!ledDevice?.artnetOutputRanges?.every(row=>Array.isArray(row)&&row.length)||Boolean(ledState?.outputTestActive)||ledState?.txBlocked||ledState?.controllerOnline===false;
 testButton.disabled=Boolean(testUnavailable);testButton.title=ledState?.txBlocked?'Select SOFTWARE CONTROL and release BLACKOUT first':ledState?.controllerOnline===false?'Waiting for a fresh ArtPoll response from the controller':'One second white, one second red, then off';
 testButton.onclick=async()=>{testButton.disabled=true;try{await post('/api/led/output-test',{controllerId:controllerIdentity(ledDevice)});await update();}catch(e){toast(e.message);testButton.disabled=false;}};
 const mapButton=root.querySelector('#edit-port-map');
 mapButton.onclick=()=>{mapForm.hidden=!mapForm.hidden;};root.querySelector('#port-map-cancel').onclick=()=>{mapForm.hidden=true;};
 const refreshPortMap=()=>{const error=root.querySelector('#port-map-error');error.textContent='';let rows=[];try{for(let p=1;p<=4;p++){const row=parsePortAddressRange(mapForm.elements.namedItem(`range${p}`).value);rows.push(row);root.querySelector(`[data-universe-range="${p}"]`).textContent=`U${row[0]}–U${row.at(-1)}`;}for(let i=1;i<rows.length;i++)if(rows[i][0]<=rows[i-1].at(-1))throw Error('Port ranges must be ordered and must not overlap.');root.querySelector('#port-map-save').disabled=false;}catch(e){root.querySelector('#port-map-save').disabled=true;error.textContent=e.message;}};
 mapForm.querySelectorAll('input').forEach(input=>input.addEventListener('input',refreshPortMap));refreshPortMap();
 mapForm.onsubmit=async e=>{e.preventDefault();const saveButton=root.querySelector('#port-map-save');saveButton.disabled=true;try{const ranges=[1,2,3,4].map(p=>parsePortAddressRange(mapForm.elements.namedItem(`range${p}`).value));const d=await post('/api/led/port-map',{controllerId:controllerIdentity(ledDevice),ranges});ledDevice.artnetOutputRanges=d.artnetOutputRanges;ledDevice.artnetOutputUniverses=d.artnetOutputUniverses;ledDevice.artnetPortMappingSource=d.source;mapForm.hidden=true;renderCanvases();toast('Port Universe ranges saved for this controller.');}catch(err){root.querySelector('#port-map-error').textContent=err.message;saveButton.disabled=false;}};
 root.querySelector('#add-canvas').onclick=()=>{
  root.closest('details')?.setAttribute('open','');
  const port=[1,2,3,4].find(p=>canvasPortUsage(p).free>=4)||1,firstUniverse=nextCanvasUniverse(port),startPixel=canvasPortUsage(port).next;
  const controllerId=controllerIdentity(ledDevice),colorOrder=ledFixtures.find(x=>x.id==='led:matrix')?.order||'BRG';
  openCanvasEditor({id:`canvas:${crypto.randomUUID()}`,name:`Canvas ${ledCanvases.length+1}`,controllerId,controllerIp:ledDevice?.ip||'192.168.18.157',port,startPixel,width:5,height:40,pixelCount:200,shape:'rectangle',order:'serpentine',startCorner:'bottom-left',scanAxis:'vertical',colorOrder,enabled:true,firstUniverse,universeCount:2});
 };
 root.querySelectorAll('[data-canvas-edit]').forEach(b=>b.onclick=()=>{const c=ledCanvases.find(x=>x.id===b.dataset.canvasEdit);if(c)openCanvasEditor({...c});});
 root.querySelectorAll('[data-canvas-sweep]').forEach(button=>button.onclick=async()=>{const canvas=ledCanvases.find(item=>item.id===button.dataset.canvasSweep);if(!canvas)return;try{const active=ledState?.status?.canvasTest?.canvasId===canvas.id;if(active)await post('/api/led/canvas-test',{action:'stop',canvasId:canvas.id});else{await activeLocatorPause?.();await post('/api/led/canvas-test',{controllerId:controllerIdentity(ledDevice),canvasId:canvas.id});}await update();}catch(error){toast(error.message);}});
 root.querySelectorAll('[data-canvas-delete]').forEach(b=>b.onclick=async()=>{const c=ledCanvases.find(x=>x.id===b.dataset.canvasDelete);if(!c||!confirm(`Delete canvas “${c.name}”?`))return;try{const d=await post('/api/led/canvases',{action:'delete',id:c.id});allLedCanvases=d.canvases;ledCanvases=allLedCanvases.filter(x=>x.controllerId===controllerIdentity(ledDevice));renderCanvases();}catch(e){toast(e.message);}});
}
function openCanvasEditor(c){
 activeLocatorStop?.();
 const isNew=!ledCanvases.some(x=>x.id===c.id),host=document.querySelector('#canvas-editor-host');editingCanvasId=c.id;host.innerHTML=canvasEditorMarkup(c,isNew);
 const form=host.querySelector('form'),preview=host.querySelector('#canvas-map-preview'),slider=host.querySelector('#canvas-point'),field=name=>form.elements.namedItem(name);
 let shape=c.shape||'rectangle',axis=c.scanAxis||'vertical',irregularCalibration=shape==='irregular'?irregularCalibrationFromCanvas(c):null,corners=isNew?{'top-left':null,'top-right':null,'bottom-left':null,'bottom-right':null}:shape==='irregular'?(irregularCalibration?.corners||{'top-left':null,'top-right':null,'bottom-left':null,'bottom-right':null}):cornerAddressMap(c),points={first:shape==='circle'||shape==='line'||shape==='single'?c.startPixel:null,last:shape==='circle'||shape==='line'?c.startPixel+c.pixelCount-1:null},edgePoints=irregularCalibration?.edgePoints||[],inferred=null;
 const currentAddress=()=>Number(slider.value),pixelLimit=()=>canvasPortCapacity(Number(field('port').value));
 const setGeometry=g=>{for(const key of ['shape','width','height','startPixel','scanAxis','order','startCorner'])field(key).value=g[key]??'';};
 slider.max=String(Math.max(1,pixelLimit()));
 const locatorData=()=>({active:true,controllerId:controllerIdentity(ledDevice),port:Number(field('port').value),pixel:currentAddress(),firstUniverse:Number(field('firstUniverse').value),colorOrder:field('colorOrder').value});
 let locatorPaused=false;
 const pushLocator=(userAction=false)=>{if(locatorPaused||document.hidden||!host.isConnected||editingCanvasId!==c.id)return;const data=locatorData();if(!ledDevice?.artnetOutputRanges?.[data.port-1])return;locatorPreview={port:data.port,pixel:data.pixel};drawControllerCoverage(document.querySelector('[data-controller-coverage]'));post('/api/led/locator',{...data,userAction}).catch(err=>{const status=host.querySelector('[data-infer-status]');if(status)status.textContent=`Address locator unavailable: ${err.message}`;});};
 const pauseLocator=()=>{locatorPaused=true;locatorPreview=null;drawControllerCoverage(document.querySelector('[data-controller-coverage]'));return post('/api/led/locator',{active:false}).catch(()=>{});};
 const onVisibility=()=>{if(document.hidden)pauseLocator();};
 const stopLocator=()=>{clearInterval(locatorHeartbeat);document.removeEventListener('visibilitychange',onVisibility);if(activeLocatorStop===stopLocator){activeLocatorStop=null;activeLocatorPause=null;}return pauseLocator();};
 const locatorHeartbeat=setInterval(()=>pushLocator(false),600);
 activeLocatorStop=stopLocator;activeLocatorPause=pauseLocator;
 document.addEventListener('visibilitychange',onVisibility);
 const renderEdgePoints=()=>{const root=host.querySelector('[data-irregular-points]');root.innerHTML=edgePoints.map((point,i)=>`<article class="irregular-line"><header><strong>Edge point ${i+1}</strong><span>${Number.isInteger(point.address)?`Pixel ${point.address}`:'Set address'}</span><button type="button" data-remove-edge-point="${i}" aria-label="Remove edge point ${i+1}">×</button></header><div class="irregular-line-point"><div><strong>Boundary address</strong><output>${Number.isInteger(point.address)?`Pixel ${point.address}`:'Not set'}</output><button type="button" data-edge-address="${i}">Set from locator</button></div>${edgeCrossMarkup(i,'edge',point.edge)}</div></article>`).join('');
  root.querySelectorAll('[data-edge-address]').forEach(button=>button.onclick=()=>{edgePoints[Number(button.dataset.edgeAddress)].address=currentAddress();refresh();});
  root.querySelectorAll('[data-line-edge]').forEach(button=>button.onclick=()=>{const [i,,edge]=button.dataset.lineEdge.split(':');edgePoints[Number(i)].edge=edge;refresh();});
  root.querySelectorAll('[data-remove-edge-point]').forEach(button=>button.onclick=()=>{edgePoints.splice(Number(button.dataset.removeEdgePoint),1);refresh();});
 };
 const refresh=()=>{const addr=currentAddress(),dot=host.querySelector('#locator-live-dot');host.querySelector('#canvas-point-label').textContent=`Pixel ${addr}`;dot.classList.toggle('active',addr>0);drawAddressLocator(host.querySelector('#canvas-address-track'),addr);pushLocator(true);
  host.querySelectorAll('[data-canvas-shape]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.canvasShape===shape)));
  host.querySelector('[data-corner-grid]').hidden=shape!=='rectangle'&&shape!=='irregular';host.querySelector('[data-point-grid]').hidden=shape==='rectangle'||shape==='irregular';host.querySelector('[data-second-point]').hidden=shape==='single';host.querySelector('[data-irregular-fields]').hidden=shape!=='irregular';
  host.querySelector('[data-point-one-label]').textContent=shape==='single'?'Pixel address':shape==='line'?'Start pixel':'First pixel';host.querySelector('[data-point-two-label]').textContent=shape==='line'?'End pixel':'Last pixel';
  for(const [key,value] of Object.entries(corners))host.querySelector(`[data-corner-value="${key}"]`).textContent=value?`Pixel ${value}`:'Not set';
  for(const key of ['first','last']){const out=host.querySelector(`[data-point-value="${key}"]`);if(out)out.textContent=points[key]?`Pixel ${points[key]}`:'Not set';}
  const axisControl=host.querySelector('[data-irregular-axis]');axisControl.value=axis;
  if(shape==='rectangle')inferred=Object.values(corners).every(Boolean)?inferCanvasFromCorners(corners,pixelLimit()):null;
  else if(shape==='single')inferred=points.first?{shape:'single',width:1,height:1,pixelCount:1,startPixel:points.first,scanAxis:'horizontal',order:'linear',startCorner:'top-left'}:null;
  else if(shape==='irregular')inferred=Object.values(corners).every(value=>value!==null)?inferCanvasFromEdgeMarks(corners,edgePoints,axis,pixelLimit()):{error:'Set the four canvas corners first.'};
  else if((shape==='line'&&points.first&&points.last)||(shape==='circle'&&points.first&&points.last)){const count=points.last-points.first+1,minimum=shape==='circle'?3:1;inferred=points.last<points.first?{error:shape==='line'?'End pixel must be at or after Start pixel.':'Last address must be greater than the first around the ring.'}:count<minimum?{error:'A ring needs at least 3 consecutive pixels.'}:count>pixelLimit()?{error:`The selected range exceeds this port's ${pixelLimit()}-pixel map.`}:{shape,width:count,height:1,pixelCount:count,startPixel:points.first,scanAxis:'horizontal',order:'linear',startCorner:'top-left'};}
  editorCanvasInferred=inferred&&!inferred.error?{...inferred,id:c.id}:null;renderEdgePoints();
  const result=host.querySelector('[data-canvas-inferred]'),config=host.querySelector('[data-canvas-config]'),status=host.querySelector('[data-infer-status]'),save=host.querySelector('[data-canvas-save]');
  if(inferred&&!inferred.error){setGeometry(inferred);c={...c,...inferred};result.hidden=false;config.hidden=false;const selected=canvasIndexForAddress(addr,inferred);preview.dataset.selected=selected>=0?String(selected):'-1';drawCanvasShape(preview,inferred);const description=shape==='rectangle'?`${inferred.width} × ${inferred.height} pixels · ${inferred.scanAxis} serpentine scan`:shape==='single'?'Single pixel · 1 × 1':shape==='line'?`Line · pixels ${inferred.startPixel}–${inferred.startPixel+inferred.pixelCount-1}`:shape==='circle'?`${inferred.pixelCount}-pixel ring · sequential addresses around circumference`:`Irregular remap · ${inferred.width} × ${inferred.height} logical rectangle · ${inferred.lines.length} mapped lines · ${inferred.pixelCount} LEDs`;status.textContent=selected>=0?`Address ${addr} is highlighted in the mapped preview. Locator output: P${field('port').value} · U${field('firstUniverse').value}.`:`Address ${addr} is outside this Canvas; locator output: P${field('port').value} · U${field('firstUniverse').value}.`;host.querySelector('[data-inferred-label]').textContent=description;
   const end=inferred.startPixel+inferred.pixelCount-1,universeCount=Math.ceil(end*3/512),port=Number(field('port').value),firstUniverse=Number(field('firstUniverse').value),universe=canvasUniverseUsage(firstUniverse,universeCount,c.id,port),pixelUse=canvasPortUsage(port,c.id),pixelCollision=pixelUse.ranges.some(([a,b])=>inferred.startPixel<=b&&end>=a),reservedConflict=(ledDevice?.artnetOutputUniverses||[]).some((u,i)=>i+1!==port&&u>=firstUniverse&&u<firstUniverse+universeCount),allocated=ledDevice?.artnetOutputRanges?.[port-1],rangeExceeded=!Array.isArray(allocated)||firstUniverse+universeCount-1>allocated.at(-1),badUniverse=!Number.isInteger(firstUniverse)||firstUniverse<0||firstUniverse+universeCount>32768||universe.collision||universe.samePortMismatch||reservedConflict||rangeExceeded;
   host.querySelector('#canvas-range-summary').textContent=`P${port} · pixels ${inferred.startPixel}–${end} · U${firstUniverse}–U${firstUniverse+universeCount-1} (${universeCount} Universes)${pixelCollision?' · pixel overlap':rangeExceeded?' · exceeds this port’s reported Universe range':reservedConflict?' · overlaps another reported output Universe':universe.collision?' · Universe overlap':universe.samePortMismatch?' · port Universes must match':badUniverse?' · invalid Universe':' · available'}`;save.disabled=pixelCollision||badUniverse;
  }else{result.hidden=true;config.hidden=true;save.disabled=true;status.textContent=inferred?.error||(ledDevice?.artnetOutputRanges?.[Number(field('port').value)-1]?`Locator output: P${field('port').value} · U${field('firstUniverse').value}. Mark all required points.`:'Waiting for the controller ArtPoll Universe map.');}
 };
 host.querySelectorAll('[data-canvas-shape]').forEach(button=>button.onclick=()=>{shape=button.dataset.canvasShape;field('shape').value=shape;corners={'top-left':null,'top-right':null,'bottom-left':null,'bottom-right':null};points={first:null,last:null};edgePoints=[];refresh();});
 host.querySelectorAll('[data-set-corner]').forEach(button=>button.onclick=()=>{corners[button.dataset.setCorner]=currentAddress();refresh();});host.querySelectorAll('[data-set-point]').forEach(button=>button.onclick=()=>{points[button.dataset.setPoint]=currentAddress();refresh();});
 host.querySelector('[data-add-irregular-point]').onclick=()=>{edgePoints.push({address:null,edge:axis==='vertical'?'top':'left'});refresh();};
 host.querySelector('[data-irregular-axis]').onchange=event=>{axis=event.target.value;refresh();};
 form.querySelector('[data-canvas-cancel]').onclick=async()=>{await stopLocator();host.innerHTML='';editingCanvasId=null;};
 field('port').addEventListener('change',()=>{field('firstUniverse').value=nextCanvasUniverse(Number(field('port').value))??'';slider.max=String(Math.max(1,pixelLimit()));if(Number(slider.value)>pixelLimit())slider.value=slider.max;});
 form.querySelectorAll('input,select').forEach(el=>el.addEventListener('input',refresh));form.querySelectorAll('select').forEach(el=>el.addEventListener('change',refresh));slider.addEventListener('input',()=>{locatorPaused=false;const dot=host.querySelector('#locator-live-dot');dot.classList.remove('pulse');void dot.offsetWidth;dot.classList.add('pulse');refresh();});slider.addEventListener('change',()=>{locatorPaused=false;refresh();});
 const sweepButton=form.querySelector('[data-canvas-sweep-editor]');sweepButton.onclick=async()=>{try{const active=ledState?.status?.canvasTest?.canvasId===c.id;if(active)await post('/api/led/canvas-test',{action:'stop',canvasId:c.id});else{if(!inferred||inferred.error)return;const universeCount=Math.ceil((inferred.startPixel+inferred.pixelCount-1)*3/512),canvas={...c,...inferred,id:c.id,name:field('name').value||c.name,shape,controllerId:field('controllerId').value,controllerIp:c.controllerIp,port:Number(field('port').value),colorOrder:field('colorOrder').value,enabled:field('enabled').checked,firstUniverse:Number(field('firstUniverse').value),universeCount,pixelCount:inferred.pixelCount};await pauseLocator();await post('/api/led/canvas-test',{controllerId:controllerIdentity(ledDevice),canvas});}await update();}catch(error){form.querySelector('[data-canvas-error]').textContent=error.message;}};
 form.onsubmit=async e=>{e.preventDefault();if(!inferred||inferred.error)return;const universeCount=Math.ceil((inferred.startPixel+inferred.pixelCount-1)*3/512),canvas={...c,...inferred,id:c.id,name:field('name').value,shape,controllerId:field('controllerId').value,controllerIp:c.controllerIp,port:Number(field('port').value),colorOrder:field('colorOrder').value,enabled:field('enabled').checked,firstUniverse:Number(field('firstUniverse').value),universeCount,pixelCount:inferred.pixelCount};try{await stopLocator();const d=await post('/api/led/canvases',{action:'save',canvas});allLedCanvases=d.canvases;ledCanvases=allLedCanvases.filter(x=>x.controllerId===controllerIdentity(ledDevice));renderCanvases();toast(d.outputNote);}catch(err){form.querySelector('[data-canvas-error]').textContent=err.message;}};
 refresh();host.scrollIntoView({behavior:'smooth',block:'nearest'});
}
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
function render(){
 document.querySelector('#led-cards').innerHTML=ledFixtures.map(f=>{
  const matrix=f.id==='led:matrix',layout=layoutOf(f),controlValues=fixturePreviewValues(f);
  return `<section class="led-mode-panel led-profile-card" data-led="${esc(f.id)}"><div class="led-active-mode"><div><h3>${matrix?'PARLED DMX Emulator':esc(f.name)}</h3><p>${matrix?`${ledCanvases.length} Canvas · ${ledCanvases.reduce((sum,c)=>sum+c.pixelCount,0)} mapped pixels`: `${f.controllerIp} · P${f.port} · U${f.firstUniverse}–${f.firstUniverse+1} · ${f.count} pixels`}</p></div><button type="button" data-edit="${esc(f.id)}">Edit mode</button></div>
   <label class="forwarding-toggle"><input type="checkbox" data-enabled ${ledState.runtime[f.id]?.enabled?'checked':''}> Enable manual output</label><div class="led-control-strip">${(matrix||f.renderMode==='parled')?`<div class="parled-preview" data-parled-preview><svg viewBox="0 0 100 104" role="img" aria-label="Front view of an LED PAR light"><path d="M18 34v36q0 24 32 24t32-24V34" fill="none" stroke="#56575d" stroke-width="5"/><path d="M18 70h-8M82 70h8M43 93v7h14v-7" fill="none" stroke="#777980" stroke-width="4" stroke-linecap="round"/><circle cx="50" cy="42" r="38" fill="#101112" stroke="#686970" stroke-width="3"/><circle cx="50" cy="42" r="33" fill="#25262a" stroke="#414146" stroke-width="2"/><circle cx="50" cy="42" r="27" fill="#0d0e0f" stroke="#777980" stroke-width="2"/><g data-parled-lens fill="${parledPreviewColor(f,controlValues)}"><circle cx="50" cy="42" r="8"/><circle cx="50" cy="25" r="5.5"/><circle cx="64.7" cy="33.5" r="5.5"/><circle cx="64.7" cy="50.5" r="5.5"/><circle cx="50" cy="59" r="5.5"/><circle cx="35.3" cy="50.5" r="5.5"/><circle cx="35.3" cy="33.5" r="5.5"/></g><circle cx="50" cy="42" r="25" fill="none" stroke="#ffffff22" stroke-width="1"/></svg><div><strong data-parled-master>Master 0/255</strong><span data-parled-color-label>${parledPreviewColor(f,controlValues)}</span></div></div>`:''}
   <div class="led-virtual-faders">${f.mapping.map((role,i)=>{const value=Number(controlValues[i]||0),h=value/255*72,y=72-h;return `<div class="led-output-fader ${value?'manual-active':''}" data-led-fader><strong>F${i+1}</strong><span class="led-fader-function">${esc(role)}</span><div class="manual-fader-track led-manual-fader-track"><input class="manual-fader-slider" type="range" min="0" max="255" value="${value}" data-channel="${i}" aria-label="${esc(f.name)} F${i+1} ${esc(role)}" orient="vertical"><svg data-led-fader-meter viewBox="0 0 12 72" preserveAspectRatio="none" aria-hidden="true"><rect class="fader-track" x="1" y="0" width="10" height="72" rx="3"/><rect class="fader-fill" x="1" y="${y}" width="10" height="${h}" rx="2"/><line class="fader-cap" x1="0" x2="12" y1="${y}" y2="${y}"/></svg></div><output class="led-fader-value" data-fader-value>${value}</output></div>`;}).join('')}</div></div>
   <p class="artnet-note">Brightness cap ${f.limit}/255 · ${f.mirror?'All four ports mirrored':'Independent port'}</p><p data-led-effective></p>
   <form data-mapping hidden><h3>PARLED DMX Emulator settings</h3>${matrix?`<label class="led-map-row">Pixel rendering<select name="renderMode"><option value="screen" ${f.renderMode==='screen'?'selected':''}>Screen · Pixel canvas</option><option value="pixel-fill" ${f.renderMode==='pixel-fill'?'selected':''}>Pixel Fill · Color height</option><option value="parled" ${f.renderMode==='parled'?'selected':''}>PAR LED · Color intensity</option></select></label>${canvasTargetsMarkup(f)}<p class="artnet-note">Canvas addressing and cable order come from each saved Canvas. Choose only how the color fill runs across it.</p>`:''}<h3>Virtual DMX channel functions</h3>${f.mapping.map((role,i)=>`<label class="led-map-row">Virtual CH ${i+1}<select data-map>${funcs.map(k=>`<option ${k===role?'selected':''}>${k}</option>`).join('')}</select></label>`).join('')}<label class="led-map-row">Brightness cap<input name="limit" type="number" min="0" max="255" value="${f.limit}" required></label><p class="artnet-note">Mapping is virtual. Each output pixel is RGB; UV is rendered as a visible violet approximation.</p><button type="submit" class="primary">Save mode</button><button type="button" data-cancel>Cancel</button><p data-form-error class="signal-error" role="alert"></p></form><p data-error class="signal-error" role="alert"></p></section>`;
 }).join('');
 renderCanvases();
 const modePlaceholder=document.querySelector('[data-led-mode-placeholder]');
 document.querySelectorAll('[data-led-mode-tab]').forEach(tab=>tab.onclick=()=>{
  const equalizer=tab.dataset.ledModeTab==='equalizer';
  document.querySelectorAll('[data-led-mode-tab]').forEach(item=>item.setAttribute('aria-selected',String(item===tab)));
  document.querySelector('#led-cards').hidden=equalizer;
  modePlaceholder.hidden=!equalizer;
  modePlaceholder.innerHTML=equalizer?'<h3>Equalizer · Sound Reactive</h3><p>Setup will be designed separately.</p>':'';
 });
 for(const card of document.querySelectorAll('[data-led]')){
  const f=ledFixtures.find(x=>x.id===card.dataset.led);let busy=false,pending=false;
  const send=async()=>{if(busy){pending=true;return;}busy=true;do{pending=false;try{await post('/api/led/control',{id:f.id,values:[...card.querySelectorAll('[data-channel]')].map(e=>Number(e.value)),enabled:card.querySelector('[data-enabled]').checked});card.querySelector('[data-error]').textContent='';}catch(e){card.querySelector('[data-error]').textContent=e.message;}}while(pending);busy=false;};
  card.querySelectorAll('[data-channel]').forEach(input=>input.oninput=()=>{activeLocatorPause?.();if(!card.querySelector('[data-enabled]').checked)card.querySelector('[data-enabled]').checked=true;paintLedFader(card,input);const values=[...card.querySelectorAll('[data-channel]')].map(e=>Number(e.value));updateParledPreview(card,f,values);drawSavedCanvasPreviews(document.querySelector('[data-canvas-controller]'),f.id,values);send();});
  card.querySelector('[data-enabled]').onchange=()=>{activeLocatorPause?.();send();};
  const form=card.querySelector('[data-mapping]');
  card.querySelector('[data-edit]').onclick=()=>{form.hidden=false;};card.querySelector('[data-cancel]').onclick=()=>{form.hidden=true;};
  if(f.id==='led:matrix'){
   const add=form.querySelector('[data-target-add]');if(add)add.onclick=()=>{const current=readCanvasTargetRows(form),next=ledCanvases.find(c=>!current.some(t=>t.id===c.id));if(next)refreshCanvasTargetRows(form,[...current,{id:next.id}]);};
   refreshCanvasTargetRows(form,readCanvasTargetRows(form));
  }
  form.onsubmit=async e=>{e.preventDefault();try{const canvasTargets=f.id==='led:matrix'?readCanvasTargetRows(form):undefined;await post('/api/led/mapping',{id:f.id,mapping:[...card.querySelectorAll('[data-map]')].map(e=>e.value),limit:Number(form.querySelector('[name=limit]').value),renderMode:form.querySelector('[name=renderMode]')?.value||'parled',reverse:f.reverse,canvasTargets});await init();}catch(err){form.querySelector('[data-form-error]').textContent=err.message;}};
  const previewValues=fixturePreviewValues(f);updateParledPreview(card,f,previewValues);
 }
}
function updateCanvasSweepButtons(){const activeId=ledState?.status?.canvasTest?.canvasId,blocked=Boolean(ledState?.txBlocked||ledState?.controllerOnline===false),running=Boolean(ledState?.outputTestActive);document.querySelectorAll('[data-canvas-sweep]').forEach(button=>{const active=activeId===button.dataset.canvasSweep;button.textContent=active?'Stop':'Sweep Test';button.disabled=blocked||(running&&!active);});const editorButton=document.querySelector('[data-canvas-sweep-editor]');if(editorButton){const id=document.querySelector('#canvas-editor [name="id"]')?.value,active=Boolean(id&&id===activeId);editorButton.textContent=active?'Stop sweep':'Sweep Test';editorButton.disabled=blocked||(running&&!active);}}
async function update(){try{const r=await fetch('/api/led');ledState=await r.json();updateCanvasSweepButtons();const outputTest=document.querySelector('#output-address-test');if(outputTest){outputTest.disabled=Boolean(ledState.outputTestActive||ledState.txBlocked||ledState.controllerOnline===false)||!ledDevice?.artnetOutputRanges?.every(row=>Array.isArray(row)&&row.length);outputTest.title=ledState.txBlocked?'Select SOFTWARE CONTROL and release BLACKOUT first':ledState.controllerOnline===false?'Waiting for a fresh ArtPoll response from the controller':'One second white, one second red, then off';}const mapState=document.querySelector('#output-map-state');if(mapState)mapState.textContent=ledState.outputMap?.source?`${ledState.outputMap.source} · live frame`:'No active Art-Net pixel output';document.querySelector('#led-status').textContent=ledState.txMuted?'Blackout · TX stopped':ledState.status.error||ledState.status.message||(ledState.status.active?'Sending LED Art-Net frames':'Output disabled');drawControllerCoverage(document.querySelector('[data-controller-coverage]'));drawSavedCanvasPreviews(document.querySelector('[data-canvas-controller]'));const editorPreview=document.querySelector('#canvas-map-preview'),editorId=document.querySelector('#canvas-editor [name="id"]')?.value;if(editorPreview&&editorCanvasInferred){const test=ledState.status?.canvasTest?.canvasId===editorId?ledState.status.canvasTest:null,loc=test?null:locatorPreview;editorPreview.dataset.selected=String(test?canvasIndexForAddress(test.address,editorCanvasInferred):loc?.port===Number(document.querySelector('#canvas-editor [name="port"]')?.value)?canvasIndexForAddress(loc.pixel,editorCanvasInferred):-1);editorPreview.dataset.selectedAddresses=JSON.stringify(test?.addresses||[]);editorPreview.dataset.testColor=test?(test.color==='red'?'#ff2028':'#ffffff'):'';drawCanvasShape(editorPreview,editorCanvasInferred);}for(const card of document.querySelectorAll('[data-led]')){const f=ledFixtures.find(x=>x.id===card.dataset.led),runtime=ledState.runtime[f?.id],effective=ledState.effectiveValues[f?.id]||runtime?.values;card.querySelector('[data-led-effective]').textContent=effective?'Effective virtual values: '+effective.join(' · '):'No active manual output or input rule';if(f){const shown=runtime?.values||effective||f.mapping.map((_,i)=>i===0?255:0);card.querySelectorAll('[data-channel]').forEach((input,i)=>{if(document.activeElement!==input)input.value=Number(shown[i]||0);paintLedFader(card,input);});updateParledPreview(card,f,shown);}}}catch{document.querySelector('#led-status').textContent='Local server unavailable';}}
async function init(){try{const s=await (await fetch('/api/state')).json();csrf=s.csrf;const params=new URLSearchParams(location.search);ledDevice=s.devices.find(d=>d.id===params.get('device'));ledState=await (await fetch('/api/led')).json();allLedCanvases=(ledState.canvases||[]).map(c=>{const pixelCount=Number(c.pixelCount)||Number(c.width)*Number(c.height)||1,startPixel=Number(c.startPixel)||1,universeCount=Math.ceil((startPixel+pixelCount-1)*3/512);return {...c,pixelCount,firstUniverse:Number.isInteger(c.firstUniverse)?c.firstUniverse:16+2*((Number(c.port)||1)-1),universeCount,colorOrder:c.colorOrder||'BRG'};});ledCanvases=allLedCanvases.filter(c=>c.controllerId===controllerIdentity(ledDevice));ledFixtures=ledState.fixtures.filter(f=>!ledDevice||f.controllerId===controllerIdentity(ledDevice));if(params.get('fixture'))ledFixtures=ledFixtures.filter(f=>f.id===params.get('fixture'));document.querySelector('#led-cards').classList.toggle('single-fixture',Boolean(params.get('fixture')));document.querySelector('#led-controller').textContent=ledDevice?`${ledDevice.name} · MAC ${controllerIdentity(ledDevice)} · ${ledDevice.ip} · ${ledDevice.reachability}`:'All imported Art-Net pixel fixtures';render();await update();}catch(e){document.querySelector('#led-controller').textContent=e.message;}}
init();setInterval(update,500);
