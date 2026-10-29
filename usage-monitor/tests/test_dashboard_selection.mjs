import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import {fileURLToPath} from 'node:url';

// Isolated UI state tests use the synthetic ledger and a small DOM adapter.
// Real keyboard, rendering, layout, CSP and downloads remain browser gates.
const root=path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const source=fs.readFileSync(path.join(root,'assets/dashboard.html'),'utf8');
const begin=source.indexOf(" const node=id=>document.getElementById(id),catalog=runtime.catalog(bundle),form=node('filters');");
const script=source.slice(begin,source.indexOf('\n}\n</script>',begin));
const runtimeSource=fs.readFileSync(path.join(root,'assets/browser_data.js'),'utf8');
const refreshSource=fs.readFileSync(path.join(root,'assets/refresh_dashboard.js'),'utf8');
const fixture=JSON.parse(fs.readFileSync(path.join(root,'tests/fixtures/project-metadata.fixture.json'),'utf8'));
const {bundle,ids,projectIds:p}=fixture;
const unescape=value=>String(value).replace(/&quot;/g,'"').replace(/&#39;/g,"'").replace(/&amp;/g,'&').replace(/&lt;/g,'<').replace(/&gt;/g,'>');
function mount({hash='',initial=null,refresh=null}={}){
 const nodes=new Map(),events={},reports=[];
 let document;
 class Element{
  constructor(id=''){this.id=id;this.value='';this.checked=false;this.hidden=false;this.dataset={};this.attrs={};this.style={};this.listeners={};this.scrollTop=0;this.offsetWidth=360;this.offsetHeight=320;this.textContent='';this.inputNodes=[];this.buttons=[];this.classList={add(){},remove(){}};}
  set innerHTML(value){this._html=value;this.inputNodes=[...value.matchAll(/<input\b[^>]*>/g)].map(match=>{const item=new Element();item.value=unescape(match[0].match(/value="([^"]*)"/)?.[1]||'');item.checked=/ checked/.test(match[0]);return item;});this.buttons=[...value.matchAll(/<button\b[^>]*>/g)].map(match=>{const item=new Element();for(const [,key,val]of match[0].matchAll(/data-([\w-]+)="([^"]*)"/g))item.dataset[key]=unescape(val);return item;});}
  get innerHTML(){return this._html||'';}
  querySelector(selector){if(selector==='span')return this.span??=new Element();return null;}
  querySelectorAll(selector){return selector==='input'?this.inputNodes:selector==='button'?this.buttons:[];}
  addEventListener(type,callback){(this.listeners[type]??=[]).push(callback);}
  dispatch(type,properties={}){for(const callback of this.listeners[type]||[])callback({target:this,preventDefault(){},...properties});}
  setAttribute(key,value){this.attrs[key]=String(value);}
  removeAttribute(key){delete this.attrs[key];}
  contains(item){return item===this||this.inputNodes.includes(item);}
  getBoundingClientRect(){return {left:50,top:100,bottom:144,width:260};}
  focus(){document.activeElement=this;}
 }
 const node=id=>{if(!nodes.has(id))nodes.set(id,new Element(id));return nodes.get(id);};
 document={getElementById:node,querySelectorAll:()=>[],activeElement:null,addEventListener(type,callback){(events[type]??=[]).push(callback);}};
 node('usage-initial-selection').textContent=JSON.stringify(initial);
 const location={hash,protocol:'file:',href:'file:///C:/synthetic/dashboard.html'+hash,origin:'null',pathname:'/C:/synthetic/dashboard.html',replace(value){this.hash=value;},assign(value){this.assigned=value;},reload(){this.reloaded=true;}};
 const window={document,location,innerWidth:1440,innerHeight:1000,history:{replaceState(_state,_title,value){location.hash=value;}},addEventListener(type,callback){(events[type]??=[]).push(callback);}};
 const context=vm.createContext({document,window,bundle,payload:{initialSelection:initial},URL,URLSearchParams,Intl,Date,console,renderReport(data){reports.push(data);window.codexUsageDashboard={data};}});
 vm.runInContext(runtimeSource,context);context.runtime=context.window.CodexUsageData;
 vm.runInContext(script,context);
 vm.runInContext(refreshSource,context);window.UsageMonitorRefresh.mount({config:refresh});
 return {window,node,events,reports,api:()=>window.codexUsageDashboard,plain:value=>JSON.parse(JSON.stringify(value))};
}
const dates={startDate:'2026-10-21',endDate:'2026-10-27'};
const params=new URLSearchParams({from:dates.startDate,to:dates.endDate});
params.append('project',p.alpha);params.append('project',p.beta);params.append('chat',ids.A);params.append('chat',ids.D);
const first=mount({hash:'#'+params.toString(),initial:{...dates,projects:[p.gamma],chats:[]}});
assert.equal(first.reports.length,1);
assert.deepEqual(first.plain(first.api().getFilters()),{...dates,projects:[p.alpha,p.beta].sort(),chats:[ids.A,ids.D].sort(),project:null,chat:null});
assert.equal(first.node('filter-current').dataset.state,'applied');
assert.equal(first.node('filter-project').querySelector('span').textContent,'2 projects selected');
assert.equal(first.node('filter-chat').querySelector('span').textContent,'2 chats selected');
assert.equal(first.node('scope-selections').buttons.length,4);
assert.equal(first.node('report').hidden,false);
const reloaded=mount({hash:first.window.location.hash});
assert.deepEqual(reloaded.plain(reloaded.api().getFilters()),first.plain(first.api().getFilters()),'reload preserves exact dates and plural scopes');
const embedded=mount({initial:{...dates,projects:[p.alpha],chats:[ids.A]}});
assert.equal(embedded.api().getFilters().project,p.alpha);
assert.equal(embedded.api().getFilters().chat,ids.A);
assert.ok(embedded.window.location.hash.includes('project='));
assert.equal(embedded.api().applyFilters({...dates,projects:[p.beta,p.alpha,p.alpha],chats:[ids.D]}),true);
assert.deepEqual(embedded.plain(embedded.api().getFilters().projects),[p.alpha,p.beta].sort(),'duplicates canonicalized');
assert.equal(embedded.node('filter-from').value,dates.startDate);
embedded.node('filter-project').dispatch('click');
const before=embedded.node('project-options').inputNodes.find(item=>item.value===p.gamma);
assert.ok(before);before.checked=true;before.dispatch('change');
assert.equal(embedded.node('filter-current').dataset.state,'pending');
assert.equal(embedded.node('filter-apply').textContent,'Apply changes');
const after=embedded.node('project-options').inputNodes.find(item=>item.value===p.gamma);
assert.equal(after.checked,true);
assert.equal(embedded.plain(embedded.api().getDraftFilters().projects).includes(p.gamma),true);
assert.equal(embedded.api().getFilters().projects.includes(p.gamma),false,'dirty UI does not broaden the displayed report');
embedded.node('project-all').dispatch('click');
assert.deepEqual(embedded.plain(embedded.api().getDraftFilters().projects),[]);
assert.deepEqual(embedded.plain(embedded.api().getDraftFilters().chats),[ids.D],'All projects retains chosen chats');
const original=mount({initial:{...dates,projects:[],chats:[]}});
assert.equal(original.api().applyFilters({...dates,project:p.alpha,chat:ids.A}),true,'legacy singular API maps to plural UI');
assert.deepEqual(original.plain(original.api().getFilters().projects),[p.alpha]);
for(const hash of ['#from=2026-02-30&to=2026-10-27','#from=2026-10-27&to=2026-10-21','#from=2026-10-21&to=2026-10-27&chat=unknown','#from=2026-10-21&to=2026-10-27&project=unknown','#from=2026-10-21&from=2026-10-22&to=2026-10-27','#from=2026-10-21&to=2026-10-27&chat=','#from=2026-10-21&to=2026-10-27&project=%xx','#from=2026-10-21&to=2026-10-27&unknown=scope']){
 const invalid=mount({hash});assert.equal(invalid.reports.length,0,hash+' never displays a broader report');assert.equal(invalid.node('report').hidden,true);assert.equal(invalid.node('filter-error').hidden,false);assert.equal(invalid.node('export').disabled,true);assert.equal(invalid.api().getFilters(),null);
}
const recovered=mount({hash:'#from=2026-10-21&to=2026-10-27&project=unknown'});
recovered.node('project-all').dispatch('click');assert.equal(recovered.api().applyFilters(recovered.api().getDraftFilters()),true,'unknown scope can be explicitly cleared and applied');
const dateLink=mount({hash:'#from=2026-10-21&to=2026-10-27',initial:{...dates,projects:[p.alpha],chats:[ids.A]}});
assert.deepEqual(dateLink.plain(dateLink.api().getFilters().projects),[],'explicit URL scopes override embedded defaults');
assert.deepEqual(dateLink.plain(dateLink.api().getFilters().chats),[]);
const base='http://127.0.0.1:45921/'+'A'.repeat(43)+'/',refresh={baseUrl:base,refreshUrl:base+'refresh.html'};
const refreshed=mount({initial:{...dates,projects:[p.alpha],chats:[ids.A]},refresh});
assert.equal(refreshed.node('reload').disabled,false);
assert.equal(refreshed.api().applyFilters({...dates,projects:[p.alpha,p.beta],chats:[ids.A,ids.D]}),true);
assert.equal(await refreshed.window.codexUsageRefresh.refresh(),true,'rendering a new applied report retains the real refresh handler');
assert.equal(refreshed.window.location.assigned,base+'refresh.html'+refreshed.window.location.hash,'refresh receives the exact current applied plural selection');
const refreshRecovery=mount({hash:'#from=2026-10-21&to=2026-10-27&project=unknown',refresh});
assert.equal(refreshRecovery.node('reload').disabled,true);
refreshRecovery.node('project-all').dispatch('click');refreshRecovery.api().applyFilters(refreshRecovery.api().getDraftFilters());
assert.equal(refreshRecovery.node('reload').disabled,false,'correcting filters reenables refresh after the report becomes visible');
console.log('Dashboard selection UI state tests passed: URL and embedded defaults, exact plural IDs, reload, drafts, chips, singular compatibility, fail-closed malformed links, explicit recovery and refresh integration.');
