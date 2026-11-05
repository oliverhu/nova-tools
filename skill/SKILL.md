---
name: nova-coordination
description: Coordinate, dispatch, implement and report Nova engineering work through the private queue using the Studio host and VM developers, with direct worker reports and Coordinator completion recording.
---

# Nova task queue

The current human-approved workflow (2026-11-03) uses three developer workers:
Studio (`oliverhu-mac-studio`), VM1 (`oliverhu-mac-studio-vm1`) and VM2
(`oliverhu-mac-studio-vm2`). Legacy Mac/Mini identities retain history and stay
retired unless explicitly restored. Use `~/.local/bin/nova-coord`; never display
private client configuration, credentials or claim tokens. The central service
is authoritative for task ownership and eligibility.

## Current agent roles

- **Nova — Delivery Coordinator:** intake, priorities, task submission/IDs,
  assignment, human decisions, status and recording developer-reported completion.
  Performs **no additional source, runtime, UI or release validation**.
- **Nova — Queue Dispatcher:** the existing 15-minute readiness/availability check;
  wakes an eligible idle mapped developer for an exact task. Does not claim for
  workers or duplicate Coordinator assignments.
- **VM1/VM2 and Studio developers:** own implementation, actual tests/acceptance,
  source audit, main integration and settled cleanup in isolated task checkouts.
  Send completion reports directly to Coordinator.
- **Publisher:** an eligible developer executing a separate release claim, owning
  actual delivery validation and reporting. Ordinary feature tasks never publish.

Coordinator and Dispatcher are chat roles, not extra worker identities. Discover
current host/chat/project bindings from the private route map; no hardcoded chat
IDs or copied credentials. Use the existing human-authorized Nova-to-Nova routing
for scoped reports. Preserve actual approval blocks and current user instructions.
The repository's `docs/agent-coordination.md` explains the full role and API mapping.

## Keep installed instructions current

The user assigned **Coordinator ownership of instruction synchronization** on
2026-11-03. After a workflow/skill change lands in `nova-tools/main`, and when a
worker is enrolled or reconnected, Coordinator ensures Studio, VM1 and VM2 have
the current `nova-coordination` and `mac-vm-setup` skill folders. Use a verified
source commit; preserve differing local files outside skill discovery before
replacement, verify installed file SHA-256 values, and keep a private per-host
receipt with source commit, paths and verification time. Never copy credentials,
claims or OS permission state as part of skill synchronization.

Also maintain the browser repository's `NOVA_CHAT_ROLES.md` and `AGENTS.md`
ownership guidance on Studio and both VMs from a verified `nova-browser/main`
commit. Keep these consistent with the current developer-owned acceptance/main
merge process and Coordinator's administrative role. Track browser-document and
tools-skill source commits and destination hashes separately. Preserve local
differences before replacement; do not update unrelated source or frozen task
artifacts. Older task checkouts may retain historical instructions: have their
owners load the current canonical project policy at a safe boundary and carry
the documentation change through normal integration when safe.

After an update, tell every active developer and Dispatcher to reread their
installed coordination skill and changed role documents at the next safe boundary
while preserving current work and claims. Coordinator also rereads the update.
Track each actor's readback of the applicable source commits/content hashes;
delivered messages alone do not prove the instructions were loaded. Coordinator
keeps the expected revision and current routes in private operational state.
This is instruction maintenance, not additional validation of developer results.
Track the skill source commit and installed content hashes separately from the
client/service code revision. After a skill change, refresh the existing client
distribution bundle so a later client upgrade carries the current instructions.
Use the existing upkeep activity; no new polling schedule or app restart.

Dispatcher checks that the mapped worker's synchronization receipt matches the
expected revision before new assignments. If stale or unavailable, report it to
Coordinator for repair; do not claim for the worker, interrupt existing work or
invent a new approval gate. Recheck after client upgrades, which may install an
older packaged skill. Reuse existing coordination/dispatch activity: do not add
another polling automation or restart apps/services just to reload instructions.

