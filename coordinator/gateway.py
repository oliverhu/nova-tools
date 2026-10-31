"""Loopback dashboard gateway: keeps the worker credential out of the browser."""
import json
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
import mimetypes


def serve(client,port=18766):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def handle_request(self):
            host=self.headers.get("Host","")
            if host not in (f"127.0.0.1:{port}",f"localhost:{port}"):
                self.send_error(403);return
            if self.headers.get("Origin") not in (None,"http://"+host):
                self.send_error(403);return
            if self.headers.get("Sec-Fetch-Site") not in (None,"same-origin","none"):
                self.send_error(403);return
            code=200
            try:
                if self.path in ("/","/app.js","/style.css") and self.command=="GET":
                    file=Path(__file__).resolve().parents[1]/"web"/("index.html" if self.path=="/" else self.path[1:])
                    data=file.read_bytes();ctype=mimetypes.guess_type(str(file))[0]
                elif self.path.startswith("/api/"):
                    if self.command=="POST":
                        # Browser can submit tasks, but cannot impersonate the
                        # runtime or release/reconcile its claims.
                        if self.path!="/api/tasks":
                            self.send_error(403);return
                        count=int(self.headers.get("Content-Length",0))
                        if not 0<count<=160000 or "application/json" not in self.headers.get("Content-Type",""):
                            self.send_error(400);return
                        body=json.loads(self.rfile.read(count))
                    else:body=None
                    data=json.dumps(client.request(self.path,body)).encode();ctype="application/json"
                else:
                    self.send_error(404);return
            except Exception as e:
                code=503;data=json.dumps({"error":str(e)}).encode();ctype="application/json"
            self.send_response(code)
            for key,val in {"Content-Type":ctype,"Content-Length":str(len(data)),"Cache-Control":"no-store",
                "X-Content-Type-Options":"nosniff","X-Frame-Options":"DENY","Referrer-Policy":"no-referrer",
                "Content-Security-Policy":"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"}.items():
                self.send_header(key,val)
            self.end_headers();self.wfile.write(data)
        do_GET=handle_request
        do_POST=handle_request
    ThreadingHTTPServer(("127.0.0.1",port),Handler).serve_forever()
