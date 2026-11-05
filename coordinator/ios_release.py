"""Explicit, source-pinned iOS upload claims without changing Mac release state."""
import hashlib
import json
import uuid


SCOPE = 'ios_upload_only'
TARGET = 'ios_app_store_connect_upload_only'
NOVA = 'oliverhu/nova-browser'
STUDIO = 'oliverhu-mac-studio'
POLICY = (
    'Archive, sign, validate and upload only the pinned iOS source through xcodebuild '
    'using the existing signed-in Xcode account. Preserve the immutable version/build, '
    'archive and upload journal; reconcile any uncertain upload before retrying. '
    'No Mac build, version bump, website deployment or Mac publication. '
    'No TestFlight distribution, testers/groups/settings changes, App Review or public iOS release. '
    'No routine App Store Connect browser/build-list or account sign-in preflights. '
    'Record actual Apple acceptance in ios_upload: status=uploaded, exact source_commit, '
    'version, build_number, upload_ref, testflight_distributed=false, '
    'submitted_for_review=false, testflight_settings_changed=false, ios_public_release=false. '
    'Finish only after owned execution settles and external state is checked. '
    'Set publication_ref to the verified upload reference; omit published_commit and release_commit. '
    'An upload does not establish Apple processing, tester availability or physical-device audio acceptance.'
)


def start(store, body):
    # Imported here to keep Store's existing exception/serialization contract.
    from .store import Conflict, Invalid, bounded, encoded
    if body.get('scope') != SCOPE or body.get('repository') != NOVA:
        raise Invalid('iOS-only scope requires oliverhu/nova-browser')
    commit = store.commit(body.get('commit'))
    dependencies = body.get('dependencies')
    if (not isinstance(dependencies, list) or not 1 <= len(dependencies) <= 50
            or not all(isinstance(x, str) for x in dependencies)
            or len(set(dependencies)) != len(dependencies)):
        raise Invalid('Provide distinct existing prerequisite task IDs')
    authorization = body.get('authorization')
    if not isinstance(authorization, dict) or set(authorization) != {'thread_id', 'message_id'}:
        raise Invalid('Bind the explicit human upload authorization source')
    authorization = {key: bounded(authorization[key], key, 200) for key in ('thread_id', 'message_id')}
    canonical = {'repository': NOVA, 'scope': SCOPE, 'commit': commit,
                 'dependencies': sorted(dependencies), 'authorization': authorization}
    digest = hashlib.sha256(encoded(canonical).encode()).hexdigest()
    key = 'ios-upload-only:' + NOVA + ':' + commit
    with store.transaction() as db:
        old = db.execute('SELECT * FROM tasks WHERE request_key=?', (key,)).fetchone()
        if old:
            if old['request_hash'] != digest:
                raise Conflict('This exact iOS target already has different immutable request bindings')
            return {'created_ids': [], 'task_ids': [old['id']], 'replayed': True}
        repo = db.execute('SELECT * FROM repositories WHERE name=?', (NOVA,)).fetchone()
        if not repo or repo['observed_at'] < store.clock() - 600 or repo['merged_head'] != commit:
            raise Conflict('A fresh remote-main observation must match the exact iOS target')
        for dep in dependencies:
            if not db.execute('SELECT id FROM tasks WHERE id=?', (dep,)).fetchone():
                raise Invalid('Dependency does not exist: ' + dep)
        for row in db.execute("SELECT * FROM tasks WHERE task_type='publish'"):
            result = json.loads(row['result'])
            target = result.get('release_target', {})
            if target.get('repository') != NOVA or target.get('commit') != commit:
                continue
            upload = result.get('ios_upload', {})
            if (row['status'] in ('running', 'held', 'pending_external_approval')
                    or upload.get('status') in ('uploading', 'started', 'needs-review', 'unknown')):
                raise Conflict('Reconcile the existing exact-target release claim/hold/upload first')
        now = store.clock(); ident = uuid.uuid4().hex[:16]
        target = {'repository': NOVA, 'branch': repo['branch'], 'commit': commit}
        result = {'release_target': target, 'release_scope': SCOPE,
                  'release_authorization': authorization}
        title = 'Upload iOS ' + NOVA + ' at ' + commit[:12]
        steps = [{'name': 'Validate and upload pinned iOS build', 'kind': 'publish', 'instructions': POLICY}]
        db.execute('''INSERT INTO tasks(id,request_key,request_hash,title,description,created,updated,
            submitted_by,priority,compute,requirements,dependencies,steps,result,task_type)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (ident, key, digest, title, POLICY, now, now, 'admin', 90, 'standard',
             encoded({'worker': STUDIO}), encoded(sorted(dependencies)), encoded(steps), encoded(result), 'publish'))
        store.event(db, 'admin', 'submitted', ident, encoded({**canonical, 'title': title}))
        return {'created_ids': [ident], 'task_ids': [ident], 'replayed': False}


def validate_upload(receipt, target):
    from .store import Invalid, bounded
    upload = receipt.get('ios_upload')
    if (receipt.get('external_state_checked') is not True
            or not isinstance(upload, dict) or upload.get('status') != 'uploaded'
            or upload.get('source_commit') != target['commit']
            or any(upload.get(key) is not False for key in (
                'testflight_distributed', 'submitted_for_review', 'testflight_settings_changed', 'ios_public_release'))):
        raise Invalid('Verified exact-source iOS upload-only acceptance and checked external state are required')
    for field in ('version', 'build_number', 'upload_ref'):
        bounded(upload.get(field), 'ios_upload.' + field, 2000)
    if 'published_commit' in receipt or 'release_commit' in receipt:
        raise Invalid('iOS-only completion must not claim a Mac publication or release metadata commit')
