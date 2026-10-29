// Receives only synthetic fixtures from test_multiselect_parity.py.
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import {fileURLToPath} from 'node:url';

const root=path.dirname(path.dirname(fileURLToPath(import.meta.url)));
vm.runInThisContext(fs.readFileSync(path.join(root,'assets/browser_data.js'),'utf8'));
const api=globalThis.CodexUsageData,fixture=JSON.parse(fs.readFileSync(0,'utf8'));
const before=JSON.stringify(fixture.bundle);
for(const item of fixture.cases){
 const actual=api.project(fixture.bundle,{...item.selection,now:fixture.now}),expected=item.report;
 for(const key of ['totals','daily','owners','models','projects','trend','features','scope','selection','comparison'])assert.deepEqual(actual[key],expected[key],item.name+' '+key);
 for(const key of ['sourceFolders','projectAttribution'])if(Object.hasOwn(expected,key))assert.deepEqual(actual[key],expected[key],item.name+' '+key);
 assert.deepEqual(actual.chats.map(chat=>chat.id),expected.chats.map(chat=>chat.id),item.name+' chat ordering');
 for(const chat of actual.chats){
  const reference=expected.chats.find(row=>row.id===chat.id);
  for(const key of [...api.fields,'owners','daily','selfTokens','agentsTokens','agentCount','responses','sharePercent'])assert.deepEqual(chat[key],reference[key],item.name+' chat '+key);
 }
}
for(const selection of fixture.invalid)assert.throws(()=>api.project(fixture.bundle,{...selection,now:fixture.now}));
assert.equal(JSON.stringify(fixture.bundle),before,'parity projections cannot alter source data');
console.log('Exact Python/JavaScript multiselect parity: '+fixture.cases.length+' valid and '+fixture.invalid.length+' rejected synthetic selections.');
