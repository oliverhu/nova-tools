# Nova tools

Source for Nova engineering tools maintained outside
[`nova-browser`](https://github.com/oliverhu/nova-browser). This repository contains:

- `coordinator/` and `web/`: the task queue API, CLI, local gateway and dashboard.
- `deploy/` and `scripts/`: service deployment, packaging and client installation.
- `tests/`: coordinator regression and dashboard checks.
- `skill/SKILL.md`: the coordination workflow installed on developer workers.
- `skills/mac-vm-setup/`: portable host/VM setup skill, references, machine template
  and read-only inventory helper.
- `docs/agent-coordination.md`: current developer, Coordinator and Dispatcher roles.
- `artifact-transfer/`: the restricted, read-only SSH artifact helper and setup notes.
- `scripts/setup-new-mac.py`: new-Mac onboarding, permission requests and private
  provisioning transfer. See [the setup guide](docs/new-mac-setup.md) and
  [example configuration](setup/new-mac.example.json). Authored; not yet runtime-tested.

The coordinator's original 18 source commits are preserved through `cc526e0`.
The artifact helper is copied from the deployed Mac helper without changing its
behavior. Browser implementation and browser-specific test tools remain in
`nova-browser`.

Only source and reusable documentation belong here. Keep live client configuration,
credentials, SSH keys, task databases, claims, approval journals, recordings,
logs, validation receipts and generated payloads outside Git. Pushing this
repository does not deploy the service or replace an installed client.

## Portable machine setup skill

Use [`mac-vm-setup`](skills/mac-vm-setup/SKILL.md) for a new Apple Silicon host
and Tart guests. It includes the single-disk/APFS lessons, Xcode/SSH setup,
actor-specific permissions and signing, current agent roles, functional checks,
a machine template and a read-only inventory helper. It contains no credentials
or machine-specific approval records.

From this repository, install on a machine where that skill is not already present:

```sh
skill_parent="${CODEX_HOME:-$HOME/.codex}/skills"
mkdir -p "$skill_parent"
test ! -e "$skill_parent/mac-vm-setup" && cp -R skills/mac-vm-setup "$skill_parent/"
```

For an existing installation, compare and preserve local edits before updating
the complete folder. Invoke `$mac-vm-setup` in a new chat. The inventory helper
needs only Python 3 on macOS; exit zero means inventory ran, not that the machine
is ready. The skill is an assisted procedure, not a tested unattended installer.
The client installer/package continues to install only `skill/SKILL.md` as
`nova-coordination`; install this additional skill explicitly from the repository.
Coordinator owns keeping both installed skills synchronized across Studio and the
two VMs, including developer/Dispatcher reread confirmation. See the
[instruction synchronization procedure](docs/agent-coordination.md#coordinator-owns-instruction-synchronization).

## Nova task queue

A private Kubernetes queue for the Studio host and its two VM developers.
Developers self-claim, implement, meaningfully validate, audit and integrate main,
then report directly to **Nova — Delivery Coordinator**. Coordinator records
completion without additional validation. **Nova — Queue Dispatcher** wakes
eligible idle workers. See [current roles and completion flow](docs/agent-coordination.md).

Ordinary work **does not publish**. Heavy engine compilation stays on the configured
Studio host; standard work follows actual capacity and capability requirements.
There are no shared publishing, integration, main or desktop locks in the queue.
Task ownership and safe settlement still apply.

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

The existing **Nova — Queue Dispatcher** checks every **15 minutes**, using the
private current worker/session map and authoritative queue readiness. It wakes
only eligible idle developers and does not duplicate direct Coordinator assignments
or claim on their behalf. Legacy independent worker polls remain paused. Developers
continue their own claims and report outcomes directly to Coordinator; after a
claim settles they may continue eligible work under the current assignment policy.
The background capacity/repository reporters run without invoking models.

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

Developers send their result and receipt directly to Coordinator, which records
completion without retesting. The owning worker submits `finish` when directed
through its existing claim; never transfer tokens or manufacture a blocked state
for administrator closure. Confirm the service readback before calling it completed.

Completion receipts require a summary and `execution_stopped: true`.
`finish` uses an explicit receipt `outcome` when `--outcome` is omitted, retaining
the legacy `completed` default only when the receipt declares no outcome.
Client and service reject conflicting outcomes and refuse completion when
`acceptance_complete` is explicitly false, including requests from older clients.

Ordinary workers record merged source/evidence and use non-force pushes, resolving main
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

## Correcting an erroneous completion

For an erroneous final completion whose original receipt explicitly says
`outcome: blocked`, `acceptance_complete: false`, `execution_stopped: true` and
`external_state_checked: true`, an administrator can run:

```sh
nova-coord --config /private/admin.json correct-outcome TASK_ID --receipt /private/correction.json
```

The correction JSON has exactly `generation`, `expected_status: "completed"`,
`expected_step`, `expected_result_sha256`, `expected_receipt_sha256`,
`outcome: "blocked"`, `execution_stopped: true`, `external_state_checked: true`
and descriptive `evidence`. Hash the current result and completed receipt envelope
with SHA-256 over `json.dumps(value, sort_keys=True, separators=(',', ':')).encode()`.
The service checks that exact final work step and original contradiction. It
refuses running/stale tasks, publisher/engine tasks and publication or repository
side effects. It restores the preceding step, fences the old claim and disables
automatic retry. Generation, result, original receipts/events, dependencies,
requirements and all attempt/reassignment counts remain unchanged.

A separate append-only correction record retains the full prior state, original
completed envelope, request and corrected state. Task detail exposes it as
`outcome_corrections`; the event references its immutable ID. An identical retry
is idempotent while the corrected state is unchanged. This operation neither
qualifies acceptance nor resumes execution; later scheduling uses the existing
supported workflow.

## Correcting omitted attempt accounting

An administrator can reconcile verified omitted attempts on unfinished ordinary
work using `nova-coord --config /private/admin.json correct-attempts TASK_ID
--receipt /private/attempt-correction.json`. This is accounting only: it cannot
grant another attempt, complete acceptance, transfer ownership or settle execution.

The request has exactly `request_id`, `generation`, `expected`, `corrected`,
`owner_proof`, `historical_proof` and descriptive `evidence`. Use a stable unique
request ID. `expected` contains the freshly observed `status`, `owner`, `step`,
`reassignment_count`, `attempts_host`, `fix_attempts_used`, `total_fix_attempts`
and `attempt_base`. `corrected` contains only the three counters. Every counter
must be monotonic and `total_fix_attempts = attempt_base + fix_attempts_used`.

The owning proof contains exact `task`, `worker`, `generation`, `step`, corrected
`fix_attempts_used`, `verified: true`, `execution_stopped: true`,
`external_state_checked: true`, `unsettled_operations: []`, `receipt`, `source`
and descriptive `evidence`. Each receipt/source reference has exactly an absolute
`path` and its immutable file `sha256`. The administrator must actually verify
the owning report, files/hashes, historical provenance and coordinated quiet
boundary; the service cannot read guest files or independently stop processes.
Refresh ownership, counters and settlement evidence immediately before applying.

Each historical proof contains exact `task`, earlier `worker`, `generation`,
`step`, `recorded_attempts`, actual `fix_attempts_used`, positive
`omitted_attempts`, `verified: true`, `receipt`, `source` and descriptive
`evidence`. This bounded path supports closed assignments on a different host.
Actual minus recorded usage must equal the omission; all historical omissions
must exactly explain the prior-base increase. The service verifies real claim,
settled receipt and host-transfer history and rejects reuse of an assignment,
including a different retry generation on that same assignment.

For example, restoring a prior host's omitted fifteen and a current host's two
changes `0/20/base20` to `2/37/base35`. The host limit remains twenty and the
reassignment limit remains six. An already consumed explicit attempt exception
may be preserved, never created or enlarged through this correction.

The transaction changes only the three counters. It preserves claims, generation,
owner, task status/step, leases, dependencies, requirements, approvals, results,
publication state and all original receipts/events. An append-only before/after
audit is returned and exposed by fresh task detail as `attempt_corrections`;
`capabilities.attempt_correction: 1` advertises support. Check the response and
fresh task detail against the exact target. An identical request is idempotent
even after later task progress: it returns the original audit and current task
without replaying a delta. A changed duplicate or stale new request is rejected.
Never compensate for missing support with a fake finish or direct database edit.

## Hourly publish tasks

A durable service timer checks each configured repository on the hour. Workers
observe `origin/main` every two minutes using existing host Git access and
`git ls-remote` (no checkout mutation). Stale/failed observations cannot trigger
a release; the service waits for a fresh observation before consuming the hour.
This also detects merges performed outside the queue. Repository configuration
records a verified published baseline, not an assumed current branch tip.

When main differs from the last verified publication, the service creates a
publish task pinned to that exact commit, available to an eligible qualified developer. A stable
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
python3 /tmp/nova-bootstrap.py --worker oliverhu-mac-studio-vm1 --repository-path /actual/nova/checkout
```

The command above illustrates an already enrolled VM1, not a new identity to
copy. Use the actual supported/enrolled worker name and checkout. Existing configuration, build path,
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
App Store Connect only**, using the pinned source. Use the existing signed-in Xcode account for the upload. Do not touch TestFlight
distribution, testers/groups or settings, and do not submit App Review or a public
iOS release. Do not add routine App Store Connect browser/list preflights. Completion
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

For an administrative correction of a nonhuman setup/dependency wait, use the
same endpoint with `wait_correction.category: "setup_dependency"`, explicit
`approval_not_required: true`, a reason identifying the verified classification,
and a nonempty `dependencies` list of existing prerequisite task IDs. Verify the
actual stopped owner receipt and scope first; an assistant's assertion alone is
not consent. Current generation, `execution_stopped`, `external_state_checked`
and descriptive evidence are required. The recorded execution must also be
stopped. Any recorded `human_stop: true` or `approval_request` field in the current
result or receipt history refuses this path, including legacy request formats.
Use the existing genuine-approval workflow for those tasks. Dependencies remain
cycle-checked and gate pickup. This correction preserves original receipts,
results, last owner, generation and attempt counts and audits the reason and
prerequisites; it does not grant access, clear an OS denial or qualify a test.

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

## Studio migration

The service recognizes `oliverhu-mac-studio`, `oliverhu-mac-studio-vm1`,
and `oliverhu-mac-studio-vm2` in addition to the two legacy identities. Enroll a
distinct random credential for each new worker in the existing private service
Secret before installing its client. Never copy an old worker identity into a VM.
Existing credentials, database, task ownership and approval receipts are retained.

After verifying destination workers and settling source execution, an authenticated
administrator can POST `/api/workers/oliver-mac/state` (or another known worker)
with `{"paused": true, "reason": "Verified migration to Studio"}`. Running or held
claims prevent this operation. Retirement is durable across reporter pulses and
service restarts; it does not requeue tasks or change approval holds. Stop the
old reporters and move schedules only at verified cutover. The same audited API
with `paused: false` supports an explicit rollback.

The Studio host uses internal build storage by the user's explicit choice. Each
VM uses its own 1 TB allocation on the new 2 TB external SSD. Configure guards
from actual mounted identities and measured resources; do not reuse old host
configuration or report host memory already allocated to guests as available.

Heavy compilation is reserved for the Studio host. Set `COORD_HEAVY_WORKER`
to `oliverhu-mac-studio` on the service at migration cutover. Heavy tasks then
wait for that host, including when it is busy, offline or lacks capacity; they
never fall back to guests. Existing claims and approval records remain intact.
The host client reports `capacity_limits: {"cpus": 14, "memory_gb": 48}` while
two 8-vCPU, 24-GiB guests run. These limits may only reduce measured capacity.
Pause settled guest work before allocating more memory/CPU to a large host build.
