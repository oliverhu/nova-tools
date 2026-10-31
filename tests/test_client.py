import sys
import time
import unittest
from coordinator.client import supervised,publish_permit


class HeartbeatClient:
    def __init__(self,fail_after=999):self.calls=0;self.fail_after=fail_after
    def heartbeat(self,ident,progress=None):
        self.calls+=1
        if self.calls>self.fail_after:raise RuntimeError('offline')


class ClientTests(unittest.TestCase):
    def test_supervisor_returns_command_exit_status(self):
        self.assertEqual(7,supervised(HeartbeatClient(),'test',[sys.executable,'-c','raise SystemExit(7)']))

    def test_heartbeat_failure_terminates_owned_process(self):
        client=HeartbeatClient(fail_after=1)
        started=time.monotonic()
        with self.assertRaisesRegex(RuntimeError,'Lost coordination'):
            supervised(client,'test',[sys.executable,'-c','import time; time.sleep(30)'],heartbeat_interval=.05)
        self.assertLess(time.monotonic()-started,3)

    def test_initial_lease_failure_prevents_start(self):
        with self.assertRaisesRegex(RuntimeError,'offline'):
            supervised(HeartbeatClient(fail_after=0),'test',['/does-not-exist'])

    def test_normal_work_cannot_obtain_publish_permit(self):
        class Client:
            config={'worker':'oliver-mac'}
            def claim_body(self,ident):return {'generation':1}
            def request(self,path):
                return {'status':'running','owner':'oliver-mac','generation':1,'current_step':{'kind':'work'}} if '/tasks/' in path else {'locks':[]}
        with self.assertRaisesRegex(RuntimeError,'exclusive publish'):
            publish_permit(Client(),'x')


if __name__=='__main__':unittest.main()
