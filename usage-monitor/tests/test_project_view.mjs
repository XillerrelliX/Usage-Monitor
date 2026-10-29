import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import {fileURLToPath} from 'node:url';

const root=path.dirname(path.dirname(fileURLToPath(import.meta.url)));
vm.runInThisContext(fs.readFileSync(path.join(root,'assets/browser_data.js'),'utf8'));
const api=globalThis.CodexUsageData,fixture=JSON.parse(fs.readFileSync(path.join(root,'tests/fixtures/project-metadata.fixture.json'),'utf8'));
const {bundle,ids,projectIds:p,folders,now,fixedVectors:fixed}=fixture,fields=api.fields;
const before=JSON.stringify(bundle),prepared=api.prepareProjectView(bundle),clone=value=>JSON.parse(JSON.stringify(value));
const base={startDate:'2026-10-21',endDate:'2026-10-27',now};
const report=selection=>api.project(bundle,{...base,...selection});
const vector=row=>fields.map(field=>row[field]);
const selections={all:{},alpha:{project:p.alpha},beta:{project:p.beta},gamma:{project:p.gamma},unassigned:{project:p.unassigned},alphaChatA:{project:p.alpha,chat:ids.A},alphaChatD:{project:p.alpha,chat:ids.D},betaChatD:{project:p.beta,chat:ids.D},unassignedChatD:{project:p.unassigned,chat:ids.D}};
assert.equal(fixture.synthetic,true);
assert.notEqual(prepared,bundle);assert.equal(api.prepareProjectView(bundle),prepared);assert.equal(api.prepareProjectView(prepared),prepared);
const detached=clone(prepared);assert.equal(api.prepareProjectView(detached),detached,'serialized prepared marker prevents regrouping twice');
assert.equal(JSON.stringify(bundle),before,'projection does not mutate numeric ledger, metadata, models, proof or cache');
assert.equal(prepared.sourceAppProjects,bundle.appProjects);
for(const [key,selection]of Object.entries(selections))assert.deepEqual(vector(report(selection).totals),fixed[key],key+' fixed five-counter vector');
const all=report({}),raw={...bundle};delete raw.projectMetadata;
assert.equal(api.prepareProjectView(raw),raw,'legacy raw-folder mode preserves original object');
for(const key of ['totals','daily','owners','models','trend','features'])assert.deepEqual(all[key],api.project(raw,base)[key],key+' global accounting unchanged');
assert.deepEqual(vector(all.projects.reduce((a,b)=>Object.fromEntries(fields.map(field=>[field,a[field]+b[field]])),Object.fromEntries(fields.map(field=>[field,0])))),fixed.all);
assert.deepEqual(all.projects.map(row=>row.label),['Project Alpha','Project Beta','No named project','Project Gamma']);
assert.deepEqual(api.catalog(bundle).projects.map(row=>row.label),all.projects.map(row=>row.label));
assert.equal(api.catalog(bundle,{project:p.alpha}).chats.some(row=>row.id===ids.C),false,'same source folder projectless owner excluded');
assert.equal(api.catalog(bundle,{project:p.alpha}).chats.some(row=>row.id===ids.E),true,'explicit project outside known root included');
assert.equal(prepared.records.find(row=>row.key==='r1').sourceProjects,bundle.records[0].projects);
assert.deepEqual(prepared.records.find(row=>row.key==='r1').projects,{[p.alpha]:Object.fromEntries(fields.map(field=>[field,bundle.records[0][field]]))});
assert.deepEqual(Object.keys(prepared.records.find(row=>row.key==='r6').projects),[p.unassigned],'projectless override inside named root');
assert.deepEqual(Object.keys(prepared.records.find(row=>row.key==='r8').projects),[p.alpha],'explicit new-folder project assignment');
assert.deepEqual(Object.keys(prepared.records.find(row=>row.key==='r9').projects),[p.unassigned],'ambiguous roots are not guessed');
assert.deepEqual(Object.keys(prepared.records.find(row=>row.key==='r4').projects),[p.alpha],'nested agent inherits resolved verified parent assignment');
assert.equal(report({project:p.alpha,chat:ids.A}).chats[0].agentCount,2);
assert.equal(report({project:p.alpha,chat:ids.A}).chats[0].agentsTokens,30);
assert.equal(report({project:p.beta,chat:ids.H}).scope.empty,true,'unrelated projectless self does not inherit named root');
assert.equal(report({project:p.alpha}).scope.label,'Project Alpha');
assert.equal(report({project:p.alpha,chat:ids.D}).scope.label,'Project Alpha • D synthetic chat');
assert.equal(report({chat:ids.D}).projects.find(row=>row.id===p.beta).label,'Project Beta','friendly names survive chat-scoped rankings');
assert.deepEqual(report({project:p.alpha,chat:ids.D}).sourceFolders,[folders.alpha]);
assert.deepEqual(report({project:p.beta,chat:ids.D}).sourceFolders,[folders.beta]);
assert.deepEqual(report({project:p.unassigned,chat:ids.D}).sourceFolders,[folders.unknown]);
assert.deepEqual(report({project:p.alpha,chat:ids.A}).sourceFolders,[folders.alpha,folders.beta,folders.unknown].sort());
assert.deepEqual(report({project:p.alpha}).projectAttribution.sourceFolders,report({project:p.alpha}).sourceFolders);
assert.deepEqual(report({project:p.alpha}).models.map(row=>[row.id,row.totalTokens]),[['model-a',157],['model-b',90]],'project model parts regroup without counting cached/reasoning twice');
assert.equal(all.totals.uncachedInputTokens,278);assert.equal(all.totals.totalTokens,742);
assert.ok(all.diagnostics.warnings.some(message=>message.includes('total differs')));
assert.ok(all.diagnostics.warnings.some(message=>message.includes('subset exceeds')));
assert.equal(report({chat:ids.R,startDate:'2026-10-24',endDate:'2026-10-24'}).trend.days[0].recorded,true,'recorded zero remains recorded');
assert.equal(report({chat:ids.R}).chats[0].reviewAlias,'Review 1');assert.equal(report({chat:ids.RR}).chats[0].reviewAlias,'Review 2');
for(const days of [1,2,3,7])assert.equal(report({startDate:api.dayOffset(base.endDate,1-days)}).trend.dayCount,days);
const sameNames=clone(bundle);sameNames.projectMetadata.catalog.find(row=>row.id===p.beta).label='Project Alpha';
assert.deepEqual(vector(api.project(sameNames,{...base,project:p.beta}).totals),fixed.beta,'project identity never joins by display name');
const absent=clone(bundle);
absent.projectMetadata.sourceFolders=absent.projectMetadata.sourceFolders.filter(row=>row.id!==folders.ambiguous);
assert.equal(api.project(absent,{...base,chat:ids.F}).projectAttribution.provenance[0],'unresolved-source-folder');
for(const corrupt of [value=>{value.projectMetadata.schemaVersion=2;},value=>{value.projectMetadata.mode='folders';},value=>{value.projectMetadata.catalog.push(clone(value.projectMetadata.catalog[0]));},value=>{value.projectMetadata.ownerAssignments[ids.A].projectId='unknown-full-id';},value=>{value.appProjects[0].daily[0].reasoningTokens++;}]){
 const invalid=clone(bundle);corrupt(invalid);assert.throws(()=>api.prepareProjectView(invalid));
}
for(const corrupt of [value=>{value.records[0].projectModels[folders.alpha]['model-a'].cachedInputTokens++;},value=>{value.records[0].models['model-a'].reasoningTokens++;},value=>{value.appModels[0].daily[0].totalTokens++;},value=>{value.features.sessionModels=false;}]){
 const invalid=clone(bundle);corrupt(invalid);const view=api.prepareProjectView(invalid);assert.equal(view.features.projectModels,false);assert.equal(view.records[0].projectModels,null);assert.deepEqual(vector(api.project(invalid,{...base,project:p.alpha}).totals),fixed.alpha);assert.equal(api.project(invalid,{...base,project:p.alpha}).features.models,false);
}
for(const feature of ['projects','sessionProjects']){
 const unavailable=clone(bundle);unavailable.features[feature]=false;const result=api.prepareProjectView(unavailable),view=api.project(unavailable,base);
 assert.equal(result.features.projectFilter,false);assert.equal(result.features.projects,false);assert.deepEqual(result.appProjects,[]);assert.equal(result.records[0].projects,null);assert.equal(view.features.projects,false);assert.deepEqual(vector(view.totals),fixed.all);assert.throws(()=>api.project(unavailable,{...base,project:p.alpha}),/unavailable/);
}
if(process.argv[2]){
 const oracle=JSON.parse(fs.readFileSync(path.resolve(process.argv[2]),'utf8'));
 for(const item of oracle.cases){const actual=report(item.selection),expected=item.report;for(const key of ['totals','daily','owners','models','projects','trend','features','scope','sourceFolders','projectAttribution'])assert.deepEqual(actual[key],expected[key],item.name+' Python '+key);for(const chat of actual.chats){const reference=expected.chats.find(row=>row.id===chat.id);for(const key of [...fields,'owners','daily','selfTokens','agentsTokens','agentCount','responses','sharePercent'])assert.deepEqual(chat[key],reference[key],item.name+' Python chat '+key);}assert.deepEqual(actual.chats.map(row=>row.id),expected.chats.map(row=>row.id),item.name+' Python chat ordering');}
 console.log('Named project Python oracle parity: '+oracle.cases.length+' selections');
}
assert.equal(JSON.stringify(bundle),before,'all repeated selections preserve source ledger');
console.log('Named project browser tests passed: synthetic fixed five-counter groups, explicit/projectless overrides, nested agents, source details, exact identity, legacy mode and conservative missing attribution.');
