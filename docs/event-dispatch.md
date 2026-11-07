# Event dispatch for enrolled Mini workers

This is an opt-in addition to the existing coordinator capacity agent and CLI
launcher. It creates no LaunchAgent, timer or worker identity. The 15-minute
Dispatcher remains the recovery path. Desktop workers keep their existing route.
Enrollment is off unless an administrator binds a qualified readiness revision.

## Delivery and settlement

The existing agent has one locally locked subscriber. Its authenticated bounded
HTTP wait sleeps on committed service changes. Dependency completion and actual
supervisor settlement wake it; a reconnect checks the durable queue. The
subscriber calls the normal exact-task launcher, with the selected generation.
It never supplies a model prompt, account, interpreter or alternate runner.

The normal launcher preserves account/model/usage checks, original local
`worker.lock`, prior-run reconciliation, and task eligibility. Both event and
manual launches reserve the same service record before starting a model. The
reservation binds worker, task, queued generation, local run, readiness revision
and a private token hash. Partial unique indexes and the existing SQLite
transaction prevent concurrent event/fallback reservations and foreign claims.
The actual CLI thread joins its exact reservation when it self-claims.

The token stays in a separate owner-only local file. Status, task progress,
service events and public projections contain neither the token nor its hash.
An exclusive model-spawn intent precedes the sole launcher invocation. A lost
response, interrupted start or unknown operation leaves the original admission
unresolved. Time passing never expires it into another model wake.

Task finish does not release the wake. The supervisor first reaps its CLI child,
closes its existing message ledger and runs the existing `cli_results` reducer.
It then publishes the final state and releases its inherited worker lock. Only
then can it send the exact run/result hash and positive settlement acknowledgement.
Changed evidence, unread messages, unknown effects, a live lock or a changed task
generation refuses that acknowledgement. The owning report and raw executor
flags remain unchanged. The acknowledgement enables other eligible tasks;
pending approval work itself remains ineligible.

An expected busy/stale race returns without a model or new admission. An actual
one-shot wake refusal stops that automatic subscriber and records a
bounded local incident. The existing Dispatcher inspects the original actor/run
boundary and applies a concrete correction before recovery. It must not erase a
reservation, replay a model-spawn intent or clear unknown effects on elapsed time.

## Readiness and rollout

`cli-dispatch.json` may contain `event_dispatch` with `enabled`, an owner-only
`readiness_manifest` path and its `readiness_revision` SHA-256. The manifest binds
schema 1, worker, hostname, saved-account hash, model, reasoning and exact installed
source/instruction file hashes. It carries no login or API credential. Existing
onboarding qualifies the planned contexts; this manifest never grants permission.

Under the existing guest worker lock, verify the latest typed result or audited
settlement, preserve installed module bytes, and stage the reviewed client change.
Keep service enrollment disabled during this preparation. Use the existing
reporter lifecycle to load its new optional subscriber; do not change schedules
or restart a live CLI. Only after actual owner qualification should the admin
configure the matching Mini revision through `POST /api/workers/WORKER/dispatch`.
Configuration changes refuse while a wake or running task is owned.
An optional `task_ids` list restricts initial qualification to exact existing pilot
tasks; an empty list admits none. A null/omitted filter uses normal eligibility.
Pilot enrollment does not authorize waking unrelated queued tasks.

The worker reports its local availability through `/api/worker/dispatch/report`.
The service also requires a fresh ordinary resource pulse, unpaused worker,
matching requirements/capabilities, original task dependencies, attempt policy and
readiness revision. A fresh heartbeat alone never establishes local availability.
Unknown or stale local state remains unavailable.

Use the supported immutable-source deployment with an exact live ConfigMap and
resource-version comparison. Preserve unrelated service changes and all dashboard
files. Deploying source, installing clients, enrolling workers, actual dispatch
acceptance and visible UI qualification are separate recorded outcomes.

## Status and metrics

`workers[].event_dispatch` is separate from the existing dashboard owner's
`worker_status.py`/CLI-status contract. It contains enrollment, availability,
staleness, issue and latest exact task/run/generation/phase metadata. Latest
progress comes from the authoritative queue, never private model messages,
prompts, stderr or transcripts. Its task-detail link resolves through the
existing service and does not publish guest filesystem paths.

Queue delay is task creation to actual claim. Elapsed time is reservation to
settlement/current time. First-test latency is claim to the first explicit
`nova-coord heartbeat TASK --phase testing`; absence stays null. Implementing and
cleanup phases are also explicit. Harness failure counts use an optional explicit
`failure_classification: harness` in the owning report; unclassified results do
not become zero failures. Manual approval count includes only audited
`approval-applied` events for the worker's dispatched tasks. It is not a count of
every possible human interaction. These observations never establish feature
acceptance or a latency guarantee.

The independent dashboard task retains actual UI acceptance and its Work Browser
hold. This module does not authorize integrating its prepared UI or using another
browser route. Large-text checks remain skipped by human policy.
