"""Private run-bound follow-ups, returned only at existing CLI tool boundaries.

No daemon, model invocation, resume, terminal injection or service mutation.
The local authenticated OS user is the sender authority; messages grant no consent.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import time

from .cli_dispatch import CONFIG, ROOT, save


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def private(path, directory=False):
    info = path.lstat()
    if (path.resolve() != path or info.st_uid != os.getuid() or info.st_mode & 0o077
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
        raise ValueError('Messaging requires canonical private owner files')


def load_run(root, run_id):
    if not re.fullmatch('[0-9a-f]{32}', run_id or ''):
        raise ValueError('Exact run ID required')
    private(root, True)
    run_dir = root/run_id
    private(run_dir, True)
    private(run_dir/'state.json')
    run = json.loads((run_dir/'state.json').read_text())
    if run.get('run_id') != run_id:
        raise ValueError('Run identity mismatch')
    return run_dir, run


def binding(root, run, config, task):
    # Recheck after acquiring the message lock; a sender may have waited while
    # the supervisor settled or a continuation replaced the launch record.
    current = json.loads((root/run['run_id']/'state.json').read_text())
    if current != run:
        raise ValueError('Run changed while preparing message operation')
    if (socket.gethostname() != config['hostname'] or run.get('worker') != config['worker']
            or run.get('account_email_sha256') != config['account_email_sha256']
            or any(run.get(k) != config[k] for k in ('model', 'reasoning'))
            or run.get('status') != 'running' or run.get('cli_exit_code') is not None
            or task.get('id') != run.get('task') or task.get('status') != 'running'
            or task.get('owner') != run['worker']
            or type(run.get('queued_generation')) is not int
            or task.get('generation') != run['queued_generation'] + 1):
        raise ValueError('Run/account/model or active task generation is unavailable')
    # A live worker lock plus latest run and live process prevents stale-state use.
    private(root/'worker.lock')
    with (root/'worker.lock').open() as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            raise ValueError('Run has no active dispatch lock')
    for path in root.glob('*/state.json'):
        other = json.loads(path.read_text())
        if other.get('created_at', 0) > run['created_at']:
            raise ValueError('A newer run supersedes this message target')
    if type(run.get('cli_pid')) is not int or run['cli_pid'] <= 1:
        raise ValueError('Missing live CLI process')
    os.kill(run['cli_pid'], 0)
    threads = set()
    with (root/run['run_id']/'events.jsonl').open() as events:
        for line in events:
            try:
                event = json.loads(line)
            except ValueError:
                continue  # The writer may be in the middle of its final line.
            if event.get('type') == 'thread.started':
                threads.add(event.get('thread_id'))
    if len(threads) != 1:
        raise ValueError('Missing unique actual CLI thread')
    thread = threads.pop()
    if not isinstance(thread, str) or not re.fullmatch('[0-9a-f-]{36}', thread):
        raise ValueError('Invalid actual thread identity')
    return {'run_id': run['run_id'], 'task': task['id'], 'generation': task['generation'],
            'step': task['step'], 'worker': run['worker'], 'thread_id': thread,
            'account_email_sha256': run['account_email_sha256'],
            'model': run['model'], 'reasoning': run['reasoning']}


@contextmanager
def ledger(run_dir):
    lock_path = run_dir/'messages.lock'
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'r+') as lock:
        private(lock_path)
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = run_dir/'messages.json'
        if path.exists() or path.is_symlink():
            private(path)
            value = json.loads(path.read_text())
        else:
            value = {'schema': 1, 'messages': [], 'events': []}
        yield value
        save(path, value)
        fd = os.open(run_dir, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def event(book, message, state):
    message['status'] = state
    book['events'].append({'sequence': message['sequence'], 'status': state, 'at': time.time()})


def submit(client, request, root=ROOT, config_path=CONFIG):
    required = {'run_id', 'task', 'generation', 'step', 'worker', 'thread_id',
                'account_email_sha256', 'model', 'reasoning', 'message_id', 'text'}
    if not isinstance(request, dict) or set(request) not in (required, required | {'media'}):
        raise ValueError('Message requires exact binding, message_id and text only')
    if (type(request['generation']) is not int or type(request['step']) is not int
            or not isinstance(request['message_id'], str)
            or not re.fullmatch('[0-9a-f]{32}', request['message_id'])
            or not isinstance(request['text'], str) or not request['text'].strip()
            or len(request['text'].encode()) > 12000
            or any(ord(c) < 32 and c not in '\n\t' for c in request['text'])):
        raise ValueError('Invalid message ID or bounded plain text')
    config = json.loads(config_path.read_text())
    if client.config['worker'] != config['worker']:
        raise ValueError('Foreign queue client')
    run_dir, run = load_run(root, request['run_id'])
    with ledger(run_dir) as book:
        target = binding(root, run, config, client.request('/api/tasks/'+run['task']))
        if any(request[k] != v for k, v in target.items()):
            raise ValueError('Follow-up binding differs from actual active session')
        existing = next((m for m in book['messages'] if m['message_id'] == request['message_id']), None)
        if existing:
            if existing['request_sha256'] != digest(request):
                raise ValueError('Message ID was reused with different content or binding')
            if 'media_manifest' in existing:
                from .cli_media import verify
                verify(run_dir, existing['media_manifest'], existing['media_manifest_sha256'])
            return public(existing)
        if len(book['messages']) >= 1000:
            raise ValueError('Run message limit reached; no message staged')
        message = {**request, 'sequence': len(book['messages'])+1,
                   'text_sha256': hashlib.sha256(request['text'].encode()).hexdigest(),
                   'request_sha256': digest(request)}
        if 'media' in request:
            from .cli_media import stage
            manifest, content_hash = stage(run_dir, request, message['sequence'])
            message.pop('media')  # Never put transport bytes into ledger/telemetry.
            message.update(media_manifest=manifest, media_manifest_sha256=content_hash,
                           media_status='staged', inspection=None)
        book['messages'].append(message)
        event(book, message, 'queued')
        return public(message)


def public(message):
    result = {k: message[k] for k in ('message_id', 'sequence', 'status', 'task', 'generation',
                                     'worker', 'run_id', 'thread_id', 'text_sha256')}
    if 'media_manifest' in message:
        result.update(media_manifest_sha256=message['media_manifest_sha256'],
                      content_sha256=[i['sha256'] for i in message['media_manifest']['attachments']],
                      media_status=message['media_status'], inspection=message['inspection'],
                      media_preparation_sha256=message.get('media_preparation_sha256'))
    return result


def closeout(run_dir):
    """Supervisor records undelivered follow-ups after its exact child exits."""
    if not (run_dir/'messages.json').exists():
        return []
    with ledger(run_dir) as book:
        outstanding = []
        for message in book['messages']:
            if message['status'] != 'readback':
                outstanding.append(message['message_id'])
                if message['status'] != 'session_ended_unread':
                    event(book, message, 'session_ended_unread')
        save(run_dir/'message-closeout.json', {'execution_stopped': True,
             'unreceived_message_ids': outstanding, 'forwarded': False})
        return outstanding


def own_run(root):
    # These are supplied by Codex to tool execution, never taken from a message.
    thread = os.environ.get('CODEX_THREAD_ID')
    if not thread:
        return None
    found = []
    for path in root.glob('*/state.json'):
        run = json.loads(path.read_text())
        if run.get('status') != 'running':
            continue
        events = path.parent/'events.jsonl'
        if events.exists():
            with events.open() as stream:
                for line in stream:
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    if item.get('type') == 'thread.started' and item.get('thread_id') == thread:
                        found.append(run['run_id'])
                        break
    if len(found) != 1:
        return None
    return found[0]


def receive(client, ident, root=ROOT, config_path=CONFIG):
    run_id = own_run(root)
    if run_id is None:
        return None
    run_dir, run = load_run(root, run_id)
    if run.get('task') != ident or not (run_dir/'messages.json').exists():
        return None
    config = json.loads(config_path.read_text())
    if client.config['worker'] != config['worker']:
        raise ValueError('Foreign receiving client')
    with ledger(run_dir) as book:
        target = binding(root, run, config, client.request('/api/tasks/'+ident))
        if target['thread_id'] != os.environ.get('CODEX_THREAD_ID'):
            raise ValueError('Follow-up belongs to another actual session')
        for message in book['messages']:
            if message['status'] == 'readback':
                continue
            if message['status'] not in ('queued', 'delivered_to_tool'):
                raise ValueError('Message delivery ended; no automatic replay')
            if any(message[k] != v for k, v in target.items()):
                raise ValueError('Queued message binding became stale; retained for inspection')
            media = {}
            if 'media_manifest' in message:
                from .cli_media import prepare
                media = {'media_manifest':message['media_manifest'],
                         'media_preparation':prepare(message, run_dir, config)}
                message['media_preparation'] = media['media_preparation']
                message['media_preparation_sha256'] = digest(media['media_preparation'])
                message['media_status'] = 'delivered'
            if message['status'] == 'queued':
                event(book, message, 'delivered_to_tool')
            return {**public(message), **media, 'text': message['text'],
                    'notice': 'Scoped follow-up; no consent or permission override. Delivery to tool is not worker readback. '
                              'Acknowledge exact message_id/sequence/text_sha256 using nova-cli-worker message-readback. '
                              'For media include manifest/content hashes and inspection records; acknowledgment alone is not inspection. '
                              'Until readback, this same sequence is returned; later messages remain queued.'}


def acknowledge(client, receipt, root=ROOT, config_path=CONFIG):
    base = {'run_id', 'message_id', 'sequence', 'text_sha256'}
    media_keys = {'media_manifest_sha256','media_preparation_sha256','content_sha256','inspection'}
    if (not isinstance(receipt, dict) or set(receipt) not in (base, base | media_keys)
            or type(receipt['sequence']) is not int):
        raise ValueError('Exact run/message/sequence/content readback required')
    if own_run(root) != receipt['run_id']:
        raise ValueError('Readback must originate in the actual receiving Codex session')
    run_dir, run = load_run(root, receipt['run_id'])
    config = json.loads(config_path.read_text())
    if client.config['worker'] != config['worker']:
        raise ValueError('Foreign readback client')
    with ledger(run_dir) as book:
        target = binding(root, run, config, client.request('/api/tasks/'+run['task']))
        message = next((m for m in book['messages'] if m['message_id'] == receipt['message_id']), None)
        if (message is None or any(message[k] != v for k, v in target.items())
                or any(message[k] != receipt[k] for k in base)
                or message['status'] not in ('delivered_to_tool', 'readback')
                or any(m['status'] != 'readback' for m in book['messages'][:message['sequence']-1])):
            raise ValueError('Message not delivered, mismatched or out of order')
        if 'media_manifest' in message:
            from .cli_media import validate_inspection
            validate_inspection(run_dir, message, receipt)
            if message['status'] == 'readback' and message['inspection'] != receipt['inspection']:
                raise ValueError('Conflicting inspection retry')
            message['inspection'] = receipt['inspection']
            message['media_status'] = 'acknowledged'
        elif set(receipt) != base:
            raise ValueError('Unexpected media receipt on legacy text')
        if message['status'] != 'readback':
            event(book, message, 'readback')
        return public(message)


def status(client, run_id, root=ROOT, config_path=CONFIG):
    run_dir, run = load_run(root, run_id)
    config = json.loads(config_path.read_text())
    if client.config['worker'] != config['worker'] or run.get('worker') != config['worker']:
        raise ValueError('Foreign message status')
    try:
        binding(root, run, config, client.request('/api/tasks/'+run['task']))
        available = True
        reason = None
    except (ValueError, OSError) as error:
        available = False
        reason = type(error).__name__
    with ledger(run_dir) as book:
        return {'run_id': run_id, 'available': available, 'availability_error_type': reason,
                'messages': [public(m) for m in book['messages']],
                'unreceived': [m['message_id'] for m in book['messages'] if m['status'] != 'readback'],
                'events': book['events']}
