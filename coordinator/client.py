"""Local client. Both Macs use the same protocol and have equal permissions."""
import argparse
import json
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


CONFIG=Path.home()/".config/nova-coordinator/client.json"


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
            raise RuntimeError(f"Service rejected request ({e.code}): {e.read().decode()}") from None

    def pulse(self):
        memory=int(subprocess.check_output(["sysctl","-n","hw.memsize"]))/(1024**3) if sys.platform=="darwin" else 0
        build_path=self.config.get("build_path",str(Path.home()))
        # Missing external storage must not be substituted with internal disk.
        disk=shutil.disk_usage(build_path).free/(1024**3) if Path(build_path).is_dir() else 0
        reserve=self.config.get("disk_reserve_gb",50)
        return self.request("/api/worker/pulse",{"capacity":{"memory_gb":round(memory,1),"cpus":os.cpu_count() or 1,
            "disk_gb":round(max(0,disk-reserve),1),"capabilities":self.config.get("capabilities",["macos","development"])}})

    def claim(self,ident=None):
        self.pulse()
        result=self.request("/api/worker/claim",{"task_id":ident})
        if result.get("task"):
            path=self.state/(result["task"]["id"]+".json")
            fd=os.open(path,os.O_CREAT|os.O_TRUNC|os.O_WRONLY,0o600)
            with os.fdopen(fd,"w") as f:
                json.dump(result,f)
        return result

    def claim_body(self,ident):
        value=json.loads((self.state/(ident+".json")).read_text())
        return {"claim_token":value["claim_token"],"generation":value["task"]["generation"],"step":value["task"]["step"]}

    def heartbeat(self,ident,progress=None):
        body=self.claim_body(ident)
        if progress is not None:
            body["progress"]=progress
        return self.request(f"/api/tasks/{ident}/heartbeat",body)

    def finish(self,ident,receipt,outcome="completed"):
        return self.request(f"/api/tasks/{ident}/finish",{**self.claim_body(ident),"receipt":receipt,"outcome":outcome})


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
        raise RuntimeError("Lost coordination service; owned process stopped. The task is held for reconciliation.")
    try:
        os.killpg(process.pid,0)
    except ProcessLookupError:
        pass
    else:
        kill_group()
        raise RuntimeError('Command left background children; they were stopped and the task requires reconciliation')
    # Do not mark complete automatically: the caller supplies actual evidence.
    return code


def publish_permit(client,ident):
    claim=client.claim_body(ident)
    task=client.request('/api/tasks/'+ident)
    state=client.request('/api/state')
    if (task['status']!='running' or task['owner']!=client.config['worker'] or
        task['generation']!=claim['generation'] or task['current_step']['kind']!='publish' or
        not any(l['resource']=='publish' and l['task_id']==ident and l['generation']==claim['generation'] for l in state['locks'])):
        raise RuntimeError('A current exclusive publish claim is required')
    client.heartbeat(ident,'Verified exclusive publish permit')
    return {'task_id':ident,'generation':claim['generation'],'worker':client.config['worker'],'exclusive':True}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config",type=Path,default=CONFIG)
    sub=parser.add_subparsers(dest="command",required=True)
    sub.add_parser("status")
    sub.add_parser("pulse")
    add=sub.add_parser("add")
    add.add_argument("title",nargs="?")
    add.add_argument("--description")
    add.add_argument("--compute",choices=["light","standard","heavy"],default="standard")
    add.add_argument("--priority",type=int,default=50)
    add.add_argument("--key")
    add.add_argument("--file",type=Path)
    claim=sub.add_parser("next");claim.add_argument("--task")
    detail=sub.add_parser("show");detail.add_argument("task")
    beat=sub.add_parser("heartbeat");beat.add_argument("task");beat.add_argument("--progress")
    done=sub.add_parser("finish");done.add_argument("task");done.add_argument("--receipt",type=Path,required=True);done.add_argument("--outcome",choices=["completed","blocked","failed"],default="completed")
    run=sub.add_parser("run");run.add_argument("task");run.add_argument("args",nargs=argparse.REMAINDER)
    publish=sub.add_parser("publish");publish.add_argument("task");publish.add_argument("--receipt",type=Path,required=True);publish.add_argument("--command",dest="publish_command",nargs=argparse.REMAINDER,required=True)
    permit=sub.add_parser("publish-permit");permit.add_argument("task",nargs="?",default=os.environ.get("NOVA_COORD_TASK_ID"))
    reconcile=sub.add_parser("reconcile");reconcile.add_argument("task");reconcile.add_argument("--file",type=Path,required=True)
    daemon=sub.add_parser("agent");daemon.add_argument("--interval",type=int,default=30)
    ui=sub.add_parser("ui");ui.add_argument("--port",type=int,default=18766)
    args=parser.parse_args()
    client=Client(args.config)
    if args.command=="status":result=client.request("/api/state")
    elif args.command=="show":result=client.request("/api/tasks/"+args.task)
    elif args.command=="pulse":result=client.pulse()
    elif args.command=="add":
        body=json.loads(args.file.read_text()) if args.file else {"title":args.title,"description":args.description or args.title,"compute":args.compute,"priority":args.priority}
        body.setdefault("request_key",args.key or uuid.uuid4().hex)
        result=client.request("/api/tasks",body)
    elif args.command=="next":
        result=client.claim(args.task)
        result.pop("claim_token",None)
        if result.get("task"):
            result["task"]=client.request("/api/tasks/"+result["task"]["id"])
    elif args.command=="heartbeat":result=client.heartbeat(args.task,args.progress)
    elif args.command=="finish":result=client.finish(args.task,json.loads(args.receipt.read_text()),args.outcome)
    elif args.command=="reconcile":result=client.request(f"/api/tasks/{args.task}/reconcile",json.loads(args.file.read_text()))
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
        sys.exit(supervised(client,args.task,command))
    elif args.command=="ui":
        from .gateway import serve
        serve(client,args.port);return
    elif args.command=="agent":
        # Capacity/inbox polling uses no language model and makes no peer calls.
        while True:
            try:
                client.pulse()
                snapshot=client.request("/api/state")
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
