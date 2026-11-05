"""Finite administrator grants for a verified human's current-host exception."""
import json
import re

from .attempt_accounting import BINDING, integer, require, text


EXPECTED = (*BINDING, 'requirements_sha256', 'attempt_allowance_sha256', 'attempt_exception_sha256')


def allowance_payload(body, workers):
    fields = {'request_id', 'task', 'generation', 'worker', 'source', 'response',
              'additional_attempts', 'expected', 'decision_verified',
              'execution_stopped', 'external_state_checked', 'evidence'}
    require(isinstance(body, dict) and set(body) == fields, 'Exact attempt-allowance fields required')
    require(len(json.dumps(body)) <= 16000, 'Allowance evidence exceeds bound')
    require(isinstance(body['request_id'], str) and re.fullmatch('[a-zA-Z0-9_-]{8,80}', body['request_id']),
            'Stable bounded request_id required')
    require(isinstance(body['task'], str) and re.fullmatch('[0-9a-f]{16}', body['task']), 'Exact existing task required')
    integer(body['generation'], 'generation', 1)
    require(body['worker'] in workers, 'Known current worker required')
    require(all(body[k] is True for k in ('decision_verified', 'execution_stopped', 'external_state_checked')),
            'Verify the primary human decision and stopped/checked execution')
    source = body['source']
    require(isinstance(source, dict) and set(source) == {'thread_id', 'message_id'}, 'Exact primary human source required')
    for key in source:
        text(source[key], 'source.' + key, 500)
    text(body['response'], 'actual human response', 2000)
    text(body['evidence'], 'scope evidence', 6000)
    integer(body['additional_attempts'], 'additional_attempts', 1)
    require(body['additional_attempts'] <= 10, 'Only a bounded grant of up to ten extra attempts is supported')
    expected = body['expected']
    require(isinstance(expected, dict) and set(expected) == set(EXPECTED), 'Exact expected task/assignment binding required')
    require(expected['status'] == 'queued' and expected['owner'] is None,
            'Allowance requires settled queued work without a live owner')
    require(expected['attempts_host'] == body['worker'], 'Allowance belongs to the current assignment host')
    for key in ('step', 'reassignment_count', 'fix_attempts_used', 'total_fix_attempts', 'attempt_base'):
        integer(expected[key], 'expected.' + key)
    for key in ('requirements_sha256', 'attempt_allowance_sha256', 'attempt_exception_sha256'):
        require(isinstance(expected[key], str) and re.fullmatch('[0-9a-f]{64}', expected[key]), 'Exact ' + key + ' required')
    return json.loads(json.dumps(body))
