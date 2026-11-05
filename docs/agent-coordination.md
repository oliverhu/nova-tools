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
