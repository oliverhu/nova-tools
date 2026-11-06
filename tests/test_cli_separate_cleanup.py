import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from test_cli_approved_wait_settlement import fixture
from coordinator import cli_dispatch as a


def setup(root):
    args = fixture(); run, task, finish, note, worker, report, _, readback = args
    p = root/run['run_id']/'stopped-cleanup.json';p.parent.mkdir()
    description = 'Owned commands stopped; immutable evidence preserved'
    report['cleanup'] = description;report['evidence_paths'] = [str(p)]
    for x in (task['result'], finish['result'], readback['result']):
        x['cleanup'] = description;x['evidence_paths'] = [str(p)]
    for x in (task, readback):x['receipts'][-1]['receipt']['receipt'] = copy.deepcopy(task['result'])
    proof = {'task': task['id'], 'generation': 1, 'worker': worker, 'cleanup': description,
             'execution_stopped': True, 'external_state_checked': True, 'allOwnedCommandsReaped': True,
             'ownedChildrenClean': True, 'inputDispatched': False}
    a.save(p, proof)
    return args, p, proof


class SeparateCleanupTests(unittest.TestCase):
    def test_real_file_identity_positive_and_replacement_refused(self):
        with tempfile.TemporaryDirectory() as d, patch.object(a, 'ROOT', Path(d).resolve()):
            args,p,proof = setup(a.ROOT); snapshot = a.validate_settlement(*args)
            self.assertEqual(p,snapshot[0]);self.assertEqual(json.loads(snapshot[1]),proof)
            replacement = p.with_suffix('.new');replacement.write_bytes(p.read_bytes());replacement.replace(p)
            run,task,finish,note,worker,report,_,readback = args
            for n,x in [('state.json',run),('finish-response.json',finish),('worker-report.json',report),('task-readback.json',readback)]:a.save(p.parent/n,x)
            with self.assertRaises(ValueError):a.settle_approved_wait(p.parent,run,note,finish,report,readback,cleanup_proof=snapshot)
            self.assertFalse((p.parent/'dispatch-settlement.json').exists())

    def test_bad_identity_unknowns_or_contradictions_refuse(self):
        for delta in ({'task':'foreign'},{'generation':2},{'worker':'other'},{'execution_stopped':False},
                      {'external_state_checked':False},{'inputDispatched':True},{'allOwnedCommandsReaped':False},
                      {'cleanup':'different'},{'unknown_operations':['input']},{'unsettled_operations':['server']},
                      {'human_stop':True}):
            with tempfile.TemporaryDirectory() as d, patch.object(a,'ROOT',Path(d).resolve()):
                args,p,proof=setup(a.ROOT);a.save(p,{**proof,**delta})
                with self.subTest(delta=delta),self.assertRaises(ValueError):a.validate_settlement(*args)

    def test_missing_reference_conflicting_description_and_symlink_refuse(self):
        for case in ('reference','description','symlink','changed-bytes'):
            with tempfile.TemporaryDirectory() as d,patch.object(a,'ROOT',Path(d).resolve()):
                args,p,proof=setup(a.ROOT)
                if case=='reference':args[5]['evidence_paths']=[]
                elif case=='description':args[5]['cleanup']='different'
                elif case=='symlink':
                    q=p.with_suffix('.old');p.rename(q);p.symlink_to(q)
                else:a.save(p,{**proof,'inputDispatched':True})
                with self.subTest(case=case),self.assertRaises(ValueError):a.validate_settlement(*args)
