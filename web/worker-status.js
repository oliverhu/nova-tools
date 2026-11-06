'use strict';
(function(root){
 const MINI_IDS=['oliverhu-mini-vm1','oliverhu-mini-vm2'];
 const NAMES={'oliverhu-mini-vm1':'Mini VM1','oliverhu-mini-vm2':'Mini VM2','oliverhu-mac-studio':'Studio',
  'oliverhu-mac-studio-vm1':'Studio VM1','oliverhu-mac-studio-vm2':'Studio VM2','oliver-mac':'Legacy Mac','oliver-mini':'Legacy Mini'};
 const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const finite=value=>typeof value==='number'&&Number.isFinite(value);
 function fresh(time,now){return finite(time)&&time>0&&now-time>=-5&&now-time<90;}
 function age(time,now){if(!finite(time)||time<=0||time-now>300)return 'unavailable';const seconds=Math.max(0,now-time);return seconds<60?'just now':seconds<3600?`${Math.floor(seconds/60)}m ago`:seconds<86400?`${Math.floor(seconds/3600)}h ago`:`${Math.floor(seconds/86400)}d ago`;}
 function blocker(task){
  if(!task)return '';
  const r=task.result||{};
  if(task.status==='queued')return task.ready===false?(task.waiting_reason||'Waiting for prerequisites'):'';
  if(!['held','blocked','failed','pending_external_approval'].includes(task.status))return '';
  return [r.requiredHumanAction,r.blocked_on,r.blocker,task.progress,r.summary,task.waiting_reason].find(v=>typeof v==='string'&&v.trim())||'';
 }
 function consistentCompleted(task){return task.status==='completed'&&task.result?.acceptance_complete!==false&&(!task.result?.outcome||task.result.outcome==='completed');}
 function project(worker,tasks,{now=Date.now()/1000,serviceAvailable=true}={}){
  const w=worker||{},mini=MINI_IDS.includes(w.id),cli=w.cli_status||{};
  const active=tasks.find(t=>t.owner===w.id&&['running','held'].includes(t.status))||null;
  const runTask=tasks.find(t=>t.id===cli.task_id)||null;
  const completed=tasks.filter(t=>(t.last_owner||t.owner)===w.id&&consistentCompleted(t)).sort((a,b)=>(b.updated||0)-(a.updated||0))[0]||null;
  const reporterOnline=w.online===true&&fresh(w.seen,now);
  const cliFresh=fresh(w.cli_status_seen,now),cliKnown=cli.available===true&&cliFresh;
  let label,tone;
  if(!serviceAvailable){label='Status unavailable';tone='offline';}
  else if(!reporterOnline){label='Worker unreachable';tone='offline';}
  else if(w.paused){label='Paused';tone='held';}
  else if(mini&&!cliKnown){label='CLI status unavailable';tone='offline';}
  else if(active){label=mini&&cli.busy!==true?'Assigned · CLI not observed':active.status==='held'?'Task held':'Task running';tone=mini&&cli.busy!==true?'held':'running';}
  else if(!mini){label='No active queue task';tone='completed';}
  else if(['needs_attention','needs_reconciliation','unknown','continued'].includes(cli.run_state)){
   label=cli.busy?'CLI busy · state unconfirmed':'Needs attention';tone='held';
  }else if(cli.busy){label=cli.run_state==='starting'?'CLI starting · no claim':'CLI running · no claim';tone='running';}
  else if(cli.run_state==='completed'&&cli.completion_verified!==true){label='Completion needs review';tone='held';}
  else{label='Idle';tone='completed';}
  const ownRunTask=runTask&&(runTask.owner||runTask.last_owner)===w.id?runTask:null;
  const progressTask=active||ownRunTask||completed;
  const progress=active?.progress||progressTask?.result?.summary||progressTask?.progress||'';
  const issue=blocker(active)||blocker(ownRunTask)||(mini&&cliKnown&&['needs_attention','needs_reconciliation'].includes(cli.run_state)?'CLI run needs reconciliation or attention; its latest result is not a current running task.':'');
  const queued=tasks.filter(t=>t.status==='queued'&&(t.requirements?.worker===w.id||t.next_worker===w.id)).length;
  return {id:w.id,name:NAMES[w.id]||w.id,mini,label,tone,active,runTask,completed,progress,issue,queued,
   reporterOnline,cliKnown,cliFresh,serviceAvailable,capacity:w.capacity||{},paused:!!w.paused,
   seen:w.seen,statusSeen:w.cli_status_seen,cli,now};
 }
 function taskLink(task){
  if(!task)return '<span class="worker-muted">None recorded</span>';
  const id=String(task.id||'');
  const title=escape(task.title||id);
  return `${/^[0-9a-f]{16}$/.test(id)?`<button class="worker-task-link" data-task="${id}">${title}</button>`:`<span>${title}</span>`}<code>${escape(id)}</code>`;
 }
 function renderCard(w,tasks,options){
  const v=project(w,tasks,options),cap=v.capacity;
  const run=v.cli.task_id?`<span>${escape(String(v.cli.run_state||'unknown').replaceAll('_',' '))}${v.cli.task_generation!=null?` · generation ${escape(v.cli.task_generation)}`:''}</span><code>${escape(v.cli.task_id)}</code>`:`<span>${v.cliKnown?'No run recorded':'Run details unavailable'}</span>`;
  const active=v.active?`${taskLink(v.active)}<small>Queue: ${escape(v.active.status)}${v.active.heartbeat_stale?' · heartbeat stale; ownership retained':''}</small>`:'<span class="worker-muted">No current queue assignment</span>';
  return `<article class="worker ${v.mini?'worker-mini':''}" data-worker="${escape(v.id)}" aria-label="${escape(v.name)} status">`+
   `<div class="worker-top"><div class="device" aria-hidden="true">${v.mini?'▣':'▱'}</div><div><h3 class="worker-name">${escape(v.name)}</h3><div class="worker-type">${escape(v.id)}</div></div><span class="badge ${escape(v.tone)}">${escape(v.label)}</span></div>`+
   `<div class="worker-connectivity">${!v.serviceAvailable?'Service unavailable · cached records':v.reporterOnline?'Resource reporter connected':'No current resource report'}${v.paused?' · Pickup paused':''}</div>`+
   `<div class="capacity"><span>${escape(cap.cpus??'—')}<small>CPU CORES</small></span><span>${escape(cap.memory_gb??'—')} GB<small>MEMORY</small></span><span>${escape(cap.disk_gb??'—')} GB<small>USABLE BUILD SPACE</small></span></div>`+
   `<div class="worker-detail"><h4>Current queue task</h4>${active}</div>`+
   (v.mini?`<div class="worker-detail"><h4>Latest CLI run</h4>${run}<small>${v.cliKnown?'CLI status checked '+age(v.statusSeen,v.now):'CLI status unavailable or stale; no idle inference'}${v.runTask?` · Queue now: ${escape(v.runTask.status)}`:''}</small></div>`:'')+
   `<div class="worker-detail"><h4>${v.active?'Current progress':'Latest recorded progress / result'}</h4><p>${escape(v.progress?v.progress.slice(0,1000):'No public progress recorded yet.')}</p></div>`+
   (v.issue?`<div class="worker-blocker"><b>Blocker / attention</b><p>${escape(v.issue.slice(0,1000))}</p></div>`:'')+
   `<div class="worker-detail worker-history"><h4>Latest completed queue task · history</h4>${taskLink(v.completed)}${v.completed?`<small>Completed ${age(v.completed.updated,v.now)} · not a current run</small>`:''}</div>`+
   `<div class="worker-bottom"><span>${v.queued} in recent queue for this worker · not running</span><span>Reporter ${age(v.seen,v.now)}</span></div></article>`;
 }
 const api={MINI_IDS,project,renderCard};
 if(typeof module!=='undefined'&&module.exports)module.exports=api;
 else root.NovaWorkerStatus=api;
})(globalThis);