Coordinator also owns the private, versioned host identity baseline and each
machine's readiness receipts described in `mac-vm-setup/references/host-baseline.md`.
The user's selected Nova policy shares the coordinator's exact GitHub client key
and selected provisioning identities; SSH server keys, queue credentials, OS
consent and machine/chat bindings remain individual. Record expected public
fingerprints, profile revisions and actual capability gaps. Compare after
onboarding, asset renewal, VM restore or a reported access failure through
existing coordination activity. Dispatcher requires the capabilities relevant
to the assigned task; installed assets alone do not establish account access,
signing use, notarization or release readiness. Repair only the differing
component and reuse current evidence rather than repeating product validation.

The user also asked on 2026-11-04 to minimize test/app permission prompts and
move predictable consent into onboarding. Developers scrutinize each request:
name the actual actor, category and required operation; use isolated fixtures
when equivalent; reuse the approved runner family and valid existing grants.
Coordinator consolidates genuinely missing setup into the per-machine onboarding
checklist and records the human's actual result. Use the portable setup skill's
`references/permission-onboarding.md`. Routine tests inspect readiness and report
missing prerequisites before starting; they do not request consent repeatedly,
add confirmation relays, reset grants or broaden access. A partial UI tree or
unsettled operation is not by itself evidence of a missing permission.

Before new feature assignments, Coordinator and Dispatcher require the mapped
worker's actual permission-onboarding report for all planned app/runner contexts,
including Local Network for NovaFeatureCheck where required. Installed skill
hashes or local tool grants alone are insufficient. Developers verify this before
normal `next`/claim; an explicitly assigned onboarding task resolves missing setup.
Keep affected work queued behind that existing prerequisite, preserving other
dependencies and active claims. Unknown/denied/untested required access keeps the
worker unavailable for those feature assignments until the setup owner qualifies
it. Record unused categories with a reason rather than requesting blanket access.
Recheck affected rows after context changes or unexpected prompts, through the
same onboarding task and existing coordination activity. See the setup skill's
permission-onboarding reference; no new claim service or polling schedule.

An intended operation that actually succeeds for the exact GUI app context with
settled cleanup can qualify observed allowed access even if no prompt or human
decision was observed. Record these facts separately; do not require another
Allow response merely to reuse working access. A user-authorized operation-only
repeat keeps the same immutable app/identity/route and fresh private session data.
It qualifies that operation/context, not a stored grant or blanket family access.
Actual denials and unresolved operations remain binding.

## Work and ownership

Submit each new request once with `add --file /absolute/task.json`, using a
stable `request_key`, title, description/acceptance criteria, and one work step.
Return the actual service task ID to the user after submission.
Only an actual engine rebuild uses `task_type: engine_rebuild` and heavy compute.
App builds, UI checks, large test suites, and ordinary features are standard work.
Engine rebuilds use the configured `COORD_HEAVY_WORKER` (Studio) and wait if it
is unavailable; never fall back to guests. Standard tasks use eligible developers
with the required capacity/capabilities. Account for guest allocations on the host.

### Use available compute

The human instructed on **2026-11-04**: use **at least 80% of available
resources** for active compute-intensive work. Do not preserve an arbitrary
conservative compiler cap while the host is mostly idle. For a parallel build,
start near 80% of logical CPUs and tune concurrency to measured host utilization,
actual guest/other-worker activity, memory pressure, swap growth and storage I/O.
Allocated guest vCPUs are not continuously busy host cores; do not subtract them
as though they were. Preserve required free-storage reserves and existing work.

Measure after startup and at materially different build phases. If compute is
available and the workload can use it, raise concurrency until the target is
met. Reduce it only for a demonstrated bottleneck or pressure condition, and
record that evidence and the resulting limit. The target is useful throughput,
not filling RAM or forcing parallelism into serial work. When a fixed running
command must restart to change its limit, settle only its owned invocation,
retain completed incremental outputs, and resume the same task. Report actual
utilization and remaining-time uncertainty rather than job count alone.

Claim with `next` or `next --task ID` and read the task and prior receipts.
A task is atomically assigned to one worker. There are NO global publishing,
main, desktop, or integration locks. Task ownership remains until the worker
reports an outcome: a missed heartbeat is shown as stale, not an administrator
hold and not a reason to give the task to another worker. Never duplicate a
running task or infer that an offline worker's processes stopped.

