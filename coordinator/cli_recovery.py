"""Bounded, append-only retained-metadata recovery. Never dispatch or settle.

This module has no process, shell, GUI, signing, network-listener or service-write
operation. A review is an exact local record-read scope, never native authority.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import itertools
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import time

from .cli_results import TASK_FIELDS, _history_claim_generation

OPERATIONS = {'read-json-record', 'verify-owned-process-settlement', 'verify-owned-server-settlement'}
ORIGINALS = {'state.json', 'worker-report.json', 'task-readback.json', 'finish-response.json',
             'wait-receipt.json', 'stopped-cleanup.json'}
REQUIRED = {'state.json', 'worker-report.json', 'task-readback.json'}
ROLES = {'host', 'controller', 'driver', 'executor'}
UNKNOWN_KEYS = {'unknown_operations', 'unsettled_operations', 'unresolved_observations'}
MAX_RECORD = 1024 * 1024
MAX_TOTAL = 16 * MAX_RECORD


def need(ok, message):
    if not ok: raise ValueError(message)


def encode(value): return (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()
def digest(raw): return hashlib.sha256(raw).hexdigest()
def finite(value): return type(value) in (int, float) and math.isfinite(value)
def exact(value, keys): return type(value) is dict and set(value) == set(keys.split())
def hex64(value): return type(value) is str and re.fullmatch('[0-9a-f]{64}', value) is not None


def unique(pairs):
    result = {}
    for key, value in pairs:
        need(key not in result, 'Duplicate metadata key'); result[key] = value
    return result


def private(path):
    info = path.lstat()
    need(path.is_absolute() and path.resolve() == path and stat.S_ISDIR(info.st_mode)
         and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700, 'Private canonical directory required')
    return info.st_dev, info.st_ino


def read(path, limit=MAX_RECORD):
    path = Path(path)
    need(path.is_absolute() and path.resolve() == path, 'Canonical metadata path required')
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
        before = os.fstat(stream.fileno())
        need(stat.S_ISREG(before.st_mode) and before.st_uid == os.getuid() and before.st_nlink == 1
             and stat.S_IMODE(before.st_mode) in (0o400, 0o600) and 0 < before.st_size <= limit, 'Bounded owner-only regular metadata required')
        raw = stream.read(limit + 1); after = os.fstat(stream.fileno())
    key = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_mode, st.st_uid, st.st_nlink)
    need(key(before) == key(after) == key(path.lstat()) and len(raw) == before.st_size, 'Metadata changed during read')
    return raw, key(before)


def bound(reference, limit=MAX_RECORD):
    need(exact(reference, 'path sha256') and hex64(reference['sha256']), 'Exact metadata path/hash required')
    raw, identity = read(Path(reference['path']), limit)
    need(digest(raw) == reference['sha256'], 'Metadata hash differs')
    value = json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: need(False, 'Nonfinite JSON'))
    return value, raw, identity


@contextmanager
def lock_file(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        need(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1
             and stat.S_IMODE(info.st_mode) == 0o600, 'Private lock file required')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        need((info.st_dev, info.st_ino) == (path.lstat().st_dev, path.lstat().st_ino), 'Lock identity changed')
        yield
        need((info.st_dev, info.st_ino) == (path.lstat().st_dev, path.lstat().st_ino), 'Lock identity changed')
    finally:
        os.close(fd)


@contextmanager
def locked(root, run_id):
    need(type(run_id) is str and re.fullmatch('[0-9a-f]{32}', run_id), 'Exact run ID required')
    root = Path(root); root_identity = private(root); run = root/run_id; run_identity = private(run)
    # Same worker lock as normal dispatch; never advertise or reserve capacity.
    with lock_file(root/'worker.lock'), lock_file(run/'scoped-recovery.lock'):
        need(private(root) == root_identity and private(run) == run_identity, 'Recovery directory identity changed')
        yield run
        need(private(root) == root_identity and private(run) == run_identity, 'Recovery directory identity changed')


def unknowns(value, pointer=''):
    result = []
    if type(value) is dict:
        for key, item in value.items():
            child = pointer + '/' + key.replace('~', '~0').replace('/', '~1')
            if key in UNKNOWN_KEYS:
                need(type(item) is list, 'Unknown inventory must remain an explicit array')
                for index, row in enumerate(item): result.append({'pointer': child+'/'+str(index), 'value': row})
            else: result.extend(unknowns(item, child))
    elif type(value) is list:
        for index, row in enumerate(value): result.extend(unknowns(row, pointer+'/'+str(index)))
    return result


def api_fingerprint(task):
    need(type(task) is dict and set(TASK_FIELDS) <= set(task)
         and type(task.get('events')) is list and task['events']
         and type(task.get('receipts')) is list, 'Fresh full task/history readback required')
    events = task['events']
    need(all(type(e) is dict and e.get('task_id') == task['id']
             and type(e.get('id')) is int and e['id'] > 0
             and finite(e.get('at')) and type(e.get('kind')) is str and e['kind']
             and type(e.get('actor')) is str and e['actor'] for e in events)
         and [e['id'] for e in events] == sorted({e['id'] for e in events}), 'Complete ordered exact-task history required')
    raw = encode({key: task.get(key) for key in TASK_FIELDS})
    need(len(raw) <= MAX_TOTAL, 'Full API/history exceeds recovery bound')
    return digest(raw)


def inventory(root, worker):
    paths = sorted(itertools.islice(root.glob('*/state.json'), 129))
    need(len(paths) <= 128, 'Run inventory exceeds recovery bound')
    result = {}; total = 0
    for path in paths:
        raw, _ = read(path)
        total += len(raw); need(total <= MAX_TOTAL, 'Run metadata inventory exceeds total bound')
        value = json.loads(raw, object_pairs_hook=unique)
        need(value.get('worker') == worker and value.get('run_id') == path.parent.name, 'Foreign run inventory')
        result[path.parent.name] = digest(raw)
    return result


def snapshot(root, run_dir, request, client, worker):
    need(exact(request, 'schema kind run_id task worker generation originals resources ttl_seconds')
         and type(request['schema']) is int and request['schema'] == 1
         and request['kind'] == 'scoped-readonly-recovery-request', 'Exact recovery request required')
    need(request['run_id'] == run_dir.name and request['worker'] == worker
         and type(request['generation']) is int and request['generation'] > 0
         and type(request['task']) is str and re.fullmatch('[0-9a-f]{16}', request['task']), 'Exact run/owner binding required')
    originals = request['originals']
    need(type(originals) is dict and REQUIRED <= set(originals) <= ORIGINALS
         and all(hex64(v) for v in originals.values()), 'Explicit original metadata inventory required')
    need(finite(request['ttl_seconds']) and 0 < request['ttl_seconds'] <= 60, 'Recovery plan TTL must be at most60 seconds')
    resources = request['resources']
    need(type(resources) is list and 0 < len(resources) <= 16, 'Bounded explicit resource list required')
    seen = set(); roles = set()
    for resource in resources:
        need(exact(resource, 'id operation reference expected') and type(resource['id']) is str
             and re.fullmatch('[a-z0-9-]{1,64}', resource['id']) and resource['id'] not in seen, 'Unique exact resource required')
        seen.add(resource['id'])
        need(type(resource['operation']) is str and resource['operation'] in OPERATIONS, 'Operation outside metadata-only allowlist')
        reference = resource['reference']
        need(exact(reference, 'path sha256') and hex64(reference['sha256'])
             and reference['path'] == str(run_dir/'recovery-inputs'/(resource['id']+'.json')), 'Resource outside exact metadata input slot')
        if resource['operation'] == 'read-json-record':
            need(resource['expected'] == {}, 'Record read has no executable predicate')
        else:
            expected = resource['expected']
            fields = 'role identity waiter observer_source_sha256 not_before_epoch not_after_epoch'
            if resource['operation'] == 'verify-owned-server-settlement': fields += ' endpoint nonce'
            need(exact(expected, fields)
                 and hex64(expected['observer_source_sha256']) and finite(expected['not_before_epoch'])
                 and finite(expected['not_after_epoch']) and 0 <= expected['not_before_epoch'] <= expected['not_after_epoch']
                 and expected['not_after_epoch'] - expected['not_before_epoch'] <= 86400, 'Bounded exact settlement provenance required')
            need(identity(expected['identity']) and identity(expected['waiter']), 'Exact owned generations required')
            role = expected['role']
            need(role not in roles and (role in ROLES if resource['operation'] == 'verify-owned-process-settlement' else role == 'server'), 'Unique owned role required')
            roles.add(role)
            if role == 'server':
                need(type(expected['endpoint']) is str and re.fullmatch(r'http://127\.0\.0\.1:[0-9]{1,5}', expected['endpoint'])
                     and 0 < int(expected['endpoint'].rsplit(':', 1)[1]) <= 65535
                     and type(expected['nonce']) is str and re.fullmatch('[0-9a-f]{32}', expected['nonce']), 'Exact owned server endpoint/nonce required')
    first = client.request('/api/tasks/'+request['task'])
    documents, refs, identities, retained = {}, {}, {}, []
    total = 0
    for name, expected in originals.items():
        value, raw, file_identity = bound({'path': str(run_dir/name), 'sha256': expected})
        total += len(raw); need(total <= MAX_TOTAL, 'Original metadata exceeds total bound')
        documents[name] = value; refs[name] = expected; identities[name] = file_identity
        retained.extend({'source': name, **row} for row in unknowns(value))
    run, report = documents['state.json'], documents['worker-report.json']
    retained.extend({'source': 'fresh-api', **row} for row in unknowns(first))
    need(run.get('run_id') == request['run_id'] and run.get('task') == request['task'] and run.get('worker') == worker
         and type(run.get('queued_generation')) is int and run['queued_generation'] + 1 == request['generation']
         and run.get('status') in ('starting', 'running', 'needs_attention', 'needs_reconciliation'), 'Original held run identity differs')
    need(report.get('task') == request['task'] and report.get('worker') == worker
         and type(report.get('generation')) is int and report['generation'] == request['generation']
         and report.get('run_id', request['run_id']) == request['run_id'], 'Original owner report differs')
    prior_api = documents['task-readback.json']
    need(prior_api.get('id') == request['task'] and type(prior_api.get('generation')) is int and prior_api['generation'] == request['generation'], 'Original task readback identity differs')
    need(retained, 'Scoped held recovery requires retained unknown inventory')
    need(first.get('id') == request['task'] and type(first.get('generation')) is int
         and first['generation'] >= request['generation'] and first.get('owner') in (None, worker)
         and first.get('last_owner') in (None, worker)
         and first.get('requirements', {}).get('worker', worker) == worker, 'Fresh API owner/generation differs')
    claims = [event for event in first.get('events', []) if event.get('kind') == 'claimed' and event.get('actor') == worker]
    generations = [_history_claim_generation(event, first, worker) for event in claims]
    need(generations.count(request['generation']) == 1, 'Original exact owner claim absent from history')
    fingerprint = api_fingerprint(first)
    second = client.request('/api/tasks/'+request['task'])
    need(api_fingerprint(second) == fingerprint, 'API/history changed during recovery snapshot')
    for name in originals:
        _, raw, current_identity = bound({'path': str(run_dir/name), 'sha256': refs[name]})
        need(current_identity == identities[name], 'Original file identity changed')
    return {'api_sha256': fingerprint, 'originals': refs, 'original_identities': identities,
            'run_inventory': inventory(root, worker), 'retained_unknowns': retained,
            'implementation_sha256': {name: digest(Path(__file__).with_name(name).read_bytes())
                                      for name in ('cli_recovery.py', 'cli_results.py', 'cli_dispatch.py')}}


def identity(value):
    return exact(value, 'pid uid birth') and type(value['pid']) is int and value['pid'] > 0 and type(value['uid']) is int and value['uid'] == os.getuid() and type(value['birth']) is list and len(value['birth']) == 2 and all(type(v) is int for v in value['birth']) and value['birth'][0] > 0 and 0 <= value['birth'][1] < 1000000


def write_exclusive(directory, name, raw):
    directory_identity = private(directory)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        need((info.st_dev, info.st_ino) == directory_identity, 'Evidence directory identity changed')
        leaf = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400, dir_fd=fd)
        with os.fdopen(leaf, 'w+b') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            stream.seek(0); need(stream.read() == raw, 'Evidence readback differs')
            info = os.fstat(stream.fileno()); public = (directory/name).lstat()
            need((info.st_dev, info.st_ino) == (public.st_dev, public.st_ino), 'Evidence leaf identity changed')
        os.fsync(fd)
        need(private(directory) == directory_identity, 'Evidence directory replaced')
    finally:
        os.close(fd)
    return {'path': str(directory/name), 'sha256': digest(raw)}


def append(run_dir, kind, value):
    directory = run_dir/'scoped-recovery'
    if not directory.exists(): directory.mkdir(mode=0o700)
    private(directory)
    need(sum(1 for _ in itertools.islice(directory.iterdir(), 4096)) < 4096, 'Recovery evidence inventory bound reached')
    raw = encode(value); need(len(raw) <= 32*MAX_RECORD, 'Recovery output exceeds bound')
    return write_exclusive(directory, kind+'-'+secrets.token_hex(16)+'.json', raw)


def plan(root, worker, run_id, request_ref, client, clock=time.time):
    with locked(root, run_id) as run_dir:
        request, _, _ = bound(request_ref)
        initial = snapshot(root, run_dir, request, client, worker)
        created = clock(); need(finite(created), 'Finite recovery clock required')
        value = {'schema': 1, 'kind': 'scoped-readonly-recovery-plan', 'request': request_ref,
                 'binding': initial, 'created_at': created, 'expires_at': created+request['ttl_seconds'],
                 'scope': 'bound-records-only', 'nativeObservationAuthorized': False,
                 'nativeExecutionAuthorized': False, 'resourceReleaseAuthorized': False,
                 'availability': 'unknown', 'wakeACKAuthorized': False}
        return append(run_dir, 'plan', value)


def observe_resource(resource, request, run_dir, now):
    private(run_dir/'recovery-inputs')
    value, raw, file_identity = bound(resource['reference'])
    result = {'id': resource['id'], 'operation': resource['operation'], 'reference': resource['reference'],
              'bytes': len(raw), 'file_identity': list(file_identity), 'read_at_epoch': now, 'verified_owned_closure': False}
    if resource['operation'] == 'read-json-record':
        result['metadata'] = value
        return result
    need(exact(value, 'schema kind run_id task worker generation resource_id observer_source_sha256 recorded_at available effects result provenance')
         and type(value['schema']) is int and value['schema'] == 1 and value['kind'] == 'owned-settlement-metadata'
         and all(value[k] == request[k] for k in ('run_id', 'task', 'worker', 'generation'))
         and type(value['generation']) is int and value['resource_id'] == resource['id'], 'Settlement receipt run/resource identity differs')
    expected = resource['expected']
    need(value['observer_source_sha256'] == expected['observer_source_sha256'] and finite(value['recorded_at'])
         and expected['not_before_epoch'] <= value['recorded_at'] <= min(now, expected['not_after_epoch']), 'Settlement source/provenance/clock differs')
    need(value['effects'] == [] and type(value['available']) is bool, 'Unknown effects or invalid observer state')
    result['metadata'] = value
    if not value['available']:
        result['reason'] = 'observer-unavailable'; return result
    row = value['result']
    fields = 'role identity waiter reaped normal_exit returncode wait_target_pid' if resource['operation'] == 'verify-owned-process-settlement' else 'role identity waiter thread_joined listener_closed connection_refused endpoint nonce'
    need(exact(row, fields) and identity(row['identity']) and identity(row['waiter'])
         and all(row[k] == expected[k] for k in ('role', 'identity', 'waiter')), 'Owned settlement identity differs')
    verify_provenance(value, resource, request, run_dir, result)
    if row['role'] in ROLES:
        need(type(row['wait_target_pid']) is int and row['wait_target_pid'] == expected['identity']['pid'], 'Wait receipt target differs from exact owned PID')
        wait_facts = ('reaped', 'normal_exit', 'returncode', 'wait_target_pid')
        need(all(value['provenance'][key]['reference'] == value['provenance']['wait_target_pid']['reference'] for key in wait_facts),
             'Wait target/reap/exit must come from one raw wait receipt')
        need(type(row['returncode']) is int and 0 <= row['returncode'] <= 255, 'Observed normal wait return required')
        result['verified_owned_closure'] = row['reaped'] is True and row['normal_exit'] is True
    else:
        need(row['endpoint'] == expected['endpoint'] and row['nonce'] == expected['nonce'], 'Owned server resource differs')
        result['verified_owned_closure'] = all(row[k] is True for k in ('thread_joined', 'listener_closed', 'connection_refused'))
    return result


def select(value, pointer):
    need(type(pointer) is str and 0 < len(pointer) <= 256 and pointer.startswith('/'), 'Bounded metadata JSON pointer required')
    for part in pointer[1:].split('/'):
        need(re.search(r'~(?![01])', part) is None, 'Invalid metadata pointer escape')
        part = part.replace('~1', '/').replace('~0', '~')
        if type(value) is list:
            need(re.fullmatch('0|[1-9][0-9]*', part) is not None, 'Exact metadata array index required')
            value = value[int(part)]
        else:
            need(type(value) is dict and part in value, 'Native receipt fact missing; no inference permitted')
            value = value[part]
    return value


def verify_provenance(value, resource, request, run_dir, result):
    facts = {key: item for key, item in value['result'].items() if key != 'role'}
    facts['recorded_at'] = value['recorded_at']
    provenance = value['provenance']
    need(type(provenance) is dict and set(provenance) == set(facts), 'Each positive settlement fact needs raw receipt provenance')
    allowed = [r['reference'] for r in request['resources'] if r['operation'] == 'read-json-record']
    allowed += [{'path': str(run_dir/name), 'sha256': digest} for name, digest in request['originals'].items()]
    cache = {}; result['provenance_reads'] = []
    for fact, expected in facts.items():
        source = provenance[fact]
        need(exact(source, 'reference pointer') and source['reference'] in allowed, 'Settlement provenance outside approved record inventory')
        key = source['reference']['path']
        if key not in cache:
            original, raw, file_identity = bound(source['reference'])
            result['bytes'] += len(raw)
            result['provenance_reads'].append({'reference': source['reference'], 'file_identity': list(file_identity)})
            cache[key] = original
        actual = select(cache[key], source['pointer'])
        # Generation projection is explicit; every required PID/UID/birth value
        # must exist in the raw record. Extra image fields remain in that record.
        if fact in ('identity', 'waiter') and type(actual) is dict:
            need(set(expected) <= set(actual), 'Raw receipt lacks complete owned generation')
            actual = {key: actual[key] for key in expected}
        need(type(actual) is type(expected) and encode(actual) == encode(expected), 'Settlement fact differs from bound raw receipt')


def observe(root, worker, run_id, plan_ref, review_ref, expected_fingerprint, client, clock=time.time):
    with locked(root, run_id) as run_dir:
        need(hex64(expected_fingerprint) and plan_ref['sha256'] == expected_fingerprint
             and Path(plan_ref['path']).parent == run_dir/'scoped-recovery', 'Exact local plan fingerprint required')
        value, _, plan_identity = bound(plan_ref, 32*MAX_RECORD)
        need(exact(value, 'schema kind request binding created_at expires_at scope nativeObservationAuthorized nativeExecutionAuthorized resourceReleaseAuthorized availability wakeACKAuthorized')
             and type(value['schema']) is int and value['schema'] == 1
             and value['kind'] == 'scoped-readonly-recovery-plan' and value['scope'] == 'bound-records-only'
             and value['availability'] == 'unknown' and all(value[k] is False for k in
             ('nativeObservationAuthorized', 'nativeExecutionAuthorized', 'resourceReleaseAuthorized', 'wakeACKAuthorized')), 'Recovery plan differs')
        request, _, request_identity = bound(value['request'])
        results = []; stage = 'review'
        audit = {'schema': 1, 'kind': 'scoped-readonly-recovery-evidence', 'plan': plan_ref, 'review': review_ref,
                 'retained_unknowns': value['binding']['retained_unknowns'], 'resources': results,
                 'availability': 'unknown', 'wakeACKAuthorized': False, 'execution_stopped': False,
                 'ownedClosureObservedHere': False,
                 'resourceReleaseAuthorized': False, 'nativeObservationAuthorized': False,
                 'nativeExecutionAuthorized': False, 'originalsRewritten': False}
        try:
            review, _, review_identity = bound(review_ref)
            need(exact(review, 'schema kind plan_sha256 decision scope resources source reviewed_at expires_at nativeObservationAuthorized nativeExecutionAuthorized resourceReleaseAuthorized')
                 and type(review['schema']) is int and review['schema'] == 1 and review['kind'] == 'scoped-readonly-recovery-review'
                 and review['plan_sha256'] == expected_fingerprint and review['decision'] == 'approved'
                 and review['scope'] == 'bound-records-only' and encode(review['resources']) == encode(request['resources'])
                 and exact(review['source'], 'thread_id message_id')
                 and all(type(v) is str and v.strip() for v in review['source'].values())
                 and all(review[k] is False for k in ('nativeObservationAuthorized', 'nativeExecutionAuthorized', 'resourceReleaseAuthorized')), 'Exact independently reviewed record-read scope required')
            now = clock()
            need(all(finite(v) for v in (now, value['created_at'], value['expires_at'], review['reviewed_at'], review['expires_at']))
                 and value['expires_at'] - value['created_at'] == request['ttl_seconds']
                 and value['created_at'] <= review['reviewed_at'] <= now < min(value['expires_at'], review['expires_at'])
                 and review['expires_at'] <= value['expires_at'], 'Recovery review/plan is stale or clock moved backwards')
            stage = 'one-shot-intent'
            intent = 'intent-'+expected_fingerprint+'.json'
            raw = encode({'plan': plan_ref, 'review': review_ref, 'scope': 'bound-records-only', 'nativeExecutionAuthorized': False})
            write_exclusive(run_dir/'scoped-recovery', intent, raw)
            stage = 'before-cas'
            before = snapshot(root, run_dir, request, client, worker)
            need(encode(before) == encode(value['binding']), 'Recovery plan API/history/original/inventory CAS differs')
            stage = 'read-resources'; total = 0
            for resource in request['resources']:
                reading = clock()
                need(finite(reading) and now <= reading < min(value['expires_at'], review['expires_at']), 'Recovery clock moved or deadline expired')
                now = reading
                result = observe_resource(resource, request, run_dir, reading); results.append(result)
                total += result['bytes']; need(total <= MAX_TOTAL, 'Resource reads exceed total bound')
            stage = 'after-cas'
            after = snapshot(root, run_dir, request, client, worker)
            need(encode(after) == encode(before), 'Recovery API/history/original/inventory changed during observation')
            finished = clock(); need(finite(finished) and now <= finished < min(value['expires_at'], review['expires_at']), 'Recovery read exceeded unchanged deadline')
            now = finished
            # Recheck each explicit resource after all reads; no changed input is accepted.
            for result in results:
                for original in [result] + result.get('provenance_reads', []):
                    _, _, current_identity = bound(original['reference'])
                    need(list(current_identity) == original['file_identity'], 'Resource file identity changed')
            for reference, expected in ((plan_ref, plan_identity), (value['request'], request_identity), (review_ref, review_identity)):
                _, _, current = bound(reference, 32*MAX_RECORD)
                need(current == expected, 'Plan/request/review identity changed during observation')
            finished = clock(); need(finite(finished) and now <= finished < min(value['expires_at'], review['expires_at']), 'Recovery evidence exceeded unchanged deadline')
            audit.update(status='recorded', recorded_at=finished, api_sha256=after['api_sha256'],
                         verified_owned_settlement=[r['id'] for r in results if r['verified_owned_closure']],
                         unresolved_owned_settlement=[r['id'] for r in results if r['operation'] != 'read-json-record' and not r['verified_owned_closure']],
                         observed_unknowns=[{'source': r['reference']['path'], **row}
                                            for r in results for row in unknowns(r.get('metadata', {}))])
        except (ValueError, OSError, KeyError, IndexError, TypeError, RuntimeError) as error:
            audit.update(status='refused', stage=stage, error={'type': type(error).__name__, 'message': str(error)[:1000], 'errno': getattr(error, 'errno', None)})
            reference = append(run_dir, 'refused', audit)
            raise ValueError('Scoped recovery refused; evidence: '+reference['path']) from error
        return append(run_dir, 'evidence', audit)
