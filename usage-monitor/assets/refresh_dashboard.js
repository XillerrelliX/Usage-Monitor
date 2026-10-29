/* Local refresh controls. The saved file navigates; only the matching local
   service page can POST a refresh. No endpoint comes from a filter link. */
(function (global) {
 'use strict';
 const REQUEST_TIMEOUT=10000,POLL_INTERVAL=1000,POLL_LIMIT=600,HEARTBEAT_INTERVAL=30000;
 function configuration(value,location){
  if(!value||typeof value!=='object'||Array.isArray(value))return null;
  try{
   const base=new URL(value.baseUrl),refresh=new URL(value.refreshUrl),dashboard=new URL(value.dashboardUrl||base.href+'index.html');
   if(base.protocol!=='http:'||base.hostname!=='127.0.0.1'||!base.port||Number(base.port)>65535||base.username||base.password||base.search||base.hash||!/^\/[A-Za-z0-9_-]{43}\/$/.test(base.pathname))return null;
   if(base.href!==value.baseUrl||refresh.href!==base.href+'refresh.html'||dashboard.href!==base.href+'index.html')return null;
   const file=location.protocol==='file:';
   if(!file&&(location.origin!==base.origin||![base.pathname+'index.html',base.pathname+'refresh.html'].includes(location.pathname)))return null;
   return {baseUrl:base.href,refreshUrl:refresh.href,dashboardUrl:dashboard.href,file,autoRefresh:!file&&value.autoRefresh===true&&location.pathname===base.pathname+'refresh.html'};
  }catch{return null;}
 }
 function selection(value){
  if(!value||typeof value!=='object')throw new Error('selection');
  const result={startDate:value.startDate,endDate:value.endDate};
  for(const key of ['startDate','endDate'])if(typeof result[key]!=='string'||!/^\d{4}-\d{2}-\d{2}$/.test(result[key])||Number.isNaN(Date.parse(result[key]+'T00:00:00Z'))||new Date(result[key]+'T00:00:00Z').toISOString().slice(0,10)!==result[key])throw new Error('selection');
  const days=(Date.parse(result.endDate+'T00:00:00Z')-Date.parse(result.startDate+'T00:00:00Z'))/86400000+1;
  if(days<1||days>366)throw new Error('selection');
  for(const key of ['projects','chats']){if(!Array.isArray(value[key])||value[key].some(id=>typeof id!=='string'||!id||id.length>512))throw new Error('selection');result[key]=[...new Set(value[key])].sort();}
  return result;
 }
 function selectionHash(value){
  const current=selection(value),params=new URLSearchParams({from:current.startDate,to:current.endDate});
  for(const id of current.projects)params.append('project',id);
  for(const id of current.chats)params.append('chat',id);
  return '#'+params.toString();
 }
 function mount({config=null}={}){
  const document=global.document,node=id=>document.getElementById(id),button=node('reload'),status=node('refresh-status'),help=node('refresh-help');
  if(!button||!status)return null;
  const endpoint=configuration(config,global.location),label=button.querySelector('span'),storageKey=endpoint?'usage-monitor:refresh:'+endpoint.baseUrl:null;
  let busy=false,state='ready',lastJobId=null,heartbeat=null;
  const readSelection=()=>{if(node('report')?.hidden)throw new Error('selection');return selection(global.codexUsageDashboard?.getFilters?.());};
  function show(next,message){state=next;status.dataset.state=next;status.textContent=message;status.hidden=!message;}
  function selectionChanged(){
   let valid=false;try{readSelection();valid=true;}catch{}
   button.disabled=busy||!endpoint||!valid;
   button.title=!endpoint?'Start Usage Monitor to enable local refresh.':!valid?'Choose valid filters and Apply before refreshing usage.':'Scan local changes and update the applied selection';
   if(!endpoint)show('unavailable','Start Usage Monitor to enable Refresh usage.');
   else if(!valid&&!busy)show('selection','Choose valid filters and Apply before refreshing usage.');
   else if(valid&&state==='selection')show('ready','');
  }
  async function request(url,options={}){
   const controller=new global.AbortController(),timeout=global.setTimeout(()=>controller.abort(),REQUEST_TIMEOUT);
   try{
    const response=await global.fetch(url,{...options,cache:'no-store',credentials:'omit',redirect:'error',signal:controller.signal});
    if(!response.ok)throw new Error(response.status===409?'busy':'unavailable');
    const data=await response.json();
    if(!data||typeof data!=='object'||Array.isArray(data))throw new Error('response');
    return data;
   }finally{global.clearTimeout(timeout);}
  }
  function rememberSuccess(hash){try{global.sessionStorage.setItem(storageKey,JSON.stringify({hash,at:Date.now()}));}catch{}}
  function consumeSuccess(){
   if(!storageKey||endpoint.file)return;
   try{const raw=global.sessionStorage.getItem(storageKey);global.sessionStorage.removeItem(storageKey);if(!raw)return;const saved=JSON.parse(raw);if(saved.hash===selectionHash(readSelection())&&Date.now()-saved.at>=0&&Date.now()-saved.at<60000)show('complete','Usage updated. Your applied selection is preserved.');}catch{}
  }
  function navigateUpdated(){
   const hash=selectionHash(readSelection()),target=endpoint.dashboardUrl+hash;rememberSuccess(hash);
   if(global.location.href.split('#')[0]===endpoint.dashboardUrl){global.history.replaceState(null,'',target);global.location.reload();}
   else global.location.replace(target);
  }
  async function refresh(){
   if(busy||!endpoint)return false;
   let current;try{current=readSelection();}catch{selectionChanged();return false;}
   busy=true;button.disabled=true;button.setAttribute('aria-busy','true');label.textContent='Refreshing usage…';show('running',endpoint.file?'Opening local refresh…':'Scanning local changes… Your saved data stays visible.');
   if(endpoint.file){
    try{global.location.assign(endpoint.refreshUrl+selectionHash(current));return true;}
    catch{busy=false;button.removeAttribute('aria-busy');label.textContent='Try refresh again';show('failed','Local refresh could not open. Start Usage Monitor, then try again.');selectionChanged();status.focus({preventScroll:true});return false;}
   }
   try{
    const job=await request(endpoint.baseUrl+'api/refresh',{method:'POST',headers:{'Content-Type':'application/json','X-Usage-Monitor':'refresh'},body:JSON.stringify(current)});
    if(typeof job.jobId!=='string'||!/^[A-Za-z0-9_-]{1,128}$/.test(job.jobId)||job.status!=='running'||job.statusUrl!==endpoint.baseUrl+'api/jobs/'+job.jobId)throw new Error('response');
    lastJobId=job.jobId;
    const deadline=Date.now()+POLL_LIMIT*POLL_INTERVAL;
    for(let attempt=0;attempt<POLL_LIMIT&&Date.now()<deadline;attempt++){
     const result=await request(job.statusUrl);
     if(result.jobId!==lastJobId||!['running','complete','failed'].includes(result.status))throw new Error('response');
     if(result.status==='failed')throw new Error(result.error==='refresh_timeout'?'timeout':'failed');
     if(result.status==='complete'){show('complete','Usage updated. Loading your applied selection…');navigateUpdated();return true;}
     await new Promise(resolve=>global.setTimeout(resolve,POLL_INTERVAL));
    }
    throw new Error('timeout');
   }catch(error){
    const message=error.message==='busy'?'A refresh is already running. Your saved data is still displayed. Try again shortly.':error.message==='timeout'?'Refresh timed out. Your saved data is still displayed. Try again, or start Usage Monitor again.':'Refresh could not finish. Your saved data is still displayed. Try again, or start Usage Monitor again.';
    show('failed',message);status.focus({preventScroll:true});return false;
   }finally{busy=false;button.removeAttribute('aria-busy');label.textContent=state==='failed'?'Try refresh again':'Refresh usage';selectionChanged();}
  }
  button.onclick=()=>refresh();
  if(help)help.textContent=endpoint?'Refresh usage scans local changes and updates this view. Applied dates, projects and chats stay selected.':'Start Usage Monitor from its Windows launcher to enable Refresh usage in this saved view.';
  const api={refresh,selectionChanged,getState:()=>({state,busy,jobId:lastJobId}),destroy(){if(heartbeat!==null)global.clearInterval(heartbeat);}};
  global.codexUsageRefresh=api;selectionChanged();consumeSuccess();
  if(endpoint?.file)global.addEventListener('pageshow',()=>{if(busy){busy=false;button.removeAttribute('aria-busy');label.textContent='Refresh usage';show('ready','');selectionChanged();}});
  if(endpoint&&!endpoint.file){
   heartbeat=global.setInterval(()=>{request(endpoint.baseUrl+'api/ping').catch(()=>{});},HEARTBEAT_INTERVAL);
   global.addEventListener('pagehide',()=>api.destroy(),{once:true});
   if(endpoint.autoRefresh&&!button.disabled)void refresh();
  }
  return api;
 }
 global.UsageMonitorRefresh={configuration,selectionHash,mount};
})(window);
