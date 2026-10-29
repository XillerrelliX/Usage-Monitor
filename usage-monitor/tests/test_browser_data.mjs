import fs from 'node:fs';
import vm from 'node:vm';
import path from 'node:path';
import assert from 'node:assert/strict';
import {fileURLToPath} from 'node:url';

const root=path.dirname(path.dirname(fileURLToPath(import.meta.url)));
vm.runInThisContext(fs.readFileSync(path.join(root,'assets/browser_data.js'),'utf8'));
const api=globalThis.CodexUsageData,F=api.fields;
const counters=(input,cached,output,reasoning,total=input+output)=>Object.fromEntries(F.map((key,index)=>[key,[input,cached,output,reasoning,total][index]]));
const add=items=>Object.fromEntries(F.map(key=>[key,items.reduce((total,item)=>total+item[key],0)]));
const A='00000000-0000-4000-8000-000000000001',AA='00000000-0000-4000-8000-000000000002',B='00000000-0000-4000-8000-000000000003',G='00000000-0000-4000-8000-000000000004',H='00000000-0000-4000-8000-000000000005',P='C:/Projects/Alpha',Q='C:/Projects/alpha';
const owner=(id,chatId=id)=>({id,chatId,parentId:chatId===id?null:chatId,kind:chatId===id?'self':'agent',label:chatId===id?'Main agent':'Research agent',path:chatId===id?null:'/root/research',model:'App cache',responses:null});
const rec=(key,ownerId,date,values,projects,model)=>({key,ownerId,date,...values,projects,models:{[model]:values},projectModels:Object.fromEntries(Object.entries(projects).map(([id,values])=>[id,{[model]:values}]))});
export const bundle={schemaVersion:1,algorithmVersion:'usage-ledger-2026-10-27.1',generatedAt:'2026-10-27T12:00:00Z',numericComputedAt:'2026-10-27T11:58:00Z',source:{name:'Codex Usage Desktop',version:'test',lastScanAt:'2026-10-27T11:58:00Z',timeZone:'Europe/Berlin',datePrecision:'day',revisionSha256:'synthetic'},coverage:{startDate:'2026-10-21',endDate:'2026-10-27',recordedDays:[],historyComplete:true},owners:[owner(A),owner(AA,A),owner(B),owner(G),owner(H)],chats:[{id:A,label:'Alpha chat',labelSource:'threads.name'},{id:B,label:'Beta chat',labelSource:'threads.name'},{id:G,label:'Guardian review',labelSource:'threads.name'},{id:H,label:'Guardian review',labelSource:'threads.name'}],records:[
 rec('one',A,'2026-10-21',counters(80,50,20,5),{[P]:counters(48,30,12,3),[Q]:counters(32,20,8,2)},'m1'),
 rec('two',AA,'2026-10-22',counters(40,20,10,3),{[P]:counters(40,20,10,3)},'m2'),
 rec('three',B,'2026-10-23',counters(180,90,20,6),{[Q]:counters(180,90,20,6)},'m1'),
 rec('four',G,'2026-10-24',counters(0,0,0,0),{[Q]:counters(0,0,0,0)},'m1'),
 rec('five',H,'2026-10-26',counters(30,40,10,12,99),{[P]:counters(30,40,10,12,99)},'m1'),
 rec('six',B,'2026-10-27',counters(24,12,6,2),{[Q]:counters(24,12,6,2)},'m2')
],daily:[],appModels:[],appProjects:[],features:{models:true,projects:true,chatFilter:true,projectFilter:true,sessionModels:true,sessionProjects:true,projectModels:true},diagnostics:{status:'incomplete',warnings:['The app total differs from input plus output; its recorded counters are preserved without adjustment.','A recorded subset exceeds its parent counter; app values are preserved without adjustment.'],attribution:{}},cache:{status:'hit',reason:'synthetic'}};
for(const date of [...new Set(bundle.records.map(row=>row.date))].sort())bundle.daily.push({date,...add(bundle.records.filter(row=>row.date===date))});
bundle.coverage.recordedDays=bundle.daily.map(row=>row.date);
for(const [kind,target] of [['models','appModels'],['projects','appProjects']])for(const id of [...new Set(bundle.records.flatMap(row=>Object.keys(row[kind])))].sort()){
 const daily=[];for(const date of bundle.coverage.recordedDays){const items=bundle.records.filter(row=>row.date===date&&row[kind][id]).map(row=>row[kind][id]);if(items.length)daily.push({date,...add(items)});}bundle[target].push({id,label:id,...add(daily),daily});
}
const now='2026-10-27T12:00:00Z',base={startDate:'2026-10-21',endDate:'2026-10-27',now};
const pick=row=>F.map(key=>row[key]);
const report=selection=>api.project(bundle,{...base,...selection});
assert.deepEqual(pick(report({}).totals),[354,212,66,28,479]);
assert.deepEqual(pick(report({project:P}).totals),[118,90,32,18,209]);
assert.deepEqual(pick(report({project:Q}).totals),[236,122,34,10,270]);
assert.deepEqual(pick(report({chat:A}).totals),[120,70,30,8,150]);
assert.deepEqual(pick(report({project:P,chat:A}).totals),[88,50,22,6,110]);
assert.deepEqual(pick(report({project:Q,chat:A}).totals),[32,20,8,2,40]);
assert.deepEqual(pick(report({startDate:'2026-10-22',endDate:'2026-10-23'}).totals),[220,110,30,9,250]);
assert.deepEqual(pick(report({startDate:'2026-10-24',endDate:'2026-10-24'}).totals),[0,0,0,0,0]);
assert.equal(report({startDate:'2026-10-24',endDate:'2026-10-24'}).trend.days[0].recorded,true);
assert.equal(report({startDate:'2026-10-25',endDate:'2026-10-25'}).trend.days[0].recorded,false);
assert.equal(report({}).trend.days.length,7);assert.equal(report({}).trend.days.filter(day=>!day.recorded).length,1);
assert.equal(report({startDate:'2026-10-27',endDate:'2026-10-27'}).totals.totalTokens,30);
assert.equal(report({project:P,startDate:'2026-10-27',endDate:'2026-10-27'}).scope.empty,true);
assert.equal(report({chat:G}).totals.sessions,1);assert.equal(report({chat:G}).scope.empty,false);
assert.deepEqual(report({project:Q,chat:H}).chats,[]);
assert.equal(api.today(bundle,'2026-10-26T23:30:00Z'),'2026-10-27');
assert.equal(api.today(bundle,'2026-10-27T23:30:00Z'),'2026-10-28');
assert.throws(()=>api.today({...bundle,source:{timeZone:'Invalid/Zone'}},now),/does not support/);
for(const selected of [{startDate:'2026-02-30'},{startDate:''},{startDate:'2026-10-29'},{startDate:'2025-10-25'},{project:' '},{chat:0}])assert.throws(()=>report(selected));
assert.throws(()=>api.project({...bundle,features:{...bundle.features,projectFilter:false}},{...base,project:P}),/unavailable/);
assert.throws(()=>api.project({...bundle,records:[...bundle.records,bundle.records[0]]},base),/duplicate/);
assert.equal(report({}).chats.find(chat=>chat.id===H).reviewAlias,'Review 2');assert.equal(report({chat:H}).chats[0].reviewAlias,'Review 2');
const unavailable=api.project({...bundle,features:{...bundle.features,sessionModels:false}}, {...base,chat:A});assert.equal(unavailable.features.models,false);assert.deepEqual(unavailable.models,[]);
const empty=api.project({...bundle,features:{...bundle.features,sessionModels:false,sessionProjects:false}}, {...base,chat:'absent'});assert.equal(empty.features.models,true);assert.equal(empty.features.projects,true);
for(const days of [1,6,7,30])assert.equal(report({startDate:api.dayOffset('2026-10-27',1-days)}).trend.dayCount,days);
{
 const before=JSON.stringify(bundle),multiple=report({projects:[Q,P,P],chats:[B,A,A.toUpperCase()]});
 assert.deepEqual(multiple.selection,{projects:[P,Q],chats:[A,B]});
 assert.deepEqual(pick(multiple.totals),[324,172,56,16,380]);
 assert.equal(multiple.totals.sessions,3);assert.deepEqual(multiple.owners.map(row=>row.id).sort(),[A,AA,B]);
 for(const key of ['daily','owners','chats','models','projects'])assert.deepEqual(pick(add(multiple[key])),pick(multiple.totals),key+' deduplicated multi reconciliation');
 assert.equal(multiple.comparison.kind,'chats');assert.deepEqual(multiple.comparison.series.map(row=>[row.id,row.label,row.totalTokens]),[[A,'Alpha chat',150],[B,'Beta chat',230]]);
 assert.deepEqual(pick(add(multiple.comparison.series)),pick(multiple.totals));
 const intersect=report({projects:[P,P],chats:[B,A,B]});assert.equal(intersect.totals.totalTokens,110);
 assert.deepEqual(intersect.comparison.series.map(row=>[row.id,row.totalTokens]),[[A,110],[B,0]]);assert.deepEqual(intersect.comparison.series[1].daily,[]);
 const projectLines=report({projects:[Q,P,Q]});assert.equal(projectLines.comparison.kind,'projects');
 assert.deepEqual(projectLines.comparison.series.map(row=>[row.id,row.totalTokens]),[[P,209],[Q,270]]);
 assert.deepEqual(pick(add(projectLines.comparison.series)),pick(projectLines.totals));
 const zero=report({projects:[Q],chats:[G,B],startDate:'2026-10-24',endDate:'2026-10-25'});
 assert.deepEqual(zero.comparison.series.map(row=>[row.id,row.daily.map(day=>[day.date,day.totalTokens])]),[[B,[]],[G,[['2026-10-24',0]]]]);
 assert.equal(zero.trend.days[0].recorded,true);assert.equal(zero.trend.days[0].totalTokens,0);assert.equal(zero.trend.days[1].recorded,false);assert.equal(zero.trend.days[1].totalTokens,null);
 assert.match(zero.comparison.series[1].label,/Guardian Review 1/);
 const narrow=report({projects:[P,Q],chats:[A,B],startDate:'2026-10-22',endDate:'2026-10-23'});assert.equal(narrow.totals.totalTokens,250);assert.deepEqual(narrow.comparison.series.map(row=>row.daily.map(day=>day.date)),[['2026-10-22'],['2026-10-23']]);
 assert.deepEqual(report({projects:[],chats:[]}).totals,report({}).totals);assert.deepEqual(report({}).comparison,{kind:null,series:[]});
 assert.deepEqual(report({project:P,projects:[P,P],chat:A.toUpperCase(),chats:[A]}).totals,report({project:P,chat:A}).totals);
 assert.deepEqual(api.catalog(bundle,{projects:[P,Q,Q]}).chats.map(row=>row.id).sort(),api.catalog(bundle).chats.map(row=>row.id).sort());
 for(const selection of [{projects:P},{chats:A},{projects:[null]},{chats:[0]},{projects:['']},{chats:[' ']},{projects:[P,'unknown']},{chats:[A,'unknown']},{project:P,projects:[]},{chat:A,chats:[B]}])assert.throws(()=>report(selection),undefined,JSON.stringify(selection));
 assert.throws(()=>api.catalog(bundle,{projects:[P,'unknown']}),/saved catalog/);
 const missing=structuredClone(bundle);delete missing.records[0].projectModels[P];
 const unavailable=api.project(missing,{...base,projects:[P,Q],chats:[A]});assert.equal(unavailable.features.models,false);assert.deepEqual(unavailable.models,[]);assert.equal(unavailable.comparison.series[0].totalTokens,150);
 const huge=9007199254740000,large=structuredClone(bundle),values=counters(huge,4,0,0);
 large.records=[rec('large',A,'2026-10-21',values,{[P]:values,[Q]:counters(0,0,0,0)},'m1')];
 const exact=api.project(large,{...base,projects:[P,Q,P],chats:[A,A]});assert.equal(exact.totals.totalTokens,huge);assert.equal(exact.comparison.series[0].inputTokens,huge);
 large.records.push(rec('overflow',B,'2026-10-21',values,{[P]:values},'m1'));
 assert.throws(()=>api.project(large,{...base,projects:[P,Q],chats:[A,B]}),/integer range/);
 assert.equal(JSON.stringify(bundle),before,'multiple projections preserve original ledger');
}
{
 const oraclePath=process.argv[2]||path.join(root,'tests/fixtures/browser-oracles.fixture.json'),fixture=JSON.parse(fs.readFileSync(oraclePath,'utf8'));fixture.bundle=fixture.bundle||JSON.parse(fs.readFileSync(path.join(path.dirname(oraclePath),process.argv[2]?'fixture-bundle.json':'browser-bundle.fixture.json'),'utf8'));
 for(const item of fixture.cases){const actual=api.project(fixture.bundle,{...item.selection,now:fixture.now}),expected=item.report,label=item.selection.name;for(const key of ['totals','daily','owners','models','projects','trend','features','scope'])assert.deepEqual(actual[key],expected[key],label+' '+key);for(const chat of actual.chats){const reference=expected.chats.find(row=>row.id===chat.id);for(const key of [...F,'owners','daily','selfTokens','agentsTokens','agentCount','responses','sharePercent'])assert.deepEqual(chat[key],reference[key],label+' chat '+key);}assert.deepEqual(actual.chats.map(row=>row.id),expected.chats.map(row=>row.id),label+' chat ordering');if(item.originalCoreReport)for(const key of ['totals','daily','models','projects'])assert.deepEqual(actual[key],item.originalCoreReport[key],label+' original core '+key);const fixed=fixture.fixedHandcomputed[label];if(fixed)assert.deepEqual(F.map(field=>actual.totals[field]),fixed,label+' fixed hand vector');}
 console.log('Python oracle parity: '+fixture.cases.length+' selections');
}
console.log('Browser projection tests passed: hand-calculated five-counter intersections, dates, zeros, missing days, stable aliases, invalid input, safe attribution and graph thresholds.');
