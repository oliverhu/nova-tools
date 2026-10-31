import concurrent.futures
import tempfile
import unittest
from pathlib import Path
from coordinator.store import Store, Conflict, Invalid


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.now=1000.0
        self.store=Store(Path(self.temp.name)/"state.db",clock=lambda:self.now,lease_seconds=60)
        self.pulse()

    def tearDown(self):self.temp.cleanup()

    def pulse(self):
        for worker,memory,cpus in [("oliver-mini",24,12),("oliver-mac",16,8)]:
            self.store.pulse(worker,{"capacity":{"memory_gb":memory,"cpus":cpus,"disk_gb":500,"capabilities":["macos"]}})

    def add(self,key="task",kind="work",**kwargs):
        return self.store.submit("oliver-mac",{"request_key":key,"title":key,"steps":[{"name":"Work","kind":kind,"instructions":"Perform actual work and record evidence"}],**kwargs})

    def finish(self,claim,**kwargs):
        t=claim["task"]
        return self.store.finish(t["owner"],t["id"],{"claim_token":claim["claim_token"],"generation":t["generation"],"step":t["step"],"receipt":{"summary":"Verified actual result","execution_stopped":True,"publication_ref":"release:test"},**kwargs})

    def test_concurrent_claims_exactly_one_winner(self):
        task=self.add()
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            results=list(pool.map(lambda n:self.store.claim(["oliver-mac","oliver-mini"][n%2]),range(32)))
        self.assertEqual(1,sum(r["task"] is not None for r in results))
        self.assertEqual(task["id"],next(r["task"]["id"] for r in results if r["task"]))

    def test_both_workers_pull_independent_work(self):
        self.add("one");self.add("two")
        self.assertIsNotNone(self.store.claim("oliver-mini")["task"])
        self.assertIsNotNone(self.store.claim("oliver-mac")["task"])
        self.assertIsNone(self.store.claim("oliver-mini")["task"])

    def test_heavy_prefers_mini_and_waits_while_mini_busy(self):
        self.add("heavy",compute="heavy")
        self.assertIsNone(self.store.claim("oliver-mac")["task"])
        claim=self.store.claim("oliver-mini")
        self.add("heavy2",compute="heavy")
        self.assertIsNone(self.store.claim("oliver-mac")["task"])
        self.finish(claim)
        self.assertIsNotNone(self.store.claim("oliver-mini")["task"])

    def test_heavy_falls_back_only_when_mini_offline(self):
        self.add(compute="heavy")
        self.now+=91
        self.store.pulse("oliver-mac",{"capacity":{"memory_gb":16,"disk_gb":500,"cpus":8}})
        self.assertIsNotNone(self.store.claim("oliver-mac")["task"])

    def test_requirements_cannot_be_overridden_by_claim(self):
        self.add(requirements={"memory_gb":20,"capabilities":["macos"]})
        self.assertIsNone(self.store.claim("oliver-mac")["task"])
        self.assertIsNotNone(self.store.claim("oliver-mini")["task"])

    def test_publish_global_lock_across_workers_and_tasks(self):
        self.add("publish1","publish");self.add("publish2","publish")
        c=self.store.claim("oliver-mac")
        self.assertEqual(["publish"],c["resources"])
        self.assertIsNone(self.store.claim("oliver-mini")["task"])
        self.finish(c)
        self.assertIsNotNone(self.store.claim("oliver-mini")["task"])

    def test_publish_expiry_holds_lock_across_restart(self):
        self.add("publish1","publish");self.add("publish2","publish")
        claim=self.store.claim("oliver-mac")
        self.now+=61
        self.store=Store(self.store.path,clock=lambda:self.now,lease_seconds=60)
        state=self.store.snapshot()
        self.assertEqual("held",next(t["status"] for t in state["tasks"] if t["id"]==claim["task"]["id"]))
        self.assertEqual(1,len(state["locks"]))
        self.assertIsNone(self.store.claim("oliver-mini")["task"])
        with self.assertRaises(Conflict):self.finish(claim)

    def test_failed_publish_is_held_even_if_local_process_stopped(self):
        self.add(kind="publish")
        claim=self.store.claim("oliver-mini")
        result=self.finish(claim,outcome="failed")
        self.assertEqual("held",result["status"])
        self.assertEqual(1,len(self.store.snapshot()["locks"]))

    def test_publish_requires_external_reference_and_rolls_back(self):
        self.add(kind="publish")
        claim=self.store.claim("oliver-mini")
        with self.assertRaises(Invalid):
            self.finish(claim,receipt={"summary":"Done","execution_stopped":True})
        self.assertEqual(1,len(self.store.snapshot()["locks"]))
        self.assertEqual("running",self.store.detail(claim["task"]["id"])["status"])

    def test_fencing_old_owner_after_explicit_reconciliation(self):
        self.add()
        old=self.store.claim("oliver-mac")
        self.now+=61
        self.store.snapshot()
        self.store.reconcile("admin",old["task"]["id"],{"action":"retry","evidence":"Verified former process stopped and no external effect","execution_stopped":True,"external_state_checked":True})
        new=self.store.claim("oliver-mini")
        self.assertEqual(2,new["task"]["generation"])
        with self.assertRaises(Conflict):self.finish(old)
        self.finish(new)

    def test_expired_work_never_silently_reassigned(self):
        self.add()
        self.store.claim("oliver-mac")
        self.now+=61
        self.assertIsNone(self.store.claim("oliver-mini")["task"])
        self.assertEqual("held",self.store.snapshot()["tasks"][0]["status"])

    def test_submission_retry_is_idempotent(self):
        self.assertEqual(self.add()["id"],self.add()["id"])
        with self.assertRaises(Conflict):self.add(compute="heavy")

    def test_finish_retry_is_idempotent(self):
        self.add()
        c=self.store.claim("oliver-mac")
        a=self.finish(c);b=self.finish(c)
        self.assertEqual(a,b)
        self.assertEqual(1,len(self.store.detail(a["id"])["receipts"]))

    def test_dependency_and_multi_step_order(self):
        first=self.add("first")
        self.add("second",dependencies=[first["id"]],steps=[{"name":"Plan","instructions":"Plan"},{"name":"Ship","instructions":"Ship","kind":"publish"}])
        c=self.store.claim("oliver-mac")
        self.assertEqual(first["id"],c["task"]["id"])
        self.assertIsNone(self.store.claim("oliver-mini")["task"])
        self.finish(c)
        plan=self.store.claim("oliver-mini")
        self.assertEqual("Plan",plan["task"]["current_step"]["name"])
        self.finish(plan)
        ship=self.store.claim("oliver-mac")
        self.assertEqual("Ship",ship["task"]["current_step"]["name"])
        self.assertEqual(["publish"],ship["resources"])
        self.assertEqual("completed",self.finish(ship)["status"])

    def test_foreign_worker_cannot_heartbeat(self):
        t=self.add();c=self.store.claim("oliver-mac")
        with self.assertRaises(Conflict):
            self.store.heartbeat("oliver-mini",t["id"],{"claim_token":c["claim_token"],"generation":1})

    def test_snapshot_does_not_expose_claim_token_or_hash(self):
        self.add();c=self.store.claim("oliver-mac")
        self.assertNotIn("claim_hash",str(self.store.snapshot()))
        self.assertNotIn(c["claim_token"],str(self.store.snapshot()))

    def test_invalid_input_and_unknown_dependency(self):
        for kwargs in ({"compute":"other"},{"priority":-1},{"requirements":{"memory_gb":-1}},{"dependencies":["missing"]}):
            with self.assertRaises(Invalid):self.add(**kwargs)


if __name__=="__main__":unittest.main()