A routine worker implements, meaningfully tests, and merges its changes into
main. Rebase/resolve conflicts and rerun affected checks if main advances;
use normal non-force pushes. Record exact branch/commit/merge and evidence.
Send Coordinator the task ID/generation, outcome, landed commit, actual results,
receipt path, limitations and settled-execution evidence. Coordinator records it
without retesting; never call partial acceptance complete. Evidence sent before
the verified finish readback is pending service completion, not a completed task.
For service completion,
the owning worker uses its existing claim-compatible `finish` when directed and
returns the readback. Do not transfer tokens, impersonate a worker or fake a
blocked state to enable administrative completion.
**DO NOT PUBLISH as part of ordinary work.** Automatic publication uses a
separate service-generated hourly publish task. Explicit user-authorized manual
publication follows the manual path below without waiting for the hourly check.

Report progress with `heartbeat ID --progress "..."`. Use `run ID -- command`
for long commands; its watchdog stops owned processes on service rejection or
unavailability. A quiet reasoning interval does not expire ownership. Stop on
an actual ownership rejection. After operations settle, finish with
`finish ID --outcome OUTCOME --receipt /path.json`, a summary and
`execution_stopped: true`. Use `completed` only for completed acceptance,
`blocked` for a technical failure, `failed` for an unknown outcome requiring
inspection, and `pending_external_approval` for Apple, an external reviewer or an
explicit human approval. Pass the outcome explicitly and make any receipt
`outcome` agree. The current client honors an explicit receipt outcome when the
argument is omitted; only a receipt without an outcome retains the legacy
completed default. Client and service reject contradictory outcomes and refuse
completion when `acceptance_complete` is explicitly false.

Before reporting completion, **developers and Dispatcher verify the actual API
outcome**, using the finish response and a fresh `nova-coord show ID` readback.
Check the expected task/generation, final `status: completed`, the completed
step position, settled execution and consistency with the actual receipt. An
explicit noncompleted receipt outcome or `acceptance_complete: false` prevents a
completion report even if a legacy API record says completed. A successful CLI
exit, a completed intermediate step, a passed subset or the receipt filename
does not establish whole-task completion. Report blocked, failed, queued or
pending states as returned, retaining incomplete acceptance and limitations.

Coordinator records completion only after this administrative API/receipt check;
it does not repeat developer product validation. Dispatcher uses current API
outcomes for completion reports and dependency readiness, and flags contradictory
records to Coordinator instead of treating them as qualified prerequisites. On
a mismatch or uncertain finish response, preserve the receipt, generation,
history, dependencies and counters, then inspect `show` before any retry. Use the
existing supported, audited admin correction for a genuinely erroneous recorded
completion; never replay a stale finish, edit the database or reset counters.
Coordinator owns keeping this rule installed and acknowledged across the three
developers and Dispatcher through the synchronization procedure above.

Pending External Approval never retries automatically; do not resubmit approval
requests. Resume only after actual approval is verified and the task is explicitly
returned to the queue. A failed/blocked task does
not consume the worker forever. Settled blocked tasks become eligible automatically after 15 minutes, up to 20 attempts per host assignment. Automatic retries stay on the current host until 20 attempts are used; then another eligible host can claim the task. Explicit scheduling handoffs still preserve hard worker/resource requirements. Inspect prior receipts and external state before side effects; apply a concrete correction before repeating a failed operation. Report `fix_attempts_used` for the current host assignment in each receipt. A claim on a different host resets this count to zero and increments `reassignment_count`; at most 6 host changes are allowed per task. Same-host retries never reset the count. Keep lifetime evidence and `total_fix_attempts`. Its owner can also use `retry ID --receipt /path.json`
after a concrete correction and proof of `execution_stopped`,
`external_state_checked`, and descriptive `evidence`. Never replay an unknown
publication or input outcome; inspect external state first. Old claims are fenced.
Administrator reconciliation remains available for abandoned legacy tasks.

## Apply actual approval responses immediately

