# Explicit task readiness and decision revisions

Task readiness is separate from capacity, permissions, approval, execution phase
and actual completion. New scoped tasks can require it at submission:

```json
{"requirements":{"readiness":{"checks":["source-cli"],"decisions":{"worker-reset-mode":"<SHA256 of exact human-authorized scope>"}}}}
```

A declaration without a service readiness record stays ineligible, including exact
`next --task`, event selection/reservation and resource-scoped claims. Existing
tasks retain their original policy until explicitly enrolled; their historical
outcomes and unknown operations are never reclassified. Absence of a declaration
is legacy compatibility, not evidence that readiness was qualified.

Authenticated administrator `POST /api/task-decisions/revise` accepts exactly
`request_id` (32 hex), `decision_key`, `expected_revision` (zero initially),
`state` (`approved`, `denied`, `superseded`), `source` (`thread_id`, `message_id`,
`verified: true`), `scope_sha256` and bounded `evidence`. Verify the primary human
message and exact scope before submitting; model prose, heartbeat, installed assets
and a copied consent record are insufficient. This endpoint records an attested
human decision; it does not answer a dialog or replace the approval receipt flow.
Revisions are immutable, monotonic and compare-and-swap. A distinct new revision
requires a new primary source message. Repeating the same immutable request is
idempotent; changed content with the same ID fails.

Authenticated administrator `POST /api/tasks/ID/readiness` accepts exactly:

- `request_id` (32 hex), current `generation`, `expected_revision` (zero initially),
  and `context_sha256` from fresh `show ID`'s `task_readiness` object.
- Exact qualified `worker`, `state` (`ready` or `waiting`), finite `valid_until`
  (future service epoch seconds, at most 24 hours), and bounded `evidence`.
- Nonempty `checks`: unique `{name, state, evidence_sha256}` entries. State is
  `qualified`, `missing` or `not_required`; record the actual qualification or
  justified nonrequirement evidence. Missing checks cannot establish ready.
- `decisions`: unique `{decision_key, revision, scope_sha256}` bindings. Every
  declared decision scope and capability name must be covered. An unapproved,
  missing, older, denied or superseded decision fails closed.

Readiness does not clear pending approvals, human stops, dependency waits,
publication restrictions, resources, worker requirements or attempt limits.
The service checks exact task step/constraints/generation, expiry, worker and
current decisions in the admission transaction. It consumes the queued-generation
attestation in exactly its next claim and binds that claim to the readiness
revision. Wait, routing, next-step and reset generation changes require fresh
qualification. Ready updates cannot rebind a running claim; only revocation to
waiting is accepted while running. A newer approved decision still requires fresh
readiness; a later approval cannot silently expand an older qualified scope.

Heartbeats check the bound revision and decisions before supervised command
launch and on the existing watchdog interval. Revocation refuses new effects and
causes the watchdog to stop its owned local process group. This is a bounded
cooperative execution fence, not cancellation of an already-issued remote effect;
unknown remote outcomes still require reconciliation. An owning worker can report
an actual prospective `--phase cleanup` and run only its authorized cleanup under
the same claim. Cleanup does not clear unknowns, change old outcomes, release a
wake or prove execution stopped. Implementing/testing and completed finish still
fail when admission is stale. Actual stopped wait/noncompleted finish remain
available. Administrative completion also cannot bypass stale readiness.

The reset/epoch implementation remains owned by a69b2f5066d64638. Its generation
and token fencing must remain in force before these checks; an epoch reset must
also require fresh readiness for affected tasks (including unclaimed tasks whose
worker context changed). This module does not archive, reset, release a hold or
infer safety of abandoned resources. The combined owner must qualify actual reset
integration and preserve intersecting unknown resources before rollout acceptance.

Rollout is separate from source integration. Preserve active wake module pins and
client readiness manifests. The existing deployed client heartbeat/watchdog API
already supports this service contract, so no guest module replacement or new
polling is required. Coordinate one service code rollout with the reset owner;
retain the database (all added tables are append-only history or current pointers),
back up original state, verify the advertised `task_readiness: 1` and
`decision_revisions: 1`, and exercise a disposable declared task with actual
qualified evidence before enrolling real tasks. Do not fabricate qualification to
activate a pilot. An old service does not enforce these declarations: disable new
dispatch/admission and settle owned commands before any rollback, preserve all
records and keep managed tasks held until a compatible service is restored.
Unobserved deployment, owner coordination and actual reset acceptance stay pending.

The reset-owner integration hook is
`task_readiness.invalidate_worker(store, db, actor, worker, evidence)`, called in
its existing reset transaction after exact decision/epoch/archive validation.
It withdraws ready attestations for that worker, including generation-zero tasks,
and appends revision/history entries. It changes no task outcome, ownership,
unknown operation, receipt or resource state. Failure rolls back with the reset;
repeated invalidation of already-waiting records is a no-op. This hook's isolated
transaction tests do not qualify the actual a69 reset path until that owner wires
and tests the call at its authorized boundary.
