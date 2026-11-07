# Nova developers, coordinator and dispatcher

Current Nova workflow as of November 3, 2026:

| Role | Work |
| --- | --- |
| Delivery Coordinator | Intake, priorities, task IDs, assignments, human decisions, status and recording completion from developer reports; no extra source/runtime/UI/release validation |
| Queue Dispatcher | Existing 15-minute readiness check; wakes an eligible idle mapped developer for a specific task without duplicate assignments or claiming for them |
| VM1/VM2 developers | Self-claim; isolated implementation, actual acceptance, source audit, main integration, cleanup and direct completion report to Coordinator |
| Studio heavy developer | Same ownership for assigned host work; configured heavy compilation stays on Studio and accounts for guest resource use |
| Publisher | A qualified developer on a separate release claim; performs actual delivery checks and reports to Coordinator |
| Service/reporters | Atomic claims, dependencies, retries, heartbeats and hourly release creation; no model or feature execution |

The active service worker IDs are `oliverhu-mac-studio`,
`oliverhu-mac-studio-vm1`, and `oliverhu-mac-studio-vm2`. Legacy `oliver-mac` and
`oliver-mini` retain history; don't reactivate their old schedules. These are
existing deployment identities, not names to reuse on arbitrary new machines.
Coordinator/Dispatcher are chat roles, not additional queue workers. Keep actual
chat/remote-host/project mappings and credentials in private machine state.

A new concurrent machine needs a distinct identity supported by the current
server/bootstrap/installer and enrolled with its own credential. A replacement
must first settle the previous owner and reporter. The VM bootstrap must not
copy another guest's worker credential, SSH host identity or app consent record.
The explicitly selected shared GitHub client key is separate from these unique
worker identities; follow the [host baseline](host-baseline.md).

Configure `COORD_HEAVY_WORKER` through the actual deployment process, not by
editing a local manifest and assuming the server changed. For Nova it selects
Studio for `engine_rebuild`; standard tasks still obey explicit capacities and
required capabilities. Missing Studio capacity does not authorize guest fallback.

Developers send Coordinator the service task ID/generation, exact landed commit,
real test results, limitations, receipt path and stopped-operation evidence.
Coordinator records completion without rerunning validation. The owning worker
still uses its claim-compatible `finish` path when directed; administrative role
does not justify copying a claim token or fabricating a blocked state. Confirm
service completion separately from receipt delivery.

Preserve the existing Dispatcher and separate hourly service release timer.
Do not create extra polls while provisioning. Hourly publishers deliver pinned
macOS installer/website metadata and iOS uploads through Xcode; they leave
TestFlight, App Review and public iOS release untouched. This is release-task
scope, never automatic follow-on from machine setup. Retain task-specific human
holds and reconcile unknown effects before retries.

The current service exposes 20 attempts per host and up to six reassignments;
read its actual counters and policy before execution. A missing heartbeat does
not relinquish a claim, and an approval wait is not a technical retry.

Coordinator owns keeping both installed skills current on Studio and both VMs.
After a source workflow change or worker enrollment/reconnection, synchronize from
the verified `nova-tools/main` commit, preserve prior local bytes outside skill
discovery, verify installed file hashes and record a private per-host receipt.
Notify developers and Dispatcher to reread the coordination skill at a safe
boundary and record their revision/hash readback. Dispatcher checks the expected
revision before new assignments and reports stale installations to Coordinator.
Recheck after client upgrades; use existing activity rather than another timer.
This maintenance neither restarts active work nor adds Coordinator product tests.

When this skill comes from `nova-tools`, read its current `docs/agent-coordination.md`
and `skill/SKILL.md` for the operational details. The portable folder remains
self-contained without them. On another organization/deployment, retain the
separation of responsibilities but discover its real roles, authorization and
queue policy instead of importing Nova's schedules or release authority.

## Compact actionable communication

Use one handoff containing task/generation, changed state or requested action,
actual evidence/receipt paths (and source commit when relevant), next action and
its owner, remaining limits and cleanup. Preserve current acceptance, claims,
dependencies, human stops and unknown operations. A handoff is not a claim,
approval, qualified result or completed service transition.

Act on an actionable request within existing authority. Send another message only
for a material state change, concrete blocker, required decision or requested
readback. Do not reply merely “acknowledged”, echo unchanged status or request an
acknowledgment of an acknowledgment. No new chats, relays, timers or polls. This
does not suppress required service/message protocol acknowledgments, actual hash
adoption readbacks, approval receipts, or finish-response/fresh-API verification.
Delivery, installation, actor adoption and actual acceptance stay distinct.

Instruction upkeep uses the existing Coordinator activity. Record each real host's
source commits, expected/observed installed hashes and verification time; record
each actual actor's loaded hashes/run or chat separately at a safe boundary.
Unobserved installation or adoption stays pending with its owner and next action;
do not copy another host's receipt or ask actors to acknowledge unchanged bytes
again. Dispatcher reports changes to these records, not repeated reminders.
