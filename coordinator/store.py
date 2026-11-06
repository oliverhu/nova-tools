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
from .approvals import approval_payload, approval_id
from .workers import WORKERS, INITIALLY_PAUSED_WORKERS, MINI_VM_WORKERS
from .outcomes import finish_outcome
from . import publication_policy
from .attempt_accounting import BINDING, correction_payload
from .attempt_allowances import allowance_payload
from .worker_status import validate_cli_status


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


RETRY_DELAY = 15 * 60
FIX_ATTEMPT_LIMIT = 20
REASSIGNMENT_LIMIT = 6


def reported_attempts(value):
    """Read cumulative counts from current and historical worker receipts."""
    if not isinstance(value, dict):
        return 0
    keys = ('fix_attempts_used', 'feature_attempts', 'attemptsConsumed', 'attemptsUsedCumulative')
    counts = [v for k, v in value.items() if k in keys and type(v) is int and v >= 0]
    return max([0, *counts, *(reported_attempts(v) for v in value.values() if isinstance(v, dict))])


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
CREATE TABLE IF NOT EXISTS approvals (
 id TEXT PRIMARY KEY, actor TEXT NOT NULL, payload TEXT NOT NULL, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS outcome_corrections (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL, generation INTEGER NOT NULL,
 actor TEXT NOT NULL, request TEXT NOT NULL, prior_state TEXT NOT NULL,
 prior_receipt TEXT NOT NULL, corrected_state TEXT NOT NULL, at REAL NOT NULL,
 UNIQUE(task_id,generation));
CREATE TABLE IF NOT EXISTS attempt_corrections (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL, generation INTEGER NOT NULL,
 request_id TEXT NOT NULL, actor TEXT NOT NULL, request TEXT NOT NULL,
 prior_state TEXT NOT NULL, corrected_state TEXT NOT NULL, at REAL NOT NULL,
 UNIQUE(task_id,request_id));
CREATE TABLE IF NOT EXISTS attempt_allowances (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL, worker TEXT NOT NULL,
 reassignment_count INTEGER NOT NULL, source_key TEXT UNIQUE NOT NULL,
 request_id TEXT NOT NULL, request TEXT NOT NULL, prior_state TEXT NOT NULL,
 granted_state TEXT NOT NULL, at REAL NOT NULL,
 UNIQUE(task_id,request_id), UNIQUE(task_id,worker,reassignment_count));
CREATE TABLE IF NOT EXISTS attempt_allowance_amendments (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL, worker TEXT NOT NULL,
 reassignment_count INTEGER NOT NULL, previous_grant_id TEXT UNIQUE NOT NULL,
 source_key TEXT UNIQUE NOT NULL, request_id TEXT NOT NULL, request TEXT NOT NULL,
 prior_state TEXT NOT NULL, granted_state TEXT NOT NULL, at REAL NOT NULL,
 UNIQUE(task_id,request_id));
"""


DEFAULT_STEPS = [
    {"name": "Implement and verify", "kind": "work", "instructions": "Implement, meaningfully test and merge the change in one session. Rebase and retest affected checks if main advances; never force-push main. Record the actual merged commit. DO NOT PUBLISH. The service creates separate hourly publish tasks. On completion or settled blocked execution, immediately pull the next eligible task."},
]

IOS_UPLOAD_POLICY = (
    "Also archive, sign, validate and upload the iOS app from the same pinned source to App Store Connect. "
    "Upload only: do not distribute via TestFlight, add testers/groups, enable automatic beta distribution, "
    "submit for App Review or release the iOS app publicly. Check existing automatic distribution settings before uploading. "
    "Verify Apple accepted the upload; record ios_upload with status='uploaded', source_commit, version, build_number, "
    "upload_ref (Apple upload/build identity or verified upload receipt), testflight_distributed=false and submitted_for_review=false. "
    "Inspect existing uploads before retrying to avoid duplicate builds. If one platform fails, retain the successful "
    "platform's evidence and report incomplete delivery; do not repeat its publication blindly. "
)


class Store:
    def __init__(self, path, clock=time.time, lease_seconds=180, heavy_worker=None):
        if heavy_worker is not None and heavy_worker not in WORKERS:
            raise Invalid("Unknown heavy compilation worker")
        self.heavy_worker = heavy_worker
        self.path, self.clock, self.lease_seconds = str(path), clock, lease_seconds
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            worker_columns = {r[1] for r in db.execute('PRAGMA table_info(workers)')}
            if 'cli_status' not in worker_columns:
                db.execute("ALTER TABLE workers ADD COLUMN cli_status TEXT NOT NULL DEFAULT '{}'")
            if 'cli_status_seen' not in worker_columns:
                db.execute('ALTER TABLE workers ADD COLUMN cli_status_seen REAL')

            db.executescript(publication_policy.SCHEMA)
            columns = {r[1] for r in db.execute("PRAGMA table_info(tasks)")}
            if "last_owner" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN last_owner TEXT")
                db.execute("UPDATE tasks SET last_owner=owner")
            if "task_type" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN task_type TEXT NOT NULL DEFAULT 'work'")
                db.execute("UPDATE tasks SET compute='standard' WHERE compute='heavy'")
            if 'fix_attempts_used' not in columns:
                db.execute('ALTER TABLE tasks ADD COLUMN fix_attempts_used INTEGER NOT NULL DEFAULT 0')
                for task in db.execute('SELECT id,result FROM tasks').fetchall():
                    receipts = [json.loads(r[0]) for r in db.execute('SELECT receipt FROM receipts WHERE task_id=?', (task['id'],))]
                    used = max([reported_attempts(json.loads(task['result'])), *(reported_attempts(r) for r in receipts)])
                    db.execute('UPDATE tasks SET fix_attempts_used=? WHERE id=?', (used, task['id']))
            if 'retry_after' not in columns:
                # Legacy blocks need classification before enabling automatic retry.
                db.execute('ALTER TABLE tasks ADD COLUMN retry_after REAL')
            if 'attempt_exception' not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN attempt_exception TEXT NOT NULL DEFAULT '{}'")
            if 'attempt_allowance' not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN attempt_allowance TEXT NOT NULL DEFAULT '{}'")
            if 'reassignment_count' not in columns:
                db.execute('BEGIN IMMEDIATE')
                db.execute('ALTER TABLE tasks ADD COLUMN reassignment_count INTEGER NOT NULL DEFAULT 0')
                db.execute('ALTER TABLE tasks ADD COLUMN attempts_host TEXT')
                db.execute('ALTER TABLE tasks ADD COLUMN attempt_base INTEGER NOT NULL DEFAULT 0')
                db.execute('ALTER TABLE tasks ADD COLUMN total_fix_attempts INTEGER NOT NULL DEFAULT 0')
                for task in db.execute('SELECT * FROM tasks').fetchall():
                    host, changes, switch_generation = None, 0, 0
                    for event in db.execute("SELECT actor,detail FROM events WHERE task_id=? AND kind='claimed' ORDER BY id", (task['id'],)):
                        if host and host != event['actor']:
                            changes += 1
                            match = re.search(r'generation (\d+)', event['detail'])
                            switch_generation = int(match[1]) if match else task['generation']
                        host = event['actor']
                    base = max([0, *(reported_attempts(json.loads(r[0])) for r in db.execute(
                        'SELECT receipt FROM receipts WHERE task_id=? AND generation<?', (task['id'], switch_generation)))])
                    total = max(base, task['fix_attempts_used'])
                    db.execute('UPDATE tasks SET reassignment_count=?,attempts_host=?,attempt_base=?,total_fix_attempts=?,fix_attempts_used=? WHERE id=?',
                               (changes, host or task['owner'] or task['last_owner'], base, total, total-base, task['id']))
                    if task['status']=='blocked' and task['fix_attempts_used']>=FIX_ATTEMPT_LIMIT and json.loads(task['result']).get('execution_stopped') is True:
                        db.execute('UPDATE tasks SET retry_after=COALESCE(retry_after,?) WHERE id=?', (self.clock()+RETRY_DELAY,task['id']))
                db.commit()
            repo_columns = {r[1] for r in db.execute("PRAGMA table_info(repositories)")}
            if "published_task_created" not in repo_columns:
                db.execute("ALTER TABLE repositories ADD COLUMN published_task_created REAL NOT NULL DEFAULT 0")
            # Retire obsolete shared locks, retaining their evidence in events.
            for lock in db.execute("SELECT * FROM locks").fetchall():
                self.event(db, "service", "legacy-lock-retired", lock["task_id"], encoded(dict(lock)))
            db.execute("DELETE FROM locks")
            for name in WORKERS:
                db.execute("INSERT OR IGNORE INTO workers(id,paused) VALUES(?,?)",
                           (name, int(name in INITIALLY_PAUSED_WORKERS)))

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
        for field in ("requirements", "dependencies", "steps", "result", "attempt_exception", "attempt_allowance"):
            task[field] = json.loads(task[field])
        task["current_step"] = task["steps"][task["step"]] if task["step"] < len(task["steps"]) else None
        task["execution_policy"] = ("Publish only the pinned release target; no global locks. Verify external outcome before reporting success."
            if task["current_step"] and task["current_step"]["kind"] == "publish" else
            "Implement, test and merge. DO NOT PUBLISH Nova from ordinary work. Only engine rebuilds are heavy. No global locks; ownership does not expire on missed heartbeat. If UI test tooling blocks a feature, enqueue or reuse a concrete UI-tool repair task and wait on its task ID; preserve feature acceptance and prior attempt history. Product defects and missing account access are not UI-tool failures. For scheduling waits, return settled work through POST /api/tasks/ID/defer with claim identity, execution_stopped, external_state_checked, evidence, and worker and/or dependencies; keep it queued rather than blocked. After settled work, immediately pull again.")
        task["heartbeat_stale"] = task["status"] == "running" and (task["lease_until"] or 0) <= self.clock()
        task['fix_attempt_limit'] = self.attempt_limit(row)
        task['fix_attempts_remaining'] = max(0, task['fix_attempt_limit'] - task['fix_attempts_used'])
        task['attempt_allowance_active'] = self.allowance_active(row)
        if task['attempt_allowance_active']:
            # Assignment identity is host + monotonic reassignment index. A
            # truthful accounting repair may change its lifetime baseline while
            # the immutable grant request retains the original observed base.
            task['attempt_allowance']['attempt_base'] = task['attempt_base']
        task['reassignment_limit'] = REASSIGNMENT_LIMIT
        task['execution_policy'] += (
            ' For external approval waits, finish with outcome pending_external_approval; never retry or resubmit approval requests automatically. '
            'Record approval_request with thread_id and question_id in pending receipts. On the actual human response, immediately run approve --receipt with its source message and every explicitly covered pending task/generation, even while another claim is active. Read back queued eligibility before saying resumed; approval is not test qualification. '
            'After a verified human approval response, the last owning worker may explicitly retry with approval_received=true, approval_evidence naming the response, and settled execution/external-state evidence. '
            'Artifact-location and verified nonhuman setup/dependency waits are scheduling waits; an administrator may correct a mistaken approval classification through defer with the supported audited wait_correction. Setup corrections require existing prerequisites and refuse any recorded human stop or approval request. '
            f"Settled blocked tasks retry after 15 minutes on the current host until {task['fix_attempt_limit']} fix attempts are used, then automatically reassign to another eligible host. "
            'An actual claim by a different host resets fix_attempts_used to zero and consumes one of at most 6 lifetime host reassignments. '
            'Same-host retries do not reset the count; the initial assignment is not a reassignment. '
            'Read all prior receipts and inspect external state before any retry side effects; apply a concrete correction. '
            'Record fix_attempts_used for the current host assignment in receipts; preserve lifetime evidence in total_fix_attempts. '
            'This user policy supersedes older instructions that forbid resetting on host transfer.')
        if task['attempt_allowance_active']:
            task['execution_policy'] += (
                f" Verified task/current-host allowance {task['attempt_allowance']['id']} sets this assignment's "
                f"limit to {task['fix_attempt_limit']} with {task['fix_attempts_remaining']} attempts remaining. "
                'Use these fresh API fields and exact task/host/assignment/grant bindings for admission; '
                'the default twenty remains for other assignments. Attendance and consent remain separate.')
        if task['task_type']=='publish' and task['result'].get('release_scope')=='ios_upload_only':
            from .ios_release import POLICY, TARGET
            task['delivery_targets'] = [TARGET]
            task['execution_policy'] += ' ' + POLICY
        elif (task['task_type']=='publish' and task['status'] not in ('completed','cancelled')
                and task['result'].get('release_target',{}).get('repository')=='oliverhu/nova-browser'):
            task['delivery_targets'] = ['macos_public_release','ios_app_store_connect_upload_only']
            task['execution_policy'] += ' ' + IOS_UPLOAD_POLICY
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
        if requirements.get("worker") not in (None, *WORKERS):
            raise Invalid("Unknown worker")
        if compute == "heavy" and self.heavy_worker and requirements.get("worker") not in (None, self.heavy_worker):
            raise Invalid("Heavy compilation requires " + self.heavy_worker)
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

    def set_worker_state(self, actor, worker, body):
        if actor != "admin" or worker not in WORKERS or type(body.get("paused")) is not bool:
            raise Invalid("Administrator, known worker and boolean paused are required")
        reason = bounded(body.get("reason"), "reason", 2000)
        with self.transaction() as db:
            if db.execute("SELECT id FROM tasks WHERE owner=? AND status IN ('running','held')", (worker,)).fetchone():
                raise Conflict("Settle owned work before changing worker state")
            db.execute("UPDATE workers SET paused=? WHERE id=?", (int(body["paused"]), worker))
            self.event(db, actor, "worker-state-changed", None, encoded({"worker": worker, "paused": body["paused"], "reason": reason}))
            return {"worker": worker, "paused": body["paused"]}

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
        cli_status = None
        if 'cli_status' in body:
            try:
                cli_status = validate_cli_status(body['cli_status'])
            except ValueError:
                raise Invalid('Invalid CLI status metadata') from None
        with self.transaction() as db:
            self.expire(db)
            if not db.execute("SELECT id FROM workers WHERE id=?", (worker,)).fetchone():
                raise Invalid("Unknown worker")
            db.execute("UPDATE workers SET capacity=?,seen=? WHERE id=?", (encoded(normalized), self.clock(), worker))
            if cli_status is not None:
                db.execute('UPDATE workers SET cli_status=?,cli_status_seen=? WHERE id=?',
                           (encoded(cli_status), self.clock(), worker))
            return {"worker": worker, "heartbeat_seconds": 30, "lease_seconds": self.lease_seconds}

    def fits(self, task, worker):
        if task["compute"] == "heavy" and self.heavy_worker and worker["id"] != self.heavy_worker:
            return False
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
        workers = [w for w in workers if self.fits(task, w) and self.assignment_allowed(task, w['id'])]
        if not workers:
            return None
        if task['status'] == 'blocked':
            # Automatic retries retain the assignment until its budget is used.
            # Explicit scheduling handoffs use defer and remain independently routed.
            if task['attempts_host'] and task['fix_attempts_used'] < self.attempt_limit(task):
                return next((w['id'] for w in workers if w['id'] == task['attempts_host']), None)
            workers.sort(key=lambda w: (w['id'] == task['attempts_host'], w['last_claim'], w['id']))
            return workers[0]['id']
        # Keep subsequent stages with the session that owns the checkout.
        # An unavailable owner can be replaced using its recorded evidence.
        previous = self.available_owner(db, task)
        if previous:
            return previous["id"] if any(w["id"] == previous["id"] for w in workers) else None
        # Heavy work stays queued for a healthy but busy mini. Fall back to the
        # Mac when the mini is offline/paused or cannot meet hard requirements.
        if task["compute"] == "heavy":
            mini = db.execute("SELECT * FROM workers WHERE id='oliver-mini' AND paused=0 AND seen>?", (self.clock()-90,)).fetchone()
            if mini and self.fits(task, mini) and self.assignment_allowed(task, mini["id"]):
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
        return previous if previous and self.fits(task, previous) and self.assignment_allowed(task,previous['id']) else None

    def assignment_allowed(self, task, worker):
        if task['attempts_host'] and task['attempts_host'] != worker:
            return task['reassignment_count'] < REASSIGNMENT_LIMIT
        return task['fix_attempts_used'] < self.attempt_limit(task)

    @staticmethod
    def allowance_active(task):
        grant = json.loads(task['attempt_allowance'])
        return bool(grant and grant.get('expired_at') is None
                    and grant['worker'] == task['attempts_host']
                    and grant['reassignment_count'] == task['reassignment_count']
                    and task['generation'] >= grant['request']['generation'])

    def host_attempt_limit(self, task):
        grant = json.loads(task['attempt_allowance'])
        return max(FIX_ATTEMPT_LIMIT, grant['effective_limit']) if self.allowance_active(task) else FIX_ATTEMPT_LIMIT

    def attempt_limit(self, task):
        limit = self.host_attempt_limit(task)
        exception = json.loads(task['attempt_exception'])
        if exception and (self.exception_available(task, exception['request']['worker']) or
                (task['status'] == 'running' and exception.get('consumed_generation') == task['generation'])):
            limit = max(limit, exception['request']['attempt_number'])
        return limit

    @staticmethod
    def exception_available(task, worker):
        exception = json.loads(task['attempt_exception'])
        if not exception or exception.get('consumed_generation') is not None or exception.get('invalidated_at') is not None:
            return False
        request = exception['request']
        return (task['status'] == 'queued' and request['worker'] == worker == task['attempts_host']
                and request['generation'] == task['generation']
                and request['expected_used'] == FIX_ATTEMPT_LIMIT
                and request['attempt_number'] == FIX_ATTEMPT_LIMIT + 1
                and task['fix_attempts_used'] == request['expected_used']
                and exception['attempt_base'] == task['attempt_base']
                and exception['reassignment_count'] == task['reassignment_count'])

    def resources(self, row, worker):
        return []

    def scheduling(self, db, row):
        """Describe the next execution, separately from historical ownership."""
        result = {"next_worker": None, "next_worker_label": "—", "ready": False,
                  "waiting_reason": "", "waiting_dependencies": []}
        if row['status'] == 'running':
            return {**result, 'next_worker': row['owner'], 'next_worker_label': row['owner']}
        policy_reason = publication_policy.admission(self, db, row) if row['status'] in ('queued', 'blocked') else ''
        if policy_reason and row['status'] in ('queued', 'blocked'):
            return {**result, 'waiting_reason': policy_reason, 'next_worker_label': 'Publication policy wait'}
        if row['status'] == 'pending_external_approval':
            return {**result, 'next_worker_label': 'Awaiting external approval',
                    'waiting_reason': 'No automatic retries. Resume only after approval is verified.'}
        if row['status'] == 'blocked':
            candidate = self.candidate(db, row)
            target = json.loads(row['requirements']).get('worker') or (
                row['attempts_host'] if row['fix_attempts_used'] < self.attempt_limit(row) else candidate)
            pending = [dict(db.execute('SELECT id,title,status FROM tasks WHERE id=?', (dep,)).fetchone())
                       for dep in json.loads(row['dependencies'])]
            pending = [d for d in pending if d['status'] != 'completed']
            reason = self.retry_wait(row)
            if not reason and pending:
                reason = 'Waiting for: ' + ', '.join(d['title'] for d in pending)
            if not reason and not candidate:
                reason = 'Waiting for an available worker that meets requirements'
            exhausted = row['fix_attempts_used'] >= self.attempt_limit(row) and row['reassignment_count'] >= REASSIGNMENT_LIMIT
            return {**result, 'next_worker': None if exhausted else target,
                    'next_worker_label': 'Retry limits reached' if exhausted else target or 'Either available Mac',
                    'waiting_dependencies': pending, 'ready': not reason,
                    'waiting_reason': reason or 'Ready for automatic retry'}
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
        if not target and row['compute'] == 'heavy' and self.heavy_worker:
            target = self.heavy_worker
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

    def retry_wait(self, row):
        if row['fix_attempts_used'] >= self.attempt_limit(row) and row['reassignment_count'] >= REASSIGNMENT_LIMIT:
            return f'{self.attempt_limit(row)} attempts on this assignment and {REASSIGNMENT_LIMIT} lifetime host reassignments exhausted'
        if json.loads(row['result']).get('execution_stopped') is not True:
            return 'Prior execution must be confirmed stopped before retry'
        if row['retry_after'] is None:
            return 'Legacy block requires classification before retry'
        if row['retry_after'] > self.clock():
            return 'Automatic retry after ' + time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(row['retry_after']))
        return ''

    def defer(self, actor, ident, body):
        """Return settled work to the queue with explicit scheduling conditions."""
        evidence = bounded(body.get('evidence'), 'evidence', 16000)
        if body.get('execution_stopped') is not True or body.get('external_state_checked') is not True:
            raise Invalid('Waiting requires settled execution and checked external state')
        with self.transaction() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (ident,)).fetchone()
            correction = body.get('wait_correction')
            correcting_approval = row and row['status'] == 'pending_external_approval'
            if correcting_approval:
                if actor != 'admin':
                    raise Conflict('Only administrators may correct an approval classification')
                if (not isinstance(correction,dict) or correction.get('category') not in ('artifact_access', 'setup_dependency')
                        or correction.get('approval_not_required') is not True):
                    raise Invalid('Explicit supported wait classification and approval_not_required evidence are required')
                bounded(correction.get('reason'), 'wait_correction.reason', 12000)
                if json.loads(row['result']).get('human_stop') is True:
                    raise Conflict('A recorded human stop cannot be reclassified as a scheduling wait')
                if correction['category'] == 'setup_dependency':
                    # This is an explicit administrative classification repair,
                    # never evidence that consent was received. Preserve bound
                    # questions and legacy requests anywhere in receipt history.
                    prior = [json.loads(row['result'])]
                    prior.extend(json.loads(r['receipt']).get('receipt', {}) for r in db.execute(
                        'SELECT receipt FROM receipts WHERE task_id=?', (ident,)))
                    if any(not isinstance(receipt, dict) or receipt.get('human_stop') is True
                           or 'approval_request' in receipt for receipt in prior):
                        raise Conflict('Recorded human stops or approval requests require their original approval workflow')
                    if json.loads(row['result']).get('execution_stopped') is not True:
                        raise Conflict('The recorded execution has not settled')
                    if not isinstance(body.get('dependencies'), list) or not body['dependencies']:
                        raise Invalid('Setup wait correction requires explicit existing prerequisite task IDs')
            elif correction is not None:
                raise Conflict('Wait correction applies only to a pending external approval')
            if not row or (not correcting_approval and row['status'] not in ('queued', 'running', 'blocked', 'failed')):
                raise Conflict('Only settled work can return to the waiting queue')
            if body.get('generation') != row['generation']:
                raise Conflict('Task generation changed; reload before scheduling')
            if actor != 'admin':
                self.owned(db, actor, ident, body.get('claim_token'), body.get('generation'))
            elif row['status'] == 'running':
                raise Conflict('Running work must be deferred by its owning worker')
            req = json.loads(row['requirements'])
            if 'worker' in body:
                if body['worker'] not in WORKERS:
                    raise Invalid('Unknown destination worker')
                if row['attempts_host'] and body['worker'] != row['attempts_host'] and row['reassignment_count'] >= REASSIGNMENT_LIMIT:
                    raise Conflict('6 host reassignments exhausted')
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
            if correcting_approval:
                self.event(db, actor, 'approval-classification-corrected', ident, encoded({
                    'previous_status':'pending_external_approval','status':'queued','wait_correction':correction,
                    'generation':row['generation'],'dependencies':deps,
                    'worker':req.get('worker'),'evidence':evidence}))
            task = db.execute('SELECT * FROM tasks WHERE id=?', (ident,)).fetchone()
            return {**self.public_task(task), **self.scheduling(db,task)}

    def claim(self, worker, task_id=None):
        with self.transaction() as db:
            self.expire(db)
            rows = db.execute("SELECT * FROM tasks WHERE status IN ('queued','blocked') ORDER BY priority DESC,created,id").fetchall()
            for row in rows:
                if task_id and row["id"] != task_id:
                    continue
                if publication_policy.admission(self, db, row):
                    continue
                if row['status'] == 'blocked' and (self.retry_wait(row) or self.candidate(db, row) != worker):
                    continue
                deps = json.loads(row["dependencies"])
                if any(db.execute("SELECT status FROM tasks WHERE id=?", (d,)).fetchone()[0] != "completed" for d in deps):
                    continue
                own = db.execute("SELECT * FROM workers WHERE id=? AND paused=0 AND seen>?", (worker,self.clock()-90)).fetchone()
                if not own or not self.fits(row,own) or not self.assignment_allowed(row,worker) or db.execute("SELECT id FROM tasks WHERE owner=? AND status IN ('running','held')", (worker,)).fetchone():
                    continue
                previous = self.available_owner(db, row) if row['status'] == 'queued' else None
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
                if row['status'] == 'blocked':
                    self.event(db, worker, 'automatic-retry', row['id'], encoded({'previous_worker': row['last_owner'], 'fix_attempts_used': row['fix_attempts_used']}))
                changed_host = bool(row['attempts_host'] and row['attempts_host'] != worker)
                if changed_host:
                    self.expire_attempt_allowance(db, row)
                    self.event(db, worker, 'host-reassigned', row['id'], encoded({'from':row['attempts_host'],'to':worker,
                        'reassignment_count':row['reassignment_count']+1,'previous_assignment_attempts':row['fix_attempts_used'],
                        'total_fix_attempts':row['total_fix_attempts']}))
                db.execute('UPDATE tasks SET reassignment_count=reassignment_count+?,attempts_host=?,fix_attempts_used=?,attempt_base=? WHERE id=?',
                    (int(changed_host),worker,0 if changed_host else row['fix_attempts_used'],
                     row['total_fix_attempts'] if changed_host else row['attempt_base'],row['id']))
                if self.exception_available(row, worker):
                    exception = json.loads(row['attempt_exception'])
                    exception['consumed_generation'] = generation
                    # Consume the single extra attempt atomically with its claim,
                    # including when that run later fails before native input.
                    db.execute('UPDATE tasks SET attempt_exception=?,fix_attempts_used=fix_attempts_used+1,total_fix_attempts=total_fix_attempts+1 WHERE id=?',
                               (encoded(exception), row['id']))
                    self.event(db, worker, 'attempt-exception-consumed', row['id'], encoded({
                        'approval_id': exception['request']['approval_id'], 'generation': generation,
                        'attempt_number': FIX_ATTEMPT_LIMIT + 1}))
                db.execute("""UPDATE tasks SET status='running',owner=?,claim_hash=?,generation=?,lease_until=?,updated=?,retry_after=NULL WHERE id=?""",
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
        try:
            outcome = finish_outcome(receipt, body.get("outcome"))
        except ValueError as error:
            raise Invalid(str(error)) from None
        with self.transaction() as db:
            self.expire(db)
            row = self.owned(db,worker,ident,body.get("claim_token"),body.get("generation"))
            target = json.loads(row["result"]).get("release_target")
            if target:
                receipt = {**receipt, "release_target": target}
            ios_only = json.loads(row['result']).get('release_scope') == 'ios_upload_only'
            receipt = {key: value for key, value in receipt.items()
                       if key not in ('release_scope', 'release_authorization')}
            if ios_only:
                receipt = {**receipt, 'release_scope': 'ios_upload_only',
                           'release_authorization': json.loads(row['result'])['release_authorization']}
            previous = db.execute("SELECT receipt FROM receipts WHERE task_id=? AND generation=? AND step=?",
                                  (ident,row["generation"],body.get("step"))).fetchone()
            if previous:
                if json.loads(previous[0]) != {"outcome":outcome,"receipt":receipt}:
                    raise Conflict("Completion already recorded with different evidence")
                return self.public_task(row)
            if row["status"] != "running" or body.get("step") != row["step"]:
                raise Conflict("Task/step is not running; reconcile held outcomes separately")
            if 'fix_attempts_used' in receipt and (type(receipt['fix_attempts_used']) is not int or receipt['fix_attempts_used'] < row['fix_attempts_used']):
                raise Invalid('fix_attempts_used must preserve the count within this host assignment')
            # Canonical counts are per assignment; older receipt aliases are lifetime totals.
            reported = receipt.get('fix_attempts_used', max(0,reported_attempts(receipt)-row['attempt_base']))
            attempts = max(row['fix_attempts_used'], reported)
            if self.allowance_active(row) and attempts > self.host_attempt_limit(row):
                raise Invalid('Reported attempts exceed this verified host allowance')
            if outcome == 'blocked':
                # A failed execution consumes at least one attempt, even on an older client.
                attempts = max(attempts, min(self.attempt_limit(row), row['fix_attempts_used'] + 1))
            kind = json.loads(row["steps"])[row["step"]]["kind"]
            next_step = row["step"] + (outcome == "completed")
            status = ("completed" if next_step == len(json.loads(row["steps"])) else "queued") if outcome == "completed" else outcome
            if kind == "publish" and outcome == "completed":
                bounded(receipt.get("publication_ref"), "publication_ref", 2000)
                target = json.loads(row["result"]).get("release_target")
                if ios_only:
                    from .ios_release import validate_upload
                    validate_upload(receipt, target)
                elif target:
                    if receipt.get("published_commit") != target["commit"]:
                        raise Invalid("published_commit must match this publish task's exact release target")
                    if target['repository']=='oliverhu/nova-browser':
                        upload=receipt.get('ios_upload')
                        if (not isinstance(upload,dict) or upload.get('status')!='uploaded'
                                or upload.get('source_commit')!=target['commit']
                                or upload.get('testflight_distributed') is not False
                                or upload.get('submitted_for_review') is not False):
                            raise Invalid('Verified iOS App Store Connect upload-only receipt is required')
                        for field in ('version','build_number','upload_ref'):
                            bounded(upload.get(field), 'ios_upload.'+field, 2000)
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
            db.execute("UPDATE tasks SET status=?,step=?,updated=?,lease_until=NULL,progress=?,result=?,last_owner=owner,fix_attempts_used=?,total_fix_attempts=?,retry_after=? WHERE id=?",
                       (status,next_step,self.clock(),receipt["summary"],encoded(receipt),attempts,
                        row['total_fix_attempts']+attempts-row['fix_attempts_used'],
                        self.clock()+RETRY_DELAY if outcome=='blocked' else None,ident))
            self.event(db,worker,status,ident,receipt["summary"])
            return self.public_task(db.execute("SELECT * FROM tasks WHERE id=?", (ident,)).fetchone())

    def retry(self, worker, ident, body):
        # Only the owning worker can recover its settled failed/blocked attempt.
        with self.connection() as db:
            self.owned(db, worker, ident, body.get("claim_token"), body.get("generation"))
        return self.reconcile(worker, ident, {**body, "action": "retry"})

    @staticmethod
    def _correction_state(row):
        """Durable row snapshot without authentication or request fingerprints."""
        state = {k: row[k] for k in row.keys() if k not in ('claim_hash', 'request_hash')}
        for field in ('requirements', 'dependencies', 'steps', 'result', 'attempt_exception', 'attempt_allowance'):
            state[field] = json.loads(state[field])
        return state

    @staticmethod
    def _correction_readback(row):
        return {**dict(row), **{k: json.loads(row[k]) for k in
                ('request', 'prior_state', 'prior_receipt', 'corrected_state', 'granted_state') if k in row.keys()}}

    def correct_outcome(self, actor, ident, body):
        """Reopen only a completed work step whose original result says blocked.

        This is an accounting correction, not another finish, attempt or retry.
        Original receipts/events/result stay untouched, and old claims are fenced.
        """
        if actor != 'admin':
            raise Conflict('Outcome correction requires administrator identity')
        fields = {'generation', 'expected_status', 'expected_step', 'expected_result_sha256',
                  'expected_receipt_sha256', 'outcome', 'execution_stopped',
                  'external_state_checked', 'evidence'}
        if not isinstance(body, dict) or set(body) != fields:
            raise Invalid('Exact bounded outcome-correction fields required')
        if (body['expected_status'] != 'completed' or body['outcome'] != 'blocked'
                or type(body['generation']) is not int or body['generation'] < 1
                or type(body['expected_step']) is not int or body['expected_step'] < 1):
            raise Invalid('Only an exact completed-to-blocked correction is supported')
        if any(body[k] is not True for k in ('execution_stopped', 'external_state_checked')):
            raise Invalid('Outcome correction requires stopped execution and checked external state')
        for key in ('expected_result_sha256', 'expected_receipt_sha256'):
            if not isinstance(body[key], str) or not re.fullmatch('[0-9a-f]{64}', body[key]):
                raise Invalid('Exact canonical JSON SHA-256 required: ' + key)
        request = {**body, 'evidence': bounded(body['evidence'], 'evidence', 6000)}
        correction_id = hashlib.sha256(encoded({'task': ident, 'request': request}).encode()).hexdigest()
        with self.transaction() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (ident,)).fetchone()
            if not row or row['generation'] != request['generation']:
                raise Conflict('Task generation changed; reload before correcting its outcome')
            prior = db.execute('SELECT * FROM outcome_corrections WHERE task_id=? AND generation=?',
                               (ident, row['generation'])).fetchone()
            if prior:
                if prior['id'] != correction_id or self._correction_state(row) != json.loads(prior['corrected_state']):
                    raise Conflict('Outcome already corrected or current state changed; inspect its audit')
                return {'task': self.public_task(row), 'correction': self._correction_readback(prior), 'replayed': True}
            steps = json.loads(row['steps'])
            if row['status'] != 'completed' or row['step'] != request['expected_step'] or row['step'] != len(steps):
                raise Conflict('Exact current completed final step required')
            result = json.loads(row['result'])
            side_effect_fields = {'publication_ref', 'published_commit', 'release_commit', 'release_target', 'ios_upload', 'merge'}
            if row['task_type'] != 'work' or any(s['kind'] != 'work' for s in steps) or side_effect_fields.intersection(result):
                raise Conflict('Publish, engine or repository side-effect corrections are unsupported')
            previous = db.execute('SELECT receipt FROM receipts WHERE task_id=? AND generation=? AND step=?',
                                  (ident, row['generation'], row['step'] - 1)).fetchone()
            envelope = json.loads(previous['receipt']) if previous else None
            if (not envelope or envelope.get('outcome') != 'completed' or envelope.get('receipt') != result
                    or result.get('outcome') != 'blocked' or result.get('acceptance_complete') is not False
                    or result.get('execution_stopped') is not True or result.get('external_state_checked') is not True):
                raise Conflict('Original completed receipt must explicitly prove blocked, incomplete, stopped and checked work')
            for value, key in ((result, 'expected_result_sha256'), (envelope, 'expected_receipt_sha256')):
                if hashlib.sha256(encoded(value).encode()).hexdigest() != request[key]:
                    raise Conflict('Original completion evidence changed; reload exact hashes')
            if db.execute('SELECT 1 FROM locks WHERE task_id=?', (ident,)).fetchone():
                raise Conflict('Unexpected retained task resource; explicit disposition required')
            before = self._correction_state(row)
            db.execute("UPDATE tasks SET status='blocked',step=step-1,last_owner=COALESCE(owner,last_owner),"
                       "owner=NULL,claim_hash=NULL,lease_until=NULL,retry_after=NULL,updated=? WHERE id=?",
                       (self.clock(), ident))
            current = db.execute('SELECT * FROM tasks WHERE id=?', (ident,)).fetchone()
            db.execute('INSERT INTO outcome_corrections VALUES(?,?,?,?,?,?,?,?,?)',
                       (correction_id, ident, row['generation'], actor, encoded(request), encoded(before),
                        encoded(envelope), encoded(self._correction_state(current)), self.clock()))
            self.event(db, actor, 'outcome-corrected', ident, encoded({
                'correction_id': correction_id, 'generation': row['generation'],
                'previous_status': 'completed', 'status': 'blocked',
                'previous_step': row['step'], 'step': current['step'],
                'evidence': request['evidence']}))
            audit = db.execute('SELECT * FROM outcome_corrections WHERE id=?', (correction_id,)).fetchone()
            return {'task': self.public_task(current), 'correction': self._correction_readback(audit), 'replayed': False}

    def correct_attempts(self, actor, ident, body):
        """Reconcile omitted attempts without changing ownership or outcomes."""
        if actor != 'admin':
            raise Conflict('Attempt correction requires administrator identity')
        try:
            request = correction_payload(body, WORKERS, None, REASSIGNMENT_LIMIT)
        except ValueError as error:
            raise Invalid(str(error)) from None
        proofs = [request['owner_proof'], *request['historical_proof']]
        if any(proof['task'] != ident for proof in proofs):
            raise Invalid('Every evidence proof must bind this exact task')
        correction_id = hashlib.sha256(encoded({'task': ident, 'request': request}).encode()).hexdigest()
        with self.transaction() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (ident,)).fetchone()
            if not row:
                raise Conflict('Unknown task for attempt correction')
            prior = db.execute('SELECT * FROM attempt_corrections WHERE task_id=? AND request_id=?',
                               (ident, request['request_id'])).fetchone()
            if prior:
                if prior['id'] != correction_id:
                    raise Conflict('Correction request_id already binds different evidence or counters')
                return {'task': self.public_task(row), 'correction': self._correction_readback(prior), 'replayed': True}
            if (row['generation'] != request['generation']
                    or any(row[key] != request['expected'][key] for key in BINDING)):
                raise Conflict('Task ownership, generation or counters changed; reload before correcting')
            if row['task_type'] != 'work' or any(s['kind'] != 'work' for s in json.loads(row['steps'])):
                raise Conflict('Only ordinary work attempt accounting can be corrected')
            if row['status'] == 'completed':
                if (row['step'] != len(json.loads(row['steps'])) or row['step'] < 1
                        or row['owner'] != request['owner_proof']['worker']):
                    raise Conflict('Exact completed final step and owning worker required')
                previous = db.execute('SELECT receipt FROM receipts WHERE task_id=? AND generation=? AND step=?',
                                      (ident, row['generation'], row['step'] - 1)).fetchone()
                envelope = json.loads(previous['receipt']) if previous else None
                result = json.loads(row['result'])
                if (not envelope or envelope.get('outcome') != 'completed'
                        or envelope.get('receipt') != result or result.get('outcome') != 'completed'
                        or result.get('acceptance_complete') is not True
                        or result.get('execution_stopped') is not True
                        or result.get('external_state_checked') is not True
                        or result.get('unsettled_operations') != []):
                    raise Conflict('Original receipt must explicitly prove completed acceptance and settled execution')
                for value, key in ((result, 'expected_result_sha256'), (envelope, 'expected_receipt_sha256')):
                    if hashlib.sha256(encoded(value).encode()).hexdigest() != request['completion_proof'][key]:
                        raise Conflict('Original completion evidence changed; reload exact hashes')
                if db.execute('SELECT 1 FROM locks WHERE task_id=?', (ident,)).fetchone():
                    raise Conflict('Unexpected retained task resource; explicit disposition required')
            used = request['corrected']['fix_attempts_used']
            exception = json.loads(row['attempt_exception'])
            consumed_extra = (row['fix_attempts_used'] == FIX_ATTEMPT_LIMIT + 1
                              and exception.get('consumed_generation') == row['generation'])
            if used > self.host_attempt_limit(row) and not (consumed_extra and used == row['fix_attempts_used']):
                raise Invalid('Correction cannot grant an attempt above the existing host limit')
            def transfer_for(proof):
                claims = db.execute("SELECT id,detail FROM events WHERE task_id=? AND actor=? AND kind='claimed'",
                                    (ident, proof['worker']))
                claim = next((c for c in claims if re.search(
                    r'\bgeneration ' + str(proof['generation']) + r';', c['detail'])), None)
                if not claim:
                    raise Conflict('Historical proof does not match an actual service-owned assignment')
                return db.execute("SELECT id,detail FROM events WHERE task_id=? AND kind='host-reassigned' AND id>? ORDER BY id LIMIT 1",
                                  (ident, claim['id'])).fetchone()

            accounted = set()
            for audit in db.execute('SELECT request FROM attempt_corrections WHERE task_id=?', (ident,)):
                for proof in json.loads(audit['request'])['historical_proof']:
                    accounted.add(transfer_for(proof)['id'])
            for proof in request['historical_proof']:
                # Claim identity comes from service history; file provenance and
                # omitted counts are explicitly verified by the administrator.
                transfer = transfer_for(proof)
                transferred = json.loads(transfer['detail']) if transfer else {}
                if (transferred.get('from') != proof['worker']
                        or transferred.get('previous_assignment_attempts') != proof['recorded_attempts']):
                    raise Conflict('Historical recorded attempts do not match the actual host transfer')
                assignment = (ident, proof['worker'], transferred['reassignment_count'] - 1)
                grants = db.execute('SELECT granted_state FROM attempt_allowances WHERE task_id=? AND worker=? AND reassignment_count=? '
                    'UNION ALL SELECT granted_state FROM attempt_allowance_amendments WHERE task_id=? AND worker=? AND reassignment_count=?',
                    (*assignment, *assignment)).fetchall()
                prior_limit = max([FIX_ATTEMPT_LIMIT, *(json.loads(g['granted_state'])['attempt_allowance']['effective_limit'] for g in grants)])
                if proof['fix_attempts_used'] > prior_limit:
                    raise Invalid('Historical attempts exceed that assignment\'s verified limit')
                if transfer['id'] in accounted:
                    raise Conflict('Historical assignment has already been accounted for')
                accounted.add(transfer['id'])
                if not db.execute('SELECT 1 FROM receipts WHERE task_id=? AND generation=? AND step=?',
                                  (ident, proof['generation'], proof['step'])).fetchone():
                    raise Conflict('Historical assignment has no settled service receipt')
            before = {key: row[key] for key in ('generation', *BINDING)}
            new = request['corrected']
            db.execute('UPDATE tasks SET fix_attempts_used=?,total_fix_attempts=?,attempt_base=? WHERE id=?',
                       (new['fix_attempts_used'], new['total_fix_attempts'], new['attempt_base'], ident))
            current = db.execute('SELECT * FROM tasks WHERE id=?', (ident,)).fetchone()
            after = {key: current[key] for key in ('generation', *BINDING)}
            db.execute('INSERT INTO attempt_corrections VALUES(?,?,?,?,?,?,?,?,?)',
                       (correction_id, ident, row['generation'], request['request_id'], actor,
                        encoded(request), encoded(before), encoded(after), self.clock()))
            self.event(db, actor, 'attempts-corrected', ident, encoded({
                'correction_id': correction_id, 'request_id': request['request_id'],
                'before': before, 'after': after, 'evidence': request['evidence']}))
            audit = db.execute('SELECT * FROM attempt_corrections WHERE id=?', (correction_id,)).fetchone()
            return {'task': self.public_task(current), 'correction': self._correction_readback(audit), 'replayed': False}

    def _approval_readback(self, db, row):
        payload = json.loads(row['payload'])
        tasks = []
        for entry in payload['tasks']:
            task = db.execute('SELECT * FROM tasks WHERE id=?', (entry['id'],)).fetchone()
            tasks.append({**{k:task[k] for k in ('id','status','generation','owner')},
                          'applied_generation':entry['generation'], **self.scheduling(db,task)})
        return {'approval_id':row['id'],'recorded':True,'actor':row['actor'],'recorded_at':row['at'],
                'source':payload['source'],'response':payload['response'],'scope':payload['scope'],'tasks':tasks}

    def approval_detail(self, actor, ident):
        with self.connection() as db:
            row = db.execute('SELECT * FROM approvals WHERE id=?',(ident,)).fetchone()
            if not row:
                raise Invalid('Unknown approval receipt')
            if actor not in ('admin', row['actor']):
                raise Conflict('Approval receipt belongs to another actor')
            return self._approval_readback(db,row)

    def apply_approval(self, actor, body):
        if actor not in ('admin', *WORKERS):
            raise Conflict('Authenticated approval actor required')
        payload = approval_payload(body)
        ident, serialized = approval_id(payload), encoded(payload)
        claims = {t['id']:t for t in body['tasks']}
        with self.transaction() as db:
            prior = db.execute('SELECT * FROM approvals WHERE id=?',(ident,)).fetchone()
            if prior:
                if prior['actor'] != actor or prior['payload'] != serialized:
                    raise Conflict('This source response was already applied with different scope or evidence')
                return {**self._approval_readback(db,prior),'replayed':True}
            rows = []
            # Validate the entire scope before making any transition.
            for entry in payload['tasks']:
                row = db.execute('SELECT * FROM tasks WHERE id=?',(entry['id'],)).fetchone()
                if not row or row['status'] != 'pending_external_approval' or row['generation'] != entry['generation']:
                    raise Conflict('Covered task is no longer pending at the supplied generation: '+entry['id'])
                if actor != 'admin':
                    self.owned(db,actor,row['id'],claims[row['id']].get('claim_token'),entry['generation'])
                receipt = json.loads(row['result'])
                if receipt.get('execution_stopped') is not True:
                    raise Conflict('Prior execution has not settled: '+row['id'])
                request = receipt.get('approval_request')
                if request is not None and (not isinstance(request,dict) or any(
                    request.get(k) != payload['source'][k] for k in ('thread_id','question_id'))):
                    raise Conflict('Response does not match the task approval question: '+row['id'])
                if receipt.get('human_stop') is True:
                    raise Conflict('A recorded human stop requires separate explicit reconciliation: '+row['id'])
                if row['fix_attempts_used'] >= self.attempt_limit(row) and row['reassignment_count'] >= REASSIGNMENT_LIMIT:
                    raise Conflict('Retry limits exhausted: '+row['id'])
                rows.append((row,entry))
            db.execute('INSERT INTO approvals VALUES(?,?,?,?)',(ident,actor,serialized,self.clock()))
            for row,entry in rows:
                self.invalidate_attempt_exception(db, row)
                progress = 'Approval recorded: '+payload['response']+'. Awaiting execution/qualification. '+entry['scope_evidence']
                db.execute("UPDATE tasks SET status='queued',last_owner=COALESCE(owner,last_owner),owner=NULL,claim_hash=NULL,lease_until=NULL,retry_after=NULL,updated=?,progress=? WHERE id=?",
                           (self.clock(),progress,row['id']))
                self.event(db,actor,'approval-applied',row['id'],encoded({'approval_id':ident,
                    'generation':entry['generation'],'source':payload['source'],'response':payload['response'],
                    'scope':payload['scope'],'scope_evidence':entry['scope_evidence']}))
            record = db.execute('SELECT * FROM approvals WHERE id=?',(ident,)).fetchone()
            return {**self._approval_readback(db,record),'replayed':False}

    def apply_attempt_exception(self, actor, body):
        """Bind one explicitly verified extra attempt to an existing approval.

        An approval's prose is never interpreted as a limit override. The caller
        must verify the actual human exception and explicitly bind its source.
        This supplemental record does not rewrite the immutable approval payload.
        """
        if actor not in ('admin', *WORKERS):
            raise Conflict('Authenticated approval actor required')
        fields = {'approval_id', 'source', 'task', 'generation', 'worker', 'expected_used',
                  'attempt_number', 'scope_evidence', 'exception_verified', 'execution_stopped', 'external_state_checked'}
        if not isinstance(body, dict) or set(body) != fields:
            raise Invalid('Exact bounded attempt-exception fields required')
        if any(body.get(k) is not True for k in ('exception_verified', 'execution_stopped', 'external_state_checked')):
            raise Invalid('Verify the explicit human exception and settled execution first')
        if (body['worker'] not in WORKERS
                or type(body['generation']) is not int or body['generation'] < 1
                or type(body['expected_used']) is not int or body['expected_used'] != FIX_ATTEMPT_LIMIT
                or type(body['attempt_number']) is not int or body['attempt_number'] != FIX_ATTEMPT_LIMIT + 1):
            raise Invalid(f'Only one extra attempt after {FIX_ATTEMPT_LIMIT} on the named current host is supported')
        if not re.fullmatch('[0-9a-f]{16}', str(body['task'])) or not re.fullmatch('[0-9a-f]{64}', str(body['approval_id'])):
            raise Invalid('Exact existing task and approval identities required')
        bounded(body['scope_evidence'], 'scope_evidence', 4000)
        with self.transaction() as db:
            approval = db.execute('SELECT * FROM approvals WHERE id=?', (body['approval_id'],)).fetchone()
            if not approval or (actor != 'admin' and approval['actor'] != actor):
                raise Conflict('Use this actor\'s existing verified approval')
            payload = json.loads(approval['payload'])
            if body['source'] != payload['source'] or not any(
                    t['id'] == body['task'] and t['generation'] == body['generation'] for t in payload['tasks']):
                raise Conflict('Exception source/task/generation must match the immutable approval')
            row = db.execute('SELECT * FROM tasks WHERE id=?', (body['task'],)).fetchone()
            if not row:
                raise Conflict('Unknown task')
            prior = json.loads(row['attempt_exception'])
            if prior:
                if prior['request'] != body or prior['actor'] != actor:
                    raise Conflict('This task already has an immutable single-attempt exception')
                return {'recorded': True, 'replayed': True, 'exception': prior,
                        'task': {**self.public_task(row), **self.scheduling(db, row)}}
            if self.allowance_active(row):
                raise Conflict('A finite host allowance cannot be stacked with a single-attempt exception')
            receipt = json.loads(row['result'])
            if (row['status'] != 'queued' or row['generation'] != body['generation']
                    or row['attempts_host'] != body['worker'] or row['fix_attempts_used'] != body['expected_used']
                    or receipt.get('execution_stopped') is not True or receipt.get('human_stop') is True
                    or (actor != 'admin' and (body['worker'] != actor or row['last_owner'] != actor))):
                raise Conflict(f'Task must remain settled, approved, queued at {FIX_ATTEMPT_LIMIT} on its original named host')
            state = self.approval_state(db, row) or {}
            if (state.get('state') != 'approved' or
                    state.get('evidence', {}).get('approval_id') != body['approval_id']):
                raise Conflict('Current task approval is not approved')
            exception = {'request': body, 'actor': actor, 'at': self.clock(),
                         'attempt_base': row['attempt_base'], 'reassignment_count': row['reassignment_count'],
                         'consumed_generation': None}
            db.execute('UPDATE tasks SET attempt_exception=?,updated=? WHERE id=?',
                       (encoded(exception), self.clock(), row['id']))
            self.event(db, actor, 'attempt-exception-approved', row['id'], encoded(exception))
            row = db.execute('SELECT * FROM tasks WHERE id=?', (row['id'],)).fetchone()
            return {'recorded': True, 'replayed': False, 'exception': exception,
                    'task': {**self.public_task(row), **self.scheduling(db, row)}}

    def apply_attempt_allowance(self, actor, body):
        """Grant a finite, human-authorized budget on one existing assignment."""
        if actor != 'admin':
            raise Conflict('Attempt allowances require administrator identity')
        try:
            request = allowance_payload(body, WORKERS)
        except ValueError as error:
            raise Invalid(str(error)) from None
        ident = request['task']
        grant_id = hashlib.sha256(encoded(request).encode()).hexdigest()
        source_key = hashlib.sha256(encoded(request['source']).encode()).hexdigest()
        with self.transaction() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (ident,)).fetchone()
            if not row:
                raise Conflict('Unknown task')
            prior = db.execute('SELECT * FROM attempt_allowances WHERE source_key=? OR (task_id=? AND request_id=?)',
                               (source_key, ident, request['request_id'])).fetchone()
            if prior:
                if prior['id'] != grant_id:
                    raise Conflict('Human source/request already binds different allowance evidence')
                return {'grant': self._correction_readback(prior), 'replayed': True,
                        'task': {**self.public_task(row), **self.scheduling(db, row)}}
            if db.execute('SELECT 1 FROM attempt_allowance_amendments WHERE source_key=? OR (task_id=? AND request_id=?)',
                          (source_key, ident, request['request_id'])).fetchone():
                raise Conflict('Human source/request already binds an allowance amendment')
            expected = request['expected']
            public = self.public_task(row)
            if (row['generation'] != request['generation']
                    or any(row[k] != expected[k] for k in BINDING)
                    or any(hashlib.sha256(encoded(public[k]).encode()).hexdigest() != expected[k + '_sha256']
                           for k in ('requirements', 'attempt_allowance', 'attempt_exception'))):
                raise Conflict('Task or assignment evidence changed; reload before granting')
            receipt = json.loads(row['result'])
            if (row['status'] != 'queued' or row['owner'] is not None
                    or row['attempts_host'] != request['worker'] or row['last_owner'] != request['worker']
                    or json.loads(row['requirements']).get('worker') != request['worker']
                    or row['fix_attempts_used'] != FIX_ATTEMPT_LIMIT
                    or row['total_fix_attempts'] != row['attempt_base'] + row['fix_attempts_used']
                    or row['reassignment_count'] > REASSIGNMENT_LIMIT
                    or receipt.get('execution_stopped') is not True or receipt.get('human_stop') is True):
                raise Conflict('Grant requires settled hard-routed current-host work at its default limit')
            if row['task_type'] != 'work' or any(s['kind'] != 'work' for s in json.loads(row['steps'])):
                raise Conflict('Only ordinary work supports a current-host allowance')
            if (self.allowance_active(row) or self.exception_available(row, request['worker'])
                    or db.execute('SELECT 1 FROM attempt_allowances WHERE task_id=? AND worker=? AND reassignment_count=?',
                                  (ident, request['worker'], row['reassignment_count'])).fetchone()):
                raise Conflict('Only one finite allowance per assignment; no exception stacking')
            now = self.clock()
            grant = {'id': grant_id, 'request': request, 'worker': request['worker'],
                     'reassignment_count': row['reassignment_count'], 'attempt_base': row['attempt_base'],
                     'effective_limit': FIX_ATTEMPT_LIMIT + request['additional_attempts'],
                     'granted_at': now, 'expired_at': None}
            before = {k: row[k] for k in ('generation', *BINDING)}
            db.execute('UPDATE tasks SET attempt_allowance=? WHERE id=?', (encoded(grant), ident))
            after = {**before, 'attempt_allowance': grant}
            db.execute('INSERT INTO attempt_allowances VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (grant_id, ident, request['worker'], row['reassignment_count'], source_key,
                        request['request_id'], encoded(request), encoded(before), encoded(after), now))
            self.event(db, actor, 'attempt-allowance-granted', ident, encoded({
                'grant_id': grant_id, 'source': request['source'], 'worker': request['worker'],
                'reassignment_count': row['reassignment_count'], 'attempt_base': row['attempt_base'],
                'used': row['fix_attempts_used'], 'effective_limit': grant['effective_limit']}))
            row = db.execute('SELECT * FROM tasks WHERE id=?', (ident,)).fetchone()
            audit = db.execute('SELECT * FROM attempt_allowances WHERE id=?', (grant_id,)).fetchone()
            return {'grant': self._correction_readback(audit), 'replayed': False,
                    'task': {**self.public_task(row), **self.scheduling(db, row)}}

    def amend_attempt_allowance(self, actor, body):
        """Append one newly authorized extension without disturbing a live claim."""
        if actor != 'admin':
            raise Conflict('Allowance amendment requires administrator identity')
        try:
            request = allowance_payload(body, WORKERS, amendment=True)
        except ValueError as error:
            raise Invalid(str(error)) from None
        ident = request['task']
        grant_id = hashlib.sha256(encoded(request).encode()).hexdigest()
        source_key = hashlib.sha256(encoded(request['source']).encode()).hexdigest()
        with self.transaction() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (ident,)).fetchone()
            if not row:
                raise Conflict('Unknown task')
            prior = db.execute('SELECT * FROM attempt_allowance_amendments WHERE source_key=? OR (task_id=? AND request_id=?)',
                               (source_key, ident, request['request_id'])).fetchone()
            if prior:
                if prior['id'] != grant_id:
                    raise Conflict('Human source/request already binds different amendment evidence')
                return {'grant': self._correction_readback(prior), 'replayed': True,
                        'task': {**self.public_task(row), **self.scheduling(db, row)}}
            if db.execute('SELECT 1 FROM attempt_allowances WHERE source_key=? OR (task_id=? AND request_id=?)',
                          (source_key, ident, request['request_id'])).fetchone():
                raise Conflict('An amendment requires a distinct new primary human authorization')
            expected = request['expected']; public = self.public_task(row)
            if (row['generation'] != request['generation']
                    or any(row[k] != expected[k] for k in BINDING)
                    or any(hashlib.sha256(encoded(public[k]).encode()).hexdigest() != expected[k + '_sha256']
                           for k in ('requirements', 'attempt_allowance', 'attempt_exception'))
                    or public['fix_attempt_limit'] != expected['fix_attempt_limit']):
                raise Conflict('Task, current grant or assignment changed; refresh before amendment')
            previous = json.loads(row['attempt_allowance']); receipt = json.loads(row['result'])
            if (not self.allowance_active(row) or previous['id'] != request['previous_grant_id']
                    or json.loads(row['requirements']).get('worker') != request['worker']
                    or row['attempts_host'] != request['worker']
                    or row['total_fix_attempts'] != row['attempt_base'] + row['fix_attempts_used']
                    or row['fix_attempts_used'] > previous['effective_limit']
                    or row['reassignment_count'] > REASSIGNMENT_LIMIT
                    or receipt.get('human_stop') is True
                    or (row['status'] != 'running' and receipt.get('execution_stopped') is not True)):
                raise Conflict('Amend only the exact active host grant with verified quiet execution')
            if row['task_type'] != 'work' or any(s['kind'] != 'work' for s in json.loads(row['steps'])):
                raise Conflict('Only ordinary work supports allowance amendment')
            if db.execute('SELECT 1 FROM attempt_allowance_amendments WHERE previous_grant_id=?',
                          (previous['id'],)).fetchone():
                raise Conflict('This grant already has a later amendment')
            now = self.clock()
            grant = {'id': grant_id, 'request': request, 'worker': request['worker'],
                     'reassignment_count': row['reassignment_count'], 'attempt_base': row['attempt_base'],
                     'effective_limit': previous['effective_limit'] + request['additional_attempts'],
                     'granted_at': now, 'expired_at': None, 'previous_grant_id': previous['id'],
                     'root_grant_id': previous.get('root_grant_id', previous['id'])}
            before = {k: row[k] for k in ('generation', *BINDING)}
            before['attempt_allowance'] = previous
            db.execute('UPDATE tasks SET attempt_allowance=? WHERE id=?', (encoded(grant), ident))
            after = {**before, 'attempt_allowance': grant}
            db.execute('INSERT INTO attempt_allowance_amendments VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (grant_id, ident, request['worker'], row['reassignment_count'], previous['id'],
                        source_key, request['request_id'], encoded(request), encoded(before), encoded(after), now))
            self.event(db, actor, 'attempt-allowance-amended', ident, encoded({
                'grant_id': grant_id, 'previous_grant_id': previous['id'], 'source': request['source'],
                'worker': request['worker'], 'reassignment_count': row['reassignment_count'],
                'previous_limit': previous['effective_limit'], 'effective_limit': grant['effective_limit'],
                'used': row['fix_attempts_used']}))
            row = db.execute('SELECT * FROM tasks WHERE id=?', (ident,)).fetchone()
            audit = db.execute('SELECT * FROM attempt_allowance_amendments WHERE id=?', (grant_id,)).fetchone()
            return {'grant': self._correction_readback(audit), 'replayed': False,
                    'task': {**self.public_task(row), **self.scheduling(db, row)}}

    def expire_attempt_allowance(self, db, row):
        if self.allowance_active(row):
            grant = json.loads(row['attempt_allowance'])
            grant['expired_at'] = self.clock()
            db.execute('UPDATE tasks SET attempt_allowance=? WHERE id=?', (encoded(grant), row['id']))
            self.event(db, 'service', 'attempt-allowance-expired', row['id'],
                       encoded({'grant_id': grant['id'], 'reason': 'genuine host reassignment'}))

    def invalidate_attempt_exception(self, db, row):
        exception = json.loads(row['attempt_exception'])
        if exception and exception.get('consumed_generation') is None and exception.get('invalidated_at') is None:
            exception['invalidated_at'] = self.clock()
            db.execute('UPDATE tasks SET attempt_exception=? WHERE id=?', (encoded(exception), row['id']))
            self.event(db, 'service', 'attempt-exception-invalidated', row['id'], encoded({
                'approval_id': exception['request']['approval_id'], 'generation': row['generation']}))

    def approval_state(self, db, row):
        if row['status'] == 'pending_external_approval':
            return {'state':'awaiting_decision'}
        if row['status'] not in ('queued','running'):
            return None
        for event in db.execute("SELECT * FROM events WHERE task_id=? AND kind IN ('approval-applied','reconciled','pending_external_approval') ORDER BY id DESC",(row['id'],)):
            if event['kind'] == 'pending_external_approval':
                break
            detail = json.loads(event['detail'])
            if event['kind'] == 'approval-applied' or detail.get('approval_received') is True:
                return {'state':'approved','recorded_at':event['at'],'evidence':detail}
        return None

    def reconcile(self, actor, ident, body):
        evidence = bounded(body.get("evidence"), "evidence", 16000)
        if body.get("execution_stopped") is not True or body.get("external_state_checked") is not True:
            raise Invalid("Reconciliation requires stopped execution and checked external state")
        action = body.get("action")
        if action not in ("retry", "complete", "cancel", "pending_external_approval"):
            raise Invalid("Invalid reconciliation action")
        with self.transaction() as db:
            self.expire(db)
            row = db.execute("SELECT * FROM tasks WHERE id=?", (ident,)).fetchone()
            queued_cancel = row and row['status'] == 'queued' and action == 'cancel' and actor == 'admin'
            if not row or (not queued_cancel and row["status"] not in ("held", "blocked", "failed", "pending_external_approval")):
                raise Conflict("Only stopped tasks can be reconciled")
            if queued_cancel and body.get('generation') != row['generation']:
                raise Conflict('Queued cancellation requires the current generation; reload before cancelling')
            if 'generation' in body and body['generation'] != row['generation']:
                raise Conflict('Task generation changed; reload before reconciliation')
            if action == 'pending_external_approval' or row['status'] == 'pending_external_approval':
                if body.get('generation') != row['generation']:
                    raise Conflict('Approval classification/resume requires the current generation')
                if actor != 'admin':
                    if row['status'] != 'pending_external_approval' or action != 'retry':
                        raise Conflict('Workers may only resume their own verified approval wait')
                    bounded(body.get('approval_evidence'), 'approval_evidence', 4000)
                if action in ('retry', 'complete') and body.get('approval_received') is not True:
                    raise Invalid('External approval must be verified before resuming')
            if action == 'retry' and row['fix_attempts_used'] >= self.attempt_limit(row) and row['reassignment_count'] >= REASSIGNMENT_LIMIT:
                raise Conflict(f'{self.attempt_limit(row)} attempts and {REASSIGNMENT_LIMIT} host reassignments exhausted')
            if actor != "admin":
                self.owned(db, actor, ident, body.get("claim_token"), body.get("generation"))
                if action != "retry":
                    raise Conflict("Workers may only retry their own stopped attempts")
            step = row["step"] + (action == "complete")
            status = "cancelled" if action=="cancel" else "completed" if step==len(json.loads(row["steps"])) else "queued"
            if action == 'pending_external_approval':
                status = action
            self.invalidate_attempt_exception(db, row)
            db.execute("DELETE FROM locks WHERE task_id=?", (ident,))
            db.execute("UPDATE tasks SET status=?,step=?,last_owner=COALESCE(owner,last_owner),owner=NULL,claim_hash=NULL,lease_until=NULL,retry_after=NULL,updated=?,progress=? WHERE id=?",
                       (status,step,self.clock(),evidence,ident))
            detail = {"action":action,"evidence":evidence}
            if row['status'] == 'pending_external_approval' and action in ('retry','complete'):
                detail.update(approval_received=True, approval_evidence=body.get('approval_evidence', evidence))
            self.event(db,actor,"reconciled",ident,encoded(detail))
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
            if result['accepted']:
                repo = db.execute('SELECT * FROM repositories WHERE name=?', (body['repository'],)).fetchone()
                publication_policy.observe(self, db, repo, body)
            return result

    def tick_releases(self, repository=None):
        """Hourly or explicit release request; both share exact-target deduplication."""
        if repository is not None:
            repository = bounded(repository, 'repository', 200)
        with self.transaction() as db:
            now = self.clock()
            created = []
            rows = (db.execute('SELECT * FROM repositories WHERE name=?',(repository,)).fetchall()
                    if repository is not None else db.execute("SELECT * FROM repositories WHERE next_check<=?", (now,)).fetchall())
            if repository is not None and not rows:
                raise Invalid('Repository is not configured')
            for repo in rows:
                # A worker observes origin/main with its existing Git access.
                # Retry stale observations without consuming this hourly check.
                if repo["observed_at"] < now-600:
                    if repository is not None:
                        raise Conflict('Observe current remote main before starting a release')
                    db.execute("UPDATE repositories SET last_error='Waiting for fresh origin/main observation' WHERE name=?", (repo["name"],))
                    continue
                db.execute("UPDATE repositories SET next_check=?,last_checked=?,last_error='' WHERE name=?", ((int(now)//3600+1)*3600, now, repo["name"]))
                policy = publication_policy.prepare_tick(self, db, repo)
                if policy and policy['defer']:
                    continue
                if repo["merged_head"] == repo["published_head"]:
                    continue
                target = {"repository": repo["name"], "branch": repo["branch"], "commit": repo["merged_head"]}
                key = "hourly-publish:"+repo["name"]+":"+repo["merged_head"]
                if db.execute("SELECT id FROM tasks WHERE request_key=?", (key,)).fetchone():
                    continue
                ident = uuid.uuid4().hex[:16]
                title = "Publish "+repo["name"]+" at "+repo["merged_head"][:12]
                instructions = ("Publish the merged source pinned in release_target after release validation. "
                    "Only workers meeting this task's requirements may claim; there is no shared publishing lock. "
                    "Use the normal signed/notarized release procedure; verify the public artifact before completing. Check the current public release before publishing and never downgrade a newer release; report a superseded target as blocked with exact evidence instead. "
                    "Record published_commit equal to the pinned target and publication_ref. If release metadata is committed to main, also record release_commit after verifying it contains only the release metadata on top of this target; this becomes the published baseline and prevents an endless release loop. "
                    "Do not incorporate later feature changes. If publication outcome is unknown, stop and inspect external state before any retry. "
                    "After settled completion or failure, immediately pull another eligible task.")
                requirements = {}
                if repo['name']=='oliverhu/nova-browser':
                    # Studio is the currently qualified combined Mac+iOS
                    # publisher. Generic development capacity does not prove
                    # signed-in Xcode upload readiness on either guest.
                    requirements = {'worker': 'oliverhu-mac-studio'}
                    instructions += ' ' + IOS_UPLOAD_POLICY
                dependencies = []
                if policy:
                    try:
                        carried = publication_policy.inherited_requirements([], {'requirements': requirements, 'dependencies': []})
                        carried = publication_policy.inherited_requirements([{'requirements': encoded(policy['requirements']), 'dependencies': encoded(policy['dependencies'])}], carried)
                    except (ValueError, TypeError):
                        db.execute('UPDATE repositories SET last_error=? WHERE name=?', ('Conflicting publication routing requirements', repo['name']))
                        continue
                    requirements, dependencies = carried['requirements'], carried['dependencies']
                steps = [{"name":"Validate and publish merged changes","kind":"publish","instructions":instructions}]
                db.execute("""INSERT INTO tasks(id,request_key,request_hash,title,description,created,updated,submitted_by,priority,compute,requirements,dependencies,steps,result,task_type)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (ident,key,hashlib.sha256(key.encode()).hexdigest(),title,instructions,now,now,"admin" if repository else "hourly-scheduler",90 if repository else 50,"standard",encoded(requirements),encoded(dependencies),encoded(steps),encoded({"release_target":target}),"publish"))
                self.event(db, "admin" if repository else "hourly-scheduler", "submitted", ident, title)
                created.append(ident)
            return created

    def snapshot(self):
        with self.transaction() as db:
            self.expire(db)
            workers=[]
            for row in db.execute("SELECT * FROM workers ORDER BY id"):
                w=dict(row); w["capacity"]=json.loads(w["capacity"]); w["online"]=w["seen"]>self.clock()-90
                w['cli_status']=json.loads(w['cli_status']); workers.append(w)
            tasks=[self.public_task(r) for r in db.execute("SELECT * FROM tasks ORDER BY created DESC LIMIT 500")]
            for t in tasks:
                row = db.execute("SELECT * FROM tasks WHERE id=?",(t['id'],)).fetchone()
                t.update(self.scheduling(db, row))
                t['approval'] = self.approval_state(db,row)
                if t["status"]=="queued":
                    t["preferred_worker"] = self.candidate(db,row) if t['ready'] else None
            # Worker cards must retain current ownership even when its task was
            # created before the dashboard's 500-task history window.
            worker_tasks = {r['id']: self.public_task(r) for r in db.execute(
                "SELECT * FROM tasks WHERE status IN ('running','held') AND owner IS NOT NULL")}
            for worker in workers:
                if worker['id'] not in MINI_VM_WORKERS:
                    continue
                run_task = worker['cli_status'].get('task_id')
                if run_task:
                    row = db.execute('SELECT * FROM tasks WHERE id=?', (run_task,)).fetchone()
                    if row:
                        worker_tasks[row['id']] = self.public_task(row)
                for row in db.execute("SELECT * FROM tasks WHERE status='completed' AND COALESCE(last_owner,owner)=? ORDER BY updated DESC", (worker['id'],)):
                    task = self.public_task(row)
                    result = task.get('result', {})
                    if result.get('acceptance_complete') is not False and result.get('outcome', 'completed') == 'completed':
                        worker_tasks[task['id']] = task
                        break
            return {"server_time":self.clock(),"workers":workers,"tasks":tasks,"worker_tasks":list(worker_tasks.values()),
                "capabilities":{"approval_receipts":1,"approval_recovery":"verified_decisions_only",
                                "outcome_correction":1,"finish_outcome_consistency":1,"attempt_correction":1,"attempt_allowances":1,
                                "attempt_allowance_amendments":1,"completed_attempt_correction":1,
                                "ios_upload_only_releases":1,"publication_policy":1},
                "publication_candidates":[publication_policy.target(r) for r in db.execute("SELECT result FROM tasks WHERE task_type='publish' AND status NOT IN ('completed','cancelled')")],
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
            task['approval'] = self.approval_state(db,row)
            task["receipts"]=[{**dict(r),"receipt":json.loads(r["receipt"])} for r in db.execute("SELECT * FROM receipts WHERE task_id=? ORDER BY at",(ident,))]
            task["events"]=[dict(r) for r in db.execute("SELECT * FROM events WHERE task_id=? ORDER BY id",(ident,))]
            task['outcome_corrections']=[self._correction_readback(r) for r in db.execute(
                'SELECT * FROM outcome_corrections WHERE task_id=? ORDER BY at', (ident,))]
            task['attempt_corrections']=[self._correction_readback(r) for r in db.execute(
                'SELECT * FROM attempt_corrections WHERE task_id=? ORDER BY at,rowid', (ident,))]
            task['attempt_allowances']=[self._correction_readback(r) for r in db.execute(
                'SELECT * FROM attempt_allowances WHERE task_id=? ORDER BY at,rowid', (ident,))]
            task['attempt_allowance_amendments']=[self._correction_readback(r) for r in db.execute(
                'SELECT * FROM attempt_allowance_amendments WHERE task_id=? ORDER BY at,rowid', (ident,))]
            return task
