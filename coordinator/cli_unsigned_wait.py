"""Bind a compact original owner report to full stopped unsigned-preparation evidence.

This only settles local admission. It cannot authorize a rejected command, change
service ownership, or prepare/sign/install/launch anything.
"""
import json

SCOPE = ('Only fresh separate guarded unsigned test app preparation under normal review; '
         'no signing, installation, launch, original replacement or rejected command retry.')


def validate(root, run, task, finish, report, readback, note, worker, read_proof):
    evidence = (task.get('approval') or {}).get('evidence')
    if (not isinstance(evidence, dict) or note['unsigned_preparation_approval'] != evidence
            or evidence.get('scope') != SCOPE or evidence.get('response') != 'approve'):
        raise ValueError('Exact authenticated unsigned-only approval binding required')
    directory = root/run['run_id']
    values = {'state.json': run, 'finish-response.json': finish,
              'worker-report.json': report, 'task-readback.json': readback,
              'finish-receipt.json': task['result']}
    snapshots = {}
    for name in (*values, 'stopped-cleanup.json', 'approval-rejection.json'):
        path = directory/name
        raw, identity = read_proof(path)
        value = json.loads(raw)
        if name in values and value != values[name]:
            raise ValueError('Original unsigned-wait files must match exact run and API result')
        snapshots[path] = (raw, identity)
    proof = json.loads(snapshots[directory/'stopped-cleanup.json'][0])
    rejection = json.loads(snapshots[directory/'approval-rejection.json'][0])
    result = task['result']; compact = report.get('cleanup')
    boundary = result.get('approval_boundary')
    if (not isinstance(proof, dict) or proof != result.get('cleanup')
            or not isinstance(compact, dict)
            or set(compact) != {'execution_stopped', 'owned_tests_reaped',
                                'matching_host_and_inert_processes_observed',
                                'original_host_pins_and_signature_unchanged', 'source_and_all_history_retained'}
            or any(compact.get(k) is not True for k in ('execution_stopped', 'owned_tests_reaped',
                   'original_host_pins_and_signature_unchanged', 'source_and_all_history_retained'))
            or type(compact.get('matching_host_and_inert_processes_observed')) is not int
            or compact['matching_host_and_inert_processes_observed'] != 0
            or any(proof.get(k) is not True for k in ('execution_stopped', 'external_state_checked',
                   'all_owned_test_invocations_reaped', 'no_matching_processes_observed', 'original_pins_unchanged'))
            or type(proof.get('original_strict_signature_exit')) is not int
            or proof['original_strict_signature_exit'] != 0
            or proof.get('exact_host_and_inert_census_rows') != []
            or any(proof.get(k) is not False for k in ('signing_performed', 'active_bundle_changed',
                   'actual_permission_probe_performed', 'CEF_or_server_or_input_started', 'staging_command_executed'))
            or not isinstance(proof.get('original_hashes'), dict) or not proof['original_hashes']
            or any(not isinstance(v, str) or len(v) != 64 or any(c not in '0123456789abcdef' for c in v)
                   for v in proof['original_hashes'].values())
            or not isinstance(boundary, dict) or boundary.get('kind') != 'automatic_review_rejection'
            or boundary.get('executed') is not False or not boundary.get('reason')
            or not isinstance(rejection, dict) or rejection.get('executed') is not False
            or rejection.get('acceptance_complete') is not False or not rejection.get('reason')
            or rejection.get('action') != 'prepare-entry.py through claim watchdog'
            or rejection.get('binding') != 'Do not bypass rejection; no signing, update, installation or exact-context probe attempted'
            or report.get('source') != {k: result.get('source', {}).get(k) for k in ('checkout', 'branch', 'base', 'commits')} | {
                'state': 'three source files staged, no integration'}
            or result.get('source', {}).get('integrated', False) is not False
            or result.get('publication_performed') is not False or report.get('publication_performed') is not False
            or report.get('next_task_claimed') is not False
            or proof.get('source_checkout_preserved') != result.get('source', {}).get('checkout')
            or any(not isinstance(x.get('summary'), str) or not x['summary'].strip() for x in (result, report))):
        raise ValueError('Full original stopped/reaped/no-effects cleanup and compact owner report required')
    for value in (proof, rejection, boundary):
        if (value.get('human_stop') is True or value.get('unknown_operations', []) != []
                or value.get('unsettled_operations', []) != []):
            raise ValueError('Original rejection or cleanup holds cannot be cleared')
    references = report.get('evidence_paths')
    if (not isinstance(references, dict)
            or any(references.get(key) != str(directory/name) for key, name in (
                ('stopped-cleanup', 'stopped-cleanup.json'), ('approval-rejection', 'approval-rejection.json'),
                ('finish-receipt', 'finish-receipt.json'), ('finish-response', 'finish-response.json'),
                ('task-readback', 'task-readback.json')))
            or not isinstance(result.get('evidence_paths'), list)
            or any(result['evidence_paths'].count(str(directory/name)) != 1
                   for name in ('stopped-cleanup.json', 'approval-rejection.json'))):
        raise ValueError('Original report and finish must reference same-run cleanup and rejection')
    return {'snapshots': snapshots}
