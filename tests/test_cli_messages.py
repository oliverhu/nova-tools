import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

from coordinator import cli_messages as m
from coordinator.cli_dispatch import save


class MessageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.root.chmod(0o700)
        self.run_id = 'a'*32
        self.thread = '01234567-89ab-cdef-0123-456789abcdef'
        self.run_dir = self.root/self.run_id
        self.run_dir.mkdir(mode=0o700)
        self.config = {'hostname': socket.gethostname(), 'worker': 'mini2',
                       'account_email_sha256': 'b'*64, 'model': 'selected', 'reasoning': 'medium'}
        self.cfg = self.root/'config.json'
        save(self.cfg, self.config)
        self.run = {**self.config, 'run_id': self.run_id, 'task': 'c'*16,
                    'queued_generation': 0, 'status': 'running', 'created_at': 10,
                    'cli_pid': os.getpid()}
        save(self.run_dir/'state.json', self.run)
        (self.run_dir/'events.jsonl').write_text(json.dumps({'type': 'thread.started', 'thread_id': self.thread})+'\n')
        self.lock = (self.root/'worker.lock').open('w')
        (self.root/'worker.lock').chmod(0o600)
        fcntl.flock(self.lock, fcntl.LOCK_EX)
        self.task = {'id': self.run['task'], 'generation': 1, 'step': 0,
                     'status': 'running', 'owner': 'mini2'}
        self.client = type('Client', (), {'config': {'worker': 'mini2'}})()
        self.client.request = lambda path: copy.deepcopy(self.task)
        self.req = {k: self.config[k] for k in ('worker','account_email_sha256','model','reasoning')}
        self.req.update(run_id=self.run_id, task=self.task['id'], generation=1, step=0,
                        thread_id=self.thread, message_id='d'*32, text='first scoped follow-up')
        self.env = patch.dict(os.environ, {'CODEX_THREAD_ID': self.thread})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.lock.close()
        self.temp.cleanup()

    def send(self, req=None):
        return m.submit(self.client, req or self.req, self.root, self.cfg)

    def receive(self):
        return m.receive(self.client, self.task['id'], self.root, self.cfg)

    def ack(self, message):
        receipt = {k: message[k] for k in ('run_id','message_id','sequence','text_sha256')}
        return m.acknowledge(self.client, receipt, self.root, self.cfg)

    def test_ordered_durable_tool_delivery_and_explicit_content_readback(self):
        first = self.send()
        second = self.send({**self.req, 'message_id': 'e'*32, 'text': 'second'})
        self.assertEqual(first['status'], 'queued')
        with self.assertRaises(ValueError): self.ack(first)
        delivered = self.receive()
        self.assertEqual(delivered['status'], 'delivered_to_tool')
        self.assertEqual(self.receive(), delivered)
        self.assertEqual(self.ack(delivered)['status'], 'readback')
        self.assertEqual(self.ack(delivered)['status'], 'readback')
        next_message = self.receive()
        self.assertEqual(next_message['sequence'], second['sequence'])
        self.ack(next_message)
        self.assertIsNone(self.receive())
        status = m.status(self.client, self.run_id, self.root, self.cfg)
        self.assertEqual(status['unreceived'], [])
        self.assertNotIn('text', status['messages'][0])
        self.assertEqual([e['status'] for e in status['events']],
                         ['queued','queued','delivered_to_tool','readback','delivered_to_tool','readback'])
        self.assertEqual((self.run_dir/'messages.json').stat().st_mode & 0o777, 0o600)

    def test_idempotent_retry_and_conflicting_reuse(self):
        self.assertEqual(self.send(), self.send())
        with self.assertRaises(ValueError): self.send({**self.req, 'text': 'changed'})
        self.assertEqual(len(json.loads((self.run_dir/'messages.json').read_text())['messages']), 1)

    def test_every_binding_dimension_refuses_misrouting_before_staging(self):
        for key, value in [('run_id','f'*32), ('task','f'*16), ('worker','mini1'),
                           ('generation',2), ('step',1), ('thread_id','f'*36),
                           ('account_email_sha256','f'*64), ('model','other'), ('reasoning','high')]:
            with self.subTest(key=key), self.assertRaises((ValueError, FileNotFoundError)):
                self.send({**self.req, key:value})
        self.assertFalse((self.run_dir/'messages.json').exists())

    def test_finished_reassigned_and_changed_generation_refuse(self):
        self.send()
        original = copy.deepcopy(self.task)
        for change in [{'status':'completed'}, {'owner':'mini1'}, {'generation':2}]:
            self.task = {**original, **change}
            with self.assertRaises(ValueError): self.send()
            with self.assertRaises(ValueError): self.receive()
        status = m.status(self.client,self.run_id,self.root,self.cfg)
        self.assertFalse(status['available'])
        self.assertEqual(status['messages'][0]['status'], 'queued')
        self.assertEqual(status['unreceived'], [self.req['message_id']])

    def test_wrong_actual_session_cannot_receive_or_ack(self):
        first = self.send()
        with patch.dict(os.environ, {'CODEX_THREAD_ID':'wrong'}):
            self.assertIsNone(self.receive())
            with self.assertRaises(ValueError): self.ack(first)
        delivered = self.receive()
        with self.assertRaises(ValueError): self.ack({**delivered,'text_sha256':'0'*64})

    def test_lost_lock_dead_pid_newer_run_and_changed_account_refuse(self):
        fcntl.flock(self.lock, fcntl.LOCK_UN)
        with self.assertRaises(ValueError): self.send()
        fcntl.flock(self.lock, fcntl.LOCK_EX)
        with patch.object(m.os, 'kill', side_effect=ProcessLookupError):
            with self.assertRaises(ProcessLookupError): self.send()
        other = self.root/('f'*32); other.mkdir(mode=0o700)
        save(other/'state.json', {**self.run,'created_at':11})
        with self.assertRaises(ValueError): self.send()
        # Preserve the newer record and verify config change as a separate fixture.
        save(self.cfg,{**self.config,'account_email_sha256':'0'*64})
        with self.assertRaises(ValueError): self.send()

    def test_concurrent_idempotent_senders_create_one_sequence(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _:self.send(), range(20)))
        self.assertEqual({r['sequence'] for r in results}, {1})
        self.assertEqual(len(json.loads((self.run_dir/'messages.json').read_text())['events']),1)

    def test_symlinks_public_files_and_controls_rejected(self):
        self.send()
        path = self.run_dir/'messages.json'
        path.chmod(0o644)
        with self.assertRaises(ValueError): self.receive()
        path.chmod(0o600)
        target = self.run_dir/'kept.json'; path.rename(target); path.symlink_to(target)
        with self.assertRaises(ValueError): self.receive()
        with self.assertRaises(ValueError): self.send({**self.req,'text':'escape\x1b[2J'})

    def test_service_failure_does_not_mark_received(self):
        self.send()
        def unavailable(_): raise RuntimeError('service unavailable')
        self.client.request = unavailable
        with self.assertRaises(RuntimeError): self.receive()
        book = json.loads((self.run_dir/'messages.json').read_text())
        self.assertEqual(book['messages'][0]['status'], 'queued')
        self.assertEqual(len(book['events']),1)

    def test_supervisor_closeout_surfaces_unread_without_replay(self):
        self.send()
        self.receive()
        self.assertEqual(m.closeout(self.run_dir), [self.req['message_id']])
        self.assertEqual(m.closeout(self.run_dir), [self.req['message_id']])
        book = json.loads((self.run_dir/'messages.json').read_text())
        self.assertEqual([e['status'] for e in book['events']],
                         ['queued','delivered_to_tool','session_ended_unread'])
        self.assertEqual(json.loads((self.run_dir/'message-closeout.json').read_text())['forwarded'],False)
        with self.assertRaises(ValueError): self.ack(book['messages'][0])


if __name__ == '__main__': unittest.main()
