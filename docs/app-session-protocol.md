# App-only service orchestration

The human approved this migration on 2026-11-07. App chats are clients of the
coordination service. They do not send tasks, status or acknowledgments to peer
chats. Service Coordinator owns intake, priorities and policy upkeep; Service
Readiness inspects scheduling prerequisites without waking workers or claiming
for them. Developers own actual acceptance, integration and outcome receipts.

This protocol is opt-in per already credentialed worker. Deploying the source
does not enroll a machine, enable a chat, qualify permissions, retire a runtime,
transfer an active claim or prove live migration acceptance. New machines first
need their own supported Nova credential enrollment. Codex login/account access
is independent; a recorded account digest is an attested identity, not an OAuth
proof or a transferable login. No private key or token belongs in the repository.

## Safe migration order

1. Verify the private task-list backup. Preserve original acceptance, holds and
   uncertain effects. Source integration and live runtime qualification are
   distinct outcomes.
2. Settle owned execution or use the separately audited epoch-abandonment route.
   Disable the old worker's settled CLI dispatch. Enrollment refuses active
   claims, dispatch wakes and resource-scoped operations. Historical unknown
   effects remain subject to their original resource constraints.
3. Create a fresh local app chat through supported app tools/UI; do not import
   conversation history. Keep it onboarding-only. Archive superseded chats after
   their operations settle. The retired dispatcher heartbeat stays disabled.
4. Administrator configures an approved instruction revision/inventory. The
   worker registers its actual new chat, host and account digest, installs the
   exact revision, reads it, and submits adoption. Administrator records actual
   task-relevant capability evidence. A heartbeat or installed file alone is
   insufficient.
5. Test supported local scheduled wake and the full claim/test/finish/readback
   flow before admitting production work. Different accounts provision their
   own local heartbeat; no cross-account chat routing is required.

## API and CLI

All endpoints require existing authenticated credentials. Administrative commands
use the existing legitimate admin client configuration; never copy that identity
to a worker. Session files and claim files stay private on the local machine.

- `POST /api/workers/WORKER/app/policy`: admin CAS with `expected_revision`,
  `enabled`, exact 40-character `source_commit`, `inventory_sha256` and `evidence`.
  Revision increments invalidate old adoption/qualification. Policy updates refuse
  active execution; use task readiness/decision revocation for a running task.
- `GET /api/worker/app-session`: authenticated worker's policy, session identity,
  adoption and qualification. No session token or token hash is returned.
- `POST /api/worker/app/register`: worker supplies `session_id`, `host_id`,
  `account_sha256`, `expected_epoch` and a private random `session_token`.
  Epoch CAS rejects concurrent/stale replacements. Registration is disabled until
  policy is enabled. Replacement resets adoption/readiness; it cannot claim until
  independently qualified. Identical retries recover a lost response.
- `POST /api/worker/app/adopt`: exact `binding`, `policy_revision`,
  `source_commit`, `inventory_sha256`, `instructions_read: true`.
- `POST /api/workers/WORKER/app/qualify`: admin supplies `session_id`, `epoch`,
  `policy_revision`, verified `capabilities`, `valid_until` (within 24 hours) and
  `evidence_sha256`. This records permission/capability evidence; it grants none.

Use `nova-coord app-policy --worker WORKER --file /private/policy.json` and
`app-qualify --worker WORKER --file /private/qualification.json` for the admin
operations. Registration persists its private token *before* requesting the
service, so retries reuse the same identity:

```sh
nova-coord app-register --session-file /private/new-chat.json \
  --session-id ACTUAL_CHAT_ID --host-id ACTUAL_HOST_ID \
  --account-sha256 VERIFIED_ACCOUNT_DIGEST --expected-epoch PREVIOUS_EPOCH
nova-coord --app-session /private/new-chat.json app-adopt --file /private/adoption.json
nova-coord --app-session /private/new-chat.json next
```

The same `--app-session` option (or `NOVA_APP_SESSION_FILE`) accompanies `next`,
`heartbeat`, `run`, `finish`, `wait`, `retry` and `inbox-post`. Claims are stored in
a separate per-session namespace. Do not set a machine-wide session default or
reuse another chat's file. Requests from the retired runtime lack the binding and
cannot claim an app-enrolled worker. This is protocol fencing; it does not attest
which executable sent a request or stop already-running processes.

Claim, heartbeat and successful finish require current adoption and qualification.
A wrong session cannot use another session's claim token. Expired qualification
blocks new effects and completion; the actual owning session can explicitly
heartbeat `--phase cleanup` and report a truthful stopped noncompleted outcome.
Never convert expiry into fabricated cleanup or automatic reassignment.

The instruction inventory digest is SHA-256 of the canonical JSON representation
of `installed_hashes` from `sync-app-worker.py` (sorted keys, compact comma/colon
separators, UTF-8). Both administrative expected inventory and actual worker
readback must use that representation. The helper and policy source commit are
recorded separately from deployed service/client code.

## Durable handoffs

`nova-coord inbox-post --file /private/handoff.json` calls `/api/inbox/post`.
The message has a stable 32-hex `request_id`, recipient `coordinator` or an enrolled
worker, exact `task_id`/`generation`, `kind` (`handoff`, `progress`, `blocker`,
`decision`, `instruction`), bounded `summary` and `artifact_refs`. Workers may
report only their own or last-owned task generation. Enrolled app workers must
also present their current session binding. Admin can post scoped orchestration
records. Retries deduplicate; conflicting reuse refuses. The service stores
references, not local files or credentials, and never follows artifact paths.

`nova-coord inbox --after CURSOR --limit 50` reads the authenticated actor's
messages. Admin reads the coordinator inbox; workers read only their own. Keep
the returned `next_cursor` only after processing the batch. Replaying a batch is
safe when follow-up operations use stable request IDs/generation CAS. Messages
remain durable across restarts; reading does not delete or mark tasks complete.
A handoff message is evidence/context, not a claim or permission. The recipient
must self-claim eligible work and verify actual API outcome before completion.

## Validation boundaries

Run the app-session unit and loopback HTTP tests plus the existing regression
suite. Live acceptance additionally needs real local app registration, approved
revision sync/readback, actual runner onboarding, a scheduled wake, concurrent
claim refusal, service handoff and finish/API verification on the intended hosts
and accounts. A local fixture, restored image or account label is not that proof.
