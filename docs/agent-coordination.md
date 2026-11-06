# Current Nova agent setup

This documents the human-approved workflow as of **2026-11-03**, after the Studio
migration and the instruction that developers report completion to Coordinator
without additional Coordinator validation. It supersedes older two-Mac and
Coordinator-retesting descriptions. Current explicit human instructions and genuine
task-specific holds still take precedence. This document does not change live
credentials, schedules, task ownership or deployment.

## Roles

| Actor | Responsibility | Boundary |
| --- | --- | --- |
| Nova — Delivery Coordinator | Intake, priorities, deduplicated task submission, assignment, human decisions, status and recording worker-reported completion | Does not repeat source review, runtime tests, UI checks or release validation. Does not claim as a developer or acquire another worker's claim token. |
| Nova — Queue Dispatcher | The existing 15-minute dispatcher checks authoritative readiness and mapped chat availability, then wakes an eligible idle developer for a concrete task | Does not claim for workers, duplicate Coordinator assignments, retest results or create additional polling schedules. |
| VM1 and VM2 Development Workers | Self-claim, implement in isolation, run meaningful acceptance, audit source, integrate main, settle operations and report directly to Coordinator | Own the full assigned result. No partial acceptance reported as complete; no publication during ordinary feature work. |
| Studio Heavy Development Worker | The same developer responsibilities for explicitly assigned host work and heavy compilation | Heavy work stays on the configured Studio host. Host capacity must account for running guests. |
| Publisher | An eligible developer temporarily executing a separately claimed hourly/manual release task | Validates and delivers the pinned macOS release and Xcode iOS upload, then reports to Coordinator. No TestFlight changes, App Review or public iOS release. |
| Queue service and background reporters | Atomic claims, generations, dependencies, retry budgets, capacity/branch observations and deduplicated hourly release creation | The service/reporters do not implement features, invoke models or validate runtime behavior. |

## Identities and routing

`coordinator/workers.py` recognizes the active worker identities
`oliverhu-mac-studio`, `oliverhu-mac-studio-vm1` and `oliverhu-mac-studio-vm2`.
`oliver-mac` and `oliver-mini` remain for retained history/explicit rollback; their
old reporters and independent idle-poll schedules must not be revived by setup.
The private live route map binds each worker to an actual remote host ID, chat ID
and canonical project path. Do not commit that map or copy IDs to a new machine.

Coordinator and Dispatcher are **chat roles**, not extra worker credentials.
The installed `nova-coord` client's identity determines which queue actions are
allowed; renaming a chat does not change it. A new machine needs a distinct
supported/enrolled worker identity and credential, or an explicitly settled
replacement. Adding an arbitrary worker name only in a manifest is insufficient:
the current server, bootstrap and installer use an explicit known-worker list.

`COORD_HEAVY_WORKER=oliverhu-mac-studio` reserves heavy compilation for Studio.
Only `task_type: engine_rebuild` selects the heavy scheduling class. Large test
suites and routine app builds remain standard unless their actual resource
requirements route them to the host. Heavy tasks wait when Studio is unavailable;
they do not fall back to guests. Historical host limits were 14 CPUs/48 GiB while
two 8-vCPU/24-GiB guests ran; remeasure before changing allocation.

## Request through completion

### CLI workers on additional guests

An explicitly enrolled CLI guest may use `nova-cli-worker status` and
`nova-cli-worker wake --task ID` instead of a desktop chat wake. This is a one-shot
route invoked by the existing Dispatcher, never another poll or worker schedule.
Private `~/.config/nova-coordinator/cli-dispatch.json` binds the hostname, worker,
repository, signed CLI executable, expected ChatGPT email hash, model, reasoning
and `admission: enabled`. Authentication remains in the guest's saved CLI login;
the route removes inherited OpenAI/Codex auth overrides and checks the effective
account, model/reasoning catalog and current usage before starting. It never
falls back to another account or model.

Dispatcher checks actual task-relevant readiness and instruction adoption before
each exact wake. The guest worker self-claims, performs actual acceptance and
finishes under its own claim. The route checks identity, capacity, prerequisites,
pause state and duplicate ownership, and excludes heavy work and publication.
Unqualified GUI, Browser, signing and simulator contexts remain unavailable until
qualified by an explicit onboarding task. A shell success does not admit them.

