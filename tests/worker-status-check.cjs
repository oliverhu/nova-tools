'use strict';
const assert=require('node:assert/strict');
const {MINI_IDS,project,renderCard}=require('../web/worker-status.js');
const id=MINI_IDS[0],taskID='a'.repeat(16),oldID='b'.repeat(16),now=1000;
const worker=()=>({id,online:true,seen:995,paused:false,capacity:{cpus:5,memory_gb:8,disk_gb:100},cli_status_seen:995,
 cli_status:{available:true,busy:false,run_state:'completed',task_id:oldID,task_generation:1,completion_verified:true}});
const completed={id:oldID,title:'Previous completed job',status:'completed',last_owner:id,updated:900,result:{summary:'Prior verified result',outcome:'completed',acceptance_complete:true}};
const current={id:taskID,title:'Current implementation',status:'running',owner:id,updated:990,progress:'Checking authored native UI',result:{}};
let checks=0;
function check(name,fn){fn();checks++;}
check('fresh completed history is idle, not a running task',()=>{const v=project(worker(),[completed],{now});assert.equal(v.label,'Idle');assert.equal(v.active,null);assert.equal(v.completed.id,oldID);});
check('active queue and latest completed stay separate',()=>{const w=worker();w.cli_status={...w.cli_status,busy:true,run_state:'running',task_id:taskID,completion_verified:false};const v=project(w,[current,completed],{now});assert.equal(v.label,'Task running');assert.equal(v.active.id,taskID);assert.equal(v.completed.id,oldID);assert.equal(v.progress,current.progress);});
check('unreachable worker is never idle even if latest run completed',()=>{const w=worker();w.online=false;assert.equal(project(w,[completed],{now}).label,'Worker unreachable');});
check('stale resource report is never idle',()=>{const w=worker();w.seen=800;assert.equal(project(w,[completed],{now}).label,'Worker unreachable');});
check('fresh capacity cannot refresh missing or stale CLI status',()=>{for(const seen of [null,800]){const w=worker();w.cli_status_seen=seen;assert.equal(project(w,[completed],{now}).label,'CLI status unavailable');}});
check('unavailable local status is never idle',()=>{const w=worker();w.cli_status.available=false;assert.equal(project(w,[completed],{now}).label,'CLI status unavailable');});
check('service error makes cached idle unavailable',()=>{assert.equal(project(worker(),[completed],{now,serviceAvailable:false}).label,'Status unavailable');});
check('paused stays paused despite fresh reporter',()=>{const w=worker();w.paused=true;assert.equal(project(w,[completed],{now}).label,'Paused');});
check('unclaimed CLI startup is not a current queue assignment',()=>{const w=worker();w.cli_status={...w.cli_status,busy:true,run_state:'starting',task_id:taskID};const t={...current,status:'queued',owner:null,requirements:{worker:id}};const v=project(w,[t,completed],{now});assert.equal(v.active,null);assert.equal(v.queued,1);assert.equal(v.label,'CLI starting · no claim');});
check('assigned task never becomes idle from local lock observation',()=>{const v=project(worker(),[current],{now});assert.equal(v.label,'Assigned · CLI not observed');assert.equal(v.active.id,taskID);});
check('unreconciled run is attention, not idle',()=>{const w=worker();w.cli_status.run_state='needs_reconciliation';assert.equal(project(w,[],{now}).label,'Needs attention');});
check('explicit incomplete result is not latest completed work',()=>{assert.equal(project(worker(),[{...completed,result:{acceptance_complete:false}}],{now}).completed,null);});
check('blocker is public queue evidence',()=>{const w=worker();w.cli_status={...w.cli_status,run_state:'needs_attention',task_id:taskID};const t={...current,owner:null,last_owner:id,status:'pending_external_approval',result:{requiredHumanAction:'Await exact candidate approval'}};assert.equal(project(w,[t],{now}).issue,'Await exact candidate approval');});
check('another owners new generation is not this workers progress',()=>{const w=worker();w.cli_status.task_id=taskID;const t={...current,owner:'another-worker',last_owner:'another-worker'};assert.notEqual(project(w,[t],{now}).progress,t.progress);});
check('rendered status escapes all task text',()=>{const malicious={...current,title:'<img src=x onerror=alert(1)>',progress:'<script>PRIVATE</script>'};const html=renderCard(worker(),[malicious,completed],{now});assert(!html.includes('<img src=x'));assert(!html.includes('<script>PRIVATE'));assert(html.includes('&lt;script&gt;'));});
check('task ID, completed history, freshness and queued counts are visible',()=>{const html=renderCard(worker(),[current,completed],{now});for(const text of [taskID,oldID,'Latest completed queue task','Reporter','not running','Current queue task'])assert(html.includes(text));});
console.log(JSON.stringify({passed:true,checks,scope:'Production worker status projection/rendering with authored data; no browser or live queue mutation.'}));
