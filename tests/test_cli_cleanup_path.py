import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from test_cli_approved_wait_settlement import fixture
from coordinator import cli_dispatch as a


def setup(root):
    args=fixture();run,task,finish,note,worker,report,_,readback=args
    path=root/run['run_id']/'stopped-cleanup.json';path.parent.mkdir()
    report['cleanup'].update(task=task['id'],generation=1,worker=worker)
    report['evidence_paths']=[str(path)]
    for r in (task['result'],finish['result'],readback['result']):r['cleanup']=str(path)
    for t in (task,readback):t['receipts'][-1]['receipt']['receipt']=copy.deepcopy(task['result'])
    a.save(path,report['cleanup'])
    return args,path


class CleanupPathTests(unittest.TestCase):
    def test_path_and_equal_report_proof_preserve_inputs(self):
        with tempfile.TemporaryDirectory() as d,patch.object(a,'ROOT',Path(d).resolve()):
            args,p=setup(a.ROOT);before=copy.deepcopy(args);proof=a.validate_settlement(*args)
            self.assertEqual(p,proof[0]);self.assertEqual(before,args)
            self.assertEqual(args[5]['cleanup'],json.loads(proof[1]))

    def test_reference_identity_contradictions_unknowns_and_symlink_refuse(self):
        for case in ('path','reference','dict','task','generation','worker','stopped','checked','unknown','human-stop','symlink'):
            with tempfile.TemporaryDirectory() as d,patch.object(a,'ROOT',Path(d).resolve()):
                args,p=setup(a.ROOT)
                if case=='path':args[1]['result']['cleanup']=str(p.parent/'other.json')
                elif case=='reference':args[5]['evidence_paths']=[]
                elif case=='dict':a.save(p,{**args[5]['cleanup'],'changed':True})
                elif case=='symlink':
                    q=p.with_suffix('.old');p.rename(q);p.symlink_to(q)
                else:
                    k,v={'task':('task','foreign'),'generation':('generation',2),'worker':('worker','foreign'),
                         'stopped':('execution_stopped',False),'checked':('external_state_checked',False),
                         'unknown':('unknown_operations',['input']),'human-stop':('human_stop',True)}[case]
                    args[5]['cleanup'][k]=v;a.save(p,args[5]['cleanup'])
                with self.subTest(case=case),self.assertRaises(ValueError):a.validate_settlement(*args)

    def test_replacement_is_rejected_before_local_audit(self):
        with tempfile.TemporaryDirectory() as d,patch.object(a,'ROOT',Path(d).resolve()):
            args,p=setup(a.ROOT);snapshot=a.validate_settlement(*args)
            run,task,finish,note,worker,report,_,readback=args
            for name,value in [('state.json',run),('finish-response.json',finish),('worker-report.json',report),('task-readback.json',readback)]:a.save(p.parent/name,value)
            replacement=p.with_suffix('.new');replacement.write_bytes(p.read_bytes());replacement.replace(p)
            with self.assertRaises(ValueError):a.settle_approved_wait(p.parent,run,note,finish,report,readback,cleanup_proof=snapshot)
            self.assertFalse((p.parent/'dispatch-settlement.json').exists())