When asking for a human decision, associate every explicitly covered pending task
with that same question. Include `approval_request: {"thread_id":"...",
"question_id":"..."}` in each pending receipt. Keep unrelated approval requests
and genuine human stops separate. Do not mark qualification as complete merely
because tool use was authorized.

When the human actually answers, process that response **in the same turn**, even
if another task is active. First verify the primary human message and map only
the explicitly covered tasks. Persist one receipt and run
`nova-coord approve --receipt /absolute/approval.json` before saying work resumed.
This replaces a chat-only acknowledgment and a series of easily forgotten retries.
The receipt has this shape (no credentials or claim tokens in the input file):

```json
{
  "source": {"thread_id":"original-thread", "question_id":"original-call", "message_id":"actual-human-response"},
  "decision":"approved", "decision_verified":true,
  "response":"Exact human decision", "scope":"Exact authorized scope and constraints",
  "tasks":[{"id":"existing-task-id", "generation":1,
    "scope_evidence":"Why this task is explicitly covered by that question/answer",
    "execution_stopped":true, "external_state_checked":true}]
}
```

Include all covered *pending* tasks at their current generations. The installed
client adds each existing owning claim, journals the exact request privately
before sending, and reads back the service receipt and current task eligibility.
The service applies the scope atomically; a missing/stale/foreign task rejects
the batch without partial changes. A structured question binding must match;
for legacy receipts without it, carefully verify and record explicit scope evidence.
Approval source identity and scope are immutable after application.

Read the returned states before reporting: queued means approved but waiting for
capacity/prerequisites; running means execution started; neither means qualification
passed. Do not interrupt another active claim. Use existing dependencies for real
qualification prerequisites. Never infer consent from timeouts, an unrelated answer,
or automatic polling, and never expand one response into blanket approval.

If delivery/readback is interrupted, rerun the same `approve` command or use
`nova-coord approval-recover`. The existing `next` command also reconciles only
previously verified pending local receipts before pulling; there is no new schedule.
Duplicate requests return their existing receipt/current task state without
replaying transitions, including if a task has since moved to a new generation.
Use `nova-coord approval-status APPROVAL_ID` for readback. An explicit service
rejection is retained as `needs_review` in the private local approval journal;
inspect the scope/current ownership instead of retrying unchanged or broadening it.
The command never parses arbitrary chat text or grants OS permissions itself.
Legacy per-task `retry` with `approval_received`/`approval_evidence` remains
supported, but prefer one durable approval receipt for the full explicit scope.

If an artifact-location wait was misclassified as Pending External Approval,
verify access first. An administrator can use the existing `defer` endpoint with
current generation, stopped/checked evidence, worker/dependencies and
`wait_correction: {"category":"artifact_access","approval_not_required":true,
"reason":"...verified artifact and classification evidence..."}`. This audits
a classification correction without inventing external approval. Preserve real
human stops, all acceptance requirements, receipts and attempt history.

When the only blocker is waiting for another worker or prerequisite, use
`wait ID --receipt /path.json` instead of marking the task blocked. The receipt
contains `execution_stopped: true`, `external_state_checked: true`, `evidence`,
and `worker` (an actual enrolled worker ID) and/or `dependencies` (existing task IDs).
Stop owned operations before transferring. This clears the old claim and keeps
the task queued. It becomes eligible automatically once every prerequisite is
completed and the destination is available. Pull another eligible task immediately.
Preserve human stops and unresolved external outcomes. A technical failure may
become a dependency wait when a concrete corrective task is queued, as below.
Preserve branch/commit/artifact evidence and prior attempt history for the next worker.

## Task dependencies

Encode actual prerequisites as existing task IDs in `dependencies` when submitting
or updating a task. Never rely on prose references or priority alone for ordering.
Inspect the current dependency chain and service `waiting_reason`; preserve
existing prerequisites when adding others. A task becomes eligible only after
all its dependencies complete, including when requested with `next --task ID`.
Keep independent work parallel: sharing a file or feature area alone is not a
reason to serialize tasks. Update only the explicitly reviewed task relationships;
do not rewrite unrelated tasks or interrupt running claims.

