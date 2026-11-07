"""Private LAN dashboard with automatic worker identity and internal HTTPS."""
import argparse
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import ssl
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from .store import Store, Conflict, Invalid
from .workers import WORKERS


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, address, store, credentials, web, allowed_hosts=None):
        self.store, self.credentials, self.web = store, credentials, Path(web)
        self.tls_context = None
        super().__init__(address, Handler)
        self.allowed_hosts = set(allowed_hosts or [f"127.0.0.1:{self.server_port}", f"localhost:{self.server_port}"])

    def get_request(self):
        connection, address = super().get_request()
        if self.tls_context is not None:
            connection.settimeout(10)
            try:
                connection = self.tls_context.wrap_socket(connection, server_side=True)
            except BaseException:
                connection.close()
                raise
        return connection, address


class Handler(BaseHTTPRequestHandler):
    server_version = "NovaCoordinator/1"

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, fmt, *args):
        # Do not log URLs, bodies, authentication or task text.
        pass

    def respond(self, code, data, content_type="application/json; charset=utf-8"):
        raw = json.dumps(data).encode() if isinstance(data,(dict,list)) else data
        self.send_response(code)
        for k,v in {"Content-Type":content_type,"Content-Length":str(len(raw)),"Cache-Control":"no-store",
            "X-Content-Type-Options":"nosniff","X-Frame-Options":"DENY","Referrer-Policy":"no-referrer",
            "Content-Security-Policy":"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"}.items():
            self.send_header(k,v)
        self.end_headers()
        self.wfile.write(raw)

    def identity(self):
        token = self.headers.get("Authorization", "").removeprefix("Bearer ")
        for name, value in self.server.credentials.items():
            if isinstance(value,str) and value and secrets.compare_digest(value,token):
                return name
        return None

    def body(self):
        try:
            count=int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise Invalid("Invalid content length")
        if not 0 < count <= 160000 or "application/json" not in self.headers.get("Content-Type", ""):
            raise Invalid("Expected JSON body (maximum 160 KB)")
        value=json.loads(self.rfile.read(count))
        if not isinstance(value,dict):
            raise Invalid("Expected a JSON object")
        return value

    def dispatch(self):
        path=self.path.split("?",1)[0]
        if self.command=="GET" and path=="/healthz":
            with self.server.store.connection() as db:
                db.execute("SELECT COUNT(*) FROM workers").fetchone()
            return self.respond(200,{"ok":True})
        if self.headers.get('Host','').lower() not in self.server.allowed_hosts:
            return self.respond(403,{'error':'Unknown service address'})
        if self.headers.get('Sec-Fetch-Site') not in (None,'same-origin','none'):
            return self.respond(403,{'error':'Cross-site requests are not allowed'})
        if self.command=="GET" and path in ("/","/app.js","/style.css"):
            file=self.server.web/("index.html" if path=="/" else path[1:])
            return self.respond(200,file.read_bytes(),mimetypes.guess_type(str(file))[0] or "text/plain")
        # The LAN dashboard has no sign-in or access-token prompt. Worker
        # mutations retain their automatic machine identity and claim fencing.
        actor=self.identity() or 'dashboard'
        if self.headers.get("Origin"):
            origin=self.headers["Origin"]
            if origin not in ("https://"+self.headers.get("Host",""),"http://"+self.headers.get("Host","")):
                return self.respond(403,{"error":"Cross-origin requests are not allowed"})
        store=self.server.store
        if self.command=="GET":
            if path == '/api/worker/resource-scope/policy':
                from .workers import MINI_VM_WORKERS
                if actor not in MINI_VM_WORKERS:
                    return self.respond(403, {'error':'Exact Mini worker required'})
                with store.connection() as db:
                    row=db.execute('SELECT * FROM resource_scope_policies WHERE worker=?',(actor,)).fetchone()
                    if not row or not row['enabled']: raise Conflict('Resource scope is not enabled')
                    return self.respond(200, {'worker':actor,'revision':row['revision'],'policy':json.loads(row['document'])})
            link = re.fullmatch(r"/api/approval-links/([0-9a-f]{64})", path)
            if link:
                if actor != "admin":
                    return self.respond(403, {"error": "Question linkage requires administrator identity"})
                from . import approval_links
                return self.respond(200, approval_links.detail(store, actor, link[1]))
            approval=re.fullmatch(r'/api/approvals/([0-9a-f]{64})',path)
            if approval:
                if actor not in ('admin', *WORKERS):
                    return self.respond(403,{'error':'Authenticated approval actor required'})
                return self.respond(200,store.approval_detail(actor,approval[1]))
            if path=="/api/state":
                return self.respond(200,{**store.snapshot(),"identity":actor})
            match=re.fullmatch(r"/api/tasks/([0-9a-f]{16})",path)
            if match:
                return self.respond(200,store.detail(match[1]))
        if self.command=="POST":
            body=self.body()
            if path == '/api/task-decisions/revise':
                if actor != 'admin': return self.respond(403, {'error':'Administrator required'})
                from . import task_readiness
                return self.respond(200, task_readiness.decision(store, actor, body))
            readiness_task = re.fullmatch(r'/api/tasks/([0-9a-f]{16})/readiness', path)
            if readiness_task:
                if actor != 'admin': return self.respond(403, {'error':'Administrator required'})
                from . import task_readiness
                return self.respond(200, task_readiness.update(store, actor, readiness_task[1], body))
            capability_task = re.fullmatch(r'/api/tasks/([0-9a-f]{16})/require-capabilities', path)
            if capability_task:
                if actor != 'admin':
                    return self.respond(403, {'error': 'Capability refinement requires administrator identity'})
                from . import event_dispatch
                return self.respond(200, event_dispatch.require_capabilities(store, actor, capability_task[1], body))
            scoped_config = re.fullmatch(r'/api/workers/([a-z0-9-]+)/resource-scope', path)
            if scoped_config:
                if actor != 'admin': return self.respond(403, {'error':'Resource policy requires administrator'})
                from . import resource_scope
                return self.respond(200, resource_scope.configure(store, actor, scoped_config[1], body))
            if path in ('/api/worker/resource-scope/reserve', '/api/worker/resource-scope/complete'):
                from .workers import MINI_VM_WORKERS
                if actor not in MINI_VM_WORKERS: return self.respond(403, {'error':'Exact Mini worker required'})
                from . import resource_scope
                operation = path.rsplit('/', 1)[-1]
                return self.respond(200, getattr(resource_scope, operation)(store, actor, body))
            dispatch_config = re.fullmatch(r'/api/workers/([a-z0-9-]+)/dispatch', path)
            if dispatch_config:
                if actor != 'admin':
                    return self.respond(403, {'error': 'Dispatch enrollment requires administrator identity'})
                from . import event_dispatch
                return self.respond(200, event_dispatch.configure(store, actor, dispatch_config[1], body))
            if path.startswith('/api/worker/dispatch/'):
                from . import event_dispatch
                from .workers import MINI_VM_WORKERS
                if actor not in MINI_VM_WORKERS:
                    return self.respond(403, {'error': 'Enrolled Mini worker identity required'})
                operation = path.rsplit('/', 1)[-1]
                if operation == 'next':
                    if set(body) != {'timeout'}:
                        raise Invalid('Only the bounded event-wait timeout is accepted')
                    return self.respond(200, event_dispatch.next_candidate(store, actor, body['timeout']))
                if operation in ('report', 'reserve', 'settle'):
                    return self.respond(200, getattr(event_dispatch, operation)(store, actor, body))
                if operation == 'preclaim-settle':
                    return self.respond(200, event_dispatch.settle_preclaim(store, actor, body))
                return self.respond(404, {'error': 'Unknown dispatch operation'})
            if path == "/api/approval-links/apply":
                if actor != "admin":
                    return self.respond(403, {"error": "Question linkage requires administrator identity"})
                from . import approval_links
                return self.respond(200, approval_links.apply(store, actor, body))
            if path in ('/api/attempt-allowances/apply', '/api/attempt-allowances/amend'):
                if actor != 'admin':
                    return self.respond(403,{'error':'Attempt allowances require administrator identity'})
                result = (store.amend_attempt_allowance(actor,body) if path.endswith('/amend')
                          else store.apply_attempt_allowance(actor,body))
                return self.respond(200,result)
            if path == '/api/attempt-exceptions/apply':
                if actor not in ('admin', *WORKERS):
                    return self.respond(403,{'error':'Authenticated approval actor required'})
                return self.respond(200,store.apply_attempt_exception(actor,body))
            if path == '/api/approvals/apply':
                if actor not in ('admin', *WORKERS):
                    return self.respond(403,{'error':'Authenticated approval actor required'})
                return self.respond(200,store.apply_approval(actor,body))
            if path in ('/api/releases/policy/plan', '/api/releases/policy/apply', '/api/releases/policy/review-unstarted'):
                if actor != 'admin':
                    return self.respond(403, {'error':'Publication policy requires administrator identity'})
                from . import publication_policy
                operation = (publication_policy.plan if path.endswith('/plan') else
                             publication_policy.retire_reviewed_handoff if path.endswith('/review-unstarted') else
                             publication_policy.apply_plan)
                return self.respond(200, operation(store, actor, body))
            if path == '/api/releases/start':
                if actor != 'admin':
                    return self.respond(403, {'error':'Explicit release requests require administrator identity'})
                if body.get('scope') == 'ios_upload_only':
                    from .ios_release import start
                    return self.respond(200, start(store, body))
                if body.get('scope') not in (None, 'combined'):
                    raise Invalid('Unsupported release scope')
                repository=body.get('repository')
                if not isinstance(repository,str) or not repository.strip():
                    raise Invalid('repository is required')
                return self.respond(200, {'created_ids':store.tick_releases(repository)})
            worker_state = re.fullmatch(r"/api/workers/([a-z0-9-]+)/state", path)
            if worker_state:
                if actor != "admin":
                    return self.respond(403, {"error": "Worker state requires administrator identity"})
                return self.respond(200, store.set_worker_state(actor, worker_state[1], body))
            if path == "/api/repositories/configure":
                if actor != "admin":
                    return self.respond(403, {"error": "Repository baseline requires administrator identity"})
                return self.respond(200, store.configure_repository(body))
            if path == "/api/repositories/observe":
                if actor not in WORKERS:
                    return self.respond(403, {"error": "Use a worker identity"})
                return self.respond(200, store.observe_repository(actor, body))
            if path=="/api/tasks":
                return self.respond(200,store.submit(actor,body))
            if path in ("/api/worker/pulse","/api/worker/claim"):
                if actor not in WORKERS:
                    return self.respond(403,{"error":"Use a worker identity"})
                result=store.pulse(actor,body) if path.endswith("pulse") else store.claim(actor,body.get("task_id"),body.get('dispatch_binding'))
                return self.respond(200,result)
            match=re.fullmatch(r"/api/tasks/([0-9a-f]{16})/(heartbeat|finish|reconcile|retry|defer|correct-outcome|correct-attempts)",path)
            if match:
                ident,action=match.groups()
                if action=='correct-attempts':
                    if actor!='admin':
                        return self.respond(403,{'error':'Attempt correction requires administrator identity'})
                    return self.respond(200,store.correct_attempts(actor,ident,body))
                if action=='correct-outcome':
                    if actor!='admin':
                        return self.respond(403,{'error':'Outcome correction requires administrator identity'})
                    return self.respond(200,store.correct_outcome(actor,ident,body))
                if action=='defer' and actor=='admin':
                    return self.respond(200,store.defer(actor,ident,body))
                if action=="reconcile":
                    if actor!="admin":
                        return self.respond(403,{"error":"Reconciliation requires administrator identity"})
                    return self.respond(200,store.reconcile(actor,ident,body))
                if actor not in WORKERS:
                    return self.respond(403,{"error":"Use the owning worker identity"})
                return self.respond(200,getattr(store,action)(actor,ident,body))
        self.respond(404,{"error":"Not found"})

    def handle_request(self):
        try:
            self.dispatch()
        except (Invalid, ValueError, TypeError) as e:
            self.respond(400,{"error":str(e)})
        except Conflict as e:
            self.respond(409,{"error":str(e)})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self.respond(503,{"error":"Service unavailable; ownership has not been released"})

    do_GET=handle_request
    do_POST=handle_request


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--host",default="0.0.0.0")
    p.add_argument("--port",type=int,default=8443)
    p.add_argument("--db",default=os.environ.get("COORD_DB","/data/coordinator.db"))
    p.add_argument("--credentials",default=os.environ.get("COORD_CREDENTIALS","/run/auth/credentials.json"))
    p.add_argument("--web",default=str(Path(__file__).resolve().parents[1]/"web"))
    p.add_argument("--cert",default=os.environ.get("COORD_CERT"))
    p.add_argument("--key",default=os.environ.get("COORD_KEY"))
    args=p.parse_args()
    credentials=json.loads(Path(args.credentials).read_text())
    if ("admin" not in credentials or not set(credentials).issubset({"admin", *WORKERS})
            or len(credentials) < 2 or any(not isinstance(x, str) or len(x) < 32 for x in credentials.values())
            or len(set(credentials.values())) != len(credentials)):
        p.error("Distinct admin and enrolled worker credentials of at least 32 characters are required")
    if not args.cert and args.host not in ("127.0.0.1","localhost"):
        p.error("TLS is required for network listeners")
    default_hosts=[f'192.168.2.{n}:31866' for n in (2,11,12,13,24)]
    default_hosts += ['nova-coordinator.nova-coordinator.svc.cluster.local','nova-coordinator.nova-coordinator.svc.cluster.local:443']
    default_hosts += [f'127.0.0.1:{args.port}',f'localhost:{args.port}']
    hosts=os.environ.get('COORD_ALLOWED_HOSTS',','.join(default_hosts)).split(',')
    server=Server((args.host,args.port),Store(args.db, heavy_worker=os.environ.get("COORD_HEAVY_WORKER") or None),credentials,args.web,allowed_hosts=hosts)
    def hourly_releases():
        while True:
            try:
                server.store.tick_releases()
            except Exception:
                print("Hourly release queue check failed; no publication was executed", flush=True)
            time.sleep(30)
    threading.Thread(target=hourly_releases, daemon=True).start()
    backup_dir=os.environ.get("COORD_BACKUPS")
    if backup_dir:
        def backups():
            directory=Path(backup_dir)
            while True:
                try:
                    target=directory/("coordinator-"+time.strftime("%Y%m%dT%H%M%SZ",time.gmtime())+".db")
                    temporary=target.with_suffix(".tmp")
                    with server.store.connection() as db, sqlite3.connect(temporary) as dest:
                        db.backup(dest)
                    temporary.replace(target)
                    for old in sorted(directory.glob("coordinator-*.db"))[:-96]:old.unlink()
                except Exception:
                    print("Database backup failed; live ownership remains unchanged",flush=True)
                time.sleep(900)
        threading.Thread(target=backups,daemon=True).start()
    if args.cert:
        def certificate_paths():
            # Kubernetes atomically replaces ..data. Bind both files to the
            # same generation so renewal cannot mix an old key and new cert.
            generation=Path(args.cert).parent/'..data'
            if generation.exists():
                root=generation.resolve()
                return root/Path(args.cert).name,root/Path(args.key).name
            return Path(args.cert),Path(args.key)
        def load_context():
            cert,key=certificate_paths()
            context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version=ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(cert,key)
            return context,(str(cert),cert.stat().st_mtime_ns,key.stat().st_mtime_ns)
        server.tls_context,signature=load_context()
        def reload_certificates():
            previous=signature
            while True:
                time.sleep(30)
                try:
                    cert,key=certificate_paths()
                    current=(str(cert),cert.stat().st_mtime_ns,key.stat().st_mtime_ns)
                    if current!=previous:
                        context,current=load_context()
                        server.tls_context=context
                        previous=current
                        print('Internal HTTPS certificate renewed',flush=True)
                except Exception:
                    print('Certificate reload deferred; previous valid context retained',flush=True)
        threading.Thread(target=reload_certificates,daemon=True).start()
    print(f"Nova coordination service listening on {args.host}:{args.port}",flush=True)
    server.serve_forever()


if __name__=="__main__":
    main()
