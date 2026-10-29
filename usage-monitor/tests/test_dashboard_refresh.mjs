import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import {fileURLToPath} from 'node:url';

// Synthetic controller tests: no source logs, native scanner or browser session.
const root=path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const source=fs.readFileSync(path.join(root,'assets/refresh_dashboard.js'),'utf8');
const template=fs.readFileSync(path.join(root,'assets/dashboard.html'),'utf8');
const base='http://127.0.0.1:45921/'+'A'.repeat(43)+'/';
const config={baseUrl:base,refreshUrl:base+'refresh.html'};
const selected={startDate:'2026-10-21',endDate:'2026-10-27',projects:['project beta','project alpha','project alpha'],chats:['chat-d','chat-a']};
const canonical={...selected,projects:['project alpha','project beta'],chats:['chat-a','chat-d']};
const hash='#from=2026-10-21&to=2026-10-27&project=project+alpha&project=project+beta&chat=chat-a&chat=chat-d';
const plain=value=>JSON.parse(JSON.stringify(value));
const response=data=>({ok:true,status:200,json:async()=>data});
const accepted={jobId:'job-1',status:'running',statusUrl:base+'api/jobs/job-1'};
function deferred(){let resolve;const promise=new Promise(done=>{resolve=done;});return {promise,resolve};}
function mount({url=base+'index.html',refresh=config,filters=selected,fetcher=()=>response(accepted),storage=new Map()}={}){
 let current=filters,address=new URL(url),clock=Date.now();
 const nodes=new Map(),calls=[],navigation=[],timers=[],events={};
 const document={activeElement:null,getElementById(id){if(!nodes.has(id))nodes.set(id,{hidden:false,disabled:false,textContent:'',title:'',dataset:{},attrs:{},span:{textContent:'Refresh usage'},querySelector(){return this.span;},setAttribute(key,value){this.attrs[key]=value;},removeAttribute(key){delete this.attrs[key];},focus(){document.activeElement=this;}});return nodes.get(id);}};
 const location={get protocol(){return address.protocol;},get origin(){return address.origin;},get pathname(){return address.pathname;},get href(){return address.href;},get hash(){return address.hash;},assign(value){navigation.push({kind:'assign',value});},replace(value){navigation.push({kind:'replace',value});},reload(){navigation.push({kind:'reload',value:address.href});}};
 class ClockDate extends Date{static now(){return clock;}}
 const window={document,location,history:{replaceState(_s,_t,value){address=new URL(value,address);}},codexUsageDashboard:{getFilters(){return current;}},AbortController,
  sessionStorage:{setItem(key,value){storage.set(key,value);},getItem(key){return storage.get(key)||null;},removeItem(key){storage.delete(key);}},
  fetch:async(url,options)=>{calls.push({url,options});return fetcher(url,options,calls.length);},
  setTimeout(callback,ms){if(ms===1000){queueMicrotask(callback);return -1;}return setTimeout(callback,ms);},clearTimeout(id){if(id!==-1)clearTimeout(id);},
  setInterval(callback,ms){timers.push({callback,ms});return timers.length;},clearInterval(id){timers[id-1].cleared=true;},
  addEventListener(type,callback){(events[type]??=[]).push(callback);}
 };
 const context=vm.createContext({window,URL,URLSearchParams,Date:ClockDate,Promise,console});vm.runInContext(source,context);
 const api=window.UsageMonitorRefresh.mount({config:refresh});
 return {api,window,location,node:id=>document.getElementById(id),calls,navigation,timers,events,storage,document,setFilters(value){current=value;api.selectionChanged();},advance(ms){clock+=ms;}};
}
const staticView=mount({url:'file:///C:/reports/index.html',refresh:null});
assert.equal(staticView.node('reload').disabled,true);
assert.equal(staticView.node('refresh-status').dataset.state,'unavailable');
assert.match(staticView.node('refresh-status').textContent,/Start Usage Monitor/);
assert.equal(await staticView.api.refresh(),false);
assert.equal(staticView.calls.length,0);
assert.equal(staticView.timers.length,0);

