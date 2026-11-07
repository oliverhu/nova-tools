"""Versioned local results. No service mutations, process inference or receipt rewriting.

Legacy reports cross this module once. Historical exceptional reconciliations stay
explicit in cli_dispatch; new normal runs need no per-task settlement adapter.
"""
from dataclasses import asdict, dataclass
from enum import Enum
import hashlib
import json
import math
import os
from pathlib import Path
import stat


class Outcome(str, Enum):
    COMPLETED = 'completed'
    WAITING = 'waiting'
    BLOCKED = 'blocked'
    APPROVAL = 'pending_external_approval'


@dataclass(frozen=True)
class TaskResult:
    schema: int
    outcome: Outcome
    acceptance_complete: bool
    cleanup_evidence: str


@dataclass(frozen=True)
class RunResult:
    schema: int
    run_id: str
    task: str
    generation: int
    worker: str
    outcome: str
    acceptance_complete: bool
    execution_stopped: bool
    availability: str
    evidence_sha256: dict

    def document(self):
        return asdict(self)


# Only fields with stable semantic meaning; heartbeat/update timestamps may move.
TASK_FIELDS = ('id', 'generation', 'step', 'steps', 'status', 'owner', 'last_owner',
               'requirements', 'dependencies', 'result', 'receipts', 'approval',
               'fix_attempts_used', 'total_fix_attempts', 'attempts_host', 'attempt_base',
               'reassignment_count', 'attempt_exception', 'attempt_allowance')


def same_task(left, right, response=False):
    # Mutation responses are task envelopes; show adds receipts/approval history.
    fields = tuple(k for k in TASK_FIELDS if not response or k not in ('receipts', 'approval'))
    return all(left.get(k) == right.get(k) for k in fields)


def read_original(path):
    if path.is_symlink() or path.resolve() != path.absolute():
        raise ValueError('Canonical original result evidence required')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('Owner-only regular result evidence required')
        raw = stream.read()
    return raw, (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)


def clean(values):
    for value in values:
        if not isinstance(value, dict):
            raise ValueError('Structured settlement evidence required')
        if any(value.get(k, []) != [] for k in ('unknown_operations', 'unsettled_operations')):
            raise ValueError('Unknown or unsettled operations require reconciliation')
        if any(k in value and value[k] is not True for k in
               ('execution_stopped', 'external_state_checked', 'input_settled')):
            raise ValueError('Contradictory settlement evidence')


def normalize_report(report, run):
    """One compatibility boundary; missing legacy proof is never invented."""
    version = report.get('result_schema')
    if version is not None and (type(version) is not int or version != 1):
        raise ValueError('Unsupported task result schema')
    cleanup = report.get('cleanup')
    if not isinstance(cleanup, dict):
        raise ValueError('Structured owned-process cleanup required')
    clean((report, cleanup))
    if version == 1:
        if (report.get('run_id') != run['run_id']
                or type(report.get('acceptance_complete')) is not bool
                or report.get('outcome') not in {x.value for x in Outcome}):
            raise ValueError('Version 1 result identity/acceptance/outcome required')
        for key in ('execution_stopped', 'external_state_checked', 'input_settled'):
            if cleanup.get(key) is not True:
                raise ValueError('Version 1 requires positive settlement predicates')
        if any(cleanup.get(k) != [] for k in ('unknown_operations', 'unsettled_operations')):
            raise ValueError('Version 1 requires explicit empty operation inventories')
        evidence = cleanup.get('evidence')
    else:
        # Older reports may keep stop predicates at top level. Do not rewrite them.
        for key in ('execution_stopped', 'external_state_checked', 'input_settled'):
            if report.get(key) is not True and cleanup.get(key) is not True:
                raise ValueError('Legacy report lacks explicit settlement proof')
        evidence = cleanup.get('evidence', cleanup.get('cleanup_evidence'))
    if not isinstance(evidence, str) or not evidence.strip():
        raise ValueError('Actual owned-process settlement evidence required')
    outcome = report.get('outcome')
    if version is None and outcome in ('queued', 'queued_wait'):
        outcome = 'waiting'
    return TaskResult(1, Outcome(outcome), report.get('acceptance_complete'), evidence)


