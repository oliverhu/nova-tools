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

## Hourly publication

Every hour the service checks for a fresh observed main commit different from
the last verified publication. The background clients observe origin/main using
their existing Git access, without modifying checkouts. The service creates a
single deduplicated publish task with an exact `release_target`; either Mac can
claim it. It does not build or publish by itself, and there is no publish lock.
An outstanding publication for that repository is not duplicated; later merges
are picked up by a later hourly check after the outstanding task is resolved.

Only a claimed publish task may run the normal signed/notarized release flow.
Use `publish ID --receipt /new/receipt.json --command ...` or the existing
publisher with `publish-permit`. That check verifies task ownership, not a global
lock. Verify the actual public artifact. A successful receipt requires
`publication_ref` and `published_commit` equal to the pinned target. If release
metadata is committed on main, record its verified `release_commit` too, so that
metadata commit does not trigger another release. Do not silently include later
feature commits. Unknown outcomes require inspection before any retry.

## Polling

Keep the existing 15-minute queue-worker schedules. Continue active work rather
than launching a duplicate. After completion OR any stopped/blocked execution
settles, immediately check `next` once in the same run. If no eligible work is
available, stop quietly until the next wake. Do not create per-task schedules.
The capacity reporter does not run models or execute tasks by itself.
Notify only completion, material failure, or a specific required human action.
