"""Atomic task ownership and durable hourly release-task creation.

Heartbeat age is diagnostic. A taken task never becomes available just because
a worker is quiet. There are no global resource or publishing locks.
"""
import contextlib
import hashlib
import json
import secrets
import re
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
CREATE TABLE IF NOT EXISTS repositories (
 name TEXT PRIMARY KEY, branch TEXT NOT NULL, merged_head TEXT NOT NULL,
 published_head TEXT NOT NULL, observed_at REAL NOT NULL, next_check REAL NOT NULL,
 last_checked REAL, last_error TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS task_queue ON tasks(status,priority,created);
"""


DEFAULT_STEPS = [
    {"name": "Implement and verify", "kind": "work", "instructions": "Implement, meaningfully test and merge the change in one session. Rebase and retest affected checks if main advances; never force-push main. Record the actual merged commit. DO NOT PUBLISH. The service creates separate hourly publish tasks. On completion or settled blocked execution, immediately pull the next eligible task."},
]


class Store:
    def __init__(self, path, clock=time.time, lease_seconds=180):
        self.path, self.clock, self.lease_seconds = str(path), clock, lease_seconds
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            columns = {r[1] for r in db.execute("PRAGMA table_info(tasks)")}
            if "last_owner" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN last_owner TEXT")
                db.execute("UPDATE tasks SET last_owner=owner")
            if "task_type" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN task_type TEXT NOT NULL DEFAULT 'work'")
                db.execute("UPDATE tasks SET compute='standard' WHERE compute='heavy'")
            repo_columns = {r[1] for r in db.execute("PRAGMA table_info(repositories)")}
            if "published_task_created" not in repo_columns:
                db.execute("ALTER TABLE repositories ADD COLUMN published_task_created REAL NOT NULL DEFAULT 0")
            # Retire obsolete shared locks, retaining their evidence in events.
            for lock in db.execute("SELECT * FROM locks").fetchall():
                self.event(db, "service", "legacy-lock-retired", lock["task_id"], encoded(dict(lock)))
            db.execute("DELETE FROM locks")
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
        # Keep ownership intact; missed heartbeats are visible, not admin holds.
        pass

    def public_task(self, row):
        task = dict(row)
        task.pop("claim_hash", None)
        task.pop("request_hash", None)
        for field in ("requirements", "dependencies", "steps", "result"):
            task[field] = json.loads(task[field])
        task["current_step"] = task["steps"][task["step"]] if task["step"] < len(task["steps"]) else None
        task["execution_policy"] = ("Publish only the pinned release target; no global locks. Verify external outcome before reporting success."
            if task["current_step"] and task["current_step"]["kind"] == "publish" else
            "Implement, test and merge. DO NOT PUBLISH Nova from ordinary work. Only engine rebuilds are heavy. No global locks; ownership does not expire on missed heartbeat. For scheduling waits, return settled work through POST /api/tasks/ID/defer with claim identity, execution_stopped, external_state_checked, evidence, and worker and/or dependencies; keep it queued rather than blocked. After settled work, immediately pull again.")
        task["heartbeat_stale"] = task["status"] == "running" and (task["lease_until"] or 0) <= self.clock()
        return task

    def submit(self, actor, body):
        if not isinstance(body, dict):
            raise Invalid("Expected a JSON object")
        key = bounded(body.get("request_key"), "request_key", 160)
        title = bounded(body.get("title"), "title", 240)
        description = bounded(body.get("description", title), "description", 40000)
        task_type = body.get("task_type", "work")
        if task_type not in ("work", "engine_rebuild"):
            raise Invalid("task_type must be work or engine_rebuild; publish tasks are generated hourly")
        compute = body.get("compute", "standard")
        if compute not in ("light", "standard", "heavy"):
            raise Invalid("compute must be light, standard or heavy")
        compute = "heavy" if task_type == "engine_rebuild" else ("light" if compute == "light" else "standard")
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
            if step.get("kind") == "publish":
                raise Invalid("Ordinary tasks must not publish; publication is a separate hourly task")
            normalized.append({"name": bounded(step.get("name"), "step name", 120),
                "instructions": bounded(step.get("instructions"), "instructions", 16000), "kind": step.get("kind", "work")})
        canonical = dict(title=title, description=description, priority=priority, compute=compute,
                         requirements=requirements, dependencies=dependencies, steps=normalized, task_type=task_type)
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
            db.execute("UPDATE tasks SET task_type=? WHERE id=?", (task_type, ident))
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
        return []

    def scheduling(self, db, row):
        """Describe the next execution, separately from historical ownership."""
        result = {"next_worker": None, "next_worker_label": "—", "ready": False,
                  "waiting_reason": "", "waiting_dependencies": []}
        if row['status'] == 'running':
            return {**result, 'next_worker': row['owner'], 'next_worker_label': row['owner']}
        if row['status'] != 'queued':
            if row['status'] in ('blocked', 'failed', 'held'):
                result.update(next_worker_label='Needs correction', waiting_reason=row['progress'])
            return result
        deps = [dict(db.execute('SELECT id,title,status FROM tasks WHERE id=?', (dep,)).fetchone())
                for dep in json.loads(row['dependencies'])]
        pending = [d for d in deps if d['status'] != 'completed']
        previous = self.available_owner(db, row)
        target = json.loads(row['requirements']).get('worker')
        if not target and previous:
            target = previous['id']
        candidate = self.candidate(db, row)
        if not target and row['compute'] == 'heavy':
            mini = db.execute("SELECT * FROM workers WHERE id='oliver-mini' AND paused=0 AND seen>?", (self.clock()-90,)).fetchone()
            target = 'oliver-mini' if mini and self.fits(row, mini) else candidate
        result.update(next_worker=target, next_worker_label=target or 'Either available Mac',
                      waiting_dependencies=pending, ready=not pending and candidate is not None)
        if pending:
            result['waiting_reason'] = 'Waiting for: ' + ', '.join(d['title'] + ' (' + d['status'] + ')' for d in pending)
        elif not candidate:
            result['waiting_reason'] = ('Waiting for ' + target + ' to be available and meet requirements') if target else 'Waiting for an available Mac that meets requirements'
        else:
            result['waiting_reason'] = 'Ready for pickup'
        return result

    def defer(self, actor, ident, body):
        """Return settled work to the queue with explicit scheduling conditions."""
        evidence = bounded(body.get('evidence'), 'evidence', 16000)
        if body.get('execution_stopped') is not True or body.get('external_state_checked') is not True:
            raise Invalid('Waiting requires settled execution and checked external state')
        with self.transaction() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (ident,)).fetchone()
            if not row or row['status'] not in ('queued', 'running', 'blocked', 'failed'):
                raise Conflict('Only settled work can return to the waiting queue')
            if body.get('generation') != row['generation']:
                raise Conflict('Task generation changed; reload before scheduling')
            if actor != 'admin':
                self.owned(db, actor, ident, body.get('claim_token'), body.get('generation'))
            elif row['status'] == 'running':
                raise Conflict('Running work must be deferred by its owning worker')
            req = json.loads(row['requirements'])
            if 'worker' in body:
                if body['worker'] not in ('oliver-mac', 'oliver-mini'):
                    raise Invalid('Unknown destination worker')
                req['worker'] = body['worker']
            deps = body.get('dependencies', json.loads(row['dependencies']))
            if not isinstance(deps, list) or len(deps)>50 or not all(isinstance(d, str) for d in deps):
                raise Invalid('dependencies must be task IDs')
            if not req.get('worker') and not deps:
                raise Invalid('Specify a destination worker or prerequisite task')
            visited = set()
            def visit(dep):
                if dep == ident:
                    raise Invalid('Dependency cycle')
                if dep in visited:
                    return
                visited.add(dep)
                found = db.execute('SELECT dependencies FROM tasks WHERE id=?', (dep,)).fetchone()
                if not found:
                    raise Invalid('Unknown dependency: ' + dep)
                for child in json.loads(found['dependencies']):
                    visit(child)
            for dep in deps:
                visit(dep)
            if row['status'] == 'running':
                db.execute('INSERT INTO receipts VALUES(?,?,?,?,?)', (ident,row['generation'],row['step'],
                    encoded({'outcome':'waiting','receipt':{'summary':evidence,'execution_stopped':True,'external_state_checked':True}}),self.clock()))
            db.execute("UPDATE tasks SET status='queued',requirements=?,dependencies=?,last_owner=COALESCE(owner,last_owner),owner=NULL,claim_hash=NULL,lease_until=NULL,progress=?,updated=? WHERE id=?",
                       (encoded(req),encoded(list(dict.fromkeys(deps))),evidence,self.clock(),ident))
            self.event(db, actor, 'waiting', ident, encoded({'worker':req.get('worker'),'dependencies':deps,'evidence':evidence}))
            task = db.execute('SELECT * FROM tasks WHERE id=?', (ident,)).fetchone()
            return {**self.public_task(task), **self.scheduling(db,task)}

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
            return {"task": None, "reason": "No eligible task. Check capacity, dependencies and existing task ownership."}

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
                raise Conflict("Task is not running; stop execution and inspect its recorded outcome")
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
            target = json.loads(row["result"]).get("release_target")
            if target:
                receipt = {**receipt, "release_target": target}
            previous = db.execute("SELECT receipt FROM receipts WHERE task_id=? AND generation=? AND step=?",
                                  (ident,row["generation"],body.get("step"))).fetchone()
            if previous:
                if json.loads(previous[0]) != {"outcome":outcome,"receipt":receipt}:
                    raise Conflict("Completion already recorded with different evidence")
                return self.public_task(row)
            if row["status"] != "running" or body.get("step") != row["step"]:
                raise Conflict("Task/step is not running; reconcile held outcomes separately")
            kind = json.loads(row["steps"])[row["step"]]["kind"]
            next_step = row["step"] + (outcome == "completed")
            status = ("completed" if next_step == len(json.loads(row["steps"])) else "queued") if outcome == "completed" else outcome
            if kind == "publish" and outcome == "completed":
                bounded(receipt.get("publication_ref"), "publication_ref", 2000)
                target = json.loads(row["result"]).get("release_target")
                if target:
                    if receipt.get("published_commit") != target["commit"]:
                        raise Invalid("published_commit must match this publish task's exact release target")
                    db.execute("UPDATE repositories SET published_head=?,published_task_created=? WHERE name=? AND published_task_created<=?", (self.commit(receipt.get("release_commit", target["commit"])),row["created"],target["repository"],row["created"]))
                    receipt = {**receipt, "release_target": target}
            elif outcome == "completed" and receipt.get("merge"):
                merge = receipt["merge"]
                self._observe(db, merge, self.clock())
            # Preserve the pinned target on failed/blocked receipts as well.
            target = json.loads(row["result"]).get("release_target")
            if target:
                receipt = {**receipt, "release_target": target}
            db.execute("INSERT INTO receipts VALUES(?,?,?,?,?)", (ident,row["generation"],row["step"],encoded({"outcome":outcome,"receipt":receipt}),self.clock()))
            db.execute("UPDATE tasks SET status=?,step=?,updated=?,lease_until=NULL,progress=?,result=?,last_owner=owner WHERE id=?",
                       (status,next_step,self.clock(),receipt["summary"],encoded(receipt),ident))
            self.event(db,worker,status,ident,receipt["summary"])
            return self.public_task(db.execute("SELECT * FROM tasks WHERE id=?", (ident,)).fetchone())

    def retry(self, worker, ident, body):
        # Only the owning worker can recover its settled failed/blocked attempt.
        with self.connection() as db:
            self.owned(db, worker, ident, body.get("claim_token"), body.get("generation"))
        return self.reconcile(worker, ident, {**body, "action": "retry"})

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
            if actor != "admin":
                self.owned(db, actor, ident, body.get("claim_token"), body.get("generation"))
                if action != "retry":
                    raise Conflict("Workers may only retry their own stopped attempts")
            step = row["step"] + (action == "complete")
            status = "cancelled" if action=="cancel" else "completed" if step==len(json.loads(row["steps"])) else "queued"
            db.execute("DELETE FROM locks WHERE task_id=?", (ident,))
            db.execute("UPDATE tasks SET status=?,step=?,owner=NULL,claim_hash=NULL,lease_until=NULL,updated=?,progress=? WHERE id=?",
                       (status,step,self.clock(),evidence,ident))
            self.event(db,actor,"reconciled",ident,encoded({"action":action,"evidence":evidence}))
            return self.public_task(db.execute("SELECT * FROM tasks WHERE id=?",(ident,)).fetchone())

    @staticmethod
    def commit(value):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
            raise Invalid("Expected an exact 40-character commit SHA")
        return value

    def configure_repository(self, body):
        name = bounded(body.get("repository"), "repository", 200)
        branch = bounded(body.get("branch", "main"), "branch", 200)
        baseline = self.commit(body.get("published_commit"))
        with self.transaction() as db:
            # Configuration is insert-only: never reset a publication baseline.
            row = db.execute("SELECT * FROM repositories WHERE name=?", (name,)).fetchone()
            if row:
                if row["branch"] != branch:
                    raise Conflict("Repository already tracks another branch")
                return dict(row)
            now = self.clock()
            db.execute("INSERT INTO repositories(name,branch,merged_head,published_head,observed_at,next_check) VALUES(?,?,?,?,?,?)",
                       (name, branch, baseline, baseline, now, (int(now)//3600+1)*3600))
            self.event(db, "admin", "repository-configured", detail=name+" baseline "+baseline)
            return dict(db.execute("SELECT * FROM repositories WHERE name=?", (name,)).fetchone())

    def _observe(self, db, body, observed):
        name = bounded(body.get("repository"), "repository", 200)
        head = self.commit(body.get("commit"))
        row = db.execute("SELECT * FROM repositories WHERE name=?", (name,)).fetchone()
        if not row or body.get("branch", "main") != row["branch"]:
            raise Invalid("Repository/branch is not configured")
        if observed >= row["observed_at"]:
            db.execute("UPDATE repositories SET merged_head=?,observed_at=?,last_error='' WHERE name=?", (head, observed, name))
        return {"repository": name, "commit": head, "accepted": observed >= row["observed_at"]}

    def observe_repository(self, worker, body):
        observed = body.get("observed_at")
        if type(observed) not in (float, int) or not self.clock()-600 <= observed <= self.clock()+30:
            raise Invalid("Branch observation must be recent")
        with self.transaction() as db:
            result = self._observe(db, body, observed)
            return result

    def tick_releases(self):
        """One durable hourly check; creates queue work, never publishes itself."""
        with self.transaction() as db:
            now = self.clock()
            created = []
            for repo in db.execute("SELECT * FROM repositories WHERE next_check<=?", (now,)).fetchall():
                # A worker observes origin/main with its existing Git access.
                # Retry stale observations without consuming this hourly check.
                if repo["observed_at"] < now-600:
                    db.execute("UPDATE repositories SET last_error='Waiting for fresh origin/main observation' WHERE name=?", (repo["name"],))
                    continue
                db.execute("UPDATE repositories SET next_check=?,last_checked=?,last_error='' WHERE name=?", ((int(now)//3600+1)*3600, now, repo["name"]))
                if repo["merged_head"] == repo["published_head"]:
                    continue
                target = {"repository": repo["name"], "branch": repo["branch"], "commit": repo["merged_head"]}
                key = "hourly-publish:"+repo["name"]+":"+repo["merged_head"]
                if db.execute("SELECT id FROM tasks WHERE request_key=?", (key,)).fetchone():
                    continue
                ident = uuid.uuid4().hex[:16]
                title = "Publish "+repo["name"]+" at "+repo["merged_head"][:12]
                instructions = ("Publish the merged source pinned in release_target after release validation. "
                    "Either Mac may claim this task; there is no shared publishing lock. "
                    "Use the normal signed/notarized release procedure; verify the public artifact before completing. Check the current public release before publishing and never downgrade a newer release; report a superseded target as blocked with exact evidence instead. "
                    "Record published_commit equal to the pinned target and publication_ref. If release metadata is committed to main, also record release_commit after verifying it contains only the release metadata on top of this target; this becomes the published baseline and prevents an endless release loop. "
                    "Do not incorporate later feature changes. If publication outcome is unknown, stop and inspect external state before any retry. "
                    "After settled completion or failure, immediately pull another eligible task.")
                steps = [{"name":"Validate and publish merged changes","kind":"publish","instructions":instructions}]
                db.execute("""INSERT INTO tasks(id,request_key,request_hash,title,description,created,updated,submitted_by,priority,compute,requirements,dependencies,steps,result,task_type)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (ident,key,hashlib.sha256(key.encode()).hexdigest(),title,instructions,now,now,"hourly-scheduler",50,"standard","{}","[]",encoded(steps),encoded({"release_target":target}),"publish"))
                self.event(db, "hourly-scheduler", "submitted", ident, title)
                created.append(ident)
            return created

    def snapshot(self):
        with self.transaction() as db:
            self.expire(db)
            workers=[]
            for row in db.execute("SELECT * FROM workers ORDER BY id"):
                w=dict(row); w["capacity"]=json.loads(w["capacity"]); w["online"]=w["seen"]>self.clock()-90; workers.append(w)
            tasks=[self.public_task(r) for r in db.execute("SELECT * FROM tasks ORDER BY created DESC LIMIT 500")]
            for t in tasks:
                row = db.execute("SELECT * FROM tasks WHERE id=?",(t['id'],)).fetchone()
                t.update(self.scheduling(db, row))
                if t["status"]=="queued":
                    t["preferred_worker"] = self.candidate(db,row) if t['ready'] else None
            return {"server_time":self.clock(),"workers":workers,"tasks":tasks,
                "locks":[], "repositories":[dict(r) for r in db.execute("SELECT * FROM repositories ORDER BY name")],
                "events":[dict(r) for r in db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 100")]}

    def detail(self, ident):
        with self.transaction() as db:
            self.expire(db)
            row = db.execute("SELECT * FROM tasks WHERE id=?",(ident,)).fetchone()
            if not row:
                raise Invalid("Unknown task")
            task=self.public_task(row)
            task.update(self.scheduling(db, row))
            task["receipts"]=[{**dict(r),"receipt":json.loads(r["receipt"])} for r in db.execute("SELECT * FROM receipts WHERE task_id=? ORDER BY at",(ident,))]
            task["events"]=[dict(r) for r in db.execute("SELECT * FROM events WHERE task_id=? ORDER BY id",(ident,))]
            return task
