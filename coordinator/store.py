"""Transactional task claims and durable, fail-closed resource ownership.

Use a local filesystem, never NFS, for the SQLite database. Workers communicate
only through this service. A missing heartbeat holds ownership until reconciled;
it never proves an old process has stopped or makes publishing safe to retry.
"""
import contextlib
import hashlib
import json
import secrets
import sqlite3
import time
import uuid
from pathlib import Path


class Conflict(Exception):
    pass


class Invalid(Exception):
    pass


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def bounded(value, name, maximum=12000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise Invalid(f"{name} must be nonempty text of at most {maximum} characters")
    return value.strip()


SCHEMA = """
CREATE TABLE IF NOT EXISTS workers (
 id TEXT PRIMARY KEY, capacity TEXT NOT NULL DEFAULT '{}', seen REAL NOT NULL DEFAULT 0,
 paused INTEGER NOT NULL DEFAULT 0, last_claim REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS tasks (
 id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL, request_hash TEXT NOT NULL,
 title TEXT NOT NULL, description TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
 submitted_by TEXT NOT NULL, priority INTEGER NOT NULL, compute TEXT NOT NULL,
 requirements TEXT NOT NULL, dependencies TEXT NOT NULL, steps TEXT NOT NULL,
 step INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'queued',
 owner TEXT, claim_hash TEXT, generation INTEGER NOT NULL DEFAULT 0,
 lease_until REAL, progress TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS locks (
 resource TEXT PRIMARY KEY, task_id TEXT NOT NULL, generation INTEGER NOT NULL,
 owner TEXT NOT NULL, acquired REAL NOT NULL);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, task_id TEXT,
 actor TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS receipts (
 task_id TEXT NOT NULL, generation INTEGER NOT NULL, step INTEGER NOT NULL,
 receipt TEXT NOT NULL, at REAL NOT NULL, PRIMARY KEY(task_id,generation,step));
CREATE INDEX IF NOT EXISTS task_queue ON tasks(status,priority,created);
"""


DEFAULT_STEPS = [
    {"name": "Implement and verify", "kind": "work", "instructions": "Handle the request in one session: inspect the code, implement in an isolated checkout, run meaningful checks and record the result. Preserve existing user constraints. Acquire a publish step only when ready for authorized publication. On completion, immediately pull the next queued task."},
]


class Store:
    def __init__(self, path, clock=time.time, lease_seconds=180):
        self.path, self.clock, self.lease_seconds = str(path), clock, lease_seconds
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            for name in ("oliver-mac", "oliver-mini"):
                db.execute("INSERT OR IGNORE INTO workers(id) VALUES(?)", (name,))

    @contextlib.contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        try:
            yield db
        finally:
            db.close()

    @contextlib.contextmanager
    def transaction(self):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def event(self, db, actor, kind, task_id=None, detail=""):
        db.execute("INSERT INTO events(at,task_id,actor,kind,detail) VALUES(?,?,?,?,?)",
                   (self.clock(), task_id, actor, kind, detail[:12000]))

    def expire(self, db):
        for row in db.execute("SELECT * FROM tasks WHERE status='running' AND lease_until<=?", (self.clock(),)).fetchall():
            db.execute("UPDATE tasks SET status='held',updated=?,progress=? WHERE id=?",
                       (self.clock(), "Worker heartbeat expired. Confirm execution stopped before retrying or releasing resources.", row["id"]))
            self.event(db, "service", "held", row["id"], "Lease expired; resource locks retained")

    def public_task(self, row):
        task = dict(row)
        task.pop("claim_hash", None)
        task.pop("request_hash", None)
        for field in ("requirements", "dependencies", "steps", "result"):
            task[field] = json.loads(task[field])
        task["current_step"] = task["steps"][task["step"]] if task["step"] < len(task["steps"]) else None
        return task

    def submit(self, actor, body):
        if not isinstance(body, dict):
            raise Invalid("Expected a JSON object")
        key = bounded(body.get("request_key"), "request_key", 160)
        title = bounded(body.get("title"), "title", 240)
        description = bounded(body.get("description", title), "description", 40000)
        compute = body.get("compute", "standard")
        if compute not in ("light", "standard", "heavy"):
            raise Invalid("compute must be light, standard or heavy")
        priority = body.get("priority", 50)
        if type(priority) is not int or not 0 <= priority <= 100:
            raise Invalid("priority must be 0–100")
        requirements = body.get("requirements", {})
        if not isinstance(requirements, dict) or set(requirements) - {"memory_gb", "disk_gb", "cpus", "capabilities", "worker"}:
            raise Invalid("Invalid requirements")
        for field in ("memory_gb", "disk_gb", "cpus"):
            if field in requirements and (type(requirements[field]) not in (int, float) or not 0 <= requirements[field] <= 100000):
                raise Invalid("Resource requirements must be nonnegative numbers")
        if requirements.get("worker") not in (None, "oliver-mac", "oliver-mini"):
            raise Invalid("Unknown worker")
        caps = requirements.get("capabilities", [])
        if not isinstance(caps, list) or len(caps) > 20 or not all(isinstance(x, str) and len(x) < 80 for x in caps):
            raise Invalid("Invalid capabilities")
        dependencies = body.get("dependencies", [])
        if not isinstance(dependencies, list) or len(dependencies) > 50 or not all(isinstance(x, str) for x in dependencies):
            raise Invalid("dependencies must be task IDs")
        steps = body.get("steps", DEFAULT_STEPS)
        if not isinstance(steps, list) or not 1 <= len(steps) <= 30:
            raise Invalid("Provide 1–30 steps")
        normalized = []
        for step in steps:
            if not isinstance(step, dict) or step.get("kind", "work") not in ("work", "publish", "integrate", "desktop"):
                raise Invalid("Invalid step kind")
            normalized.append({"name": bounded(step.get("name"), "step name", 120),
                "instructions": bounded(step.get("instructions"), "instructions", 16000), "kind": step.get("kind", "work")})
        canonical = dict(title=title, description=description, priority=priority, compute=compute,
                         requirements=requirements, dependencies=dependencies, steps=normalized)
        digest = hashlib.sha256(encoded(canonical).encode()).hexdigest()
        with self.transaction() as db:
            old = db.execute("SELECT * FROM tasks WHERE request_key=?", (key,)).fetchone()
            if old:
                if old["request_hash"] != digest:
                    raise Conflict("request_key already exists with different content")
                return self.public_task(old)
            for dep in dependencies:
                if not db.execute("SELECT id FROM tasks WHERE id=?", (dep,)).fetchone():
                    raise Invalid("Dependency does not exist: " + dep)
            ident = uuid.uuid4().hex[:16]
            now = self.clock()
            db.execute("""INSERT INTO tasks(id,request_key,request_hash,title,description,created,updated,
                       submitted_by,priority,compute,requirements,dependencies,steps) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (ident, key, digest, title, description, now, now, actor, priority, compute,
                        encoded(requirements), encoded(dependencies), encoded(normalized)))
            self.event(db, actor, "submitted", ident, title)
            return self.public_task(db.execute("SELECT * FROM tasks WHERE id=?", (ident,)).fetchone())

    def pulse(self, worker, body):
        capacity = body.get("capacity", {})
        if not isinstance(capacity, dict):
            raise Invalid("Invalid capacity")
        normalized = {}
        for key in ("memory_gb", "disk_gb", "cpus"):
            val = capacity.get(key, 0)
            if type(val) not in (int, float) or not 0 <= val <= 100000:
                raise Invalid("Invalid capacity " + key)
            normalized[key] = val
        caps = capacity.get("capabilities", [])
        if not isinstance(caps, list) or not all(isinstance(x, str) and len(x) < 80 for x in caps) or len(caps) > 20:
            raise Invalid("Invalid capabilities")
        normalized["capabilities"] = caps
        with self.transaction() as db:
            self.expire(db)
            if not db.execute("SELECT id FROM workers WHERE id=?", (worker,)).fetchone():
                raise Invalid("Unknown worker")
            db.execute("UPDATE workers SET capacity=?,seen=? WHERE id=?", (encoded(normalized), self.clock(), worker))
            return {"worker": worker, "heartbeat_seconds": 30, "lease_seconds": self.lease_seconds}

    def fits(self, task, worker):
        req = json.loads(task["requirements"])
        cap = json.loads(worker["capacity"])
        if req.get("worker") and req["worker"] != worker["id"]:
            return False
        if any(cap.get(k, 0) < req.get(k, 0) for k in ("memory_gb", "disk_gb", "cpus")):
            return False
        return set(req.get("capabilities", [])).issubset(cap.get("capabilities", []))

    def candidate(self, db, task):
        workers = db.execute("""SELECT * FROM workers WHERE paused=0 AND seen>? AND id NOT IN
            (SELECT owner FROM tasks WHERE status IN ('running','held') AND owner IS NOT NULL)""", (self.clock()-90,)).fetchall()
        workers = [w for w in workers if self.fits(task, w)]
        if not workers:
            return None
        # Keep subsequent stages with the session that owns the checkout.
        # An unavailable owner can be replaced using its recorded evidence.
        previous = self.available_owner(db, task)
        if previous:
            return previous["id"] if any(w["id"] == previous["id"] for w in workers) else None
        # Heavy work stays queued for a healthy but busy mini. Fall back to the
        # Mac when the mini is offline/paused or cannot meet hard requirements.
        if task["compute"] == "heavy":
            mini = db.execute("SELECT * FROM workers WHERE id='oliver-mini' AND paused=0 AND seen>?", (self.clock()-90,)).fetchone()
            if mini and self.fits(task, mini):
                return "oliver-mini" if any(w["id"] == "oliver-mini" for w in workers) else None
            def power(w):
                c = json.loads(w["capacity"])
                return c.get("memory_gb",0)*2 + c.get("cpus",0)*4 + min(c.get("disk_gb",0),2000)/100
            workers.sort(key=lambda w: (-power(w), w["last_claim"], w["id"]))
        else:
            # Ordinary work rotates between equally authorized workers.
            workers.sort(key=lambda w: (w["last_claim"], w["id"]))
        return workers[0]["id"]

    def available_owner(self, db, task):
        previous = db.execute("SELECT * FROM workers WHERE id=? AND paused=0 AND seen>?",
                              (task["owner"], self.clock()-90)).fetchone()
        return previous if previous and self.fits(task, previous) else None

    def resources(self, row, worker):
        step = json.loads(row["steps"])[row["step"]]
        # Legacy step labels remain readable, but only publication needs a
        # shared lane. Each worker already owns at most one active task.
        return ["publish"] if step["kind"] == "publish" else []

    def claim(self, worker, task_id=None):
        with self.transaction() as db:
            self.expire(db)
            rows = db.execute("SELECT * FROM tasks WHERE status='queued' ORDER BY priority DESC,created,id").fetchall()
            for row in rows:
                if task_id and row["id"] != task_id:
                    continue
                deps = json.loads(row["dependencies"])
                if any(db.execute("SELECT status FROM tasks WHERE id=?", (d,)).fetchone()[0] != "completed" for d in deps):
                    continue
                own = db.execute("SELECT * FROM workers WHERE id=? AND paused=0 AND seen>?", (worker,self.clock()-90)).fetchone()
                if not own or not self.fits(row,own) or db.execute("SELECT id FROM tasks WHERE owner=? AND status IN ('running','held')", (worker,)).fetchone():
                    continue
                previous = self.available_owner(db, row)
                if previous and previous["id"] != worker:
                    continue
                if row["compute"] == "heavy" and self.candidate(db, row) != worker:
                    continue
                resources = self.resources(row, worker)
                if any(db.execute("SELECT resource FROM locks WHERE resource=?", (r,)).fetchone() for r in resources):
                    continue
                token, generation = secrets.token_urlsafe(32), row["generation"]+1
                now = self.clock()
                for resource in resources:
                    db.execute("INSERT INTO locks VALUES(?,?,?,?,?)", (resource,row["id"],generation,worker,now))
                db.execute("""UPDATE tasks SET status='running',owner=?,claim_hash=?,generation=?,lease_until=?,updated=? WHERE id=?""",
                           (worker,hashlib.sha256(token.encode()).hexdigest(),generation,now+self.lease_seconds,now,row["id"]))
                db.execute("UPDATE workers SET last_claim=? WHERE id=?", (now,worker))
                self.event(db, worker, "claimed", row["id"], f"Step {row['step']+1}; generation {generation}; resources {resources}")
                task = self.public_task(db.execute("SELECT * FROM tasks WHERE id=?", (row["id"],)).fetchone())
                return {"task": task, "claim_token": token, "resources": resources, "heartbeat_seconds": 30}
            return {"task": None, "reason": "No eligible step. Check capacity, dependencies, existing ownership and resource locks."}

    def owned(self, db, worker, ident, token, generation):
        row = db.execute("SELECT * FROM tasks WHERE id=?", (ident,)).fetchone()
        if (not row or row["owner"] != worker or row["generation"] != generation or
            not isinstance(token, str) or not secrets.compare_digest(row["claim_hash"] or "", hashlib.sha256(token.encode()).hexdigest())):
            raise Conflict("Stale or foreign claim")
        return row

    def heartbeat(self, worker, ident, body):
        with self.transaction() as db:
            self.expire(db)
            row = self.owned(db,worker,ident,body.get("claim_token"),body.get("generation"))
            if row["status"] != "running":
                raise Conflict("Task is not running; stop execution and reconcile")
            progress = body.get("progress", row["progress"])
            if not isinstance(progress, str) or len(progress)>12000:
                raise Invalid("Invalid progress")
            db.execute("UPDATE tasks SET lease_until=?,updated=?,progress=? WHERE id=?",
                       (self.clock()+self.lease_seconds,self.clock(),progress,ident))
            return {"lease_until":self.clock()+self.lease_seconds}

    def finish(self, worker, ident, body):
        receipt = body.get("receipt", {})
        if not isinstance(receipt, dict) or len(encoded(receipt))>40000:
            raise Invalid("Invalid receipt")
        bounded(receipt.get("summary"), "receipt summary", 12000)
        if receipt.get("execution_stopped") is not True:
            raise Invalid("Confirm execution_stopped after all owned operations settle")
        outcome = body.get("outcome", "completed")
        if outcome not in ("completed", "blocked", "failed"):
            raise Invalid("Invalid outcome")
        with self.transaction() as db:
            self.expire(db)
            row = self.owned(db,worker,ident,body.get("claim_token"),body.get("generation"))
            previous = db.execute("SELECT receipt FROM receipts WHERE task_id=? AND generation=? AND step=?",
                                  (ident,row["generation"],body.get("step"))).fetchone()
            if previous:
                if json.loads(previous[0]) != {"outcome":outcome,"receipt":receipt}:
                    raise Conflict("Completion already recorded with different evidence")
                return self.public_task(row)
            if row["status"] != "running" or body.get("step") != row["step"]:
                raise Conflict("Task/step is not running; reconcile held outcomes separately")
            kind = json.loads(row["steps"])[row["step"]]["kind"]
            if kind == "publish" and outcome != "completed":
                # Failure may follow an external side effect. Never unlock it as
                # an ordinary failed/retryable step.
                status, next_step = "held", row["step"]
            else:
                next_step = row["step"] + (outcome == "completed")
                status = ("completed" if next_step == len(json.loads(row["steps"])) else "queued") if outcome=="completed" else outcome
                db.execute("DELETE FROM locks WHERE task_id=?", (ident,))
            if kind == "publish" and outcome == "completed":
                bounded(receipt.get("publication_ref"), "publication_ref", 2000)
            db.execute("INSERT INTO receipts VALUES(?,?,?,?,?)", (ident,row["generation"],row["step"],encoded({"outcome":outcome,"receipt":receipt}),self.clock()))
            db.execute("UPDATE tasks SET status=?,step=?,updated=?,lease_until=NULL,progress=?,result=? WHERE id=?",
                       (status,next_step,self.clock(),receipt["summary"],encoded(receipt),ident))
            self.event(db,worker,status,ident,receipt["summary"])
            return self.public_task(db.execute("SELECT * FROM tasks WHERE id=?", (ident,)).fetchone())

    def reconcile(self, actor, ident, body):
        evidence = bounded(body.get("evidence"), "evidence", 16000)
        if body.get("execution_stopped") is not True or body.get("external_state_checked") is not True:
            raise Invalid("Reconciliation requires stopped execution and checked external state")
        action = body.get("action")
        if action not in ("retry", "complete", "cancel"):
            raise Invalid("Invalid reconciliation action")
        with self.transaction() as db:
            self.expire(db)
            row = db.execute("SELECT * FROM tasks WHERE id=?", (ident,)).fetchone()
            if not row or row["status"] not in ("held", "blocked", "failed"):
                raise Conflict("Only held, blocked or failed tasks can be reconciled")
            step = row["step"] + (action == "complete")
            status = "cancelled" if action=="cancel" else "completed" if step==len(json.loads(row["steps"])) else "queued"
            db.execute("DELETE FROM locks WHERE task_id=?", (ident,))
            db.execute("UPDATE tasks SET status=?,step=?,owner=NULL,claim_hash=NULL,lease_until=NULL,updated=?,progress=? WHERE id=?",
                       (status,step,self.clock(),evidence,ident))
            self.event(db,actor,"reconciled",ident,encoded({"action":action,"evidence":evidence}))
            return self.public_task(db.execute("SELECT * FROM tasks WHERE id=?",(ident,)).fetchone())

    def snapshot(self):
        with self.transaction() as db:
            self.expire(db)
            workers=[]
            for row in db.execute("SELECT * FROM workers ORDER BY id"):
                w=dict(row); w["capacity"]=json.loads(w["capacity"]); w["online"]=w["seen"]>self.clock()-90; workers.append(w)
            tasks=[self.public_task(r) for r in db.execute("SELECT * FROM tasks ORDER BY created DESC LIMIT 500")]
            for t in tasks:
                if t["status"]=="queued":
                    t["preferred_worker"] = self.candidate(db,db.execute("SELECT * FROM tasks WHERE id=?",(t["id"],)).fetchone())
            return {"server_time":self.clock(),"workers":workers,"tasks":tasks,
                "locks":[dict(r) for r in db.execute("SELECT * FROM locks ORDER BY acquired")],
                "events":[dict(r) for r in db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 100")]}

    def detail(self, ident):
        with self.transaction() as db:
            self.expire(db)
            row = db.execute("SELECT * FROM tasks WHERE id=?",(ident,)).fetchone()
            if not row:
                raise Invalid("Unknown task")
            task=self.public_task(row)
            task["receipts"]=[{**dict(r),"receipt":json.loads(r["receipt"])} for r in db.execute("SELECT * FROM receipts WHERE task_id=? ORDER BY at",(ident,))]
            task["events"]=[dict(r) for r in db.execute("SELECT * FROM events WHERE task_id=? ORDER BY id",(ident,))]
            return task
