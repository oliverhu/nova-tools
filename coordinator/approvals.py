"""Shared, non-secret identity and scope validation for explicit approvals."""
import hashlib
import json
import re


def approval_payload(body):
    def text(value, field, limit=2000):
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError(field + ' must be nonempty text')
        return value.strip()

    if not isinstance(body, dict) or body.get('decision') != 'approved' or body.get('decision_verified') is not True:
        raise ValueError('An explicitly verified approved decision is required; missing/denied decisions cannot resume work')
    source = body.get('source')
    if not isinstance(source, dict):
        raise ValueError('Original approval source is required')
    source = {k:text(source.get(k), 'source.'+k, 500) for k in ('thread_id','question_id','message_id')}
    tasks = body.get('tasks')
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= 50:
        raise ValueError('Explicit covered tasks are required (1–50)')
    normalized = []
    for task in tasks:
        if not isinstance(task, dict) or not re.fullmatch('[0-9a-f]{16}', str(task.get('id',''))):
            raise ValueError('Invalid covered task ID')
        if type(task.get('generation')) is not int or task['generation'] < 1:
            raise ValueError('Each covered task requires its exact pending generation')
        if task.get('execution_stopped') is not True or task.get('external_state_checked') is not True:
            raise ValueError('Each covered task requires settled execution and checked external state')
        normalized.append({'id':task['id'],'generation':task['generation'],
            'scope_evidence':text(task.get('scope_evidence'), 'task.scope_evidence'),
            'execution_stopped':True,'external_state_checked':True})
    if len({t['id'] for t in normalized}) != len(normalized):
        raise ValueError('Duplicate covered task IDs')
    payload = {'source':source,'decision':'approved','decision_verified':True,
            'response':text(body.get('response'), 'response'),
            'scope':text(body.get('scope'), 'scope'),
            'tasks':sorted(normalized,key=lambda t:t['id'])}
    if len(json.dumps(payload)) > 140000:
        raise ValueError('Approval receipt exceeds the request size limit')
    for task in normalized:
        if len(json.dumps({'source':source,'response':payload['response'],'scope':payload['scope'],
                           'scope_evidence':task['scope_evidence']})) > 11000:
            raise ValueError('Approval evidence exceeds the audit event size limit')
    return payload


def approval_id(payload):
    return hashlib.sha256(json.dumps(payload['source'],sort_keys=True,separators=(',',':')).encode()).hexdigest()
