"""Local client. Both Macs use the same protocol and have equal permissions."""
import argparse
import json
import math
import os
from pathlib import Path
import shutil
import signal
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import tempfile
from .approvals import approval_payload, approval_id
from .outcomes import FINISH_OUTCOMES, finish_outcome


CONFIG=Path.home()/".config/nova-coordinator/client.json"


class ServiceRejected(RuntimeError):
    def __init__(self, status, message):
        self.status=status
        super().__init__(message)


class LocalResourceDenied(RuntimeError):
    """The sandbox denied a local memory query; no capacity was reported."""


class Client:
    def __init__(self, path=CONFIG):
        self.path=Path(path)
        self.config=json.loads(self.path.read_text())
        url=self.config["url"]
        if not url.startswith("https://") and not url.startswith("http://127.0.0.1:"):
            raise ValueError("Use HTTPS or loopback")
        ctx=ssl.create_default_context(cafile=self.config.get("ca_file"))
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=ctx))
        self.state=self.path.parent/"claims"
        self.state.mkdir(mode=0o700,exist_ok=True)

    def request(self,path,body=None):
        request=urllib.request.Request(self.config["url"].rstrip("/")+path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization":"Bearer "+self.config["token"],"Content-Type":"application/json"})
        try:
            with self.opener.open(request,timeout=15) as response:
                return json.load(response)
        except urllib.error.HTTPError as e:
            raise ServiceRejected(e.code,f"Service rejected request ({e.code}): {e.read().decode()}") from None

    def pulse(self):
        try:
            memory=int(subprocess.check_output(["/usr/sbin/sysctl","-n","hw.memsize"], stderr=subprocess.PIPE))/(1024**3) if sys.platform=="darwin" else 0
        except subprocess.CalledProcessError as error:
            if error.stderr and any(reason in error.stderr for reason in (b'Operation not permitted', b'Permission denied')):
                raise LocalResourceDenied('Local memory query denied; require fresh existing reporter evidence') from None
            raise
        cpus = os.cpu_count() or 1
        limits = self.config.get("capacity_limits", {})
        for key, value in limits.items():
            if key not in ("cpus", "memory_gb") or type(value) not in (int, float) or value <= 0:
                raise ValueError("Invalid worker capacity limit")
        memory = min(memory, limits.get("memory_gb", memory))
        cpus = min(cpus, limits.get("cpus", cpus))
        build_path=self.config.get("build_path",str(Path.home()))
        # Missing external storage must not be substituted with internal disk.
        disk=shutil.disk_usage(build_path).free/(1024**3) if Path(build_path).is_dir() else 0
        reserve=self.config.get("disk_reserve_gb",50)
        return self.request("/api/worker/pulse",{"capacity":{"memory_gb":round(memory,1),"cpus":cpus,
            "disk_gb":round(max(0,disk-reserve),1),"capabilities":self.config.get("capabilities",["macos","development"])}})

    def require_fresh_reporter(self):
        """Read existing own reporter evidence without writing substitute capacity."""
        workers = [w for w in self.request('/api/state')['workers'] if w.get('id') == self.config['worker']]
        if len(workers) != 1:
            raise RuntimeError('Missing unique own resource reporter')
        worker = workers[0]; seen = worker.get('seen'); now = time.time()
        if (worker.get('online') is not True or worker.get('paused')
                or type(seen) not in (int, float) or not math.isfinite(seen) or not -5 <= now-seen < 90):
            raise RuntimeError('Own resource reporter is unavailable or stale')
        capacity = worker.get('capacity', {}); limits = self.config.get('capacity_limits', {})
        if not isinstance(capacity, dict) or not isinstance(limits, dict):
            raise ValueError('Invalid reported capacity or configured limits')
        for key, value in limits.items():
            if (key not in ('cpus', 'memory_gb') or type(value) not in (int, float)
                    or not math.isfinite(value) or value <= 0):
                raise ValueError('Invalid worker capacity limit')
        for key in ('memory_gb', 'cpus', 'disk_gb'):
            value = capacity.get(key)
            if (type(value) not in (int, float) or not math.isfinite(value)
                    or value < (0 if key == 'disk_gb' else 1)
                    or value > limits.get(key, 100000)):
                raise RuntimeError('Own reported resources are unknown or exceed configured limits')
        caps = capacity.get('capabilities')
        if (not isinstance(caps, list) or not all(isinstance(x, str) for x in caps)
                or not set(caps).issubset(self.config.get('capabilities', ['macos', 'development']))):
            raise RuntimeError('Reported capabilities differ from current worker configuration')
        return {'source': 'fresh_existing_reporter', 'worker': self.config['worker'],
                'seen': seen, 'local_memory_query_denied': True}

    def claim(self,ident=None):
        # Recover only decisions already explicitly verified and journaled locally.
        recovered=self.recover_approvals()
        resource_readiness = None
        try:
            self.pulse()
        except LocalResourceDenied:
            resource_readiness = self.require_fresh_reporter()
        body = {"task_id": ident}
        from .workers import MINI_VM_WORKERS
        if getattr(self, 'config', {}).get('worker') in MINI_VM_WORKERS:
            from .cli_messages import own_run, ROOT
            from .cli_event_dispatch import claim_binding
            binding = claim_binding(ROOT, own_run(ROOT))
            if binding is not None:
                body['dispatch_binding'] = binding
        result=self.request("/api/worker/claim",body)
        if resource_readiness:result['resource_readiness']=resource_readiness
        if recovered:result['approval_recovery']=recovered
        if result.get("task"):
            path=self.state/(result["task"]["id"]+".json")
            fd=os.open(path,os.O_CREAT|os.O_TRUNC|os.O_WRONLY,0o600)
            with os.fdopen(fd,"w") as f:
                json.dump(result,f)
        return result

    def claim_body(self,ident):
        value=json.loads((self.state/(ident+".json")).read_text())
        return {"claim_token":value["claim_token"],"generation":value["task"]["generation"],"step":value["task"]["step"]}

    def heartbeat(self,ident,progress=None,phase=None):
        body=self.claim_body(ident)
        if progress is not None:
            body["progress"]=progress
        if phase is not None:
            body['phase'] = phase
        return self.request(f"/api/tasks/{ident}/heartbeat",body)

    def finish(self,ident,receipt,outcome=None):
        outcome = finish_outcome(receipt, outcome)
        return self.request(f"/api/tasks/{ident}/finish",{**self.claim_body(ident),"receipt":receipt,"outcome":outcome})

    @staticmethod
    def save_approval(path, value, create=False):
        path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.approval-',dir=path.parent)
        try:
            with os.fdopen(fd,'w') as out:
                json.dump(value,out,sort_keys=True);out.flush();os.fsync(out.fileno())
            if create:
                try:os.link(temporary,path)
                except FileExistsError:pass
            else:os.replace(temporary,path)
            directory=os.open(path.parent,os.O_RDONLY)
            try:os.fsync(directory)
            finally:os.close(directory)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def apply_approval(self, receipt):
        payload=approval_payload(receipt)
        ident=approval_id(payload)
        path=self.path.parent/'approvals'/(ident+'.json')
        if not path.exists():
            body={**payload,'tasks':[]}
            for task in payload['tasks']:
                identity=self.claim_body(task['id'])
                if identity['generation'] != task['generation']:
                    raise RuntimeError('Local claim generation differs from approval scope: '+task['id'])
                body['tasks'].append({**task,'claim_token':identity['claim_token']})
            # Persist the exact decision/scope/claims before any network mutation.
            self.save_approval(path,{'state':'pending','request':body},create=True)
        journal=json.loads(path.read_text())
        if approval_payload(journal['request']) != payload:
            raise RuntimeError('Approval source already journaled with different scope/evidence')
        return self.deliver_approval(path,journal)

    def deliver_approval(self, path, journal):
        payload=approval_payload(journal['request']);ident=approval_id(payload)
        if journal['state']=='needs_review':
            raise RuntimeError('Approval requires scope/ownership review; inspect local journal '+str(path))
        if journal['state'] != 'applied':
            try:self.request('/api/approvals/apply',journal['request'])
            except ServiceRejected as error:
                if 400 <= error.status < 500:
                    self.save_approval(path,{**journal,'state':'needs_review','error':str(error)})
                raise
        # A POST or a chat acknowledgment is insufficient: read current service state.
        verified=self.request('/api/approvals/'+ident)
        expected=sorted((t['id'],t['generation']) for t in payload['tasks'])
        observed=sorted((t['id'],t['applied_generation']) for t in verified.get('tasks',[]))
        if verified.get('approval_id') != ident or verified.get('recorded') is not True or observed != expected:
            raise RuntimeError('Approval readback mismatch; journal retained for reconciliation')
        self.save_approval(path,{**journal,'state':'applied','readback':verified})
        return verified

    def recover_approvals(self):
        results=[]
        for path in sorted((self.path.parent/'approvals').glob('*.json')):
            journal=json.loads(path.read_text())
            if journal.get('state')=='pending':
                try:results.append(self.deliver_approval(path,journal))
                except ServiceRejected as error:
                    if not 400 <= error.status < 500:raise
                    results.append({'approval_id':approval_id(journal['request']),'requires_review':True,'error':str(error)})
        return results


def supervised(client,ident,command,env=None,heartbeat_interval=25):
    """A lease watchdog kills the owned process group on heartbeat failure.

    The service retains ownership on unknown/failed outcomes. Killing a local
    process never proves an already-issued remote side effect was cancelled.
    """
    client.heartbeat(ident,"Executing supervised command")
    stop=threading.Event()
    lost=[]
    process=subprocess.Popen(command,start_new_session=True,env=env)
    def kill_group():
        try:
            os.killpg(process.pid,signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError:
            pass
    def watch():
        while not stop.wait(heartbeat_interval):
            try:
                client.heartbeat(ident)
            except Exception:
                lost.append(True)
                kill_group()
                return
    thread=threading.Thread(target=watch,daemon=True)
    thread.start()
    try:
        code=process.wait()
    except BaseException:
        kill_group()
        raise
    finally:
        stop.set(); thread.join(timeout=20)
    if lost:
        raise RuntimeError("Lost coordination service; owned process stopped. Task ownership remains unchanged; inspect and report the stopped attempt.")
    try:
        os.killpg(process.pid,0)
    except ProcessLookupError:
        pass
    else:
        kill_group()
        raise RuntimeError('Command left background children; they were stopped and the task requires reconciliation')
    # Do not mark complete automatically: the caller supplies actual evidence.
    return code


def observe_main(client, snapshot=None):
    """Report actual remote main with existing host Git credentials; no fetch/write."""
    repo = client.config.get("repository")
    if not repo:
        return {"observed": False, "reason": "Repository checkout is not configured"}
    started = time.time()
    branch = repo.get("branch", "main")
    result = subprocess.run(["git", "-C", repo["path"], "ls-remote", "--exit-code", repo.get("remote", "origin"), "refs/heads/"+branch],
        capture_output=True, text=True, timeout=25, env={**os.environ,"GIT_TERMINAL_PROMPT":"0"})
    if result.returncode:
        raise RuntimeError("Remote main observation failed; previous release state unchanged")
    lines = [line.split() for line in result.stdout.splitlines()]
    heads = [line[0] for line in lines if len(line)==2 and line[1]=="refs/heads/"+branch]
    if len(heads)!=1:
        raise RuntimeError("Remote main observation was ambiguous")
    body = {"repository":repo["name"],"branch":branch,"commit":heads[0],"observed_at":started}
    from .publication_ancestry import attestation
    proof = attestation(repo, heads[0], snapshot)
    if proof is not None:
        body['publication_ancestry'] = proof
    return client.request("/api/repositories/observe", body)


def publish_permit(client,ident):
    claim=client.claim_body(ident)
    task=client.request('/api/tasks/'+ident)
    if (task['status']!='running' or task['owner']!=client.config['worker'] or
        task['generation']!=claim['generation'] or task['current_step']['kind']!='publish'):
        raise RuntimeError('A current hourly publish task claim is required')
    client.heartbeat(ident,'Verified publish task ownership (no global lock)')
    return {'task_id':ident,'generation':claim['generation'],'worker':client.config['worker'],'exclusive':False}


def cli_followup(client, ident):
    """Only foreground CLI tool boundaries deliver; watchdog threads never do."""
    if not os.environ.get('CODEX_THREAD_ID'):
        return None
    from .cli_messages import receive
    try:
        return receive(client, ident)
    except Exception as error:
        # Preserve the command/heartbeat outcome and leave messages unacknowledged.
        # Do not print exception data which might contain unrelated local content.
        return {'status': 'delivery_unavailable', 'error_type': type(error).__name__,
                'notice': 'Follow-ups remain unacknowledged; inspect message-status before retrying.'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config",type=Path,default=CONFIG)
    sub=parser.add_subparsers(dest="command",required=True)
    sub.add_parser("status")
    sub.add_parser("pulse")
    add=sub.add_parser("add")
    add.add_argument("title",nargs="?")
    add.add_argument("--description")
    add.add_argument("--task-type",choices=["work","engine_rebuild"],default="work")
    add.add_argument("--compute",choices=["light","standard","heavy"],default="standard")
    add.add_argument("--priority",type=int,default=50)
    add.add_argument("--key")
    add.add_argument("--file",type=Path)
    claim=sub.add_parser("next");claim.add_argument("--task")
    detail=sub.add_parser("show");detail.add_argument("task")
    beat=sub.add_parser("heartbeat");beat.add_argument("task");beat.add_argument("--progress")
    beat.add_argument('--phase', choices=('implementing', 'testing', 'cleanup'))
    done=sub.add_parser("finish");done.add_argument("task");done.add_argument("--receipt",type=Path,required=True);done.add_argument("--outcome",choices=FINISH_OUTCOMES,help="Defaults to the explicit receipt outcome, otherwise completed; contradictions are rejected")
    run=sub.add_parser("run");run.add_argument("task");run.add_argument("args",nargs=argparse.REMAINDER)
    publish=sub.add_parser("publish");publish.add_argument("task");publish.add_argument("--receipt",type=Path,required=True);publish.add_argument("--command",dest="publish_command",nargs=argparse.REMAINDER,required=True)
    permit=sub.add_parser("publish-permit");permit.add_argument("task",nargs="?",default=os.environ.get("NOVA_COORD_TASK_ID"))
    reconcile=sub.add_parser("reconcile");reconcile.add_argument("task");reconcile.add_argument("--file",type=Path,required=True)
    correct=sub.add_parser("correct-outcome",help="Admin-only correction of an evidence-bound erroneous completion")
    correct.add_argument("task");correct.add_argument("--receipt",type=Path,required=True)
    attempts=sub.add_parser("correct-attempts",help="Admin-only monotonic correction of verified omitted attempts")
    attempts.add_argument("task");attempts.add_argument("--receipt",type=Path,required=True)
    retry=sub.add_parser("retry");retry.add_argument("task");retry.add_argument("--receipt",type=Path,required=True)
    approve=sub.add_parser('approve',help='Apply one verified human decision to its explicitly covered pending tasks')
    approve.add_argument('--receipt',type=Path,required=True)
    exception=sub.add_parser('attempt-exception',help='Bind one explicitly verified extra attempt to an applied human approval')
    exception.add_argument('--receipt',type=Path,required=True)
    allowance=sub.add_parser('attempt-allowance',help='Admin-only finite current-host allowance from an explicit human decision')
    allowance.add_argument('--receipt',type=Path,required=True)
    amend=sub.add_parser('attempt-allowance-amend',help='Admin-only extension from a distinct new primary human authorization')
    amend.add_argument('--receipt',type=Path,required=True)
    sub.add_parser('approval-recover',help='Reconcile interrupted, already verified local approval receipts')
    approval_status=sub.add_parser('approval-status',help='Read an applied approval and current task eligibility')
    approval_status.add_argument('approval_id')
    wait=sub.add_parser("wait",help="Queue settled work until its worker/prerequisites are ready")
    wait.add_argument("task");wait.add_argument("--receipt",type=Path,required=True)
    sub.add_parser("observe-main")
    daemon=sub.add_parser("agent");daemon.add_argument("--interval",type=int,default=30)
    ui=sub.add_parser("ui");ui.add_argument("--port",type=int,default=18766)
    args=parser.parse_args()
    client=Client(args.config)
    if args.command=="status":result=client.request("/api/state")
    elif args.command=="show":result=client.request("/api/tasks/"+args.task)
    elif args.command=="pulse":result=client.pulse()
    elif args.command=="add":
        body=json.loads(args.file.read_text()) if args.file else {"title":args.title,"description":args.description or args.title,"compute":args.compute,"task_type":args.task_type,"priority":args.priority}
        body.setdefault("request_key",args.key or uuid.uuid4().hex)
        result=client.request("/api/tasks",body)
    elif args.command=="next":
        result=client.claim(args.task)
        result.pop("claim_token",None)
        if result.get("task"):
            result["task"]=client.request("/api/tasks/"+result["task"]["id"])
    elif args.command=="heartbeat":
        result=client.heartbeat(args.task,args.progress,phase=args.phase)
        message=cli_followup(client,args.task)
        if message is not None:result['cli_followup']=message
    elif args.command=="finish":result=client.finish(args.task,json.loads(args.receipt.read_text()),args.outcome)
    elif args.command=="reconcile":result=client.request(f"/api/tasks/{args.task}/reconcile",json.loads(args.file.read_text()))
    elif args.command=="correct-outcome":result=client.request(f"/api/tasks/{args.task}/correct-outcome",json.loads(args.receipt.read_text()))
    elif args.command=="correct-attempts":result=client.request(f"/api/tasks/{args.task}/correct-attempts",json.loads(args.receipt.read_text()))
    elif args.command=="retry":
        receipt=json.loads(args.receipt.read_text())
        result=client.request(f"/api/tasks/{args.task}/retry", {**receipt, **client.claim_body(args.task)})
    elif args.command=='approve':result=client.apply_approval(json.loads(args.receipt.read_text()))
    elif args.command=='attempt-exception':
        receipt=json.loads(args.receipt.read_text())
        result=client.request('/api/attempt-exceptions/apply',receipt)
        observed=client.request('/api/tasks/'+receipt['task'])
        if observed.get('attempt_exception',{}).get('request') != receipt:
            raise RuntimeError('Attempt exception readback did not match; inspect before executing')
        result['task']=observed
    elif args.command in ('attempt-allowance','attempt-allowance-amend'):
        receipt=json.loads(args.receipt.read_text())
        amendment=args.command=='attempt-allowance-amend'
        result=client.request('/api/attempt-allowances/'+('amend' if amendment else 'apply'),receipt)
        observed=client.request('/api/tasks/'+receipt['task'])
        grant_id=result['grant']['id']
        history='attempt_allowance_amendments' if amendment else 'attempt_allowances'
        if not any(g['id']==grant_id and g['request']==receipt for g in observed.get(history,[])):
            raise RuntimeError('Attempt allowance audit readback did not match; inspect before executing')
        result['task']=observed
    elif args.command=='approval-recover':result={'recovered':client.recover_approvals()}
    elif args.command=='approval-status':result=client.request('/api/approvals/'+args.approval_id)
    elif args.command=="wait":
        receipt=json.loads(args.receipt.read_text())
        result=client.request(f"/api/tasks/{args.task}/defer", {**receipt, **client.claim_body(args.task)})
    elif args.command=="observe-main":result=observe_main(client, snapshot=client.request('/api/state'))
    elif args.command=="publish-permit":
        if not args.task:parser.error('NOVA_COORD_TASK_ID or task ID is required')
        result=publish_permit(client,args.task)
    elif args.command=="publish":
        if not args.publish_command:parser.error('Specify the publisher command after --command')
        if args.receipt.exists():parser.error('Use a new receipt path; existing evidence cannot prove a new publication')
        publish_permit(client,args.task)
        # The child must write a receipt describing a verified external result.
        env={**os.environ,'NOVA_COORD_TASK_ID':args.task,'NOVA_COORD_CONFIG':str(args.config)}
        try:
            code=supervised(client,args.task,args.publish_command,env=env)
        except Exception:
            # Unknown side effects retain the lock. Never auto-complete/undo.
            raise
        if code:
            client.finish(args.task,{'summary':f'Publisher exited {code}; external outcome requires reconciliation','execution_stopped':True},'failed')
            sys.exit(code)
        receipt=json.loads(args.receipt.read_text())
        result=client.finish(args.task,receipt)
    elif args.command=="run":
        command=args.args[1:] if args.args[:1]==["--"] else args.args
        if not command:parser.error("Specify a command after --")
        code=supervised(client,args.task,command)
        message=cli_followup(client,args.task)
        if message is not None:print(json.dumps({'cli_followup':message}),flush=True)
        sys.exit(code)
    elif args.command=="ui":
        from .gateway import serve
        serve(client,args.port);return
    elif args.command=="agent":
        # Keep the existing capacity/inbox schedule. An explicitly enrolled Mini
        # also has one event subscriber; it uses the normal fenced CLI launcher.
        from .cli_event_dispatch import start_listener
        start_listener(client)
        last_observation=0
        while True:
            try:
                client.pulse()
                snapshot=client.request("/api/state")
                if time.monotonic()-last_observation >= 120:
                    observe_main(client, snapshot=snapshot)
                    last_observation=time.monotonic()
                inbox={"updated":time.time(),"worker":client.config["worker"],
                    "tasks":[t for t in snapshot["tasks"] if t.get("preferred_worker")==client.config["worker"] or t.get("owner")==client.config["worker"]]}
                path=client.path.parent/"inbox.json"
                temp=path.with_suffix(".tmp");temp.write_text(json.dumps(inbox,indent=2));temp.chmod(0o600);temp.replace(path)
            except Exception as e:
                print(type(e).__name__+": coordinator unavailable; no ownership changes",file=sys.stderr,flush=True)
            time.sleep(max(10,args.interval))
    print(json.dumps(result,indent=2))


if __name__=="__main__":
    try:
        main()
    except (OSError,ValueError,RuntimeError) as e:
        print(str(e),file=sys.stderr)
        sys.exit(1)
