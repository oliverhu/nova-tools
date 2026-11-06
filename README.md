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

Mini VM1 and Mini VM2 have a separate dashboard section. Their existing resource
pulse optionally includes bounded local CLI state; no model or new poller runs.
Resource connectivity and CLI freshness use separate server timestamps. Older
clients, stale reports and unreachable workers display unavailable status rather
than idle. Current queue ownership, historical CLI runs, completed work and the
recent queued-task count are shown separately. Active tasks and each Mini's latest
completed task remain available outside the normal 500-task history window.
Only allowlisted IDs, states, flags and timestamps leave local CLI storage;
progress and blockers come from the existing public queue, never private CLI logs.

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

## Finite current-host allowances

For a separately verified human decision adding up to ten attempts to one
settled ordinary task at its default twenty, an administrator uses
`nova-coord --config /private/admin.json attempt-allowance --receipt
/private/allowance.json` (`POST /api/attempt-allowances/apply`). This does not
reset counters, change ownership/generation/status/dependencies, or authorize
attendance, native interaction, publication or other hosts/tasks.

The exact request fields are `request_id`, `task`, `generation`, `worker`,
`source: {thread_id, message_id}`, the actual human `response`,
`additional_attempts` (integer 1–10), `expected`, `decision_verified: true`,
`execution_stopped: true`, `external_state_checked: true` and descriptive
`evidence`. Independently verify the primary human message and explicit scope;
the service never interprets arbitrary prose as approval. `expected` binds
`status`, `owner`, `step`, `reassignment_count`, `attempts_host`,
`fix_attempts_used`, `total_fix_attempts`, `attempt_base`, and three hashes:
`requirements_sha256`, `attempt_allowance_sha256`, `attempt_exception_sha256`.
Hash each corresponding fresh public JSON object with SHA-256 over sorted compact
JSON. The task must be stopped, queued without an owner and hard-routed to its
existing assignment host. Re-read these bindings immediately before applying.

The service exposes `capabilities.attempt_allowances: 1`, effective
`fix_attempt_limit`, `fix_attempts_remaining`, `attempt_allowance_active` and
`attempt_allowance: {id, request, worker, reassignment_count, attempt_base,
effective_limit, granted_at, expired_at}`. A grant of ten at used twenty reports
limit thirty, remaining ten, without consuming an attempt at grant or claim.
Failed-finish accounting consumes the slots normally. A caller must verify the
fresh task/host/assignment/grant/generation/usage at prepare and run/admission;
never substitute an unverified local maximum. For an active grant, its public
`attempt_base` reflects current truthful accounting; the immutable request retains
the originally observed base and generation. Same-host generations and separate
attendance/approval cycles preserve this finite budget. A real host transfer
expires it permanently, including if the original host later returns.

Task detail exposes append-only `attempt_allowances` with request and before/after
evidence. A primary human source cannot mint multiple grants or be retargeted;
only one initial grant per host assignment is supported. Exact replay returns its original
audit and current state without replenishment. Default twenty, the six-transfer
cap and the legacy single-extra path remain unchanged; grants cannot stack with
that exception. A fresh attendance response is still required where applicable.

A **distinct new primary human authorization** can extend the exact current grant
by another 1–50 through admin `POST /api/attempt-allowances/amend` or
`attempt-allowance-amend --receipt /private/amendment.json`. Add
`previous_grant_id`, `unsettled_operations: []` and an independently verified
`settlement_receipt: {path, sha256}` to the request; add `fix_attempt_limit` to
the exact expected-state fields. It supports a genuinely quiet running task
owned by the same worker, or settled queued/failed/blocked work, preserving the live claim,
generation, status, counters and all existing evidence. The administrator must
actually verify owning settlement at apply; the service cannot inspect guest
processes/files. No fake finish or deferral is needed.
Failed/blocked outcomes, pending observations, approval metadata and retry timers
stay unchanged; an amendment does not itself queue or resume execution.

`capabilities.attempt_allowance_amendments: 1` advertises support. Latest
`attempt_allowance.id` hashes the new immutable request; its `previous_grant_id`
and `root_grant_id` link the original audit. The new limit is the exact prior
limit plus the new authorization, never an automatic refill. Initial audits stay
unchanged; append-only `attempt_allowance_amendments` records before/after state.
Each distinct authorization can extend the latest parent, with stale-parent
comparison and no forks. Exact replay returns the old audit/current state without
adding again. Primary sources cannot be reused across initial grants/amendments.
The initial grant remains capped at ten. A fifty-attempt amendment requires one
verified human authorization for fifty; do not split it into invented approvals.
Same-host attendance cycles preserve the latest authority; real host transfer
expires the entire chain. A caller must reject a candidate bound to an older
grant and qualify fresh authority using the amendment's expected prior limit
and matching immutable parent audit, rather than assume `20 + additional`.

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