## Fix-attempt limit

The user set **20 fix attempts per host assignment**, with at most **6 host
reassignments per task**. This supersedes older instructions prohibiting resets
on transfer. An initial assignment does not consume a reassignment. Count a host
change only when the new host actually claims the task, not when it is queued.
The service resets `fix_attempts_used` to zero on a host change and preserves
`total_fix_attempts` and all receipts. Returning to a previously used host counts
as another reassignment and grants a fresh 20 attempts. Same-host retries keep
the current count. After six host changes, the final host may finish its remaining
attempts, but no seventh transfer is allowed. Hard requirements still apply.

Read current task counters on pickup (and after policy changes). Report
`fix_attempts_used` for this assignment; do not copy lifetime counts into this
field. Keep historical evidence, apply a concrete correction before retrying,
preserve acceptance checks and verify prior execution has settled. Pending
External Approval never retries automatically, regardless of remaining budget.

## A human-approved single extra attempt

The default remains twenty attempts. If the actual human response explicitly grants
one additional attempt on the same host, apply that answer using `approve` first,
then use `attempt-exception --receipt` to bind the grant to its immutable
approval ID/source, task/generation, worker, `expected_used: 20` and
`attempt_number: 21`. Include `exception_verified: true`, stopped/checked
evidence and an exact scope explanation. A generic tool permission or ordinary
approval is not an attempt exception. Do not infer it from free-form task text.

Read back eligibility. The existing task's next eligible claim consumes the one
exception and records attempt 21 and increments the lifetime counter. Do not
reset counts, create a replacement feature task or pretend a host reassignment.
One task supports one extra attempt; failed or interrupted acceptance cannot
renew that grant. Other ownership, dependencies, access and publication rules
still apply. A later human approval wait invalidates an unconsumed older grant.

## UI-test tooling blockers

When a feature is blocked by broken UI tools, collectors, fixture startup or
focus/input delivery, enqueue a separate repair task immediately. Reuse an
existing repair for the same root cause using a stable request key; do not create
duplicates on each poll or another repair identical to a blocked repair. Include
the affected feature IDs, exact failure/evidence, reproduction environment and
checks that demonstrate the tool is repaired. A product assertion failure stays
with the feature; missing credentials, account access or human actions are not
UI-tool defects.

After confirming the feature's owned operations stopped and checking external
state, use `wait` to add the repair ID to its existing dependencies. Preserve
other prerequisites, feature acceptance and source evidence. The feature becomes
eligible automatically after the repair completes; it must then run the missing
acceptance checks before merge. Repair completion never means feature acceptance.
Keep consumed attempts as history; use a new corrected test run only after the
repair is verified, without replaying unknown inputs or weakening assertions.
Do not mark a repair completed merely because its tests were skipped or limits
were increased. Diagnose shared tooling once and link all affected features.


## Hourly publication

Every hour the service checks for a fresh observed main commit different from
the last verified publication. The background clients observe origin/main using
their existing Git access, without modifying checkouts. The service creates a
single deduplicated publish task with an exact `release_target`; an eligible
qualified developer can claim it. It does not build or publish by itself, and there is no publish lock.
The same repository/commit is not queued twice. A later merged commit gets its
own task at the next hourly check, even if an earlier task is still running or
failed. Workers check actual release state and never downgrade a newer release.

For automatic publication, only a claimed publish task may run the normal signed/notarized release flow.
Use `publish ID --receipt /new/receipt.json --command ...` or the existing
publisher with `publish-permit`. That check verifies task ownership, not a global
lock. Verify the actual public artifact. A successful receipt requires
`publication_ref` and `published_commit` equal to the pinned target. If release
metadata is committed on main, record its verified `release_commit` too, so that
metadata commit does not trigger another release. Do not silently include later
feature commits. Unknown outcomes require inspection before any retry.

## iOS release upload

