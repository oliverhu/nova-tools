"""Read-only exact-owner closure proof for failed local admission, never runtime success."""
import hashlib
import json
from pathlib import Path


def validate(root, run, task, report, note, readback, reader):
    refs = note.get('owner_review')
    if (not isinstance(refs, dict) or set(refs) not in ({'review_path', 'review_sha256'}, {'review_path', 'review_sha256', 'interpretation_path', 'interpretation_sha256'})
            or not isinstance(readback, dict) or run.get('completion_verified') is not False
            or run.get('queued_generation') != task['generation']-1 or run.get('cli_exit_code') != 0):
        raise ValueError('Explicit exact owner-review references and original ended run required')
    for value in (run, report, note, task.get('result', {})):
        if (value.get('human_stop') is True or value.get('unknown_operations', []) != []
                or value.get('unsettled_operations', []) != []
                or any(k in value and value[k] is not True for k in ('execution_stopped','external_state_checked','input_settled'))):
            raise ValueError('Independent holds or contradictory stop/input evidence must remain unresolved')
    if note.get('approval_request') or report.get('approval_request'):
        raise ValueError('A current approval request cannot be settled as an owner closure review')
    snapshots = {}
    def load(path, digest=None, parse=True):
        path = Path(path)
        raw, identity = reader(path)
        if digest is not None and hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError('Owner-reviewed evidence hash changed')
        snapshots[path] = (raw, identity)
        return json.loads(raw) if parse else raw
    review = load(refs['review_path'], refs['review_sha256'])
    interpretation = load(refs['interpretation_path'], refs['interpretation_sha256']) if 'interpretation_path' in refs else None
    for value in ([review, interpretation] if interpretation is not None else [review]):
        if (value.get('taskID') != task['id'] or type(value.get('generation')) is not int or value['generation'] != task['generation']
                or value.get('dispatchRunID') != run['run_id']
                or value.get('resourceReservationID') != 'own-claim-'+task['id']+'-generation-'+str(task['generation'])):
            raise ValueError('Owner review must bind the exact task generation and run')
    if review.get('uiEvidence', {}).get('classification') == 'read-and-window-capture-completed-selector-refused-before-native-input':
        if interpretation is not None:
            raise ValueError('Observed profile retains its interpretation in the one original review file')
        validate_observed(root, run, task, report, review, load)
        unchanged_wait(task, readback)
        return {'closure': json.loads(snapshots[root/run['run_id']/'stopped-cleanup.json'][0]),
                'snapshots': snapshots, 'flags_preserved': review['executorOriginalFlags']}
    if interpretation is None:
        raise ValueError('This owner review requires its separate original interpretation')
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
    unchanged_wait(task, readback)
    return {'closure': stop, 'snapshots': snapshots, 'flags_preserved': flags}


def unchanged_wait(task, readback):
    keys = ('id', 'generation', 'step', 'status', 'owner', 'last_owner', 'requirements', 'dependencies', 'result',
            'receipts', 'events', 'approval', 'fix_attempts_used', 'total_fix_attempts', 'attempts_host', 'attempt_base',
            'reassignment_count', 'attempt_exception', 'attempt_allowance')
    if any(k not in readback or readback[k] != task.get(k) for k in keys):
        raise ValueError('Original wait history and fresh API must match without subsequent claims')


def input_entries(directory):
    return {p.name for p in directory.iterdir()}


