(function(global){
'use strict';
const fields=['inputTokens','cachedInputTokens','outputTokens','reasoningTokens','totalTokens'];
const caveat='Stored or synced history is not verified execution on this device or by the currently selected account.';
const compare=(a,b)=>a<b?-1:a>b?1:0;
const rows=value=>Array.isArray(value)?value:[];
const dateKey=value=>typeof value==='string'&&/^\d{4}-\d{2}-\d{2}$/.test(value)&&!Number.isNaN(Date.parse(value+'T00:00:00Z'))&&new Date(value+'T00:00:00Z').toISOString().slice(0,10)===value;
function fail(message){throw new Error(message);}
function sum(values){const total=Object.fromEntries(fields.map(field=>[field,0]));for(const row of values)for(const field of fields){const value=row[field];if(!Number.isSafeInteger(value)||value<0)fail('Saved token counters are invalid. Run the local refresh command.');total[field]+=value;if(!Number.isSafeInteger(total[field]))fail('Selected token totals exceed the supported integer range.');}return total;}
function selectionIds(singular,plural,catalog,chats=false){
 const identity=value=>{if(typeof value!=='string'||!value.trim())fail('The scope must use a nonempty exact cached identity.');return chats&&/^[a-f\d]{8}(?:-[a-f\d]{4}){3}-[a-f\d]{12}$/i.test(value)?value.toLowerCase():value;};
 const single=singular===null?null:identity(singular);if(plural===undefined||plural===null)return single===null?[]:[single];
 if(!Array.isArray(plural))fail('Scope selections must be arrays of exact cached identities.');
 const selected=[...new Set(plural.map(identity))].sort(compare);
 if(single!==null&&(selected.length!==1||selected[0]!==single))fail('Singular and multiple scope selections disagree.');
 if(selected.some(id=>!catalog.has(id)))fail('A selected '+(chats?'chat':'project')+' identity is not in the saved catalog.');
 return selected;
}
const dayOffset=(date,offset)=>new Date(Date.parse(date+'T00:00:00Z')+offset*86400000).toISOString().slice(0,10);
const projectViews=new WeakMap(),preparedProjectViews=new WeakSet();
function rawProjectModelsValid(bundle){
 if(!bundle.features?.projectModels||!bundle.features?.sessionModels)return false;
 try{
  const same=(a,b)=>fields.every(field=>a[field]===b[field]),sameMap=(a,b)=>a.size===b.size&&[...a].every(([id,value])=>b.has(id)&&same(value,b.get(id)));
  const modelDays=new Map(),appDays=new Map(),add=(map,id,value)=>map.set(id,sum([...(map.has(id)?[map.get(id)]:[]),value]));
  for(const record of rows(bundle.records)){
   if(!record.projects||!record.projectModels||!record.models||Object.keys(record.projects).length!==Object.keys(record.projectModels).length)return false;
   const models=new Map();for(const [folder,values] of Object.entries(record.projects)){const parts=record.projectModels[folder];if(!parts||!same(sum(Object.values(parts)),values))return false;for(const [id,part] of Object.entries(parts))add(models,id,part);}
   if(!sameMap(models,new Map(Object.entries(record.models)))||!same(sum(Object.values(record.models)),record))return false;
   if(!modelDays.has(record.date))modelDays.set(record.date,new Map());for(const [id,part] of Object.entries(record.models))add(modelDays.get(record.date),id,part);
  }
  if(bundle.features?.models){for(const item of rows(bundle.appModels))for(const day of rows(item.daily)){if(!appDays.has(day.date))appDays.set(day.date,new Map());if(appDays.get(day.date).has(item.id))return false;appDays.get(day.date).set(item.id,day);}if(modelDays.size!==appDays.size||[...modelDays].some(([date,models])=>!appDays.has(date)||!sameMap(models,appDays.get(date))))return false;}
  return true;
 }catch{return false;}
}
function prepareProjectView(bundle){
 if(!bundle||typeof bundle!=='object'||!bundle.projectMetadata)return bundle;
 if(preparedProjectViews.has(bundle))return bundle;
 if(projectViews.has(bundle))return projectViews.get(bundle);
 const metadata=bundle.projectMetadata;
 if(metadata.schemaVersion!==1||metadata.mode!=='named-projects')fail('The saved project metadata is unsupported. Run the local refresh command.');
 const catalogRows=rows(metadata.catalog),catalogById=new Map();
 for(const item of catalogRows){if(!item||typeof item.id!=='string'||!item.id||typeof item.label!=='string'||!item.label.trim()||catalogById.has(item.id))fail('The saved named project catalog is invalid. Run the local refresh command.');catalogById.set(item.id,item);}
 const bucket=metadata.unassignedId;
 if(typeof bucket!=='string'||!catalogById.has(bucket))fail('The saved projectless bucket is missing. Run the local refresh command.');
 const projectView={schemaVersion:1,mode:'named-projects',labels:Object.fromEntries(catalogRows.map(item=>[item.id,item.label])),unassignedId:bucket};
 if(bundle.projectView?.schemaVersion===1&&bundle.projectView.mode==='named-projects'){preparedProjectViews.add(bundle);return bundle;}
 if(!bundle.features?.projects||!bundle.features?.sessionProjects){
  const warning='Named project attribution is unavailable without independently verified source-folder slices.',warnings=[...new Set([...rows(bundle.diagnostics?.warnings),warning])];
  const result={...bundle,records:rows(bundle.records).map(record=>({...record,sourceProjects:record.projects,sourceProjectModels:record.projectModels,sourceFolders:Object.keys(record.projects||{}),projects:null,projectModels:null,projectSources:{}})),sourceAppProjects:bundle.appProjects,appProjects:[],features:{...bundle.features,projects:false,sessionProjects:false,projectFilter:false,projectModels:false},diagnostics:{...bundle.diagnostics,status:'incomplete',warnings},projectView};
  projectViews.set(bundle,result);preparedProjectViews.add(result);return result;
 }
 const folderAssignments=new Map();
 for(const item of rows(metadata.sourceFolders)){if(!item||typeof item.id!=='string'||folderAssignments.has(item.id)||!catalogById.has(item.projectId??bucket))fail('The saved source folder attribution is invalid. Run the local refresh command.');folderAssignments.set(item.id,{...item,projectId:item.projectId??bucket});}
 const ownerAssignments=metadata.ownerAssignments||{},sourceGroups=new Map(),provenanceGroups=new Map(),projectParts=new Map(),mappedRecords=[],modelAvailable=rawProjectModelsValid(bundle);
 for(const assignment of Object.values(ownerAssignments))if(!assignment||typeof assignment.wholeOwner!=='boolean'||(assignment.wholeOwner?!catalogById.has(assignment.projectId??bucket):assignment.projectId!==null))fail('The saved owner project attribution is invalid. Run the local refresh command.');
 const append=(map,id,value)=>{if(!map.has(id))map.set(id,[]);map.get(id).push(value);};
 for(const record of rows(bundle.records)){
  if(!record.projects||typeof record.projects!=='object')fail('Named project grouping requires verified source folder counters. Run the local refresh command.');
  const assignment=ownerAssignments[record.ownerId];
  if(assignment?.wholeOwner===true&&assignment.projectId!==null&&!catalogById.has(assignment.projectId))fail('The saved owner project attribution is invalid. Run the local refresh command.');
  const grouped=new Map(),groupedModels=new Map(),folders=Object.keys(record.projects),recordSources=new Map();
  for(const [folder,values] of Object.entries(record.projects)){
   const folderAssignment=folderAssignments.get(folder),id=assignment?.wholeOwner===true?(assignment.projectId??bucket):(folderAssignment?.projectId||bucket),reason=assignment?.wholeOwner===true?(assignment.reason||'owner-assignment'):(folderAssignment?.reason||(folderAssignment?'source-folder':'unresolved-source-folder'));
   append(grouped,id,values);append(projectParts,id,{date:record.date,...values});
   if(!sourceGroups.has(id))sourceGroups.set(id,new Set());sourceGroups.get(id).add(folder);
   if(!provenanceGroups.has(id))provenanceGroups.set(id,new Set());provenanceGroups.get(id).add(reason);
   if(!recordSources.has(id))recordSources.set(id,{sourceFolders:new Set(),provenance:new Set()});recordSources.get(id).sourceFolders.add(folder);recordSources.get(id).provenance.add(reason);
   if(modelAvailable){if(!groupedModels.has(id))groupedModels.set(id,new Map());for(const [model,counters] of Object.entries(record.projectModels[folder]))append(groupedModels.get(id),model,counters);}
  }
  const projects=Object.fromEntries([...grouped].map(([id,values])=>[id,sum(values)]));
  const projectTotal=sum(Object.values(projects));if(fields.some(field=>projectTotal[field]!==record[field]))fail('Named project and source record counters disagree. Run the local refresh command.');
  const projectModels=modelAvailable?Object.fromEntries([...groupedModels].map(([id,models])=>[id,Object.fromEntries([...models].map(([model,values])=>[model,sum(values)]))])):null;
  if(projectModels)for(const [id,models] of Object.entries(projectModels))if(fields.some(field=>sum(Object.values(models))[field]!==projects[id][field]))fail('Named project model counters disagree. Run the local refresh command.');
  const projectSources=Object.fromEntries([...recordSources].map(([id,source])=>[id,{sourceFolders:[...source.sourceFolders].sort(compare),provenance:[...source.provenance].sort(compare)}]));
  mappedRecords.push({...record,sourceProjects:record.projects,sourceProjectModels:record.projectModels,sourceFolders:folders.sort(compare),projects,projectModels,projectSources});
 }
 const allTotals=sum(rows(bundle.daily)),appProjects=[...projectParts].map(([id,parts])=>{
  const days=new Map();for(const part of parts)append(days,part.date,part);
  const item=catalogById.get(id),totals=sum(parts);return {id,label:item.label,...totals,daily:[...days].sort(([a],[b])=>compare(a,b)).map(([date,values])=>({date,...sum(values)})),sharePercent:allTotals.totalTokens?totals.totalTokens/allTotals.totalTokens*100:0,sourceFolders:[...(sourceGroups.get(id)||[])].sort(compare),provenance:[...(provenanceGroups.get(id)||[])].sort(compare)};
 }).sort((a,b)=>b.totalTokens-a.totalTokens||compare(a.id,b.id));
 const sourceTotal=sum(rows(bundle.appProjects)),groupTotal=sum(appProjects);
 if(fields.some(field=>sourceTotal[field]!==groupTotal[field]||allTotals[field]!==groupTotal[field]))fail('Named project and independent source folder totals disagree. Run the local refresh command.');
 const sourceDays=new Map(),groupDays=new Map();for(const item of rows(bundle.appProjects))for(const day of rows(item.daily))append(sourceDays,day.date,day);for(const item of appProjects)for(const day of item.daily)append(groupDays,day.date,day);
 const rawFolderDays=new Map(),independentFolderDays=new Map();for(const record of rows(bundle.records))for(const [folder,values]of Object.entries(record.projects)){if(!rawFolderDays.has(folder))rawFolderDays.set(folder,new Map());append(rawFolderDays.get(folder),record.date,values);}
 for(const item of rows(bundle.appProjects)){if(independentFolderDays.has(item.id))fail('Named project source contains duplicate folder rankings. Run the local refresh command.');const days=new Map();for(const day of rows(item.daily)){if(days.has(day.date))fail('Named project source contains duplicate folder ranking days. Run the local refresh command.');days.set(day.date,day);}const total=sum(rows(item.daily));if(fields.some(field=>item[field]!==total[field]))fail('Named project source folder period counters disagree. Run the local refresh command.');independentFolderDays.set(item.id,days);}
 for(const folder of new Set([...rawFolderDays.keys(),...independentFolderDays.keys()])){const actual=rawFolderDays.get(folder)||new Map(),expected=independentFolderDays.get(folder)||new Map();for(const date of new Set([...actual.keys(),...expected.keys()])){const left=sum(actual.get(date)||[]),right=sum(expected.has(date)?[expected.get(date)]:[]);if(fields.some(field=>left[field]!==right[field]))fail('Named project source folder daily counters disagree. Run the local refresh command.');}}
 const independentDays=new Map();for(const day of rows(bundle.daily)){if(independentDays.has(day.date))fail('Named project source contains duplicate app days. Run the local refresh command.');independentDays.set(day.date,day);}
 for(const date of new Set([...sourceDays.keys(),...groupDays.keys(),...independentDays.keys()])){const before=sum(sourceDays.get(date)||[]),after=sum(groupDays.get(date)||[]),independent=sum(independentDays.has(date)?[independentDays.get(date)]:[]);if(fields.some(field=>before[field]!==after[field]||independent[field]!==after[field]))fail('Named project and independent source folder daily counters disagree. Run the local refresh command.');}
 const modelWarning='Named project model rankings are unavailable because independently verified nested source parts are missing.',diagnostics=bundle.features?.projectModels&&!modelAvailable?{...bundle.diagnostics,status:'incomplete',warnings:[...rows(bundle.diagnostics?.warnings),modelWarning]}:bundle.diagnostics;
 const result={...bundle,records:mappedRecords,sourceAppProjects:bundle.appProjects,appProjects,features:{...bundle.features,projects:true,sessionProjects:true,projectFilter:true,projectModels:modelAvailable},diagnostics,projectView};
 projectViews.set(bundle,result);preparedProjectViews.add(result);return result;
}
function sourceSummary(records,projectId=null){
 const folders=new Set(),provenance=new Set();for(const record of records)for(const [id,item] of Object.entries(record.projectSources||{})){if(projectId&&id!==projectId)continue;for(const folder of rows(item.sourceFolders))folders.add(folder);for(const reason of rows(item.provenance))provenance.add(reason);}
 return {sourceFolders:[...folders].sort(compare),provenance:[...provenance].sort(compare)};
}
function today(bundle,now=new Date()){
 const zone=bundle?.source?.timeZone;
 if(typeof zone!=='string'||!zone)fail('The saved app time zone is unavailable. Refresh the saved data or enter dates manually.');
 const instant=new Date(now);if(Number.isNaN(instant.valueOf()))fail('The current date is unavailable. Enter dates manually.');
 try{const parts=new Intl.DateTimeFormat('en-US',{timeZone:zone,year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(instant),get=type=>parts.find(part=>part.type===type)?.value;return [get('year'),get('month'),get('day')].join('-');}catch{fail('This browser does not support the saved app time zone ('+zone+'). Enter dates manually.');}
}
function catalog(bundle,{project=null,projects}={}){
 bundle=prepareProjectView(bundle);
 const projectIds=selectionIds(project,projects,new Set(rows(bundle.appProjects).map(item=>item.id))),projectSet=new Set(projectIds);
 const ownerChats=new Map(rows(bundle.owners).map(owner=>[owner.id,owner.chatId]));
 const chatIds=projectIds.length?new Set(rows(bundle.records).filter(row=>row.projects&&Object.keys(row.projects).some(id=>projectSet.has(id))).map(row=>ownerChats.get(row.ownerId))):null;
 const datesByChat=new Map();for(const record of rows(bundle.records)){const id=ownerChats.get(record.ownerId);if(!datesByChat.has(id))datesByChat.set(id,new Set());datesByChat.get(id).add(record.date);}
 const reviews=rows(bundle.chats).filter(chat=>String(chat.label).trim().toLocaleLowerCase('en-US')==='guardian review').map(chat=>({...chat,dates:[...(datesByChat.get(chat.id)||[])].sort()})).sort((a,b)=>(a.dates[0]||'9999').localeCompare(b.dates[0]||'9999')||a.id.localeCompare(b.id));
 const reviewLabels=new Map(reviews.map((chat,index)=>[chat.id,{label:'Review '+(index+1),startDate:chat.dates[0]||null,endDate:chat.dates.at(-1)||null}]));
 return {startDate:bundle.coverage?.startDate||null,endDate:bundle.coverage?.endDate||null,timeZone:bundle.source?.timeZone||null,
  projects:rows(bundle.appProjects).map(item=>({id:item.id,label:item.label||item.id,...(bundle.projectView?{sourceFolders:rows(item.sourceFolders),provenance:item.provenance}: {})})),
  chats:rows(bundle.chats).filter(chat=>!chatIds||chatIds.has(chat.id)).map(chat=>{const review=reviewLabels.get(chat.id);return {...chat,displayLabel:review?'Guardian '+review.label+(review.startDate?' · '+review.startDate+(review.endDate!==review.startDate?' – '+review.endDate:''):''):chat.label,reviewAlias:review?.label||null,reviewStartDate:review?.startDate||null,reviewEndDate:review?.endDate||null};})};
}
function project(bundle,{startDate,endDate,project:singularProject=null,chat:singularChat=null,projects:selectedProjects,chats:selectedChats,now=new Date()}={}){
 bundle=prepareProjectView(bundle);
 if(!bundle||bundle.schemaVersion!==1||!Array.isArray(bundle.records))fail('The saved usage data is unsupported. Run the local refresh command.');
 if(!dateKey(startDate)||!dateKey(endDate))fail('Enter valid From and To dates.');
 const dayCount=Math.round((Date.parse(endDate+'T00:00:00Z')-Date.parse(startDate+'T00:00:00Z'))/86400000)+1;
 if(dayCount<1)fail('From must be on or before To.');if(dayCount>366)fail('Select 366 calendar days or fewer.');
 const projectIds=selectionIds(singularProject,selectedProjects,new Set(rows(bundle.appProjects).map(item=>item.id))),chatIds=selectionIds(singularChat,selectedChats,new Set(rows(bundle.chats).map(item=>item.id)),true),projectSet=new Set(projectIds),chatSet=new Set(chatIds),projectId=projectIds.length===1?projectIds[0]:null,chatId=chatIds.length===1?chatIds[0]:null;
 if(projectIds.length&&!bundle.features?.projectFilter)fail('Project filtering is unavailable because the saved project attribution could not be verified.');
 const timestamp=new Date(now);if(Number.isNaN(timestamp.valueOf()))fail('The current date is unavailable.');
 const inWindow=row=>row.date>=startDate&&row.date<=endDate;
 const catalogOwners=rows(bundle.owners),ownerById=new Map(catalogOwners.map(owner=>[owner.id,owner]));
 const seen=new Set(),selected=[];
 for(const record of bundle.records){
  if(seen.has(record.key))fail('Saved records contain a duplicate identity. Run the local refresh command.');seen.add(record.key);
  if(!inWindow(record))continue;const owner=ownerById.get(record.ownerId);if(!owner)fail('Saved owner attribution is missing. Run the local refresh command.');
  if(chatIds.length&&!chatSet.has(owner.chatId))continue;
  if(projectIds.length){
   const parts=Object.fromEntries(Object.entries(record.projects||{}).filter(([id])=>projectSet.has(id)));if(!Object.keys(parts).length)continue;
   const modelParts=new Map(),modelsAvailable=bundle.features?.projectModels&&Object.keys(parts).every(id=>record.projectModels?.[id]&&typeof record.projectModels[id]==='object');if(modelsAvailable)for(const id of Object.keys(parts))for(const [model,values] of Object.entries(record.projectModels[id])){if(!modelParts.has(model))modelParts.set(model,[]);modelParts.get(model).push(values);}
   selected.push({...record,...sum(Object.values(parts)),models:modelsAvailable?Object.fromEntries([...modelParts].map(([id,values])=>[id,sum(values)])):null,projects:parts,...(bundle.projectView?{projectSources:Object.fromEntries(Object.keys(parts).map(id=>[id,record.projectSources?.[id]]))}:{})});
  }
  else selected.push(record);
 }
 const scoped=Boolean(projectIds.length||chatIds.length),byDate=new Map();for(const row of selected){if(!byDate.has(row.date))byDate.set(row.date,[]);byDate.get(row.date).push(row);}
 const daily=scoped?[...byDate].sort(([a],[b])=>a.localeCompare(b)).map(([date,items])=>({date,...sum(items)})):rows(bundle.daily).filter(inWindow).map(row=>({date:row.date,...sum([row])}));
 const totals=sum(daily);const selectedTotals=sum(selected);if(fields.some(field=>totals[field]!==selectedTotals[field]))fail('Saved daily and session counters disagree. Run the local refresh command.');
 const ownerParts=new Map();for(const row of selected){if(!ownerParts.has(row.ownerId))ownerParts.set(row.ownerId,[]);ownerParts.get(row.ownerId).push(row);}
 const share=value=>totals.totalTokens?value/totals.totalTokens*100:0;
 const owners=catalogOwners.filter(owner=>ownerParts.has(owner.id)).map(owner=>({...owner,...sum(ownerParts.get(owner.id)),sharePercent:share(sum(ownerParts.get(owner.id)).totalTokens)})).sort((a,b)=>b.totalTokens-a.totalTokens);
 const allCatalog=catalog(bundle),chatCatalog=new Map(rows(bundle.chats).map(chat=>[chat.id,chat])),chats=[...new Set(owners.map(owner=>owner.chatId))].map(id=>chatCatalog.get(id)).map(chat=>{
  const parts=owners.filter(owner=>owner.chatId===chat.id),records=selected.filter(row=>ownerById.get(row.ownerId).chatId===chat.id),days=new Map();for(const row of records){if(!days.has(row.date))days.set(row.date,[]);days.get(row.date).push(row);}const total=sum(parts),display=allCatalog.chats.find(item=>item.id===chat.id);
  return {...chat,...total,owners:parts.map(owner=>owner.id),daily:[...days].sort(([a],[b])=>a.localeCompare(b)).map(([date,items])=>({date,...sum(items)})),selfTokens:parts.filter(owner=>owner.kind==='self').reduce((s,row)=>s+row.totalTokens,0),agentsTokens:parts.filter(owner=>owner.kind==='agent').reduce((s,row)=>s+row.totalTokens,0),agentCount:parts.filter(owner=>owner.kind==='agent').length,responses:null,sharePercent:share(total.totalTokens),reviewAlias:display?.reviewAlias||null,reviewStartDate:display?.reviewStartDate||null,reviewEndDate:display?.reviewEndDate||null};
 }).sort((a,b)=>b.totalTokens-a.totalTokens);
 const warnings=rows(bundle.diagnostics?.warnings).filter(warning=>!warning.startsWith('The app cache is ')&&!warning.startsWith('The app scan timestamp is ahead '));const warn=message=>{if(!warnings.includes(message))warnings.push(message);};
 function ranking(kind){
  const source=kind==='models'?bundle.appModels:bundle.appProjects;
  if(!scoped){if(!bundle.features?.[kind]){warn('Scoped '+kind+' rankings are unavailable because independently verified attribution is missing.');return {available:false,items:[]};}const items=rows(source).map(item=>{const days=rows(item.daily).filter(inWindow);return {id:item.id,label:item.label||item.id,...sum(days),daily:days,sharePercent:share(sum(days).totalTokens),...(kind==='projects'&&bundle.projectView?sourceSummary(selected,item.id):{})};}).filter(item=>item.daily.length).sort((a,b)=>b.totalTokens-a.totalTokens||compare(a.id,b.id));return {available:true,items};}
  const capability=kind==='projects'?'sessionProjects':projectIds.length?'projectModels':'sessionModels';
  if(selected.length&&(!(projectIds.length&&kind==='projects')&&!bundle.features?.[capability]||selected.some(row=>!row[kind]))){warn('Scoped '+kind+' rankings are unavailable because independently verified attribution is missing.');return {available:false,items:[]};}
  const groups=new Map();for(const row of selected)for(const [id,counters] of Object.entries(row[kind]||{})){if(!groups.has(id))groups.set(id,[]);groups.get(id).push({date:row.date,...counters});}
  const items=[...groups].map(([id,items])=>{const days=new Map();for(const row of items){if(!days.has(row.date))days.set(row.date,[]);days.get(row.date).push(row);}const total=sum(items);return {id,label:kind==='projects'&&bundle.projectView&&Object.hasOwn(bundle.projectView.labels,id)?bundle.projectView.labels[id]:id,...total,daily:[...days].sort(([a],[b])=>a.localeCompare(b)).map(([date,values])=>({date,...sum(values)})),sharePercent:share(total.totalTokens),...(kind==='projects'&&bundle.projectView?sourceSummary(selected,id):{})};}).sort((a,b)=>b.totalTokens-a.totalTokens||compare(a.id,b.id));return {available:true,items};
 }
 const models=ranking('models'),projects=ranking('projects');
 const checked=[...daily,...owners,...models.items,...projects.items,...models.items.flatMap(row=>row.daily),...projects.items.flatMap(row=>row.daily)];
 if(checked.some(row=>row.inputTokens+row.outputTokens!==row.totalTokens))warn('The app total differs from input plus output; its recorded counters are preserved without adjustment.');
 if(checked.some(row=>row.cachedInputTokens>row.inputTokens||row.reasoningTokens>row.outputTokens))warn('A recorded subset exceeds its parent counter; app values are preserved without adjustment.');
 const chatLabel=rows(bundle.chats).find(chat=>chat.id===chatId)?.label||(chatId?'Chat '+chatId.slice(0,8):'');
 const projectLabel=bundle.projectView&&Object.hasOwn(bundle.projectView.labels,projectId)?bundle.projectView.labels[projectId]:projectId;
 const multiple=projectIds.length>1||chatIds.length>1;
 const scope=multiple?{kind:'selection',id:null,projectIds,chatIds,label:[projectIds.map(id=>bundle.projectView?.labels?.[id]||id).join(', '),chatIds.map(id=>rows(bundle.chats).find(chat=>chat.id===id)?.label||'Chat '+id.slice(0,8)).join(', ')].filter(Boolean).join(' • '),empty:selected.length===0}:{kind:projectId&&chatId?'project_chat':projectId?'project':chatId?'chat':'all',id:projectId&&chatId?null:projectId||chatId,label:projectId&&chatId?projectLabel+' • '+chatLabel:projectLabel||chatLabel||(bundle.projectView?'All chats and named projects':'All chats and project folders'),empty:selected.length===0};if(!multiple&&projectId&&chatId){scope.projectId=projectId;scope.chatId=chatId;}
 const comparisonKind=chatIds.length?'chats':projectIds.length?'projects':null,comparisonSeries=(chatIds.length?chatIds:projectIds).map(id=>{
  const parts=chatIds.length?selected.filter(row=>ownerById.get(row.ownerId).chatId===id):selected.filter(row=>Object.hasOwn(row.projects,id)).map(row=>({date:row.date,...row.projects[id]})),days=new Map();for(const row of parts){if(!days.has(row.date))days.set(row.date,[]);days.get(row.date).push(row);}
  return {id,label:chatIds.length?(allCatalog.chats.find(chat=>chat.id===id)?.displayLabel||'Chat '+id.slice(0,8)):(bundle.projectView?.labels?.[id]||allCatalog.projects.find(project=>project.id===id)?.label||id),...sum(parts),daily:[...days].sort(([a],[b])=>compare(a,b)).map(([date,values])=>({date,...sum(values)}))};
 });
 const dailyMap=new Map(daily.map(day=>[day.date,day])),days=Array.from({length:dayCount},(_,index)=>{const date=dayOffset(startDate,index),row=dailyMap.get(date);return {date,recorded:Boolean(row),...Object.fromEntries(fields.map(field=>[field,row?row[field]:null]))};});
 let currentDay=null,currentDayError=null;try{currentDay=today(bundle,timestamp);}catch(error){currentDayError=error.message;warn(currentDayError);}
 const partial=currentDay?startDate<=currentDay&&currentDay<=endDate:null;if(partial)warn('The selected period includes the current app calendar day; that day is partial and can change after the next app refresh.');
 const age=(timestamp.valueOf()-Date.parse(bundle.source?.lastScanAt))/1000;if(age< -120)warnings.unshift("The app scan timestamp is ahead of this computer's clock; check the clock and refresh local usage.");else if(age>900)warnings.unshift('The app cache is '+new Intl.NumberFormat('en-US').format(Math.floor(age/60))+' minutes old. Open Codex Usage Desktop, refresh local usage, then rerun this report.');
 return {schemaVersion:1,mode:'Codex Usage Desktop cache',generatedAt:timestamp.toISOString(),currentDay,currentDayError,scope,scopeNote:'Report scope: '+scope.label+'. '+caveat+(scope.empty?' No usage records match this scope and date range.':''),source:{...Object.fromEntries(Object.entries(bundle.source||{}).filter(([key])=>key!=='revisionSha256')),ageSeconds:Math.max(0,Math.floor(age))},
  window:{label:(startDate===endDate?startDate:startDate+' – '+endDate)+' • full app day'+(startDate!==endDate?'s':''),startDate,endDate,timeZone:bundle.source?.timeZone,precision:'day',currentAppDate:currentDay,includesCurrentAppDay:partial},totals:{...totals,responses:null,sessions:owners.length,uncachedInputTokens:totals.cachedInputTokens<=totals.inputTokens?totals.inputTokens-totals.cachedInputTokens:null},chats,owners,daily,selection:{projects:projectIds,chats:chatIds},comparison:{kind:comparisonKind,series:comparisonSeries},trend:{startDate,endDate,dayCount,timeZone:bundle.source?.timeZone,days},models:models.items,projects:projects.items,features:{models:models.available,projects:projects.available},diagnostics:{status:warnings.length?'incomplete':'complete',warnings},notes:[caveat,'App totals are shown as recorded. Cached input and reasoning are not added again.','The app cache has calendar-day precision; exact hourly or last-eight-hours totals are unavailable.','All adapter processing is local. No account quota or credit balance is inferred.'],cache:bundle.cache||{},...(bundle.projectView?{sourceFolders:sourceSummary(selected,projectId).sourceFolders,projectAttribution:{mode:'named-projects',unassignedId:bundle.projectView.unassignedId,...sourceSummary(selected,projectId)}}:{}),browserSnapshot:{generatedAt:bundle.generatedAt,numericComputedAt:bundle.numericComputedAt,coverage:bundle.coverage}};
}
global.CodexUsageData=Object.freeze({project,catalog,prepareProjectView,today,dayOffset,dateKey,fields:Object.freeze(fields.slice())});
})(typeof window==='undefined'?globalThis:window);