for(const refresh of [{...config,baseUrl:'https://example.test/'},{...config,refreshUrl:'http://127.0.0.1:45921/elsewhere'},{...config,baseUrl:base+'?host=example.test'},{...config,dashboardUrl:'http://127.0.0.1:45922/'},{...config,baseUrl:'http://user:pass@127.0.0.1:45921/'+'A'.repeat(43)+'/'},{...config,baseUrl:'http://localhost:45921/'+'A'.repeat(43)+'/'}]){
 const invalid=mount({refresh});assert.equal(invalid.node('reload').disabled,true);assert.equal(invalid.calls.length,0);
}
for(const url of ['https://example.test/index.html',base+'unrelated.html','http://127.0.0.1:45922/'+'A'.repeat(43)+'/index.html'])assert.equal(mount({url}).node('reload').disabled,true,'only the configured local session can POST');

const saved=mount({url:'file:///C:/reports/index.html#host=https://example.test',refresh:{...config,autoRefresh:true}});
assert.equal(saved.node('reload').disabled,false);
assert.equal(saved.calls.length,0,'a saved file never performs a cross-origin fetch');
assert.equal(await saved.api.refresh(),true);
assert.deepEqual(saved.navigation,[{kind:'assign',value:base+'refresh.html'+hash}]);
assert.equal(saved.calls.length,0);
assert.equal(await saved.api.refresh(),false,'a second activation cannot navigate again');
saved.events.pageshow[0]();
assert.equal(saved.node('reload').disabled,false,'Back/BFCache restores a usable file refresh button');
assert.equal(saved.node('reload').span.textContent,'Refresh usage');
assert.equal(await saved.api.refresh(),true,'a returned file view can open local refresh again');
assert.equal(saved.navigation.length,2);

const pending=deferred();
const active=mount({url:base+'index.html#host=https://example.test',fetcher:(_url,_options,count)=>count===1?pending.promise:response({jobId:'job-1',status:'complete'})});
assert.equal(active.calls.length,0,'ordinary page load cannot start a scan');
const run=active.api.refresh();
assert.equal(active.node('reload').disabled,true);
assert.equal(active.node('reload').attrs['aria-busy'],'true');
assert.equal(active.node('refresh-status').dataset.state,'running');
assert.equal(active.node('report').hidden,false,'the last good report remains visible while scanning');
assert.equal(await active.api.refresh(),false,'double activation is suppressed');
assert.equal(active.calls.length,1);
assert.equal(active.calls[0].url,base+'api/refresh');
assert.equal(active.calls[0].options.headers['X-Usage-Monitor'],'refresh');
assert.equal(active.calls[0].options.credentials,'omit');
assert.equal(active.calls[0].options.redirect,'error');
assert.deepEqual(JSON.parse(active.calls[0].options.body),canonical,'the applied selection supplies the request, never untrusted fragment fields');
const updatedFilters={...canonical,startDate:'2026-10-23',projects:['project beta']};
active.setFilters(updatedFilters);pending.resolve(response(accepted));
assert.equal(await run,true);
assert.equal(active.node('refresh-status').dataset.state,'complete');
assert.deepEqual(active.navigation,[{kind:'reload',value:base+'index.html#'+new URLSearchParams({from:'2026-10-23',to:'2026-10-27',project:'project beta'}).toString()+'&chat=chat-a&chat=chat-d'}],'a selection applied during the scan also survives completion');
assert.equal(active.node('report').hidden,false);
assert.equal(active.timers[0].ms,30000);
await active.timers[0].callback();
await Promise.resolve();
assert.equal(active.calls.at(-1).url,base+'api/ping');
active.events.pagehide[0]();assert.equal(active.timers[0].cleared,true);
const reloaded=mount({url:active.navigation[0].value,filters:updatedFilters,storage:active.storage});
assert.equal(reloaded.node('refresh-status').dataset.state,'complete','confirmed success is announced on the newly loaded view');
assert.equal(reloaded.storage.size,0,'a success announcement is consumed once');

