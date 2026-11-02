# Nova tools

Source for Nova engineering tools maintained outside
[`nova-browser`](https://github.com/oliverhu/nova-browser). This repository contains:

- `coordinator/` and `web/`: the task queue API, CLI, local gateway and dashboard.
- `deploy/` and `scripts/`: service deployment, packaging and client installation.
- `tests/`: coordinator regression and dashboard checks.
- `skill/SKILL.md`: the coordination workflow installed on both workers.
- `artifact-transfer/`: the restricted, read-only SSH artifact helper and setup notes.

The coordinator's original 18 source commits are preserved through `cc526e0`.
The artifact helper is copied from the deployed Mac helper without changing its
behavior. Browser implementation and browser-specific test tools remain in
`nova-browser`.

Only source and reusable documentation belong here. Keep live client configuration,
credentials, SSH keys, task databases, claims, approval journals, recordings,
logs, validation receipts and generated payloads outside Git. Pushing this
repository does not deploy the service or replace an installed client.

## Nova task queue

A private Kubernetes task queue for `oliver-mac` and `oliver-mini`. Both workers
claim untaken tasks atomically, implement and test the change, and merge it into
main. Ordinary work **does not publish**. There are no shared publishing,
integration, main, or desktop locks.

Only engine rebuilds are heavy. They prefer Mini; application builds, UI tests,
and ordinary features are standard work available to either qualified Mac.
Explicit memory, disk, CPU, capability and machine requirements still apply.

## Service and workers

- API/dashboard: `https://192.168.2.13:31866`
- Kubernetes namespace/deployment: `nova-coordinator`
- Optional local dashboard gateway: `http://127.0.0.1:18766`
- Client: `~/.local/bin/nova-coord`
- Installed instructions: `~/.codex/skills/nova-coordination/SKILL.md`

The LAN dashboard opens without a login prompt. Automatic worker credentials
protect mutations; internal CA verification protects HTTPS. Browser OS trust is
separate from client trust. No public ingress or tunnel is installed. Never
print client configurations, credentials, or claim tokens.

The existing **Nova coordination service** chat checks every **15 minutes** and
wakes an eligible idle **Nova development air** or **Nova development mini**
session only when the service has ready work. Both independent worker polling
automations are paused. The dispatcher never claims on a worker's behalf and
checks current session/queue state to avoid duplicate wake-ups. Workers continue
their own claims and immediately pull again after settled work, stopping when
no eligible task remains. Pending approvals and dependency waits do not wake them.
The background capacity/repository reporters stay running without invoking models.


## Claims and recovery

```sh
nova-coord add 'Rebuild pinned engine' --task-type engine_rebuild --key engine-001
nova-coord add 'Fix a sidebar bug' --key sidebar-001
nova-coord next
nova-coord show TASK_ID
nova-coord heartbeat TASK_ID --progress 'Checking native behavior'
nova-coord run TASK_ID -- python3 scripts/check.py
nova-coord finish TASK_ID --receipt /absolute/receipt.json
```

Task JSON accepts `request_key`, `title`, `description`, optional `task_type`
(`work` or `engine_rebuild`), priority, resource requirements and dependencies.
Default workflow is one Implement and verify step, including merge. Only
`engine_rebuild` receives heavy scheduling; old generic heavy requests normalize
to standard. Manually adding a publish step to ordinary work is rejected.

SQLite transactions prevent two workers from claiming the same task. Opaque
claim tokens and generations fence foreign/stale completions. Heartbeat age is
diagnostic: ownership does not expire, become administratively held, or become
available to another worker when a heartbeat is missed. Supervised commands
still stop on actual service loss/rejection. The worker may resume its claim
once it verifies the operation state.

Completion receipts require a summary and `execution_stopped: true`. Ordinary
workers record merged source/evidence and use non-force pushes, resolving main
movement and rerunning affected checks. Use `--outcome blocked` or `failed` for
incomplete work. Those outcomes free the worker for other tasks. To retry the
same task, its owner supplies verified evidence:

```sh
nova-coord retry TASK_ID --receipt /absolute/stopped-attempt.json
```

The receipt must contain `execution_stopped: true`, `external_state_checked:
true` and `evidence` explaining settlement and a concrete correction. No unknown
input/publication may be replayed. Admin `reconcile` remains for abandoned
legacy tasks; existing holds are not silently reopened by a schema migration.

## Explicit one-attempt exceptions

The ordinary limit remains twenty attempts per host assignment. An actual human
answer may explicitly authorize **one** additional attempt on the same named
host. Apply the original answer with `approve` first. An ordinary permission
answer alone never raises a limit; the worker must inspect the primary answer
and verify that it explicitly grants the extra attempt for that task.

`nova-coord attempt-exception --receipt /absolute/exception.json` binds that
exception to the immutable applied approval's ID and exact source identity,
task/generation, current host and used count. Its receipt contains `approval_id`,
`source`, `task`, `generation`, `worker`, `expected_used: 20`,
`attempt_number: 21`, descriptive `scope_evidence`, and three true attestations:
`exception_verified`, `execution_stopped`, `external_state_checked`.

The server accepts this supplemental record only for the currently approved,
settled queued task at twenty. It does not edit the original approval or lower any
counter. The next eligible claim atomically consumes the exception and records
attempt 21 in the assignment count and increments the lifetime count. Dependencies, ownership,
capacity, human stops and normal claim fencing still apply. A failed attempt or
replayed request cannot renew it. One task supports at most one such exception;
it does not grant publication authority or a general policy override. Source
rollout and feature acceptance remain separately recorded.

## Hourly publish tasks

A durable service timer checks each configured repository on the hour. Workers
observe `origin/main` every two minutes using existing host Git access and
`git ls-remote` (no checkout mutation). Stale/failed observations cannot trigger
a release; the service waits for a fresh observation before consuming the hour.
This also detects merges performed outside the queue. Repository configuration
records a verified published baseline, not an assumed current branch tip.

When main differs from the last verified publication, the service creates a
publish task pinned to that exact commit, available to either Mac. A stable
repository+commit key and a transaction prevent duplicate tasks across timer
races/restarts. The same repository/commit is not queued twice. A new merged commit gets its own
task at the next hourly check, regardless of an earlier publication task. Workers
check the actual public release and must not downgrade it. An out-of-order old
receipt cannot regress the stored publication baseline. There is no resource
lock, per-repository publication gate, or lock table used by claims/publishers.

Only the worker that claims the generated task publishes:

```sh
nova-coord publish TASK_ID --receipt /new/publication.json --command python3 publisher.py
```

`publish-permit` verifies ownership of that task; it does not acquire/check a
shared lock. Preserve signing/notarization and actual public-download checks.
Successful receipt fields are `publication_ref`, `published_commit` matching
`release_target.commit`, and normal stopped-execution evidence. If the release
procedure commits version metadata on main, include its verified `release_commit`
to advance the publication baseline without creating a release loop. Do not
include newer feature commits silently. A failed/unknown publication stays an
unresolved task for inspection, without holding other engineering work.

The initial baseline is configured through admin-only
`POST /api/repositories/configure` with repository, branch, published_commit.
Existing configuration cannot silently reset the baseline. Worker-only
`POST /api/repositories/observe` records fresh exact remote branch observations.
No repository credential is added to Kubernetes, and its pod needs no GitHub
egress. `nova-coord observe-main` performs a manual read-only observation.

## Installation and deployment

The service is deployed with `python3 deploy/deploy.py --apply` using existing
cluster SSH access. Source is an immutable ConfigMap; Python is digest-pinned.
SQLite WAL lives on retained local storage, with separate 15-minute backups
retaining96 snapshots. The service normally has one replica. Updates briefly overlap replicas on the
same local SQLite volume, whose transactions fence claims and scheduler ticks;
readiness keeps the API available. This is not automatic storage failover. Source rollback preserves the database and evidence.

Clients are distributed from the cluster host:

```sh
ssh pi@192.168.2.13 'cat /home/pi/.local/share/nova-coordinator/bootstrap.py' > /tmp/nova-bootstrap.py
python3 /tmp/nova-bootstrap.py --worker oliver-mac --repository-path /actual/nova/checkout
```

Use the actual worker name and checkout. Existing configuration, build path,
claims and credentials are preserved on client upgrades. Do not reinstall OS
trust just to update the client. The optional `--trust-internal-ca` flag is only
for deliberate initial trust setup. The installer manages only its own local
client/reporter/gateway and skill files.

## Verification

Run `python3 -m unittest discover -s tests -v`. Tests exercise atomic claims,
engine-only scheduling, persistent stale-heartbeat ownership, worker retry
fencing, legacy migration, HTTP identity boundaries, hourly no-change/new-change
behavior, duplicate suppression through restart/concurrent ticks, fresh branch
observations, exact published targets, and release metadata loop prevention.

`tests/browser-check.cjs` checks dashboard rendering/submission/details/search
on an isolated service. It creates one test task, so do not use it casually
against a production queue. Production verification should be read-only unless
an explicit task is being submitted.

## Waiting for a worker or prerequisite

Scheduling waits stay `queued`, with `requirements.worker` and/or task IDs in
`dependencies`. All prerequisites must complete before claim succeeds. A busy
or offline destination does not remove the task from the queue. The next normal
worker pull takes it once ready; no retry or administrator action is needed.

An owning worker can return settled work with `nova-coord wait ID --receipt FILE`.
The JSON needs `execution_stopped: true`, `external_state_checked: true`, descriptive
`evidence`, and `worker` and/or `dependencies`. This atomically clears the old
claim, retains its receipts and last worker, and fences stale updates. Dependency
cycles are rejected. The `/api/tasks/ID/defer` endpoint also supports administrator
migration of stopped blocked/failed tasks with an expected `generation`; it refuses
administrator reassignment of running work. Technical failures, human stops and
unresolved publication outcomes remain blocked until corrected.

The dashboard shows the next/active worker separately from `Last worked` and
explains queued prerequisites. `Either available Mac` means there is no fixed
assignment until one worker atomically claims the task.

For source-only upgrades on the existing cluster, use
`python3 deploy/deploy.py --apply --code-only`. This preserves live TLS names,
networking and deployment settings while replacing the immutable source bundle.

UI test-tooling failures create a separate, deduplicated repair task. After
execution settles, the affected feature waits in the queue on that repair,
retaining other dependencies and all acceptance evidence. Repair completion
allows the feature to resume its missing checks; it does not count as feature
acceptance. Product failures and missing account access are classified separately.

The user-set fix-attempt limit is 20 per host assignment for feature and UI-tool
repairs. A new host resets that count; at most 6 host reassignments are allowed
per task. Lifetime attempts and historical receipts remain available.

Nova release tasks now include the macOS public release and an **iOS upload to
App Store Connect only**, using the pinned source. No TestFlight distribution,
App Review submission or public iOS release is part of this task. Completion
requires a verified `ios_upload` receipt (status, source commit, version/build,
upload reference and explicit no-distribution/no-review flags). Completed past
releases remain historical; they are not retroactively marked as iOS uploads.
Apple upload workflow: https://developer.apple.com/help/app-store-connect/manage-builds/upload-builds/

An explicit user release request can start immediately through authenticated
`POST /api/releases/start` with `repository`. It requires a fresh remote-main
observation and shares hourly exact-commit deduplication; it does not replay
blocked releases of the same commit. New explicit releases have priority 90.

## External approval and technical retries

`pending_external_approval` is displayed as **Pending External Approval** in its
own dashboard category. Workers report it with
`finish ID --outcome pending_external_approval --receipt /path.json`. It releases
the worker and never retries automatically. An administrator can classify an
existing stopped task using `reconcile` with `action: pending_external_approval`,
current `generation`, settled execution, checked external state and evidence.
Returning it to the queue requires `action: retry`, `approval_received: true`
and evidence of the verified approval. Existing approval requests are not resubmitted.

Technical `blocked` tasks become claimable automatically after 15 minutes. They
stay on the current host until its 20-attempt budget is exhausted, then move to
another eligible host. Explicit scheduling handoffs, host/capacity requirements and
dependencies still apply. A running claim is never reassigned. The UI distinguishes
the actual next worker from the last worker and shows retry readiness. Workers
read previous evidence and inspect external state before executing a correction.

Receipts report `fix_attempts_used` within the current host assignment. An actual
claim by a different host resets this count and increments `reassignment_count`,
up to 6 changes per task. The initial assignment is not a reassignment. Returning
to a previous host consumes another change. Same-host retries do not reset the
count. The final host may use its remaining attempts after the sixth transfer.
`total_fix_attempts` retains lifetime history. The service enforces these budgets
atomically for automatic, manually retried, and deferred claims. Deployment
reconstructs previous host changes from claim events and preserves receipts.
Older cumulative receipt aliases are translated using the assignment baseline;
new workers should use the per-assignment `fix_attempts_used` field. Failed
unknown outcomes and human holds retain explicit reconciliation.

## Cancel queued work

Administrators can cancel a queued task through the existing
`POST /api/tasks/ID/reconcile` endpoint (or `nova-coord reconcile ID --file FILE`).
Provide `action: "cancel"`, the current `generation`, `execution_stopped: true`,
`external_state_checked: true`, and the user's cancellation reason in `evidence`.
Cancellation preserves the task, receipts and audit history. The transaction
rejects cancellation if a worker has already claimed the task; running work
keeps its ownership. Workers and anonymous dashboard clients cannot cancel.

## Correcting approval waits

An authenticated last-owning worker can explicitly resume its settled pending
approval through `/api/tasks/ID/retry` after a verified human response. Supply the
current claim identity, `approval_received: true`, `approval_evidence` identifying
the human response, and the usual stopped execution/checked external state and
evidence fields. Other workers and anonymous callers cannot resume it. Polling
alone never supplies approval. Existing admin reconciliation remains supported.

For a misclassified artifact-location wait, only an administrator may use
`/api/tasks/ID/defer` with the current generation, verified artifact access in
`evidence`, a destination worker and/or dependencies, and `wait_correction` with
`category: "artifact_access"`, `approval_not_required: true`, and a nonempty
`reason`. The audit records a classification correction, not received approval.
A recorded `human_stop: true` cannot be cleared this way. Original receipts,
acceptance and attempt/reassignment counts are preserved; no active task is moved.

## Durable approval responses

`nova-coord approve --receipt approval.json` applies one verified human response
to explicit pending task IDs/generations atomically. Its JSON format and same-turn
worker procedure are documented in `skill/SKILL.md`. Source thread, question and
message IDs identify an immutable approval receipt. Each task needs scope evidence,
settled execution, checked external state and the existing authenticated owner claim.
A pending receipt's structured `approval_request` must match the response question.
For legacy receipts without a binding, the worker must explicitly verify scope.

The client journals privately (0600) before POST, then GETs the authoritative receipt
and current task states. Lost responses are recovered by reusing the receipt, by
`approval-recover`, or by the existing next-task pull; no new background schedule.
Rejected scope/ownership is retained for review rather than retried forever.
The server API is `POST /api/approvals/apply` and authenticated
`GET /api/approvals/APPROVAL_ID`. Source/scope conflicts reject instead of widening
approval; duplicates never requeue a later generation or disturb active work.
Task results, dependencies, counters and prior receipts remain unchanged.

The dashboard labels approved queued/running work separately from an unanswered
approval wait. Approval never means tests passed or removes prerequisite/capacity
checks. `/api/state` advertises `capabilities.approval_receipts: 1`; use the installed
client and deployed capability rather than an older copied server-source cache.
