# Resource-scoped eligibility

A Mini can retain an unresolved native resource and still perform an explicitly
bounded, disjoint source, inert Python, or media task. The optional lane requires
positive settlement evidence for the prior invocation's owned processes, a
complete retained unknown-operation inventory, and a reviewed resource policy.
It leaves the normal wake claimed and all original evidence and approvals intact.
An unknown effect, intersecting resource, or global stop prevents admission.

## Keep the operator workflow small

Setup owns the actual permission-onboarding evidence for each app and launch
context. Reuse successful exact-context access and valid grants. Routine tests
check readiness before starting; missing setup returns to the existing setup
prerequisite instead of asking for consent on every retry. This lane neither
requests nor grants Local Network, signing, Accessibility, browser, or TCC access.
A scoped task must complete its entire acceptance using declared source/inert/media
operations; it cannot substitute source checks for required native acceptance.

Review and enroll one reusable policy for the held invocation, then let the
existing Dispatcher select explicitly eligible task IDs. Each wake rechecks
unchanged proof, account/model limits, current requirements, and full API history.
No new timer, reviewer chat, approval relay, or permission question is introduced.
Current implementation supports one held normal invocation per worker, matching
the existing unique active normal wake. An additional unresolved invocation denies
local admission rather than being omitted.

## Policy and declaration

Administrator `POST /api/workers/WORKER/resource-scope` takes exactly
`{policy, expected_revision}`. `expected_revision` is null for first enrollment;
later changes compare the current SHA-256 revision. Enrollment never changes
normal dispatch configuration. It requires an enabled existing CLI enrollment,
nonavailable normal state, and the exact retained claimed wake/worker/run/claim
generation. It refuses active scoped ownership. The policy expires within 24 hours.

The version-1 policy has these exact fields (see `resource_scope.validate_policy`):

- `schema: 1`, `worker`, `uid`, `held`, `resources`.
- `owned_settlement`, `proof_sources`, `local_fence_sha256`.
- `allowed_workspaces`, `allowed_kinds`, `allowed_capabilities`, `expires_at`.
- `review_source: {thread_id, message_id}` identifying the actual scoped review.

The held entry binds `run_id`, `task`, owning `generation`, full-history
`api_sha256`, `originals`, `unknown_bindings`, `approval_sha256`,
`approval_resources`, and `approval_scope_known: true`. Required original files are
state, worker report and task readback; finish, wait, and stopped-cleanup records
are supported and must be included when they carry relevant original unknowns.
Every unknown in each retained original and the fresh API is bound by source,
JSON pointer and value SHA-256 to explicit resource indexes with known effects.
The approval fingerprint/resource mapping preserves the hold; it grants nothing.

Six owned roles require actual raw evidence: host, driver, executor, controller,
native-main, and server. Each binds the original run/task/generation, full owned
and waiter PID/UID/birth identities, observer source hash, recorded epoch and
original invocation time window. Process proofs require positive normal exit,
return code, reap, and the exact wait target from one raw wait receipt. Server
proof requires thread join, listener closure and fresh connection refusal for the
exact loopback endpoint/nonce. Every fact, identity, role and binding has raw
path/hash/JSON-pointer provenance. Missing historical facts remain missing; do not
assemble a wait for one PID using another PID's exit record. Absence is insufficient.
The private local fence hashes original/proof bytes and filesystem identities;
same-byte replacement, symlink aliases and changed histories refuse admission.

A task retains its normal requirements and adds exactly:

```json
{
  "resource_scope": {
    "schema": 1,
    "kind": "source",
    "operations": ["source-read", "source-edit"],
    "workspace": "/ABSOLUTE/ISOLATED/TASK/CHECKOUT",
    "resources": [
      {"kind": "path", "value": "/ABSOLUTE/ISOLATED/TASK/CHECKOUT", "access": "write"},
      {"kind": "capability", "value": "cli-shell"}
    ],
    "required_capabilities": ["cli-shell"],
    "effects_declared": true,
    "acceptance_within_scope": true
  }
}
```

The outer hard worker must match the policy; outer capabilities must equal the
declaration. Source operations are source-read/edit, git-local/transfer, and
source-audit. Inert work permits source-read, inert-python and source-audit.
Media permits media-read/image-inspect/video-inspect, with read-only media path
and content SHA-256. Video needs actual cli-media-video readiness; media needs
cli-media; Git transfer needs git-source and the exact reviewed Nova source remote.
Every required capability must also be resource-declared. Writes stay inside the
isolated workspace. Paths, capabilities, Git remotes and media are compared with
held resources; metadata/proof roots and aliases are protected locally too.
Native operations, process resources, release tasks and unknown operations deny.
These declarations are scheduling contracts, not OS sandbox or permission grants.
Existing tool approval/review boundaries still apply to each actual operation.

