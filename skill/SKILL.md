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
**DO NOT PUBLISH as part of ordinary work.** Publication happens only in a
separate service-generated hourly publish task.

Report progress with `heartbeat ID --progress "..."`. Use `run ID -- command`
for long commands; its watchdog stops owned processes on service rejection or
unavailability. A quiet reasoning interval does not expire ownership. Stop on
an actual ownership rejection. Finish using `finish ID --receipt /path.json`
with summary and `execution_stopped: true` only after operations settle; use
`--outcome blocked` or `failed` for incomplete work. A failed/blocked task does
not consume the worker forever. Its owner can use `retry ID --receipt /path.json`
after a concrete correction and proof of `execution_stopped`,
`external_state_checked`, and descriptive `evidence`. Never replay an unknown
publication or input outcome; inspect external state first. Old claims are fenced.
Administrator reconciliation remains available for abandoned legacy tasks.

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

## Fix-attempt limit

The user set the limit to **10 fix attempts per issue**, including current
feature and UI-tool repair tasks. This supersedes older lower limits in task
notes or worker prompts. Count attempts already used; do not reset the total on
retry, reassignment or a new test-run identity. For example, three used attempts
leave seven. Record the cumulative count in progress and receipts. Apply a
concrete correction before retrying, preserve the acceptance checks, and verify
previous execution has settled. If ten attempts fail, record the remaining
blocker and evidence. This changes the fix-attempt count, not acceptance criteria.

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

Only a claimed publish task may run the normal signed/notarized release flow.
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

## Polling

Keep the existing 15-minute queue-worker schedules. Continue active work rather
than launching a duplicate. After completion OR any stopped/blocked execution
settles, immediately check `next` once in the same run. If no eligible work is
available, stop quietly until the next wake. Do not create per-task schedules.
The capacity reporter does not run models or execute tasks by itself.
Notify only completion, material failure, or a specific required human action.
