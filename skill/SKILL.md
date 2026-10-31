---
name: nova-coordination
description: Submit, claim, execute and report Nova browser engineering tasks through the private Kubernetes coordination service. Use for Nova implementation, build, validation, integration, publishing and queue-status requests on oliver-mac or oliver-mini.
---

# Nova central coordination

The user selected a central service on 2026-10-30. `oliver-mac` and
`oliver-mini` are equal pull workers. Neither sends task messages, requests
resource grants, or hands work directly to the other. The Kubernetes service
is the source of task state, step instructions, scheduling and exclusive
publishing ownership. This replaces the former session-coordinator role.
Existing human stops, acceptance requirements and actual access restrictions
remain in force.

Use `~/.local/bin/nova-coord`. Credentials are already in
`~/.config/nova-coordinator/client.json`; never display that file or claim-token
files. The dashboard is at https://192.168.2.13:31866 and opens directly on the
LAN without a sign-in or access-token prompt. The certificate is signed by the internal Nova CA; the client verifies it with
its installed CA bundle. Browser trust is a separate one-time macOS setup. Automatic machine identity protects task claims;
`status` reports actual workers, tasks and locks.

For a new user request, submit a task with `add --file /absolute/task.json`.
The JSON contains `request_key` (a stable unique key reused on retries), `title`,
`description` including acceptance criteria, and `compute`: light, standard or
heavy. Use heavy for native/engine builds, large test suites and CPU/disk-heavy
work. Optional `requirements` can specify `memory_gb`, `disk_gb`, `cpus`,
`capabilities`, or a hard `worker` binding for machine-specific work.
Task dependencies are existing task IDs in `dependencies`.

A routine task has one Implement and verify stage, handled by one session.
Do not split planning, coding and testing into coordination handoffs or add
coordinator review/approval stages. Use `work` for development and a final
`publish` step when publication is authorized. Publishing is the only shared
exclusive lane; older `integrate` and `desktop` labels remain readable as work.
Any operation changing a production release, deployment or release pointer
belongs in a publish step, acquired only when ready to publish.

Existing worker sessions check the queue every 15 minutes while idle. Continue
current claimed work instead of starting a duplicate. After finishing a task,
immediately pull another in the same run; do not wait for the next scheduled
check. If there is no eligible work, stop quietly until the next wake. Keep
the existing worker schedule; do not create a new schedule per request.

Pull with `next` or `next --task ID`. Only execute after a returned task claim;
the service may route heavy work to the mini. Read the returned step and prior
receipts before proceeding. `show ID` retrieves the full record. No chat-to-chat
handoff is needed. Keep work in an isolated checkout and include exact branch,
commit, artifacts and verification evidence in the receipt so either Mac can
continue. Local paths alone do not transfer artifacts between Macs.

While executing, send `heartbeat ID --progress "..."` at least every 60 seconds.
For long commands use `run ID -- command args...`; its watchdog renews ownership
and stops its process group if the service becomes unavailable. Never continue
an operation after a heartbeat rejection. `run` does not itself mark a step done.

Finish with `finish ID --receipt /absolute/receipt.json [--outcome completed|blocked|failed]`.
The receipt needs `summary` and `execution_stopped:true` only after all owned
operations have settled; add evidence/commit/artifact references as appropriate.
Successful publishing also requires `publication_ref`, the verified external
result. For a task with a remaining publish step, immediately try `next --task ID`;
the service keeps that stage with the same healthy worker. If the publish slot
is busy, leave the stage queued and pull other eligible work. After the task
completes, immediately use `next` for the next task. Unknown publication outcomes
remain held, preserving the global lock. Do not retry an unknown publication.
Use `publish ID --receipt /new/receipt.json --command command args...` to run
a publisher under the lease watchdog and record its fresh receipt. Existing
publish entrypoints can call `publish-permit` immediately before their external
mutation; it checks `NOVA_COORD_TASK_ID` and the current global lock.

An expired lease is held, never silently reassigned. Reconciliation requires
administrator identity plus evidence that execution stopped and external state
was checked. Report a real blocker instead of bypassing a held task. An unrelated
eligible task can continue. Preserve old in-flight work and holds until explicitly
adopted; a service migration alone does not terminate a legacy process.

The local background agent reports capacity and polls an inbox; it consumes no
model tokens and does not execute work by itself. Work is executed by a chat
that pulls a claim or a supervised command. Do not claim that an idle inbox is an
autonomous build runner. Report completion, meaningful changes and actual user
actions; avoid repeated unchanged status messages.