def evaluate(run, task, documents, hashes, validate_wait):
    """Pure reduction after the exact supervisor child is reaped; never auto-retry."""
    if (type(run.get('cli_exit_code')) is not int
            or type(run.get('ended_at')) not in (int, float)
            or not math.isfinite(run['ended_at']) or run['ended_at'] <= 0
            or run.get('unreceived_message_ids', []) != []):
        raise ValueError('Ended child and message closeout required')
    report = documents['worker-report.json']; finish = documents['finish-response.json']
    readback = documents['task-readback.json']
    generation = task.get('generation')
    if (type(generation) is not int or type(run.get('queued_generation')) is not int
            or generation != run['queued_generation'] + 1
            or task.get('id') != run['task']
            or any(v.get('task') != run['task'] or type(v.get('generation')) is not int
                   or v.get('generation') != generation
                   or v.get('worker') != run['worker'] for v in (report,))
            or not same_task(task, finish, response=True) or not same_task(task, readback)):
        raise ValueError('Exact run generation, owning report, finish and fresh readback required')
    clean((run, task, task.get('result', {}), finish, readback))
    normalized = normalize_report(report, run)
    outcome = normalized.outcome
    complete = outcome == Outcome.COMPLETED
    if normalized.acceptance_complete is not complete:
        raise ValueError('Acceptance must agree with outcome')
    if outcome != Outcome.APPROVAL and any(v.get('human_stop') or v.get('approval_request')
            for v in (run, task, report, report['cleanup'], task.get('result', {}))):
        raise ValueError('Human hold cannot become an ordinary outcome')
    if outcome == Outcome.WAITING:
        if task.get('approval') or task.get('human_stop') or report.get('human_stop') or report.get('approval_request'):
            raise ValueError('Independent hold prevents dependency-wait classification')
        wait = documents['wait-receipt.json']
        # Existing audited validators bind real defer receipts and destination;
        # a queued status or adapter document alone never releases capacity.
        note = {'run_id': run['run_id'], 'task': run['task'], 'generation': generation,
                'worker': run['worker'], 'execution_stopped': True,
                'external_state_checked': True, 'input_settled': True,
                'unknown_operations': [], 'cleanup_evidence': normalized.cleanup_evidence}
        validate_wait({**run, 'status': 'needs_attention', 'completion_verified': False},
                      task, finish, note, run['worker'], report, wait, readback)
    else:
        result = task.get('result', {})
        clean((result.get('cleanup', {}),))
        if (task.get('owner') != run['worker'] or task.get('status') != outcome.value
                or result.get('outcome') != outcome.value
                or result.get('acceptance_complete') is not complete
                or result.get('execution_stopped') is not True):
            raise ValueError('Actual owning finish and acceptance required')
        if outcome != Outcome.APPROVAL and any(v.get('human_stop') or v.get('approval_request')
                                               for v in (report, result, task)):
            raise ValueError('Human hold cannot become ordinary availability')
        if complete and task.get('step') != len(task.get('steps', [])):
            raise ValueError('All steps must complete')
        # The exact service receipt binds the owner report; unknown failed outcomes
        # deliberately have no automatic classification.
        matches = [r for r in task.get('receipts', []) if r.get('task_id') == task['id']
                   and r.get('generation') == generation
                   and r.get('receipt', {}).get('outcome') == outcome.value
                   and r.get('receipt', {}).get('receipt') == result]
        if len(matches) != 1:
            raise ValueError('Exact authoritative owning finish receipt required')
        if any(report.get(k) != result.get(k) for k in
               ('task', 'generation', 'worker', 'outcome', 'acceptance_complete', 'cleanup',
                'run_id', 'result_schema')):
            raise ValueError('Report and authoritative result disagree')
    return RunResult(1, run['run_id'], run['task'], generation, run['worker'],
                     outcome.value, complete, True, 'available', hashes)


def reconcile(run_dir, run, task, validate_wait):
    """Validate immutable originals and produce an additive local result only."""
    names = ['finish-response.json', 'worker-report.json', 'task-readback.json']
    if task.get('status') == 'queued':
        names.append('wait-receipt.json')
    snapshots = {name: read_original(run_dir/name) for name in names}
    documents = {name: json.loads(raw) for name, (raw, _) in snapshots.items()}
    hashes = {name: hashlib.sha256(raw).hexdigest() for name, (raw, _) in snapshots.items()}
    result = evaluate(run, task, documents, hashes, validate_wait)
    if any(read_original(run_dir/name) != snapshot for name, snapshot in snapshots.items()):
        raise ValueError('Original result evidence changed during evaluation')
    return result
