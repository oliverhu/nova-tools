# Scoped retained-metadata recovery v1

This is a separate evidence-collection path for an exact held local run. It does
not settle the run, change a service task, report capacity, close out messages,
ACK a wake, launch an observer or authorize any guest-native operation. Normal
admission, `cli_results.clean`, `admission_inventory` and ACK gates are unchanged.
A recorded recovery is not a successful task result or resource release.

The source implementation adds `nova-cli-worker recovery-plan` and
`recovery-observe`. Both acquire the existing nonblocking `worker.lock` and an
exact-run `scoped-recovery.lock`. A busy owner refuses; absence of the lock is
never proof that its operations stopped. Only explicit metadata files are read.
The mode cannot execute commands, import observer code, query/sign a native app,
listen on a port, send input, decide consent, signal a process or publish.

## Concrete operator sequence

1. First review the actual operation against existing authorization and holds.
   **Do not run even `recovery-plan` on a held guest merely because this source
   landed.** This task qualifies only source and inert local fixtures. A later
   permitted operation must identify the guest, run, worker, task/generation,
   exact metadata reads, lock/evidence writes and current resource/approval holds.
   Reuse valid exact-scope authorization; this is not a blanket permission grant
   or another automatic request for human consent.
2. At an authorized installation boundary, use the fixed `scoped-recovery-v1`
   profile of `scripts/rollout-cli-results.py` to prepare/review an exact module
   update. It includes only `cli_recovery.py`, `cli_results.py`, `cli_dispatch.py`
   and `client.py`; original rollout profiles stay unchanged. Preserve before
   bytes and safe rollback. Installation, reporter/readiness qualification and
   access to a held guest are separate operations, not performed by this source
   task. Do not restart an owning supervisor or advertise availability.
3. Create a private immutable request from actual retained metadata, not inferred
   process absence. Record exact SHA-256 values of the run's original `state.json`,
   `worker-report.json`, `task-readback.json` and any explicitly included
   `finish-response.json`, `wait-receipt.json` or `stopped-cleanup.json`. The three
   first files are required; every original stays byte-for-byte unchanged.
   Copy only separately reviewed metadata records into exact private slots
   `RUN/recovery-inputs/RESOURCE_ID.json`. Copying/appending these inputs itself
   needs the applicable filesystem scope. Keep their source provenance; never
   manufacture native observations or rewrite the original evidence to fit a
   schema. Use plain `read-json-record` when positive facts are incomplete.
4. Run the plan command from the actual owning worker context:

   ```sh
   nova-cli-worker recovery-plan --run-id EXACT_32HEX_RUN \
     --note-file /absolute/private/request.json --request-sha256 ACTUAL_REQUEST_SHA
   ```

   The returned plan reference is an immutable file under `RUN/scoped-recovery`.
   It binds the entire request, exact file identities/hashes, bounded own-run
   inventory, current full API/history fingerprint, implementation source hashes,
   retained unknowns and an unchanged deadline of at most 60 seconds. Plan
   creation reads metadata and appends that plan only; it reads no listed resource
   record yet. It does not establish native readiness or authorize a probe.
5. Obtain/record the applicable independent scoped review of this exact plan and
   resource list. Its source thread/message IDs record provenance, not a claim
   that arbitrary local JSON authenticates a human decision. Genuine decisions
   still require their existing trusted provenance and hold-handling workflow.
   The review's scope is **bound-records-only** and all three native-observation,
   native-execution and resource-release authority flags must be false.
6. If that exact record-read scope is permitted and still fresh, run:

   ```sh
   nova-cli-worker recovery-observe --run-id EXACT_32HEX_RUN \
     --note-file /absolute/RUN/scoped-recovery/plan-UNIQUE.json \
     --request-sha256 ACTUAL_PLAN_SHA --expected-fingerprint ACTUAL_PLAN_SHA \
     --review-file /absolute/private/review.json --review-sha256 ACTUAL_REVIEW_SHA
   ```

   The worker/run locks, original/request/plan/review identities, implementation,
   own-run inventory and fresh API/history CAS are checked before and after the
   bounded reads. A durable exclusive intent consumes this plan before reading
   resources. Replays refuse. A race, expired clock, changed file, missing observer
   receipt or invalid fact appends a refusal where possible; nothing is silently
   resumed. A crash leaves an intent, not success. Prepare a fresh scoped plan
   only after reviewing the actual failure; do not retry an unchanged operation.
7. Inspect the returned evidence and actual original API state. The evidence
   separates verified **retained owned-settlement facts**, incomplete owned
   evidence, original unknowns and newly read unknown metadata. No process closure
   was observed here. Even when all supplied host/controller/driver/executor
   wait/reap and server closure facts verify, `execution_stopped` remains false,
   `availability` remains `unknown`, `wakeACKAuthorized` and
   `resourceReleaseAuthorized` remain false. It is not a normal result, settlement,
   state rewrite, service mutation or permission to launch the next task.