Each Nova release task includes the normal macOS release plus an iOS archive,
signing, validation and upload to **App Store Connect**, from the same pinned
source. The user authorized upload only: no TestFlight distribution, tester/group
assignment, automatic beta distribution, App Review submission or public iOS release.
Use `xcodebuild` with the existing signed-in Xcode account. Do not touch TestFlight
settings, testers/groups or distribution, or add routine App Store Connect browser,
version/build-list or Safari sign-in preflights. Preserve the pinned version/build
and upload journal; reconcile uncertain uploads before retrying. Handle a concrete
Apple rejection within existing attempt limits without inventing a new release.

Completion requires `ios_upload` in the release receipt: `status: "uploaded"`,
`source_commit` matching the pinned release, `version`, `build_number`, `upload_ref`
(Apple upload/build identity or verified receipt), `testflight_distributed: false`,
and `submitted_for_review: false`. An archive or simulator build is not an upload.
Record actual Apple acceptance. If either platform fails, retain partial delivery
evidence and report incomplete delivery; avoid repeating a successful publication.

## Explicit manual publication

The user authorized this additional path on 2026-10-31. An explicit request such
as "publish it now" authorizes immediate publication of the named, prepared
change. Do not require another confirmation or wait for the hourly scheduler.
Keep the hourly flow unchanged; ordinary implementation requests do not imply
this exception.

For an explicit full release, use the existing authenticated administrator
`POST /api/releases/start` with `repository`. It requires a fresh main observation
and reuses exact-target deduplication. Report its returned task ID; the eligible
publisher claims it normally. Do not invent an ordinary work task to bypass an
existing blocked/unknown release. Scoped website-only requests follow their
existing explicit authorization and artifact scope; do not widen them to an app
release. Check actual external state before retrying and preserve real gates.

For a website-only request, publish the reviewed website change while preserving
the latest public installer, release metadata and live infrastructure. Do not
bump/rebuild the app or mark unrelated app source as published. A full app release
still requires its exact signed/notarized artifacts and public verification.

## Polling

Only the existing **Nova — Queue Dispatcher** polls every 15 minutes. Legacy
independent worker polls remain paused. Its private current mapping identifies
Studio and VM developers; old names or queued heartbeats must not revive retired
workers. Keep this separate from the service's hourly release timer.

Dispatcher checks authoritative readiness and actual chat availability, then
wakes an eligible idle developer for a concrete task without duplicating a direct
Coordinator assignment. Workers self-claim and continue their existing claims
first. Pending approvals, unresolved dependencies and retry cooldowns are not
wake-up reasons. No extra Coordinator validation or peer approval stages.

After completion OR any stopped/blocked execution settles, immediately check
`next` once in the same run. Continue eligible tasks until none remain, then stop
until a dispatcher wake-up or direct human request. Do not create additional
worker or per-task schedules. Keep the lightweight capacity/repository reporters
running; these do not invoke models or execute engineering tasks.
Notify only completion, material failure, or a specific required human action.

## Legacy reference artifacts from oliver-mac

The historical Mini route uses the `nova-artifacts-mac` SSH alias for recordings in the
Mac intake root `/Users/oliverhu/Documents/ChatGPT/exports/nova-task-intake`.
It uses a dedicated local key, a verified host-key pin and fresh BatchMode
authentication without SSH-agent or ControlMaster dependence. Its scoped
`AddressFamily inet` setting matches the Mac's Mini source-IP restriction.
Do not assume that route is enrolled on Studio or a new VM. Verify its current
availability without restarting a retired worker. The source permits only this
read-only contract; do not use arbitrary shell
commands or SCP:

- `ssh nova-artifacts-mac ping`: connection/service JSON.
- `ssh nova-artifacts-mac stat 'RELATIVE/PATH'`: size and SHA256 JSON.
- `ssh nova-artifacts-mac fetch 'RELATIVE/PATH'`: raw file bytes.

Fetch into a private staging file, verify the task's expected byte count/SHA256,
then rename without overwriting an unrelated artifact. Use the relative intake
path from the task, e.g.
`20261031/download-experience/download-experience-reference.mov`. Never print or
transfer the private key, disable host-key checking, depend on an expired control
socket, or retry password prompts. Failed access is an artifact scheduling/repair
issue unless a real human consent decision is outstanding. Preserve active claims
and original reference acceptance.
