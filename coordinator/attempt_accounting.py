"""Bounded administrative evidence for monotonic attempt accounting.

References bind the administrator's verified owning/historical receipts. The
service cannot open a worker's filesystem or independently stop its processes.
This is accounting evidence, never permission, acceptance or an extra attempt.
"""
import json
import re
from pathlib import PurePosixPath


COUNTERS = ('fix_attempts_used', 'total_fix_attempts', 'attempt_base')
BINDING = ('status', 'owner', 'step', 'reassignment_count', 'attempts_host', *COUNTERS)


def require(ok, message):
    if not ok:
        raise ValueError(message)


def text(value, name, maximum=4000):
    require(isinstance(value, str) and value.strip() and len(value) <= maximum,
            name + ' must be bounded nonempty text')


def integer(value, name, minimum=0):
    require(type(value) is int and value >= minimum, name + ' must be a nonnegative integer')


def reference(value):
    require(isinstance(value, dict) and set(value) == {'path', 'sha256'},
            'Exact immutable path/SHA-256 reference required')
    path = value['path']
    text(path, 'reference path', 2048)
    require(path.startswith('/') and '\x00' not in path and '..' not in PurePosixPath(path).parts,
            'Absolute reference path without traversal required')
    require(isinstance(value['sha256'], str) and re.fullmatch('[0-9a-f]{64}', value['sha256']),
            'Exact reference SHA-256 required')


def correction_payload(body, workers, host_limit, reassignment_limit):
    fields = {'request_id', 'generation', 'expected', 'corrected', 'owner_proof',
              'historical_proof', 'evidence'}
    require(isinstance(body, dict) and set(body) == fields, 'Exact attempt-correction fields required')
    require(len(json.dumps(body)) <= 40000, 'Attempt correction exceeds bound')
    require(isinstance(body['request_id'], str) and re.fullmatch('[a-zA-Z0-9_-]{8,80}', body['request_id']),
            'Stable bounded correction request_id required')
    integer(body['generation'], 'generation', 1)
    text(body['evidence'], 'evidence', 6000)
    old, new = body['expected'], body['corrected']
    require(isinstance(old, dict) and set(old) == set(BINDING), 'Exact expected task/counter binding required')
    require(isinstance(new, dict) and set(new) == set(COUNTERS), 'Exact corrected counter tuple required')
    require(old['status'] in ('queued', 'running', 'held', 'blocked', 'failed', 'pending_external_approval'),
            'Only existing unfinished work can have accounting corrected')
    require(old['owner'] is None or old['owner'] in workers, 'Unknown expected owner')
    require(old['attempts_host'] in workers, 'Known assignment host required')
    for key in ('step', 'reassignment_count', *COUNTERS):
        integer(old[key], 'expected.' + key)
    require(old['reassignment_count'] <= reassignment_limit, 'Existing reassignment limit exceeded')
    for key in COUNTERS:
        integer(new[key], 'corrected.' + key)
        require(new[key] >= old[key], 'Attempt accounting cannot decrease ' + key)
    for value in (old, new):
        require(value['total_fix_attempts'] == value['attempt_base'] + value['fix_attempts_used'],
                'Total attempts must equal prior-assignment base plus current usage')
    require(new != {key: old[key] for key in COUNTERS}, 'Correction must record actual omitted attempts')

    owner = body['owner_proof']
    owner_fields = {'task', 'worker', 'generation', 'step', 'fix_attempts_used', 'verified',
                    'execution_stopped', 'external_state_checked', 'unsettled_operations',
                    'receipt', 'source', 'evidence'}
    require(isinstance(owner, dict) and set(owner) == owner_fields, 'Exact owning settlement proof required')
    require(owner['verified'] is True and owner['execution_stopped'] is True
            and owner['external_state_checked'] is True and owner['unsettled_operations'] == [],
            'Verified owning proof must confirm stopped, checked execution with no unsettled operations')
    require(type(owner['unsettled_operations']) is list, 'Unsettled operations must be an empty list')
    require(owner['worker'] == old['attempts_host'] and old['owner'] in (None, owner['worker']),
            'Settlement proof must belong to the actual assignment owner')
    for key, expected in (('generation', body['generation']), ('step', old['step']),
                          ('fix_attempts_used', new['fix_attempts_used'])):
        integer(owner[key], 'owner_proof.' + key)
        require(owner[key] == expected, 'Owning proof does not match ' + key)
    for key in ('receipt', 'source'):
        reference(owner[key])
    text(owner['evidence'], 'owner proof evidence')

    history = body['historical_proof']
    require(isinstance(history, list) and len(history) <= reassignment_limit,
            'Bounded earlier assignment proofs required')
    generations = set()
    for proof in history:
        fields = {'task', 'worker', 'generation', 'step', 'omitted_attempts',
                  'recorded_attempts', 'fix_attempts_used', 'verified',
                  'receipt', 'source', 'evidence'}
        require(isinstance(proof, dict) and set(proof) == fields, 'Exact historical proof required')
        require(proof['verified'] is True and proof['worker'] in workers
                and proof['worker'] != owner['worker'], 'Verified earlier other-host assignment required')
        integer(proof['generation'], 'historical generation', 1)
        integer(proof['step'], 'historical step')
        integer(proof['omitted_attempts'], 'omitted attempts', 1)
        integer(proof['recorded_attempts'], 'historical recorded attempts')
        integer(proof['fix_attempts_used'], 'historical actual attempts')
        require(proof['generation'] < body['generation'] and proof['generation'] not in generations,
                'Historical generations must be earlier and unique')
        generations.add(proof['generation'])
        require(proof['fix_attempts_used'] <= host_limit
                and proof['fix_attempts_used'] - proof['recorded_attempts'] == proof['omitted_attempts'],
                'Historical actual/recorded/omitted counts must agree within the host limit')
        for key in ('receipt', 'source'):
            reference(proof[key])
        text(proof['evidence'], 'historical proof evidence')
    require(sum(p['omitted_attempts'] for p in history) == new['attempt_base'] - old['attempt_base'],
            'Prior-base increase must exactly match omitted historical attempts')
    # Freeze caller-owned objects before comparison/audit inside the transaction.
    return json.loads(json.dumps(body))