let phase='failed';
const failed=mount({fetcher:(_url,_options,count)=>count%2?response(accepted):response({jobId:'job-1',status:phase,error:'DO NOT DISPLAY PRIVATE ERROR TEXT'})});
assert.equal(await failed.api.refresh(),false);
assert.equal(failed.node('reload').disabled,false);
assert.equal(failed.node('reload').span.textContent,'Try refresh again');
assert.equal(failed.node('refresh-status').dataset.state,'failed');
assert.doesNotMatch(failed.node('refresh-status').textContent,/PRIVATE/);
assert.equal(failed.document.activeElement,failed.node('refresh-status'),'failure feedback receives accessible focus');
assert.equal(failed.node('report').hidden,false);
assert.equal(failed.navigation.length,0);
phase='complete';assert.equal(await failed.api.refresh(),true,'retry can complete without changing filters');
assert.deepEqual(JSON.parse(failed.calls[2].options.body),canonical);

for(const fetcher of [()=>({ok:false,status:409,json:async()=>({})}),()=>Promise.reject(new Error('PRIVATE PATH')),()=>response({...accepted,statusUrl:'https://example.test/api/jobs/job-1'}),(_url,_options,count)=>count===1?response(accepted):response({jobId:'wrong-job',status:'complete'}),(_url,_options,count)=>count===1?response(accepted):response({jobId:'job-1',status:'failed',error:'refresh_timeout'})]){
 const problem=mount({fetcher});assert.equal(await problem.api.refresh(),false);assert.equal(problem.navigation.length,0);assert.equal(problem.node('report').hidden,false);assert.equal(problem.node('reload').disabled,false);assert.equal(problem.node('refresh-status').dataset.state,'failed');assert.doesNotMatch(problem.node('refresh-status').textContent,/PRIVATE/);
}
const timed=mount({fetcher:(_url,_options,count)=>{if(count===1){timed.advance(600001);return response(accepted);}timed.advance(600001);return response({jobId:'job-1',status:'running'});}});
assert.equal(await timed.api.refresh(),false);assert.match(timed.node('refresh-status').textContent,/timed out/);assert.equal(timed.navigation.length,0);

const invalidSelection=mount({filters:{...selected,startDate:'2026-02-30'}});
assert.equal(invalidSelection.node('reload').disabled,true);
assert.equal(await invalidSelection.api.refresh(),false);assert.equal(invalidSelection.calls.length,0);
invalidSelection.setFilters(selected);assert.equal(invalidSelection.node('reload').disabled,false);
assert.equal(invalidSelection.node('refresh-status').hidden,true);

const auto=mount({url:base+'refresh.html',refresh:{...config,autoRefresh:true},fetcher:(_url,_options,count)=>count===1?response(accepted):response({jobId:'job-1',status:'complete'})});
await new Promise(resolve=>setImmediate(resolve));
assert.equal(auto.calls.length,2);assert.deepEqual(auto.navigation,[{kind:'replace',value:base+'index.html'+hash}]);
assert.equal(mount({url:base+'index.html',refresh:{...config,autoRefresh:true}}).calls.length,0,'auto-refresh is restricted to the explicit refresh route');
assert.equal(mount({url:base+'refresh.html',refresh:config}).calls.length,0,'the route alone cannot trigger a scan');

assert.doesNotMatch(template,/id="reload"/);
assert.doesNotMatch(template,/id="refresh-status"/);
assert.match(template,/script-src 'unsafe-inline' 'self' file:/);
assert.match(template,/connect-src 'self'/);
assert.doesNotMatch(template,/Reload saved data|onclick=\(\)=>window\.location\.reload\(\)/);
assert.match(template,/\/\*__REFRESH_RUNTIME__\*\//);
console.log('Dashboard refresh tests passed: static/file/local routing, endpoint guards, applied plural filters, pending/success/failure/retry, single activation, verified completion, bounded polling, heartbeat and accessible feedback.');
