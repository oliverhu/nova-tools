---
name: nova-coordination
description: Submit, claim, execute and report Nova engineering tasks through the private Kubernetes queue on oliver-mac or oliver-mini.
---

# Nova task queue

The user selected this workflow on 2026-10-30. Both Macs are equal pull workers.
Use `~/.local/bin/nova-coord`; never display its private client configuration,
credentials or claim-token files. The central service at
https://192.168.2.13:31866 owns the queue. No peer task messages or coordinator
approval stages are required. Preserve genuine user stops and acceptance criteria.

## Work and ownership

Submit each new request once with `add --file /absolute/task.json`, using a
stable `request_key`, title, description/acceptance criteria, and one work step.
Only an actual engine rebuild uses `task_type: engine_rebuild` and heavy compute.
App builds, UI checks, large test suites, and ordinary features are standard work.
Engine rebuilds prefer the Mini; either Mac can take ordinary tasks that fit its
explicit resource requirements.

Claim with `next` or `next --task ID` and read the task and prior receipts.
A task is atomically assigned to one worker. There are NO global publishing,
main, desktop, or integration locks. Task ownership remains until the worker
reports an outcome: a missed heartbeat is shown as stale, not an administrator
hold and not a reason to give the task to another worker. Never duplicate a
running task or infer that an offline worker's processes stopped.

A routine worker implements, meaningfully tests, and merges its changes into
main. Rebase/resolve conflicts and rerun affected checks if main advances;
use normal non-force pushes. Record exact branch/commit/merge and evidence.
**DO NOT PUBLISH as part of ordinary work.** Automatic publication uses a
separate service-generated hourly publish task. Explicit user-authorized manual
publication follows the manual path below without waiting for the hourly check.

Report progress with `heartbeat ID --progress "..."`. Use `run ID -- command`
for long commands; its watchdog stops owned processes on service rejection or
unavailability. A quiet reasoning interval does not expire ownership. Stop on
an actual ownership rejection. Finish using `finish ID --receipt /path.json`
with summary and `execution_stopped: true` only after operations settle; use
`--outcome blocked` for a technical failure or `failed` for an unknown outcome requiring inspection. Use `--outcome pending_external_approval` when work waits on Apple, an external reviewer or an explicit human approval. Pending External Approval never retries automatically; do not resubmit approval requests. Resume only after actual approval is verified and the task is explicitly returned to the queue. A failed/blocked task does
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
and `worker` (oliver-mini or oliver-mac) and/or `dependencies` (existing task IDs).
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
single deduplicated publish task with an exact `release_target`; either Mac can
claim it. It does not build or publish by itself, and there is no publish lock.
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
Check existing automatic distribution settings before uploading; do not silently
change account-wide settings. Inspect existing uploads before a retry.

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

Record the user's instruction, exact source/artifact, destination and scope in
one deduplicated queue task. The current API reserves publish-kind tasks for the
hourly scheduler, so record an explicitly named manual-publication task as
`task_type: work`, claim it normally, and supervise execution with `nova-coord run`.
The explicit user authorization overrides its default ordinary-work no-publish
instruction. Do not mislabel it as a service-generated task or modify another
worker's claim. Check for active publication and inspect actual external state
before executing or retrying; preserve all release validation and real approval
blocks. Finish with a receipt identifying manual authorization, published scope,
public verification and settled operations.

For a website-only request, publish the reviewed website change while preserving
the latest public installer, release metadata and live infrastructure. Do not
bump/rebuild the app or mark unrelated app source as published. A full app release
still requires its exact signed/notarized artifacts and public verification.

## Polling

Keep the existing 15-minute queue-worker schedules. Continue active work rather
than launching a duplicate. After completion OR any stopped/blocked execution
settles, immediately check `next` once in the same run. If no eligible work is
available, stop quietly until the next wake. Do not create per-task schedules.
The capacity reporter does not run models or execute tasks by itself.
Notify only completion, material failure, or a specific required human action.

## Reference artifacts from oliver-mac

On Mini, use the persistent `nova-artifacts-mac` SSH alias for recordings in the
Mac intake root `/Users/oliverhu/Documents/ChatGPT/exports/nova-task-intake`.
It uses a dedicated local key, a verified host-key pin and fresh BatchMode
authentication without SSH-agent or ControlMaster dependence. Its scoped
`AddressFamily inet` setting matches the Mac's Mini source-IP restriction.
The Mac permits only this read-only contract; do not use arbitrary shell
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