## Exact request and resource contract

A request has exactly `schema: 1`, `kind: scoped-readonly-recovery-request`,
`run_id`, `task`, `worker`, original owning `generation`, `originals` (filename to
SHA-256), `resources` and `ttl_seconds`. The run's queued generation plus one,
original owner report and original task readback must match that generation.
Current API owner/last owner, hard worker and exact original claim history must
match the worker. Later original-owner generations may be retained in full;
changed history or reassignment cannot pass an earlier plan's CAS. Missing,
partial or unordered API history refuses.

Each resource has exactly `id`, `operation`, `reference: {path, sha256}` and
`expected`. The path must be its exact `RUN/recovery-inputs/ID.json` slot; no glob,
external command, source import, path traversal or arbitrary filesystem read is
accepted. The finite operation allowlist is:

| Operation | Meaning |
| --- | --- |
| `read-json-record` | Retain bound JSON as metadata; `expected` is `{}`; never verifies owned closure |
| `verify-owned-process-settlement` | Validate exact source/clock/run/resource/generation and positive normal wait/reap facts for one of host/controller/driver/executor |
| `verify-owned-server-settlement` | Validate exact owner/waiter generations, loopback endpoint/nonce, joined thread, closed listener and refused connection facts |

Settlement `expected` fields are `role`, `identity`, `waiter`,
`observer_source_sha256`, `not_before_epoch`, `not_after_epoch`; server adds
`endpoint` and `nonce`. A generation is exactly `{pid, uid, birth}` with current
UID and positive PID/microbirth. Historical fact windows are explicit and at
most one day; they never refresh the plan deadline or imply a fresh native probe.
Nonzero normal return codes can prove reaping without passing feature acceptance.

A settlement metadata record has exactly `schema: 1`,
`kind: owned-settlement-metadata`, `run_id`, `task`, `worker`, `generation`,
`resource_id`, `observer_source_sha256`, `recorded_at`, `available`, `effects`,
`result`, `provenance`. Effects must be `[]`. An unavailable observer stays
unverified. Process result fields are `role`, `identity`, `waiter`, `reaped`,
`normal_exit`, `returncode`, `wait_target_pid`; server fields are `role`, `identity`, `waiter`,
`thread_joined`, `listener_closed`, `connection_refused`, `endpoint`, `nonce`.

Every result fact except the review-bound role, plus `recorded_at`, needs a
`provenance` entry `{reference, pointer}` into an explicitly allowlisted raw
`read-json-record` or original file. The bounded JSON pointer extracts the exact
fact; no evaluation or inferred boolean is supported. Identity/waiter projections
retain all required PID/UID/microbirth values from an existing full raw identity;
missing fields refuse. Extra raw image fields remain in the retained record.
The wait target PID must equal the exact owned identity PID, and wait-target,
reap, normal-exit and return-code facts must come from one raw wait receipt.
Every provenance source and resource is rechecked by hash and file identity after
all reads. The qualified observer/source hash and reviewer must come from actual
reviewed evidence. Source hashes are binding/provenance, not OS attestations.

The review has exactly `schema: 1`, `kind: scoped-readonly-recovery-review`,
`plan_sha256`, `decision: approved`, `scope: bound-records-only`, the exact
`resources`, `source: {thread_id, message_id}`, `reviewed_at`, `expires_at`,
`nativeObservationAuthorized: false`, `nativeExecutionAuthorized: false`,
`resourceReleaseAuthorized: false`. It cannot outlive the plan. This accepted
metadata record cannot authorize a native observer, consent, signal or cleanup.

Reads are owner-only regular single-link nonsymlink files, at most 1 MiB each;
original and resource/provenance byte budgets are at most 16 MiB each. At most
16 resources, 128 own run-state records (16 MiB total), and 4096 recovery entries
are considered. Inputs are not followed through links. New plans, intents,
refusals and evidence use exclusive 0400 files with file/directory fsync/readback.
Deadlines reject late results; filesystem/network I/O has no kernel hard-timeout
guarantee. No deadline or unavailable read is converted into positive evidence.

## Native observation and actual recovery remain separate

For a future PID 1189/native census investigation, independently review the exact
native metadata operation, resource identity, observer source, observer-owned
children/settlement, clocks/bounds, expected effects and actual existing approval
and ownership holds. This module implements **no native probe**. A plan, a valid
record, historical consent classification or positive settlement of other owned
processes never licenses that probe or grants ownership of an opaque process.

Preserve original false census, raw runtime cleanup result and resource holds.
An eligible owner must obtain actual independent observation/reconciliation
evidence and use the existing supported lifecycle process under the original
claim/acceptance. Do not convert resource uncertainty to available, ACK a wake,
clear PID 1189, signal foreign processes, edit TCC, copy grants or release based
on this journal. No active run, held guest or native resource was touched in
source qualification. Large-text checks are skipped by human policy.