Each run persists private events, worker report, exact finish response and fresh
API readback in `~/.local/state/nova-cli-dispatch/RUN_ID/`. Dispatcher reads these
and the authoritative API and reports directly to Coordinator under the existing
communication authorization. Cross-account desktop messaging is not required.
The worker stops after the assigned task so the existing Dispatcher can select
the next exact eligible task. There is no unfiltered claim loop. A process exit,
unknown effect, failed model run or incomplete acceptance leaves the run needing
attention; it cannot be automatically replayed. Preserve evidence and reconcile
the actual owning claim/operations before a supported manual recovery. A kernel
file lock prevents concurrent duplicate supervisors, including across SSH loss.

`resume --run-id ID --note-file /absolute/private-note` explicitly continues an
ended blocked/failed run in its original recorded CLI session. It requires the
same owner, matching finish/API generation, stopped execution and a concrete
correction note; the worker still inspects external state and performs its own
supported retry and exact claim. Running/unknown operations and approval waits
are not automatically resumed. Historical failed-run evidence remains intact.
`scripts/cli-worker-status.py --host SAVED_SSH_ALIAS` reads current task progress
and the last worker message without starting a run. It can query several aliases
in parallel and makes no scheduling or claim changes.

A fully settled blocked task must not occupy the whole worker. After reading the
actual owner evidence, Dispatcher may use `settle --run-id ID --note-file RECEIPT`
to admit other work. This changes only local dispatch admission, never queue
ownership, outcome, approvals or retry counts. The private JSON receipt must bind
`run_id`, `task`, `generation`, `worker`, positive `execution_stopped`,
`external_state_checked`, `input_settled`, `unknown_operations: []` and descriptive
`cleanup_evidence`. The actual owning finish and fresh non-running API state must
match. Missing evidence or unknown effects keep admission held. A later eligible
blocked retry uses the service's actual readiness and current owner; the worker
reads prior receipts and makes a concrete correction before repeating operations.

A genuine queued dependency wait can also settle local admission. Its owning
`nova-coord wait` response may be retained at `finish-response.json`; that filename
does not make it a successful finish. Preserve the actual input at
`wait-receipt.json` and the same run's `worker-report.json`. The adapter requires
an ended run, matching task/generation/step and last owner, an unowned queued
response with unchanged nonempty dependencies, a stopped worker report and
stopped/checked wait input. The fresh API must contain the matching owning
`waiting` receipt and worker audit event. `result` may remain empty after a defer.
The same positive cleanup/input/external-state note is still required. Missing,
foreign, stale or contradictory evidence keeps admission held. Settlement never
completes or retries that task, changes its dependencies/counters/holds, resumes
its old model session or starts the next task; Dispatcher selects new work.

An ended run that failed locally before its first claim needs distinct evidence:
an explicit `unclaimed_stopped` worker report with stopped/checked cleanup, no
app/simulator/source effects and a failed, unrepeated local claim attempt; a
truthful `finish_invoked: false` marker; and the retained task readback. The fresh
API must still match a never-owned generation-zero task, original submission
history, empty result/receipts and zero attempts. The exact positive operator
settlement note remains mandatory. This releases only local admission; the
marker is never treated as a service finish or owning defer.

For a truthful readiness stop **before any claim attempt**, use the separate
`nova-cli-worker reconcile-preclaim --run-id ID --note-file /absolute/note.json`
operation. Do not invent a failed claim or finish marker to satisfy `settle`.
The existing exclusive worker lock must be free. Only the latest ended
`needs_attention` run with CLI exit zero is admitted, with its canonical original
report and task readback, no claim/finish/wait artifacts, and the explicit
`queued_prerequisite_not_claimed` outcome. The owner must report no claim or
finish attempt, stopped execution, empty runtime/input operations, no app/server,
consent or source changes, and preserved unrelated work. The fresh API must
match a never-owned queued generation-zero task, expected worker, unchanged
dependencies/submission history, empty result/receipts and zero counters.

The operator note binds `run_id`, `task`, `generation` and `worker`, with
`execution_stopped`, `external_state_checked` and `input_settled` all true,
`unknown_operations: []` and descriptive `cleanup_evidence`. Real approval holds,
unknown effects, contradictory reports, changed claims or evidence keep admission
held. Reconciliation retains a byte-exact `preclaim-state-before.json` and private
`preclaim-reconciliation.json` audit, then changes only local admission to
`settled` while preserving `prior_status: needs_attention`. It performs no service
mutation, model launch, claim, retry or finish. The missing readiness plan or OS
consent still needs its own actual qualification before another exact-task wake.