def validate_observed(root, run, task, report, review, load):
    """Two completed observations with capture, followed by refusal before native input."""
    flags = {'resourceReleaseAuthorized': False, 'featureCleanupState': 'unknown', 'uiEvidenceState': 'unknown'}
    if (review.get('kind') != 'exact-run-resource-ui-review' or review.get('recordBindingsVerified') is not True
            or review.get('recordedEvidenceHashesMatch') is not True or review.get('verificationCommandExitCode') != 0
            or review.get('executorOriginalFlags') != flags or review.get('localWorkerAdmissionMayBeSettled') is not True
            or review.get('separatePositiveDesktopLeaseReleaseRequired') is not False
            or not isinstance(review.get('interpretation'), str) or not review['interpretation'].strip()
            or review.get('sourceCommit') != report.get('source_commit_used')):
        raise ValueError('Exact independent observed-run interpretation and source required')
    ui = review['uiEvidence']; closure = review.get('positiveClosure', {})
    for k in ('nativeInputDispatched', 'clickIntentExists', 'navigationReceiverObserved', 'postNavigationPNGExists', 'featureAcceptance'):
        if ui.get(k) is not False:
            raise ValueError('Click, receiver, post-navigation capture or feature effects are not covered')
    for k in ('nativeAppAndEngineNormalExit0', 'nativeReaped', 'allNineHistoricalAppHelperGenerationsAbsent',
              'serverStopped', 'observerNormallyReaped', 'metadataSettlementVerified', 'ownedControllerGroupReaped',
              'hostDriverNormalExit78AndAbsent', 'executorAndOuterOpenReaped'):
        if closure.get(k) is not True:
            raise ValueError('All observed-run process closure must be positive')
    manifest = review.get('manifest', {}); prepared = Path(manifest.get('preparedRoot', ''))
    if prepared.name != 'prepared' or prepared.parent.name != review.get('runtimeSessionID') or prepared.parent.parent.name != 'runtime-sessions':
        raise ValueError('Exact reviewed session/prepared root required')
    core = {'spec.json', 'snapshot/.build/qualification/controller.py', 'snapshot/scripts/ui_test_runner_executor.py',
            'snapshot/scripts/peekaboo_bounded_click.py', 'run/qualification-result.json', 'run/owner-result.json',
            'run/consumption.json', 'run/historical-join.json', 'run/settlement.json', 'run/outcome.json',
            'run/final-census.json', 'run/native-readiness.json', 'run/input/binding.json', 'run/controller.stderr'}
    labels = ('navigate-before', 'navigate-window')
    required = core | {'run/input/'+label+'.'+suffix for label in labels for suffix in ('intent.json','process.json','result.json','stdout.json')}
    hashes = manifest.get('sha256', {})
    if set(hashes) != required:
        raise ValueError('Exactly the source-bound observed operation manifest is required')
    records = {rel: load(prepared/rel, sha, parse=rel.endswith('.json')) for rel, sha in hashes.items()}
    directory = root/run['run_id']; dispatch = manifest.get('dispatchReceiptSHA256', {})
    if set(dispatch) != {'runtime-failure-and-cleanup.json','stopped-cleanup.json','worker-report.json'}:
        raise ValueError('Original dispatch receipt hashes required')
    original = {name: load(directory/name, sha) for name, sha in dispatch.items()}
    stop = original['stopped-cleanup.json']; failure = original['runtime-failure-and-cleanup.json']
    if original['worker-report.json'] != report or stop != report.get('cleanup'):
        raise ValueError('Original stopped proof and owner report must agree exactly')
    if (not isinstance(report.get('evidence_paths'), list) or report['evidence_paths'].count(str(directory/'stopped-cleanup.json')) != 1
            or stop.get('task') != task['id'] or stop.get('generation') != task['generation'] or stop.get('worker') != run['worker']
            or stop.get('runtimeSession') != str(prepared.parent) or failure.get('session') != str(prepared.parent)
            or failure.get('cleanup') != str(directory/'stopped-cleanup.json') or failure.get('acceptance_complete') is not False):
        raise ValueError('Exact original closure identity/session/reference required')
    for k in ('execution_stopped','external_state_checked','nativeProcessReaped','serverStopped','finalExactNamespaceCensusClean',
              'normalCleanup','executorReaped','controllerExitedAndReaped','outerOpenReaped','initialObservationPixelsCaptured',
              'unknownFlagsRetained','consumedNonceNotReplayed'):
        if stop.get(k) is not True:
            raise ValueError('Observed invocation must be positively closed')
    for k in ('finalOwned','finalOpaque','signals','executorProcessGroupMembers','supervisorChildren'):
        if stop.get(k) != []:
            raise ValueError('Other operations or unknown processes prevent settlement')
    if (stop.get('nativeInputDispatched') is not False or stop.get('postNavigationCaptureProduced') is not False
            or stop.get('resourceReleaseAuthorized') is not False or stop.get('commonExecutorFeatureCleanupState') != 'unknown'
            or stop.get('hostAfter') is not None or stop.get('driverAfter') is not None):
        raise ValueError('Original no-click disposition and false/unknown flags must remain intact')
    native = stop.get('nativeExitReceipt', {}); nonce = review.get('runNonce')
    spec = records['spec.json']; outcome = records['run/outcome.json']; qualified = records['run/qualification-result.json']
    binding = records['run/input/binding.json']; identity = records['run/native-readiness.json'].get('nativeIdentity')
    if (spec.get('taskID') != task['id'] or spec.get('runNonce') != nonce or spec.get('runRoot') != str(prepared/'run')
            or native.get('nonce') != nonce or native.get('engineExited') is not True or native.get('engineExitCode') != 0
            or native.get('featureAcceptance') is not False or not isinstance(identity, dict) or identity.get('pid') != native.get('pid')
            or binding.get('identity') != identity or binding.get('windowID') != ui.get('exactOwnedWindowID')
            or outcome.get('runNonce') != nonce or any(outcome.get(k) != val for k, val in flags.items())
            or outcome.get('remainingControllerGroup') != [] or outcome.get('controllerExitedAndReaped') is not True
            or outcome.get('spec') != {'path': str(prepared/'spec.json'), 'sha256': hashes['spec.json']}
            or qualified.get('taskID') != task['id'] or qualified.get('generation') != task['generation'] or qualified.get('worker') != run['worker']
            or qualified.get('acceptanceComplete') is not False or qualified.get('featureAcceptance') is not False
            or qualified.get('executionStopped') is not True or qualified.get('nativeExitReceipt') != native
            or qualified.get('error') != failure.get('error') or failure.get('error', {}).get('message') != 'Unique enabled authored link unavailable'):
        raise ValueError('Nonce, original failure, target identity and incomplete acceptance must bind')
    for key, rel in [('controller','snapshot/.build/qualification/controller.py'),('executor','snapshot/scripts/ui_test_runner_executor.py')]:
        if spec.get(key) != {'path': str(prepared/rel), 'sha256': hashes[rel]}:
            raise ValueError('Original controller/executor source binding changed')
    for key in ('host','driver'):
        exit_record = stop.get('hostDriverKernelExits', {}).get(key, {})
        if exit_record.get('normalExit') is not True or exit_record.get('exitCode') != 78 or exit_record.get('signal') is not None:
            raise ValueError('Normal original host/driver closure required')
    for name in ('run/consumption.json','run/historical-join.json','run/settlement.json'):
        record = records[name]
        if record.get('claims', {}).get('resourceReleaseAuthorized') is not False or any(v is not False for v in record['claims'].values()):
            raise ValueError('Historical responsibility records cannot grant fresh authority')
    joined = records['run/historical-join.json']
    if joined.get('currentResponsibilityClaimed') is not False or joined.get('historicalCapturedIntervalOnly') is not True:
        raise ValueError('Historical-only responsibility binding required')
    ledger = closure.get('observationLedger')
    if not isinstance(ledger, list) or len(ledger) != 2:
        raise ValueError('Exactly two completed observations required')
    for label, operation, entry in zip(labels, ('see','window'), ledger):
        prefix = 'run/input/'+label+'.'
        intent, process, result, reply = [records[prefix+suffix] for suffix in ('intent.json','process.json','result.json','stdout.json')]
        command = intent.get('command', [])
        if (entry.get('label') != label or entry.get('operation') != operation or entry.get('accepted') is not True
                or entry.get('exit') != 0 or entry.get('waited') is not True or entry.get('phase') != 'observation'
                or entry.get('freshReadOnlyPrivateGroupMembers') != [] or entry.get('pid') != process.get('pid')
                or process.get('privateProcessGroup') != process.get('pid')
                or result.get('pid') != entry['pid'] or result.get('accepted') is not True or result.get('exit') != 0
                or result.get('waited') is not True or result.get('execution_stopped') is not True or result.get('error') is not None
                or result.get('phase') != 'observation' or result.get('operation') != operation or reply.get('success') is not True
                or len(command) < 2 or Path(command[0]).name != 'peekaboo' or command[1] != operation
                or (operation == 'window' and command[2:3] != ['list']) or '--no-remote' not in command
                or '--pid' not in command or command[command.index('--pid')+1] != str(native['pid'])):
            raise ValueError('Exact successful/reaped read-only operation ledger required')
        if operation == 'see' and ('--window-id' not in command or command[command.index('--window-id')+1] != str(binding['windowID'])):
            raise ValueError('Observation must remain bound to the original exact window')
    see = records['run/input/navigate-before.stdout.json']; data = see.get('data', {})
    window_data = records['run/input/navigate-window.stdout.json'].get('data', {})
    if (window_data.get('target_application_info', {}).get('pid') != native['pid']
            or not any(w.get('window_id') == binding['windowID'] for w in window_data.get('windows', []))):
        raise ValueError('Window inventory must retain the original scoped target')
    birth = identity.get('birth', [])
    if (len(birth) != 2 or see.get('target_receipt', {}).get('process_start_identity_decimal') != str(birth[0]*1000000+birth[1])
            or see['target_receipt'].get('pid') != native['pid'] or see['target_receipt'].get('window_id') != binding['windowID']
            or data.get('observation', {}).get('target', {}).get('window_id') != binding['windowID']
            or not any(s.get('name') == 'capture.window' and s.get('metadata', {}).get('engine') == 'CGWindowList'
                       for s in data.get('observation', {}).get('spans', []))
            or data.get('screenshot_raw') != '' or data.get('screenshot_annotated') != ''):
        raise ValueError('Exact original initial capture span, without invented PNG acceptance, required')
    matches = failure.get('matchingElements', [])
    translated = [{'id': e.get('id'), 'role': e.get('ax_role'), 'actionable': e.get('is_actionable'), 'enabled': e.get('is_enabled')} for e in matches]
    if (translated != ui.get('selectorMatches') or len(translated) != 2 or sum(e['actionable'] is True for e in translated) != 1):
        raise ValueError('Exact original ambiguous selector refusal required')
    expected = {'binding.json'} | {label+'.'+suffix for label in labels for suffix in ('intent.json','process.json','result.json','stdout.json')}
    actual = input_entries(prepared/'run/input')
    if not expected.issubset(actual) or actual-expected-{'navigate-before.stderr','navigate-window.stderr'}:
        raise ValueError('Unexpected click, HOLD, receiver, image or other input operation evidence')
    for name in actual-expected:
        if load(prepared/'run/input'/name, parse=False) != b'':
            raise ValueError('Unreviewed observation diagnostics cannot be treated as settled')
    for value in (stop, failure, review):
        if value.get('human_stop') is True or value.get('unknown_operations', []) != [] or value.get('unsettled_operations', []) != []:
            raise ValueError('Independent holds or other unknown effects prevent settlement')
