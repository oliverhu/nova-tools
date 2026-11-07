# Nova app workers and service orchestration

The human-approved workflow as of **2026-11-07** uses Codex app chats and the Nova
service through `nova-coord`. All task intake, progress, handoffs, decisions and
outcomes live in the service. Do not message peer chats, revive retired
Coordinator/Dispatcher routes or launch a Codex CLI model for new work.

| Role | Work |
| --- | --- |
| Service Coordinator | Intake, priorities, deduplicated tasks, scoped human decisions, administrative outcome checks and instruction upkeep; no repeated product validation. |
| Service Readiness | Observe eligibility, adoption and concrete readiness gaps; no worker chat wake-ups or claims on their behalf. |
| App developer | Self-claim, implement/validate in isolation, audit/integrate main, settle execution, finish and verify actual API outcome. |
| Publisher | Qualified app developer on a separate authorized release claim; ordinary setup/features do not publish. |

## Enroll a machine and fresh chat

Use a unique supported Nova worker credential and SSH host identity for each
machine. Keep the selected shared GitHub client key separate; follow the
[host baseline](host-baseline.md). Codex accounts may differ. Do not copy another
worker's queue credential, active claim, session token or OS consent state.

Inspect current service ownership before replacement. Existing operations must
settle or use the explicitly audited abandonment route; historical unknown
resources remain unknown. Create a fresh local app chat through supported app
creation tools/UI with repo/service bootstrap context, not old chat history.
Archive superseded sessions only after this boundary. A new chat does not inherit
another session's execution authority.

Obtain the approved instruction commit from authenticated service policy. Fetch
that exact repository revision through verified GitHub access, install both
managed skills, preserve differing local bytes outside skill discovery, and
verify the complete installed hash inventory. Read the current instructions and
record actual adoption. Keep source, deployment, installation and adoption
receipts separate. A successful sync is not machine readiness.

Register the actual chat/host/account binding through the app-session API and
keep its private session file local. Qualify actual task-relevant tools and
permissions under [permission onboarding](permission-onboarding.md), including
GitHub/service access and planned runner contexts. Administrator qualification
records evidence; it never grants OS consent. Missing protocol deployment or
unknown/denied required access keeps the worker onboarding-only.

## Local wake and task loop

Provision one supported local app heartbeat in the worker's own account, normally
every 15 minutes, only within the user's authorized setup. Test an actual wake.
The machine and app must be available. A shell poll, copied automation or running
app alone does not prove it can start a chat turn. Retire the old Dispatcher wake
and CLI model paths; preserve the separate hourly service release timer and
unrelated schedules. No per-task monitors or peer-chat assignment loop.

Use the session file on `nova-coord` task commands. Read the service/inbox, verify
current policy/adoption/readiness, then resume only that session's own claim or
atomically pull eligible work. Continue until none remains. Post durable service
handoffs containing task/generation, actual outcome, evidence, next action,
limitations and settled execution. Verify finish plus fresh API readback before
completion. Delivery, installation, adoption and actual acceptance are distinct.

Studio remains the configured engine-rebuild host; routine work uses actual
capabilities/capacity. Preserve existing attempt budgets, dependencies, human
holds and uncertain outcomes. A stale heartbeat does not release ownership.
No task token transfer, fake completion, TCC edits or new blanket permission.

When installed from nova-tools, the current `docs/app-session-protocol.md` and
`docs/app-worker-bootstrap.md` provide exact API/CLI schemas. This portable guide
still applies without those files; discover another deployment's real policy and
enrollment mechanism rather than importing Nova worker IDs or release authority.
