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

Prepare/sign the selected runners under existing storage guards, then use their
supported permission-only LaunchServices preflight. The runner performs no
feature work. Optional visible permission-only windows support named-app
controller setup under the current supervisor contract; opening that window or
closing its ACK never constitutes consent. Reuse supported packaging of qualified
libraries inside the signed app so external reads do not occur unexpectedly
before the permission bootstrap. Do not loosen signature or library validation.

The human completes actual OS/app consent. Record observed allowed, denied,
restart-required, unknown and not-requested separately; qualify only the requested
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