When a sandbox explicitly denies the client's local memory query, `next` may
use its existing own resource reporter from the authoritative service. It must
be online, unpaused and less than 90 seconds old, with finite positive CPU/memory,
known disk capacity, configured guest limits and capabilities respected. The
client records this resource evidence and uses the normal atomic service claim;
it does not write substitute capacity. Unknown/stale resources and other query,
configuration or service failures still stop claiming. The separate reporter
continues normally. This changes no sandbox/OS permissions or task-specific
UI, signing or simulator readiness, and never authorizes an automatic retry.

1. Coordinator submits once with a stable `request_key`, concrete acceptance and
   real dependency IDs. Report the **service-returned task ID** to the user.
2. Coordinator may assign directly; Dispatcher handles eligible idle wake-ups
   without duplicating assignments. Read actual queue and chat state, not an old
   summary or a missed heartbeat. A running claim retains ownership.
3. The developer self-claims with `next --task ID`, reads prior receipts and does
   implementation, testing, required source audit and normal non-force main
   integration in its own checkout. If main advances, resolve and rerun affected
   checks. Preserve original acceptance and unknown operation outcomes.
4. Send the result **directly to Nova — Delivery Coordinator** using the current
   private route and applicable standing Nova-to-Nova messaging authorization.
   Include task ID/generation, outcome, exact branch/base/landed commit, actual
   tests and remaining limits, receipt path, and settled execution/cleanup.
   A pre-finish evidence report is pending service completion; reserve completed
   reports for the verified API outcome below.
5. Coordinator records the reported evidence without an additional review or
   test pass, and records completion only after the API check below. Respect the
   API's claim fencing: `finish` authenticates the owning
   worker and claim. When needed, Coordinator directs that owner to submit the
   supplied receipt through `nova-coord finish`; the owner returns the service
   readback. Never copy claim tokens, impersonate a worker, or mark a running task
   blocked merely to make administrator reconciliation possible.
6. The owner finishes with an explicit `--outcome` matching any receipt `outcome`.
   Client and service reject contradictory outcomes and completion with
   `acceptance_complete: false`. Omission honors an explicit receipt outcome;
   the legacy completed default applies only when the receipt has no outcome.
   Before developers or Dispatcher report **completed**, inspect both the finish
   response and a fresh `nova-coord show ID`: expected task/generation, completed
   final step/status, stopped execution and no contradictory receipt outcome or
   acceptance flag. A successful command, intermediate step or partial test pass
   is insufficient. Report actual blocked/failed/queued/pending states and limits.
7. Coordinator performs this administrative API/receipt consistency check before
   recording completion, without product retesting. Dispatcher verifies current
   API outcomes for completion and dependency readiness. Preserve contradictory
   or uncertain finish records and route them to Coordinator for the existing
   supported audited correction; never replay stale finishes, edit the database,
   erase history or reset counters. Once a claim settles, its owner may continue
   eligible work under current policy without interrupting another claim.

The coordinator's lack of retesting does not reduce developer acceptance. Repair
tasks remain separate, deduplicated prerequisites; a tool repair's completion does
not complete its dependent feature. No extra permanent integration/reviewer chat
or acknowledgement chain is required.

## Timers, retries and release scope

The existing Dispatcher wakes every 15 minutes. Lightweight capacity/branch
reporters remain active. Independent legacy worker polls remain paused. Onboarding
does not create per-worker/per-task timers. A separate existing service timer
checks fresh main observations every hour and creates one immutable release task
per new target. No new changes means no release. Do not add a second hourly chat
automation for the same repository.

The service currently allows 20 fix attempts per host assignment and six host
reassignments. Automatic technical retries stay with the current host until its
budget is used; hard worker requirements, dependencies, stops and documented
one-attempt human exceptions still apply. Read current task counters/capabilities
rather than restoring stale configuration. Pending External Approval does not
retry automatically. Missed heartbeats do not release claims.

The November 3 human instruction authorizes separate hourly publishers to build,
sign, notarize/staple and verify the macOS installer, publish installer plus
matching website metadata, and archive/upload iOS through the existing signed-in
Xcode account. The publisher does the validation and reports actual acceptance
and settled operations. **Do not touch TestFlight**: no distribution, tester/group
or settings actions; no App Review or public iOS release. Do not add routine App
Store Connect browser/list preflights to the Xcode upload. Reconcile uncertain
upload/publication outcomes before retrying and never downgrade a newer release.

New combined Nova macOS+iOS publisher tasks use the existing hard requirement
`worker: oliverhu-mac-studio`, for hourly and explicit starts. Studio currently
has qualified Xcode upload readiness; a generic development heartbeat does not
qualify VM1 or VM2. Normal claim eligibility enforces the route, including
unfiltered `next`, and waits if Studio is busy/offline/paused. Coordinator keeps
the actual readiness baseline current and revises routing only when another
publisher is qualified. No account probe, extra approval, timer or peer review
is added. Existing claims/holds/targets are preserved rather than regenerated;
ordinary developer work and other repositories retain their current routing.

