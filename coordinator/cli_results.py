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
import re
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
               'requirements', 'dependencies', 'result', 'receipts', 'approval', 'events',
               'fix_attempts_used', 'total_fix_attempts', 'attempts_host', 'attempt_base',
               'reassignment_count', 'attempt_exception', 'attempt_allowance')


def same_task(left, right, response=False):
    # Mutation responses are task envelopes; show adds receipts/approval history.
    fields = tuple(k for k in TASK_FIELDS if not response or k not in ('receipts', 'approval', 'events'))
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
        if value.get('unresolved_observations', []) != []:
            raise ValueError('Unresolved observations require independent owner reconciliation')
        if any(value.get(k, []) != [] for k in ('unknown_operations', 'unsettled_operations')):
            raise ValueError('Unknown or unsettled operations require reconciliation')
        if any(k in value and value[k] is not True for k in
               ('execution_stopped', 'external_state_checked', 'input_settled')):
            raise ValueError('Contradictory settlement evidence')


def cleanup_evidence_text(evidence):
    """Normalize prose internally; original reports and receipt hashes stay exact."""
    if isinstance(evidence, str) and evidence.strip():
        return evidence
    if (isinstance(evidence, list) and 1 <= len(evidence) <= 32
            and all(isinstance(item, str) and item.strip() for item in evidence)):
        text = '\n'.join(evidence)
        if len(text.encode('utf-8')) <= 16384:
            return text
    raise ValueError('Actual owned-process settlement evidence required')


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
    evidence = cleanup_evidence_text(evidence)
    outcome = report.get('outcome')
    if version is None and outcome in ('queued', 'queued_wait'):
        outcome = 'waiting'
    return TaskResult(1, Outcome(outcome), report.get('acceptance_complete'), evidence)


def _history_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _history_require(ok, message):
    if not ok:
        raise ValueError('Historical approval: ' + message)


def _history_text(value):
    return isinstance(value, str) and bool(value.strip())


