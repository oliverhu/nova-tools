# Nova service orchestration

The human-approved workflow as of **2026-11-07** uses Codex app workers and the
coordination service through `nova-coord`. No peer-chat task routing, status
relays, acknowledgment chains or CLI model execution for new work. Fresh chats
load repository instructions and service records without inheriting old chat
history. The old Coordinator and Dispatcher chats and dispatcher heartbeat are
retired.

## Roles

| Role | Responsibility |
| --- | --- |
| Service Coordinator | Intake, priorities, deduplicated submission, scoped human decisions, instruction policy/adoption and administrative outcome consistency. |
| Service Readiness | Observe service eligibility and concrete capability gaps; no worker wake-ups or claims on behalf of workers. |
| App worker | Self-claim, implement, validate, audit, integrate main, settle owned operations, finish and verify actual API outcome. |
| Publisher | An eligible app worker on a separate authorized release claim; ordinary features do not publish. |

Coordinator does not repeat developer acceptance. Service messages are durable
context, not ownership or proof of completion. Only the actual eligible worker
claims. Human approval remains required only for an actual outstanding boundary;
ordinary qualified merges do not introduce another approval stage.

## Enrollment and execution

Follow [App session protocol](app-session-protocol.md) for authenticated session
registration, policy/adoption, capability qualification, claim fencing and the
service inbox. Follow [Repo bootstrap](app-worker-bootstrap.md) for verified
instruction installation and local app wake. The installed
[nova-coordination skill](../skill/SKILL.md) preserves task lifecycle, acceptance,
release scope, resource and permission rules.

Each machine needs its own enrolled service credential; each chat has a distinct
session binding and private claim directory. Codex accounts can differ. Before
feature claims, qualify actual GitHub/service access and required runner contexts,
including Accessibility and Local Network where needed. An account label, VM
image, installed tool or copied skill is not readiness proof.

Each qualified worker has one supported local app heartbeat, normally every
15 minutes. It reads service state, resumes only its own claim or pulls eligible
work, and continues until none remains. Keep the machine/app available and test
the actual scheduled wake. Do not create per-task polling automations or wake
workers by chat messages. Retain lightweight capacity/repository reporters.

Post handoffs with `nova-coord inbox-post` and read with `nova-coord inbox`.
Include exact task/generation, changed state, evidence references, next action,
limitations and settled execution. Preserve dependency chains, attempt history,
real human holds and unknown effects. Verify `finish` and fresh `show` before
reporting completion; skipped or partial acceptance stays incomplete.

## Policy upkeep and cutover

Coordinator advances the approved repo revision and verified inventory through
the service. Workers install/read it at safe boundaries and acknowledge actual
adoption. Registration and installation do not establish permission readiness.
Retain active claims during migration; archive old sessions only after their
operations settle or through the explicitly audited abandonment route. Old
unknown effects are never relabeled successful cleanup. Live deployment,
installation, adoption and end-to-end acceptance are separate outcomes.

Large-text, largest-text, accessibility-size and Dynamic Type expansion checks
remain **skipped by human policy** from 2026-11-06 until changed by the human.
This does not change product behavior or grant OS/tool permission. Preserve other
checks, actual approval boundaries and unrelated schedules.

The [historical coordination reference](archive/agent-coordination-before-app-only.md)
is retained only to interpret old receipts. Its chat routing, dispatcher polling
and CLI-launch instructions must not be used for new work.

See [Task readiness and decision revisions](task-readiness.md) for scoped admission and human-decision fencing.