These are Nova release-task instructions, not permission to publish while doing
onboarding or updating this repository. Source push, client install, service
deployment, macOS delivery and iOS upload are distinct actions.

## New host or VM handoff

Use [the portable setup skill](../skills/mac-vm-setup/SKILL.md) for storage,
toolchain, permissions and functional readiness. The setup owner provides a
capability ledger and exact host/project/worker mapping, not copied readiness
flags. The developer performs any assigned qualification in its real guest chat;
Coordinator records the report without repeating it. Dispatch requires the
capabilities actually needed by that task, not merely an online heartbeat.

See [the coordination skill](../skill/SKILL.md) for claim/approval commands and
[new-Mac setup](new-mac-setup.md) for provisioning and explicit installation.

## Coordinator owns instruction synchronization

Coordinator also maintains the private [host identity baseline](../skills/mac-vm-setup/references/host-baseline.md)
and per-host readiness/drift receipts. The November 4 user choice shares Studio's
exact GitHub client key with both VMs; it does not share queue credentials, SSH
server host identities, app sessions or consent. Keep actual GitHub authentication,
repository read, signing/provisioning parity and release capabilities distinct.
Use existing coordination activity after onboarding or material changes; no new
polling schedule. Missing release readiness does not block unrelated development.

Permission onboarding is a prerequisite to new feature assignment and pickup:
check the mapped worker's actual report for every planned app/runner context,
including Local Network when required. Skill-install receipts do not establish
permission readiness. Resolve missing access under the single existing onboarding
task and retain it as a dependency on affected queued/new work. Preserve active
claims and let the owner settle operations before deferral. An explicit onboarding
assignment may perform setup; feature acceptance must not discover consent on the
fly. See [permission onboarding](../skills/mac-vm-setup/references/permission-onboarding.md).

The user explicitly assigned ongoing upkeep to Coordinator on November 3, 2026.
The source of truth is the current user-authorized workflow in `nova-tools/main`:
`skill/SKILL.md` installs as `nova-coordination/SKILL.md`, and the complete
`skills/mac-vm-setup/` directory installs as `mac-vm-setup/`.

The browser repository's `NOVA_CHAT_ROLES.md` and `AGENTS.md` ownership guidance
are also managed instructions. Coordinator synchronizes them from a verified
`nova-browser/main` commit to the canonical Studio/VM projects, preserving local
differences and recording destination hashes. Keep their source revision separate
from the tools/skill revision. Coordinator, developers and Dispatcher reread the
changed role documents as well as the installed skill and confirm adoption at a
safe boundary. Older task checkouts load the current canonical policy and carry
the documentation change through normal integration when safe; do not rewrite
frozen artifacts or interrupt active claims to update instructions.

After policy changes land, new enrollment/reconnection, or a client upgrade:

1. Resolve the approved source commit in a separate clean checkout or verified
   package; preserve developer worktrees and staged work.
2. Compare all source skill files against installed Studio/VM1/VM2 files. Back up
   differing local bytes outside discoverable skill folders, install only these
   two skills, and verify SHA-256 readback on the actual destination. Preserve
   unrelated skills, local credentials, claims, schedules and OS consent.
3. Store a private per-host receipt with source commit, expected/observed file
   hashes, installation paths and timestamp. Keep the expected revision/current
   host/chat routes in Coordinator's private operational state, never in Git.
   Track instruction adoption separately from client/service code revisions;
   Coordinator maintains the finish-outcome/API-readback rule as part of this
   expected instruction baseline. Refresh the existing client distribution bundle
   after skill changes so upgrades cannot restore older instructions.
4. Notify all developers and Dispatcher to reread the installed coordination
   skill at a safe boundary without stopping ongoing work. Obtain readback of the
   revision/coordination hash. Message delivery and installed bytes are separate
   from confirmation that an active chat loaded the instructions.
5. Dispatcher compares the mapped worker's receipt with the expected revision
   before new assignments and reports stale/missing state to Coordinator. Repair
   the instructions without claiming work for another actor or inventing approval
   gates. A client update can replace a skill from an old bundle: recheck it.

Use the existing coordination and dispatcher activity for this upkeep. No new
timer, worker poll, app restart or service deployment is required. This does not
authorize Coordinator to retest developer output; content synchronization and
developer acceptance remain separate responsibilities.