## Runtime and settlement

`GET /api/worker/resource-scope/policy` returns only the authenticated Mini's
policy. `/api/state` adds exact `resource_scope.eligible_task_ids`, policy revision,
expiry and false generic/ACK/release flags; normal availability is unchanged.
Only the existing Dispatcher uses this information, at its existing cadence.
It invokes `nova-cli-worker scoped-wake --task ID --scope-revision REVISION`.
The dedicated route takes the existing worker lock before checks. A separate
private `resource_scope.enabled: true` enrollment pins the actual installed
module/instruction paths and hashes in `resource_scope.sources`; all nine runtime
module dependencies are mandatory. Include current instruction/readiness hashes
in the reviewed source set too. The normal event pilot filter is replaced only
by the separately reviewed scoped policy; other capacity/dependency/account and
assignment gates remain binding.

For a source-only preview before service enrollment, use:
`nova-cli-worker scoped-check --note-file /PRIVATE/policy.json --request-sha256 SHA`.
It reads bounded local evidence and held API state under the worker lock, with no
model, capacity report, original wake ACK, or service mutation.

A successful wake journals private intent before reserving a one-shot scoped
lease through `POST /api/worker/resource-scope/reserve`. It uses a separate
`lane: resource-scoped` claim binding through the existing normal self-claim API.
A concurrent reservation, normal wake collision, changed full target/held history,
wrong owner/token/generation, expired policy or failed account check denies.
No reservation expires into replay; a lost response remains unknown.

The supervisor rechecks evidence before model launch. Its writable roots are only
the isolated workspace, its new run directory, and its normal claim directory.
Model sandbox network access is false unless source Git transfer is declared;
normal queue control still uses the existing reviewed route and tool boundaries.
An exclusive model-spawn intent prevents replay. Normal result reduction verifies
this new task only, then writes `scoped-run-result.json` with `scope-only`
availability. It never creates an old run-result, erases unknown arrays, or calls
the normal wake ACK. After its inherited worker lock is released, completion
reacquires the worker lock around the separate scoped completion API.

Only a fully accepted, completed new task with stopped own execution and empty
own unknown/message inventories closes its scoped lease. A failed, waiting,
approval-held, interrupted or ambiguous scoped run stays reserved for explicit
reconciliation. It cannot silently widen the policy or admit another scope.
The result and service response remain separate private records. Closing one
scope never releases held native resources or makes the worker generically ready.

## Controlled rollout and rollback

Source integration is not rollout or permission readiness. The original 909
historical packet cannot meet the six-role positive wait/epoch/provenance contract;
its false census and unknown process remain held. Do not enroll it using invented
facts. A source-only negative assessment is useful evidence, not native clearance.

At an explicitly assigned safe boundary, the setup/runtime owner:

1. Preserves current service/client source revisions, exact installed before bytes,
   normal wake/state, original evidence hashes and current readiness. It checks
   no active owned runtime/worker lock conflict and coordinates other module owners.
2. Uses the existing rollout script's fixed `resource-scope-v1` profile to prepare
   before/after bytes for workers, cli_results, cli_recovery, resource_scope,
   cli_resource_scope and cli_dispatch. Existing client/messages/event modules
   must already be qualified and are verified in the pinned local source set.
   Service uses the matching Store/server/event-dispatch source. No held guest
   action or service restart is authorized by this document alone.
3. Reviews the concrete plan and rollback, installs qualified source without
   touching original run state or holds, and performs the bounded scoped-check.
   If full historical proof is missing, stop enrollment and report the exact gap.
4. Enrolls only the actually reviewed policy with CAS, then records matching local
   source/instruction/readiness hashes. Proves an explicitly assigned inert task
   end to end, checking old API/state/wake/evidence unchanged and scoped closeout.
   Media needs separate actual media readiness; source installation does not prove it.
5. Enables only the intended exact declared tasks through existing dispatch.
   Records actual finish/readback and source/rollout evidence; no generic capacity.

For rollback, disable local scoped admission first; leave all old holds untouched.
An active/unknown scoped reservation is an operation hold, not permission to
rollback executing code or clear its row. Reconcile it under its original owner
before changing installed modules. The existing rollout script restores only
captured exact before bytes with full compare checks, preserving unrelated files.
Stop using the service policy (or let its bounded expiry deny new admission);
never edit SQLite, clear a wake, manufacture a clean result or reset permissions.
