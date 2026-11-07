"""Read-only exact-owner closure proof for failed local admission, never runtime success."""
import hashlib
import json
from pathlib import Path


def validate(root, run, task, report, note, readback, reader):
    refs = note.get('owner_review')
    if (not isinstance(refs, dict) or set(refs) != {'review_path', 'review_sha256', 'interpretation_path', 'interpretation_sha256'}
            or not isinstance(readback, dict) or run.get('completion_verified') is not False
            or run.get('queued_generation') != task['generation']-1 or run.get('cli_exit_code') != 0):
        raise ValueError('Explicit exact owner-review references and original ended run required')
    snapshots = {}
    def load(path, digest=None):
        path = Path(path)
        raw, identity = reader(path)
        if digest is not None and hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError('Owner-reviewed evidence hash changed')
        snapshots[path] = (raw, identity)
        return json.loads(raw)
    review = load(refs['review_path'], refs['review_sha256'])
    interpretation = load(refs['interpretation_path'], refs['interpretation_sha256'])
    for value in (review, interpretation):
        if (value.get('taskID') != task['id'] or type(value.get('generation')) is not int or value['generation'] != task['generation']
                or value.get('dispatchRunID') != run['run_id']
                or value.get('resourceReservationID') != 'own-claim-'+task['id']+'-generation-'+str(task['generation'])):
            raise ValueError('Owner review must bind the exact task generation and run')
    flags = {'resourceReleaseAuthorized': False, 'featureCleanupState': 'unknown', 'uiEvidenceState': 'unknown'}
    if (review.get('kind') != 'exact-run-resource-ui-review' or review.get('recordBindingsVerified') is not True
            or review.get('allRecordedEvidenceHashesMatch') is not True
            or review.get('executorOriginalFlags') != flags or interpretation.get('rawFieldsMustRemainUnchanged') != flags
            or interpretation.get('reviewRef', {}).get('sha256') != refs['review_sha256']
            or interpretation.get('localWorkerAdmissionMayBeSettled') is not True
            or interpretation.get('separatePositiveDesktopReleaseRequired') is not False
            or interpretation.get('runtimeActionPerformed') is not False
            or interpretation.get('sourceOrQueueMutationPerformed') is not False):
        raise ValueError('Positive owner interpretation with unchanged conservative flags required')
    ui = review.get('uiEvidence', {})
    if (ui.get('classification') != 'not-reached-before-input-or-capture' or ui.get('featureAcceptance') is not False
            or ui.get('fixtureReadiness') != 'failed-original-35-second-bound'):
        raise ValueError('Original failed readiness and source-bound no-input review required')
    directory = root/run['run_id']
    stop = load(directory/'stopped-cleanup.json')
    for k, expected in [('task', task['id']), ('generation', task['generation']), ('worker', run['worker']),
                        ('execution_stopped', True), ('external_state_checked', True)]:
        if stop.get(k) != expected:
            raise ValueError('Exact same-run stopped/checked cleanup required')
    if (stop.get('executor_conservative_status_preserved') != flags
            or stop.get('facts') != report.get('cleanup')
            or not isinstance(report.get('evidence_paths'), list)
            or report['evidence_paths'].count(str(directory/'stopped-cleanup.json')) != 1):
        raise ValueError('Original owner report must bind the separate stopped facts')
    hashes = review.get('evidenceSHA256', {})
    def reviewed(path):
        if str(path) not in hashes:
            raise ValueError('Evidence is absent from the original owner review')
        return load(path, hashes[str(path)])
    failure = reviewed(directory/'runtime-failure-and-cleanup.json')
    observation = reviewed(directory/'runtime-observation.json')
    if (stop.get('evidence') != str(directory/'runtime-failure-and-cleanup.json')
            or failure.get('cleanup') != stop['facts'] or failure.get('executor_conservative_status') != flags
            or failure.get('acceptance_complete') is not False
            or failure.get('error', {}).get('message') != 'Fixture readiness deadline exceeded'
            or any(failure.get('actual_results', {}).get(k) is not False
                   for k in ('fixture_ready', 'native_input_dispatched', 'capture_performed', 'renderer_helper_qualification'))):
        raise ValueError('Unchanged failed invocation and no input/capture evidence required')
    facts = stop['facts']
    for k in ('native_process_reaped', 'server_stopped', 'final_census_clean', 'controller_reaped',
              'host_driver_absent', 'watchdog_returned', 'original_deadlines_preserved', 'no_cleanup_signals'):
        if facts.get(k) is not True:
            raise ValueError('Positive exact process closure required')
    for k in ('opaque_processes', 'owned_remaining', 'controller_group_remaining'):
        if facts.get(k) != []:
            raise ValueError('Other unknown or remaining effects prevent settlement')
    native = facts.get('native_exit', {})
    if (native.get('engineExited') is not True or native.get('engineExitCode') != 0
            or native.get('featureAcceptance') is not False):
        raise ValueError('Original native exit must prove closure without acceptance')
    for value in facts.get('host_driver_kernel_exits', {}).values():
        if value.get('normalExit') is not True or value.get('exitCode') != 78 or value.get('signal') is not None:
            raise ValueError('Original normal host/driver failure exits required')
    closure = review.get('positiveClosure', {})
    for k in ('exactNativeGenerationAbsentInFinalCensus', 'hostAndDriverAbsent', 'metadataSettlementVerified',
              'nativeAppAndEngineNormalExit', 'nativeReaped', 'observerNormallyReaped', 'ownedControllerGroupReaped', 'serverStopped'):
        if closure.get(k) is not True:
            raise ValueError('Independent owner positive closure required')
    if closure.get('hostAndDriverNormalExit78') != facts.get('host_driver_kernel_exits'):
        raise ValueError('Original host/driver exits must match owner review')
    session = Path(observation.get('session', ''))
    if session.name != review.get('runtimeSessionID') or session.parent.name != 'runtime-sessions':
        raise ValueError('Exact reviewed runtime session required')
    def session_file(name):
        return reviewed(session/'prepared/run'/name)
    outcome = session_file('outcome.json'); qualification = session_file('qualification-result.json')
    consumed = session_file('consumption.json'); joined = session_file('historical-join.json')
    session_file('final-census.json'); session_file('observer-return.json')
    reviewed(session/'executor-return.json'); reviewed(session/'host-admission.json')
    for name, expected in [('controller.py', review.get('sourceControllerSHA256')),
                           ('ui_test_runner_executor.py', review.get('sourceExecutorSHA256'))]:
        paths = [p for p, sha in hashes.items() if Path(p).name == name and sha == expected]
        if len(paths) != 1:
            raise ValueError('Exact reviewed controller/executor source required')
        raw, identity = reader(Path(paths[0]));
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError('Reviewed source bytes changed')
        snapshots[Path(paths[0])] = (raw, identity)
    nonce = review.get('runNonce')
    if (outcome.get('runNonce') != nonce or facts.get('native_exit', {}).get('nonce') != nonce
            or any(outcome.get(k) != value for k, value in flags.items())
            or outcome.get('remainingControllerGroup') != [] or outcome.get('controllerExitedAndReaped') is not True
            or qualification.get('nativeExitReceipt') != facts['native_exit']
            or qualification.get('acceptanceComplete') is not False or qualification.get('featureAcceptance') is not False
            or qualification.get('taskID') != task['id'] or qualification.get('generation') != task['generation']
            or qualification.get('worker') != run['worker'] or qualification.get('executionStopped') is not True
            or qualification.get('nativeProcessReaped') is not True or qualification.get('serverStopped') is not True
            or consumed.get('target', {}).get('pid') != native.get('pid')
            or joined.get('currentResponsibilityClaimed') is not False or joined.get('historicalCapturedIntervalOnly') is not True
            or consumed.get('claims', {}).get('resourceReleaseAuthorized') is not False
            or joined.get('claims', {}).get('resourceReleaseAuthorized') is not False
            or any(v is not False for item in (consumed, joined) for v in item.get('claims', {}).values())):
        raise ValueError('Consumed nonce, historical binding and conservative outcome must remain unchanged')
    for value in (stop, facts, failure):
        if value.get('unknown_operations', []) != [] or value.get('unsettled_operations', []) != [] or value.get('human_stop') is True:
            raise ValueError('Other unknowns or real holds cannot be resolved by owner review')
    keys = ('id', 'generation', 'step', 'status', 'owner', 'last_owner', 'requirements', 'dependencies', 'result',
            'receipts', 'events', 'approval', 'fix_attempts_used', 'total_fix_attempts', 'attempts_host', 'attempt_base',
            'reassignment_count', 'attempt_exception', 'attempt_allowance')
    if any(k not in readback or readback[k] != task.get(k) for k in keys):
        raise ValueError('Original wait history and fresh API must match without subsequent claims')
    return {'closure': stop, 'snapshots': snapshots, 'flags_preserved': flags}
