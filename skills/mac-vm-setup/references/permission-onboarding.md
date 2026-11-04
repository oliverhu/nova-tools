# Fewer prompts through permission onboarding

The Nova user asked to scrutinize test/app requests, minimize interruptions and
bring predictable permission setup into onboarding. Plan by workload and actual
requester. A machine-wide “all permissions” flag is not useful evidence.

## Scrutinize before requesting

For each expected test/app operation, record the actor's path, bundle ID and
signing requirement, launch context, target app/category, why the operation needs
access, existing grant/evidence and the smallest missing human action. Keep this
private in the machine manifest's `permission_inventory`.

First distinguish these causes:

| Observation | Appropriate response |
| --- | --- |
| Model/unit test touches real browser data, protected folders or Keychain unnecessarily | Use owned temporary fixtures and injected/fake services; keep real access only for required integration acceptance |
| New nonce/worktree/build creates another requester | Reuse the registered runner family and unchanged valid signing requirement; keep run identity in private data and receipts |
| Wrong app, Bridge/local route or desktop context | Repair the intended supported route; do not ask for broader grants or switch actors around a denial |
| Actual missing category for a required operation | Add one precise setup item for the actual actor before the test lifetime |
| Partial AX tree, absent window, focus mismatch or timeout | Inspect readiness/target/operation evidence; do not infer a consent failure or replay a consumed action |
| Existing denial or unresolved request | Preserve its result, settle the original operation and report the exact prerequisite; no repeated prompt loops |

Read the current Nova `Config/UITestSuites.json`, `Config/UITestRunners.json`,
`Tests/Support/UITestRunnerPermissions.md` and `UITestReliability.md`. Their
registered identities and current setup commands take precedence over historical
examples. New general tests use `browser-features` / **Nova Feature Test Runner**.
Use a specialized existing family only when its test contract requires it; do
not merge existing families or impersonate the production app to inherit grants.

## One planned session per machine

Choose the capabilities expected on that worker and list missing consent before
development begins. Group the session and explanation, not the OS decisions:
macOS or Codex may still require a separate human response for each actor/category.
Do not promise zero prompts after upgrades or identity/context changes.

| Capability | Scope to prepare |
| --- | --- |
| Peekaboo UI work | Selected actual host's Accessibility and Screen Recording; use status before request |
| Synthetic pointer/background keyboard input | Event Synthesizing only when the selected actions need it |
| Runner reads of guarded build storage | Selected stable runner families' permission-only LaunchServices preflight |
| Local Network | Actual stable app/runner and helper responsibility, user account and normal GUI launch; verify an intended operation against an owned destination before feature work |
| Codex Browser / Computer Use | Actual worker chat, app connection and required target-app first-use access; qualify before timed UI operations |
| Product camera, microphone, Bluetooth/passkey, notifications or automation | Only categories exercised by the intended workload, on the actual product/test identity using supported setup; otherwise not requested |
| Signing/keychain | Selected identity and actual execution context, only when signing is needed; keep account enrollment separate |

