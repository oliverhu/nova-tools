import fcntl
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from coordinator.client import Client
from coordinator.store import Store, Invalid
from coordinator.worker_status import collect_cli_status, validate_cli_status, FIELDS
from coordinator.workers import MINI_VM_WORKERS, STUDIO_WORKERS


FIRST, SECOND = MINI_VM_WORKERS
TASK = 'a' * 16


def sample(**changes):
    return dict(available=True, busy=True, run_state='running', task_id=TASK,
                task_generation=1, completion_verified=False, **changes)


class LocalCLIStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)/'private-cli'
        self.root.mkdir(mode=0o700)
        (self.root/'worker.lock').touch(mode=0o600)
        self.run = self.root/('b'*32); self.run.mkdir(mode=0o700)

    def tearDown(self): self.temp.cleanup()

    def state(self, **changes):
        data = dict(worker=FIRST, status='running', task=TASK, created_at=100,
                    cli_pid=1337, command=['PRIVATE_COMMAND'], model='PRIVATE_MODEL',
                    thread='PRIVATE_THREAD', error='PRIVATE_ERROR', secret='PRIVATE_CREDENTIAL')
        data.update(changes)
        (self.run/'state.json').write_text(json.dumps(data))

    def test_live_lock_and_whitelist_never_publish_private_data(self):
        self.state()
        with (self.root/'worker.lock').open() as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = collect_cli_status(FIRST, self.root)
        self.assertTrue(result['available']); self.assertTrue(result['busy'])
        self.assertEqual('running', result['run_state'])
        self.assertEqual(TASK, result['task_id'])
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertEqual(FIELDS, set(result))

    def test_unlocked_running_record_requires_reconciliation_not_idle(self):
        self.state()
        self.assertEqual('needs_reconciliation', collect_cli_status(FIRST, self.root)['run_state'])

    def test_completed_run_retains_verified_result_and_generation_only(self):
        self.state(status='completed', completion_verified=True, ended_at=200)
        (self.run/'task-readback.json').write_text(json.dumps({'id': TASK, 'generation': 3, 'private': 'PRIVATE_TOKEN'}))
        result = collect_cli_status(FIRST, self.root)
        self.assertEqual('completed', result['run_state']); self.assertTrue(result['completion_verified'])
        self.assertEqual(3, result['task_generation']); self.assertFalse(result['busy'])
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_missing_state_foreign_identity_or_symlink_is_unavailable(self):
        self.assertFalse(collect_cli_status(FIRST, self.root/'missing')['available'])
        self.state(worker=SECOND)
        self.assertFalse(collect_cli_status(FIRST, self.root)['available'])
        (self.run/'state.json').unlink()
        target = self.root/'unrelated'; target.write_text('{"secret":"PRIVATE"}')
        (self.run/'state.json').symlink_to(target)
        self.assertFalse(collect_cli_status(FIRST, self.root)['available'])

    def test_local_status_does_not_create_root_or_start_any_process(self):
        missing = self.root/'missing'
        with patch('subprocess.run') as run, patch('subprocess.Popen') as spawn:
            result = collect_cli_status(FIRST, missing)
        self.assertFalse(result['available']); self.assertFalse(missing.exists())
        run.assert_not_called(); spawn.assert_not_called()

    def test_special_state_file_is_unavailable_without_blocking_reporter(self):
        os.mkfifo(self.run/'state.json')
        self.assertFalse(collect_cli_status(FIRST,self.root)['available'])


class WorkerStatusStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.now = 1000
        self.path = Path(self.temp.name)/'db'; self.store = Store(self.path, clock=lambda:self.now)
        self.capacity = {'cpus':5,'memory_gb':8,'disk_gb':100}

    def tearDown(self): self.temp.cleanup()

    def test_pulse_updates_only_authenticated_workers_metadata_and_keeps_pause(self):
        self.store.pulse(FIRST, {'capacity':self.capacity,'cli_status':sample()})
        rows = {w['id']:w for w in self.store.snapshot()['workers']}
        self.assertEqual('running', rows[FIRST]['cli_status']['run_state'])
        self.assertEqual(1000, rows[FIRST]['cli_status_seen']); self.assertTrue(rows[FIRST]['paused'])
        self.assertEqual({}, rows[SECOND]['cli_status']); self.assertIsNone(rows[SECOND]['cli_status_seen'])

    def test_old_capacity_pulse_does_not_refresh_old_cli_freshness(self):
        self.store.pulse(FIRST, {'capacity':self.capacity,'cli_status':sample()})
        self.now += 100; self.store.pulse(FIRST, {'capacity':self.capacity})
        row = next(w for w in self.store.snapshot()['workers'] if w['id']==FIRST)
        self.assertTrue(row['online']); self.assertEqual(1000,row['cli_status_seen'])
        self.assertEqual(1100,row['seen'])

    def test_raw_private_extra_fields_or_nonfinite_timestamps_are_rejected_before_write(self):
        for change in [{'claim_token':'PRIVATE'},{'prompt':'PRIVATE'},{'activity_at':float('nan')},
                       {'task_id':'not-a-task'},{'task_generation':True},{'busy':'yes'}]:
            body=sample();body.update(change)
            with self.assertRaises(Invalid): self.store.pulse(FIRST, {'capacity':self.capacity,'cli_status':body})
        row = next(w for w in self.store.snapshot()['workers'] if w['id']==FIRST)
        self.assertEqual(0,row['seen']);self.assertEqual({},row['cli_status'])

    def test_metadata_schema_migration_preserves_task_claim_and_counters(self):
        self.store.pulse(FIRST, {'capacity':self.capacity})
        self.store.set_worker_state('admin',FIRST,{'paused':False,'reason':'Authored readiness fixture'})
        task=self.store.submit('admin',{'request_key':'fixture','title':'Existing active task','requirements':{'worker':FIRST}})
        self.store.claim(FIRST,task['id'])
        with self.store.connection() as db:
            before=dict(db.execute('SELECT * FROM tasks WHERE id=?',(task['id'],)).fetchone())
            db.execute('ALTER TABLE workers DROP COLUMN cli_status');db.execute('ALTER TABLE workers DROP COLUMN cli_status_seen')
        self.store=Store(self.path,clock=lambda:self.now)
        self.store.pulse(FIRST,{'capacity':self.capacity,'cli_status':sample()})
        with self.store.connection() as db:after=dict(db.execute('SELECT * FROM tasks WHERE id=?',(task['id'],)).fetchone())
        self.assertEqual(before,after)
        self.assertFalse(next(w for w in self.store.snapshot()['workers'] if w['id']==FIRST)['paused'])

    def test_worker_context_survives_recent_history_limit_without_changing_claims(self):
        history = self.store.submit('admin', {'request_key':'old-history','title':'Old completed work'})
        with self.store.connection() as db:
            db.execute("UPDATE tasks SET status='completed',last_owner=?,updated=999 WHERE id=?", (FIRST,history['id']))
        self.store.pulse(FIRST, {'capacity':self.capacity})
        self.store.set_worker_state('admin',FIRST,{'paused':False,'reason':'Authored readiness fixture'})
        active = self.store.submit('admin', {'request_key':'old-active','title':'Long running task','requirements':{'worker':FIRST}})
        self.store.claim(FIRST, active['id'])
        for index in range(501):
            self.now += 1
            self.store.submit('admin', {'request_key':f'newer-{index}','title':'Newer queue entry'})
        with self.store.connection() as db:
            before = dict(db.execute('SELECT * FROM tasks WHERE id=?',(active['id'],)).fetchone())
        snapshot = self.store.snapshot()
        self.assertEqual(500,len(snapshot['tasks']))
        self.assertNotIn(active['id'], {t['id'] for t in snapshot['tasks']})
        context = {t['id']:t for t in snapshot['worker_tasks']}
        self.assertEqual(FIRST, context[active['id']]['owner'])
        self.assertEqual('completed', context[history['id']]['status'])
        self.assertNotIn('claim_token', context[active['id']])
        with self.store.connection() as db:
            after = dict(db.execute('SELECT * FROM tasks WHERE id=?',(active['id'],)).fetchone())
        self.assertEqual(before,after)


class WorkerStatusClientTests(unittest.TestCase):
    def test_existing_pulse_adds_status_only_for_minis_without_changing_adapter(self):
        for worker in (FIRST, SECOND, STUDIO_WORKERS[0]):
            client=Client.__new__(Client);client.config={'worker':worker};sent=[]
            client.request=lambda path,body:sent.append((path,body))
            with patch('coordinator.client.subprocess.check_output',return_value=b'8589934592'),\
                 patch('coordinator.client.shutil.disk_usage',return_value=type('Disk',(),{'free':100*1024**3})()),\
                 patch('coordinator.worker_status.collect_cli_status',return_value=sample()) as collect:
                client.pulse()
            self.assertEqual('/api/worker/pulse',sent[0][0])
            self.assertEqual(worker in MINI_VM_WORKERS,'cli_status' in sent[0][1])
            self.assertEqual(worker in MINI_VM_WORKERS,collect.called)


if __name__=='__main__':unittest.main()