An administrator can reconcile verified omitted attempts on unfinished or
genuinely completed ordinary work using `nova-coord --config /private/admin.json correct-attempts TASK_ID
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

For `expected.status: "completed"`, also supply `completion_proof` with exactly
`receipt: {path, sha256}` referencing the immutable original completion file,
`expected_result_sha256` and `expected_receipt_sha256`. Hash the fresh public
`result` and final service receipt envelope (`{outcome, receipt}`), respectively,
using sorted compact JSON. `owner_proof.step` binds the current completed step;
the final stored receipt is at that step minus one in the same generation.
The existing `owner_proof.receipt` references the
owning worker's immutable omission addendum, not a replacement completion.
The administrator verifies both files and the exact task/generation/assignment.
The service requires the completed final step, actual owner and matching stored
receipt/result explicitly stating `outcome: "completed"`,
`acceptance_complete: true`, `execution_stopped: true`,
`external_state_checked: true` and `unsettled_operations: []`. Missing or
contradictory acceptance, retained resources and publish/engine tasks are rejected.
The corrected counts must remain within the existing assignment allowance;
no new attempts or grant are created. This path preserves completed acceptance,
main/result evidence, original receipt bytes, generation, claims and all grants.
It does not reopen, retest or reclaim the feature. Unrelated work continues;
coordinate only the short service rollout boundary if support needs deploying.

Each historical proof contains exact `task`, earlier `worker`, `generation`,
`step`, `recorded_attempts`, actual `fix_attempts_used`, positive
`omitted_attempts`, `verified: true`, `receipt`, `source` and descriptive
`evidence`. This bounded path supports closed assignments on a different host.
Actual minus recorded usage must equal the omission; all historical omissions
must exactly explain the prior-base increase. The service verifies real claim,
settled receipt and host-transfer history and rejects reuse of an assignment,
including a different retry generation on that same assignment.

For example, restoring a prior host's omitted fifteen and a current host's two
changes `0/20/base20` to `2/37/base35`. The default host limit remains twenty;
existing verified assignment allowances remain unchanged. The reassignment
limit remains six. An already consumed explicit attempt exception
may be preserved, never created or enlarged through this correction.

The transaction changes only the three counters. It preserves claims, generation,
owner, task status/step, leases, dependencies, requirements, approvals, results,
publication state and all original receipts/events. An append-only before/after
audit is returned and exposed by fresh task detail as `attempt_corrections`;
`capabilities.attempt_correction: 1` advertises support;
`capabilities.completed_attempt_correction: 1` advertises the completed-work
extension. Check the response and
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

New `oliverhu/nova-browser` combined macOS+iOS release tasks, hourly or explicitly
started, use the existing hard `requirements.worker: "oliverhu-mac-studio"`.
Studio is the currently qualified publisher with the required signed-in Xcode
upload readiness. Generic `macos`/`development` capacity does not establish that
readiness on a guest. The existing eligibility/claim checks enforce this routing
even for an unfiltered worker `next`; if Studio is busy, offline or paused, the
release waits without falling back to a VM. Ordinary work and other repositories
keep their existing routing. This is assignment policy, not an account probe or
additional approval/validation stage. Coordinator maintains actual onboarding
readiness and revises the route when another publisher is genuinely qualified.
Existing tasks, source pins, holds, claims and publication evidence are untouched;
exact-target deduplication does not replace an earlier task to change its route.

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

When the reviewed service source is a minimal port based on an older live
revision, add `--skip-client-package` to that code-only command. This preserves
the existing distributed client bundle. Publish clients separately with
`scripts/package-client.py` from their verified current source revision; do not
replace newer CLI adapters, skills or client fixes with an older service tree.
The default deployment behavior still packages clients from its own checkout.

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

An explicit **iOS upload-only** request uses the same administrator endpoint with
`scope: "ios_upload_only"`, `repository: "oliverhu/nova-browser"`, the exact
40-character `commit`, existing prerequisite task IDs in `dependencies`, and
`authorization: {"thread_id": "primary-human-chat", "message_id": "human-request"}`.
The target must match a fresh remote-main observation. This creates a normal
Studio-only `publish` claim, separately deduplicated by repository/commit/scope;
`task_ids` returns its ID on both creation and replay. Request bindings remain
immutable. Existing exact-target running claims, human holds and uncertain upload
outcomes must be reconciled first. This does not rewrite old release tasks or
alter the hourly combined-release timer.

The publisher uses the existing Xcode upload pipeline and retained immutable
iOS version/build/archive/journal. Completion requires `publication_ref`,
`external_state_checked: true`, and the exact-source `ios_upload` receipt above,
also recording `testflight_settings_changed: false` and `ios_public_release: false`.
Omit `published_commit` and `release_commit`: an iOS-only completion never updates
the Mac published baseline. Apple upload acceptance is separate from processing,
tester availability and physical-device feature acceptance.

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

The additional Mini VM identities are `oliverhu-mini-vm1` and `oliverhu-mini-vm2`.
New records for these identities start **paused**. Reporter pulses and service
restarts do not enable them. The retired `oliver-mini` host identity and every
existing worker's pause/claim state remain separate and unchanged. Each new VM
needs its own distinct random credential in the existing private service Secret;
never clone a Studio client config, token, claim or approval journal. Bootstrap
selects the requested credential on the cluster host so the guest receives only
its own token. Use `--no-start` during onboarding.

After the setup owner verifies actual storage, toolchain, permissions, identity
and current instructions, Coordinator may enable that exact VM through the
existing administrator `POST /api/workers/WORKER/state` with `paused: false` and
the readiness-receipt reference in `reason`. Until then, both explicit and
unfiltered claims are refused even if capacity is reported. The planned 5 CPU /
8 GiB guest allocation is configuration, not proof of measured available capacity
or readiness. Preserve any unrelated dispatch hold and existing engine/publisher
requirements. Enrollment is not VM provisioning or feature acceptance.

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
