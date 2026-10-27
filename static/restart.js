'use strict';
(()=>{
  const header=document.querySelector('header.brand');
  if(!header||header.querySelector('[data-restart-node]'))return;
  const button=document.createElement('button');
  button.type='button';button.className='restart-node-button';button.dataset.restartNode='';
  button.title='Restart SiteFlow Node';button.setAttribute('aria-label','Restart SiteFlow Node');
  button.innerHTML='<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 7v5h-5M4 17v-5h5"/><path d="M5.6 9a7 7 0 0 1 11.7-2.2L20 12M4 12l2.7 5.2A7 7 0 0 0 18.4 15"/></svg><span>Restart</span>';
  const badge=header.querySelector('.local-badge');
  if(badge)header.insertBefore(button,badge);else header.append(button);
  button.addEventListener('click',async()=>{
    const message='Restart SiteFlow Node?\n\nArt-Net TX pauses during restart. Temporary fader overrides, active FX sequences and FX Activate states are cleared. Saved settings remain. TX resumes only after the controller is verified in SOFTWARE CONTROL.';
    if(!window.confirm(message))return;
    button.disabled=true;button.classList.add('restarting');button.querySelector('span').textContent='Restarting…';
    try{
      const stateResponse=await fetch('/api/state',{cache:'no-store'}),state=await stateResponse.json();
      if(!stateResponse.ok||!state.csrf)throw Error(state.error||'Could not authorize restart');
      const response=await fetch('/api/restart',{method:'POST',headers:{'Content-Type':'application/json','X-SiteFlow-Token':state.csrf},body:'{}'});
      const result=await response.json();if(!response.ok)throw Error(result.error||'Could not restart');
      const started=Date.now();
      while(Date.now()-started<45000){
        await new Promise(resolve=>setTimeout(resolve,500));
        try{const check=await fetch('/api/state',{cache:'no-store'});if(check.ok){location.reload();return;}}catch{}
      }
      button.querySelector('span').textContent='Restart pending';
      window.alert('The server has not returned yet. Keep this page open and retry once the local node responds.');
    }catch(error){button.disabled=false;button.classList.remove('restarting');button.querySelector('span').textContent='Restart';window.alert(error.message);}
  });
})();