Peekaboo uses native UI APIs and does not require generic AppleScript Automation
permission. Event Synthesizing is action-specific. Inspect the selected source
with `peekaboo permissions status --all-sources --json`; do not mix Bridge and
local grants. [Official permission behavior](https://peekaboo.sh/commands/permissions.html).

The `nova-tools` setup configuration selects the local Peekaboo categories and
runner families before applying anything:

```json
"permissions": {
  "peekaboo": ["accessibility", "screen-recording"],
  "runner_families": ["browser-features"]
}
```

These are the defaults for configurations without the optional section. Add
`event-synthesizing` for the applicable input workflow and select the other
registered families needed by planned suites. Use empty arrays for a headless
worker. The read-only setup plan lists the selected categories and runners;
`--apply` requests only observed missing selected categories in the logged-in
interactive desktop. Existing grants need no extra confirmation. There is one
grouped completion pause for missing local grants, no per-runner Return prompts,
and no automatic request from SSH/background test execution. The local Peekaboo
phase does not qualify a Bridge route or Codex app access.

## Local Network is a separate prerequisite

The November 4 human screenshot showed **“Allow NovaFeatureCheck to find devices
on local networks?”**. This identifies the displayed requester and Local Network
category; it is not an Allow response or proof of the requester's PID/bundle.
Keep the source screenshot/private receipt outside Git. Attribute the actual
main/helper process before arranging any missing human action.

Use System Settings > Privacy & Security > Local Network and the supported
operation in the actual signed app/runner's normal launch context. Apple notes
that macOS tracks the responsible app and has user-specific Local Network state.
Shell/SSH connectivity can have different treatment, so it does not qualify a
GUI app. A loopback fixture or a usage-description string alone is not proof of
the app's Local Network access. See [Apple TN3179](https://developer.apple.com/documentation/technotes/tn3179-understanding-local-network-privacy).

For planned LAN work, qualify only an owned, reachable test destination or the
explicitly needed service; do not scan/discover unrelated devices to provoke a
dialog. Record the actual operation/result and the responsible stable identity.
Readiness remains pending if actual app attribution, operation result or cleanup
is unknown, or the required operation is denied. A successful intended operation
in the exact GUI app context with settled cleanup may qualify **observed allowed
access** even when no prompt or human decision was observed. Record operation
access, permission UI and human response separately; an unobserved human decision
alone is not a new prerequisite and must not cause another consent request.
Do not infer a saved grant, a human click or access for other programs/contexts.

When the user's scope includes reuse, an operation-only repeat can use the same
immutable signed app, executable, bundle ID, designated requirement, UUID and
normal launch route with fresh private session data. Keep its existing bounded
supervisor and settle its app/server operations. A prior canceled endpoint stays
closed; a separately arranged repeat uses a newly bounded owned endpoint, not an
extension or retry loop. This qualifies only the actual repeated operation/context.

For a workflow requiring no LAN access, remove unintended
discovery from that test path where appropriate; record why the category is not
required instead of broadening access. A denied category is not a failed feature.
Do not change system-wide network exceptions or reset privacy state to avoid it.

## Verify before taking feature tasks

Before initial worker admission, finish the permission inventory for **all
planned apps, runner families and execution routes**, including Local Network,
Accessibility, capture/input, protected folders/volumes, Codex target-app access
and any planned media, Photos, Bluetooth, notification or automation operation.
Each required row needs actual observed qualification and cleanup evidence for
that machine/actor/context. Mark unused categories not required with a reason;
never mark unknown, denied or not tested as passed.

Coordinator records the developer's readiness report and prerequisite task IDs;
Dispatcher checks that report **before offering a new feature assignment**.
Developers check it before a normal `next`/claim. A skill-sync receipt, local-tool
preflight, running app, shell network success or prepared setup report is not
permission admission. The explicit onboarding/repair task is the permitted way
to resolve missing setup; it must not run feature acceptance to elicit consent.

Use existing queue dependencies to enforce the actual prerequisite for affected
work. For an existing worker, preserve active claims/operations, then have the
owner queue settled affected work behind its single onboarding task. Coordinator
retains/adds the prerequisite on new affected submissions, and Dispatcher does
not wake that worker for feature work while required readiness is pending.
Do not create a separate queue identity, polling service or approval gate.
Onboarding completion must describe actual required permission qualification;
installation or answering one unrelated dialog cannot close it. Genuine missing
human consent remains a precise setup item, never inferred from silence.

After an app/signing/route/user/VM change or an unexpected prompt, invalidate the
affected readiness rows and resolve them in onboarding before another affected
assignment. Keep valid grants and unrelated evidence; do not repeat whole setup.

Prepare/sign the selected runners under existing storage guards, then use their
supported permission-only LaunchServices preflight. The runner performs no
feature work. Optional visible permission-only windows support named-app
controller setup under the current supervisor contract; opening that window or
closing its ACK never constitutes consent. Reuse supported packaging of qualified
libraries inside the signed app so external reads do not occur unexpectedly
before the permission bootstrap. Do not loosen signature or library validation.

The human completes actual OS/app consent when required. Working access does not
need a new consent response. Record observed allowed access, denied access,
restart-required, unknown and not-requested separately from any observed UI or
human decision; qualify only the requested
operation and its cleanup. Never click security dialogs synthetically, transplant
TCC grants, grant Full Disk Access as a shortcut, or access unrelated private data.

## Keep development quiet

Routine tests should use the existing read-only candidate/tool/storage preflight,
then start their bounded lifetime only when the required context is ready. Missing
consent returns a setup prerequisite with the single actor/category/action needed;
it does not trigger another permission request or consume the feature timeout.
Do not add new approval packets or Coordinator retesting. Reuse existing valid
authorization; repeat only checks invalidated by a changed actor, category,
launch route, volume or supported app update.

Coordinator owns the consolidated inventory and missing setup list. Dispatcher
routes tasks by the capabilities they actually require; one unused denied
capability does not block unrelated work. Keep recurrent unexpected prompts as
diagnostic evidence (actor, category, trigger, prior result and count) so the
developer can fix identity churn or unnecessary access instead of asking again.
