"""Authenticated HTTPS API and dashboard; no third-party runtime dependencies."""
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


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, address, store, credentials, web):
        self.store, self.credentials, self.web = store, credentials, Path(web)
        super().__init__(address, Handler)


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
                db.execute("SELECT 1").fetchone()
            return self.respond(200,{"ok":True})
        if self.command=="GET" and path in ("/","/app.js","/style.css"):
            file=self.server.web/("index.html" if path=="/" else path[1:])
            return self.respond(200,file.read_bytes(),mimetypes.guess_type(str(file))[0] or "text/plain")
        actor=self.identity()
        if actor is None:
            return self.respond(401,{"error":"Authentication required"})
        if self.headers.get("Origin"):
            origin=self.headers["Origin"]
            if origin not in ("https://"+self.headers.get("Host",""),"http://"+self.headers.get("Host","")):
                return self.respond(403,{"error":"Cross-origin requests are not allowed"})
        store=self.server.store
        if self.command=="GET":
            if path=="/api/state":
                return self.respond(200,{**store.snapshot(),"identity":actor})
            match=re.fullmatch(r"/api/tasks/([0-9a-f]{16})",path)
            if match:
                return self.respond(200,store.detail(match[1]))
        if self.command=="POST":
            body=self.body()
            if path=="/api/tasks":
                return self.respond(200,store.submit(actor,body))
            if path in ("/api/worker/pulse","/api/worker/claim"):
                if actor not in ("oliver-mac","oliver-mini"):
                    return self.respond(403,{"error":"Use a worker identity"})
                result=store.pulse(actor,body) if path.endswith("pulse") else store.claim(actor,body.get("task_id"))
                return self.respond(200,result)
            match=re.fullmatch(r"/api/tasks/([0-9a-f]{16})/(heartbeat|finish|reconcile)",path)
            if match:
                ident,action=match.groups()
                if action=="reconcile":
                    if actor!="admin":
                        return self.respond(403,{"error":"Reconciliation requires administrator identity"})
                    return self.respond(200,store.reconcile(actor,ident,body))
                if actor not in ("oliver-mac","oliver-mini"):
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
    if set(credentials)!={"admin","oliver-mac","oliver-mini"} or any(len(x)<32 for x in credentials.values()) or len(set(credentials.values()))!=3:
        p.error("Three distinct credentials of at least 32 characters are required")
    if not args.cert and args.host not in ("127.0.0.1","localhost"):
        p.error("TLS is required for network listeners")
    server=Server((args.host,args.port),Store(args.db),credentials,args.web)
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
        ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version=ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(args.cert,args.key)
        server.socket=ctx.wrap_socket(server.socket,server_side=True)
    print(f"Nova coordination service listening on {args.host}:{args.port}",flush=True)
    server.serve_forever()


if __name__=="__main__":
    main()