def _history_timestamp(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _history_detail(event):
    try: value = json.loads(event.get('detail', ''))
    except (ValueError, TypeError):
        raise ValueError('Historical approval: structured event detail required') from None
    _history_require(isinstance(value, dict), 'structured event detail required')
    return value


def _history_claim_generation(event, task, owner):
    match = re.fullmatch(r'Step (\d+); generation (\d+); resources (\[.*\])', event.get('detail',''))
    _history_require(event.get('actor') == owner and match is not None
            and int(match[1]) == task['step'] + 1, 'exact owning claim audit required')
    try: resources = json.loads(match[3])
    except ValueError: raise ValueError('Historical approval: malformed claim resources') from None
    _history_require(isinstance(resources, list), 'claim resources required')
    return int(match[2])


def classify_historical_approval(run, task, report, wait, readback):
    """Prove a retained approved question predates the current owning wait.

    Authoritative original records are never modified. Positive classification
    grants no permission and says nothing about operation cleanup or readiness.
    """
    _history_require(all(isinstance(v, dict) for v in (run, task, report, wait, readback)), 'original owning records required')
    approval = task.get('approval'); result = task.get('result')
    _history_require(isinstance(approval, dict) and approval.get('state') == 'approved'
            and isinstance(result, dict), 'recorded approved historical result required')
    evidence = approval.get('evidence'); question = result.get('approval_request')
    _history_require(isinstance(evidence, dict) and set(evidence) in (
        {'approval_id','generation','source','response','scope','scope_evidence'},
        {'approval_id','generation','source','response','scope','scope_evidence','question_link'}),
        'exact authenticated approval evidence required')
    old = evidence['generation']; current = task.get('generation'); owner = run.get('worker')
    _history_require(type(old) is int and type(current) is int and 0 < old < current and _history_text(owner)
            and type(task.get('step')) is int and task['step'] >= 0
            and isinstance(task.get('id'), str) and re.fullmatch('[a-f0-9]{16}',task['id'])
            and isinstance(run.get('run_id'), str) and re.fullmatch('[a-f0-9]{32}',run['run_id'])
            and run.get('task') == task.get('id') and type(run.get('queued_generation')) is int
            and run['queued_generation'] == current - 1
            and task.get('status') == 'queued' and task.get('owner') is None and task.get('last_owner') == owner
            and result.get('task') == task['id'] and type(result.get('generation')) is int
            and result['generation'] == old and result.get('worker') == owner
            and result.get('outcome') == 'pending_external_approval' and result.get('acceptance_complete') is False
            and result.get('execution_stopped') is True and result.get('external_state_checked') is True
            and not result.get('human_stop')
            and all(result.get(k, []) == [] for k in ('unknown_operations','unsettled_operations')),
            'same owner and an earlier stopped pending result required')
    for value in (run, task, report, report.get('cleanup', {}), wait, wait.get('cleanup', {})):
        _history_require(isinstance(value, dict) and not any(value.get(k) for k in
                ('human_stop','approval_request','human_action_required')), 'current independent human hold')
    _history_require(report.get('task') == task['id'] and report.get('generation') == current
            and report.get('worker') == owner and report.get('outcome') in ('waiting','queued','queued_wait')
            and report.get('acceptance_complete') is False, 'actual current owning wait report required')
    old_cleanup = result.get('cleanup', {})
    _history_require((isinstance(old_cleanup, dict) and all(old_cleanup.get(k,[]) == []
            for k in ('unknown_operations','unsettled_operations','unresolved_observations')))
            or _history_text(old_cleanup),
            'historical operation holds remain unresolved')
    source = evidence['source']
    _history_require(isinstance(source, dict) and set(source) == {'thread_id','question_id','message_id'}
            and all(_history_text(v) for v in source.values()) and evidence['approval_id'] == _history_digest(source)
            and all(_history_text(evidence[k]) for k in ('response','scope','scope_evidence')),
            'exact approval identity and scope required')
    _history_require(isinstance(question, dict) and set(question) == {'thread_id','question_id'}
            and all(_history_text(v) for v in question.values()), 'exact original structured question required')
    link = evidence.get('question_link')
    if link is None:
        _history_require(question == {k:source[k] for k in ('thread_id','question_id')}, 'original question/source mismatch')
    else:
        identity = {'task':task['id'],'generation':old,'original_question':question,'source':source}
        _history_require(isinstance(link, dict) and set(link) == {'link_id','original_question','task','generation','result_sha256'}
                and link['original_question'] == question and link['task'] == task['id']
                and type(link['generation']) is int and link['generation'] == old
                and link['result_sha256'] == _history_digest(result) and link['link_id'] == _history_digest(identity)
                and question['thread_id'] != source['thread_id'], 'exact authenticated cross-chat link required')
    receipts = task.get('receipts'); events = task.get('events')
    _history_require(isinstance(receipts, list) and all(isinstance(r,dict) for r in receipts)
            and isinstance(events, list) and events and same_task(task,readback)
            and readback.get('receipts') == receipts and readback.get('events') == events
            and readback.get('approval') == approval and readback.get('result') == result,
            'fresh unchanged original receipts and full history required')
    _history_require(all(isinstance(e, dict) and e.get('task_id') == task['id'] and _history_text(e.get('kind'))
                and type(e.get('id')) is int and e['id'] > 0 and _history_timestamp(e.get('at')) for e in events)
            and all(a['id'] < b['id'] and a['at'] <= b['at'] for a,b in zip(events,events[1:])),
            'ordered exact-task event history required')
    pending = [r for r in receipts if r.get('generation') == old]
    _history_require(len(pending) == 1 and pending[0].get('task_id') == task['id']
            and type(pending[0].get('generation')) is int
            and type(pending[0].get('step')) is int and pending[0]['step'] == task.get('step') and _history_timestamp(pending[0].get('at'))
            and _history_digest(pending[0].get('receipt')) == _history_digest({'outcome':'pending_external_approval','receipt':result}),
            'one exact original pending receipt required')
    applications = [i for i,e in enumerate(events) if e['kind'] == 'approval-applied'
                    and _history_detail(e).get('generation') == old]
    _history_require(len(applications) == 1 and applications[0] > 0, 'one matching approval application required')
    index = applications[0]; event = events[index]; previous = events[index-1]
    old_claims = [e for e in events[:index-1] if e['kind'] == 'claimed']
    _history_require(old_claims and _history_claim_generation(old_claims[-1],task,owner) == old,
            'original pending generation claim required')
    _history_require(event.get('actor') == 'admin' and _history_digest(_history_detail(event)) == _history_digest(evidence)
            and _history_timestamp(approval.get('recorded_at')) and approval['recorded_at'] == event['at']
            and previous.get('actor') == owner and previous['kind'] == 'pending_external_approval'
            and previous.get('detail') == result.get('summary')
            and old_claims[-1]['at'] <= pending[0]['at'] <= previous['at'] <= event['at'],
            'exact pending event followed by authenticated approval required')
    later = events[index+1:]
    _history_require(later and all(e['kind'] in ('claimed','waiting','wake-reserved','execution-phase')
                         and e.get('actor') == owner for e in later), 'later human or unsupported transitions refuse classification')
    claims = [(i,_history_claim_generation(e,task,owner)) for i,e in enumerate(later) if e['kind'] == 'claimed']
    _history_require(len(claims) == current-old and all(g == old+1+n for n,(_,g) in enumerate(claims)),
            'complete later owning generations required')
    for n,(i,generation) in enumerate(claims):
        segment = later[i+1:claims[n+1][0]] if n+1 < len(claims) else later[i+1:]
        for phase in (e for e in segment if e['kind'] == 'execution-phase'):
            value = _history_detail(phase)
            _history_require(set(value) == {'generation','phase'} and type(value['generation']) is int
                    and value['generation'] == generation and _history_text(value['phase']),
                    'exact generation execution phase required')
        waits = [e for e in segment if e['kind'] == 'waiting']
        rows = [r for r in receipts if r.get('generation') == generation]
        _history_require(len(waits) == len(rows) == 1, 'one owning wait per later generation required')
        event = waits[0]; row = rows[0]; envelope = row.get('receipt', {})
        body = envelope.get('receipt', {}) if isinstance(envelope,dict) else None
        _history_require(isinstance(body, dict) and row.get('task_id') == task['id'] and type(row.get('step')) is int and row['step'] == task['step']
                and type(row.get('generation')) is int and envelope.get('outcome') == 'waiting'
                and body.get('execution_stopped') is True and body.get('external_state_checked') is True
                and _history_text(body.get('summary')) and _history_timestamp(row.get('at'))
                and later[i]['at'] <= row['at'] <= event['at']
                and not any(body.get(k) for k in ('human_stop','approval_request','human_action_required'))
                and all(body.get(k,[]) == [] for k in ('unknown_operations','unsettled_operations','unresolved_observations')),
                'stopped later waiting receipts required')
        record = _history_detail(event)
        _history_require(set(record) == {'worker','dependencies','evidence'} and _history_text(record['worker'])
                and isinstance(record['dependencies'], list) and all(_history_text(v) for v in record['dependencies'])
                and record['evidence'] == body['summary'],
                'exact later waiting event/receipt binding required')
        if generation == current:
            _history_require(event == events[-1] and record['dependencies'] == task.get('dependencies')
                    and record['worker'] == task.get('requirements',{}).get('worker')
                    and wait.get('evidence') == body['summary'], 'current destination/dependencies/wait input required')
    return {'classification':'historical-approved-question','approval_id':evidence['approval_id'],
            'generation':old,'current_generation':current,'approval_event_id':events[index]['id'],
            'original_result_sha256':_history_digest(result)}


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
    historical = None
    if outcome == Outcome.WAITING and (task.get('approval') or task.get('result', {}).get('approval_request')):
        historical = classify_historical_approval(run, task, report, documents['wait-receipt.json'], readback)
    if outcome != Outcome.APPROVAL:
        current = [run, task, report, report['cleanup'], finish, readback]
        if outcome == Outcome.WAITING:
            wait = documents['wait-receipt.json']
            current.extend((wait, wait.get('cleanup', {})))
        clean(current)
        if any(v.get('human_stop') or v.get('approval_request') or v.get('human_action_required')
               for v in current):
            raise ValueError('Human hold cannot become an ordinary outcome')
        result = task.get('result', {})
        if (result.get('human_stop') or (not historical and
                (result.get('approval_request') or result.get('human_action_required')))):
            raise ValueError('Human hold cannot become an ordinary outcome')
    if outcome == Outcome.WAITING:
        if task.get('approval') and not historical:
            raise ValueError('Independent hold prevents dependency-wait classification')
        wait = documents['wait-receipt.json']
        clean((wait, wait.get('cleanup', {})))
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
