# Audited worker session reset

A reset explicitly abandons an old active session. It does not establish process
cleanup, cancellation, permission readiness or resource safety. The same worker,
queue credential, VM, account and model remain. There is no replacement identity,
VM action, TCC change, automatic old task migration or fabricated wake ACK.

The existing Setup owner archives local records and stops only its owned capacity
agent under the actual human decision. The source owner qualifies this API and
local helper separately. Coordinator retains intake and the supported administrative
record; the service remains the authoritative task/event ledger.

## State and fences

`POST /api/workers/WORKER/session-reset/plan` takes `{}` under the existing
administrator identity. It returns an exact snapshot fingerprint, expected current
epoch, held task generations/history hashes, active normal/scoped wake IDs and
resources retained from a previous epoch. A paused worker is required to apply.
The fingerprint includes the actual dispatch row/report, complete old wakes,
active scoped leases and complete task/API history. A race refuses the reset.

`POST /api/workers/WORKER/session-reset/apply` takes exactly:

```text
id: fresh 32-character hex reset ID (also the new epoch)
worker: exact existing Mini worker
expected_epoch: null for first reset, otherwise exact current epoch
fingerprint: fresh plan fingerprint
archive: verified immutable archive-binding sidecar described below
decision: {source:{thread_id,message_id},response,
           scope:"archive-active-session-only",verified:true}
policy: {schema:1,worker,held:[{task:ID}],resources,
         allowed_workspaces,allowed_kinds,allowed_capabilities,
         unknown_effects_bounded,unknown_bindings}
local_admission: {worker_lock_held:true,listener_lock_held:true,agent_stopped:true}
```

The primary human decision must actually authorize this session abandonment.
Administrator authentication is not evidence that an arbitrary transcript was a
human decision. Preserve that source and scope without putting private approvals
or machine receipts in Git. Local admission fields attest actual operator checks;
they never assert that all historical/native execution stopped. Keep the worker
paused and owned agent stopped between verification and commit.

The transaction stores the complete before snapshot and immutable request/response.
Each old task's row/API/approval snapshot is retained privately; its original
results, receipts, generation, requirements, dependencies and counters remain.
Unfinished lifecycle state becomes `abandoned_unverified`, owner/claim hash is
fenced, and an audited abandonment event is appended. Original approval state is
still visible. Completed/cancelled acceptance is not rewritten. Old wakes/scoped
leases become `quarantined`, not settled. Their original rows/reports remain in
the immutable reset record. Worker pause stays true, dispatch/scoped enrollment is
disabled, and availability becomes unknown. No response claims old cleanup or ACK.

The normal worker-wake index now excludes quarantined history while retaining its
old uniqueness fence for actual active wakes. Existing indexes migrate atomically;
there is no interval with duplicate active reservations allowed. The old task-wake
fence remains. Quarantined tasks cannot be claimed, resumed or adopted into new
scope even if a stale actor presents an old claim/wake token or generation.

Reset request IDs are one-shot. An identical replay returns the existing receipt
and current active epoch; it never reactivates a superseded epoch. A changed
request with the same ID refuses. A lost response remains journaled for readback
or exact idempotent retry, never a new automatic reset.

## Honest fresh eligibility

The reset is allowed without reconstructing missing historical wait records.
The separate strict resource-scoped proof contract remains unchanged. A reset
policy instead names retained held native/path/process/capability resources and
fresh isolated workspaces. Every current API and relevant archived unknown is
bound by source, JSON pointer and value hash to explicit held resource indexes.
`unknown_effects_bounded` and each binding's `effects_bounded` are actual reviewed
scope statements, not a claim that unknowns disappeared. False remains valid to
record abandonment, but prohibits all fresh task effects. An unclassified or
unbounded effect must not be labeled bounded merely to enable work.

Only explicit source/inert/media declarations whose entire acceptance fits the
reviewed footprint can run. Native, browser, input, signing, permission, process,
publication, archive/intersecting or unknown operations remain denied. Existing
actual tool review/permission holds still apply. A same-VM reset is not another
actor or a way to replay an operation rejected by automatic approval review.

New tasks must carry the normal `requirements.resource_scope` contract. Historical
tasks are not automatically retried in a new epoch. For an existing queued media
qualification, Coordinator may explicitly refine its scope through administrator
`POST /api/tasks/ID/session-scope` with `{worker,epoch,task_sha256,resource_scope,
evidence}`. It compares complete history and current epoch, adds requirements
without removing capabilities, and audits the actual scope. This grants no OS
permission and cannot adopt an original quarantined task or weaken acceptance.

Media can use exact pre-staged read-only content/path hashes. For ordinary ordered
handoffs whose content arrives after launch, only the reviewed current epoch's
read-only inbox namespace is permitted, requiring actual `cli-media-v1` plus the
normal media/video capabilities. Existing media-v1 then binds each operation's
actual content SHA, task/generation/run/thread/account and inspection readback.
No future content hash is fabricated. Old epoch/root/archive namespaces remain
ineligible. This option applies only to explicit reset epochs; strict old scoped
eligibility retains its content requirement. Symlink aliases refuse locally.

## Archive contract

Preserve the original canonical record root by an authorized atomic rename into a
separate private archive, preserving all bytes, links and metadata. Do not stop a
VM, delete locks/evidence, copy account auth, or infer backend cleanup. Capture
actual agent/plist/config state separately. Keep the original pre-reset manifest
unchanged. Its schema is:

```text
{schema:1,kind:"worker-records-pre-reset-manifest",worker,source_root,
 entries:[{path:relative,kind:"directory"|"file"|"symlink", ...metadata,
           size:BYTES,sha256:HASH} or {path,kind:"symlink",target:TARGET}],...}
```

Existing `mode,uid,gid,device,inode,mtime_ns,nlink` fields are checked when present.
File size/hash fields apply only to regular files; symlinks are preserved and
never followed. Unsafe or duplicate paths, symlink ancestors, changed bytes or
metadata, extra/missing entries and omitted unknowns refuse verification. Bound:
8192 entries, 16 MiB manifest; file hashing streams instead of loading payloads.
The separate immutable sidecar is:

```text
{schema:1,kind:"worker-session-archive",worker,reset_id,
 original_root,archive_root,manifest:{path,sha256},verified:true,
 entry_count,total_file_bytes}
```

For `unknown_bindings`, `source` is the exact task ID for API records or
`archive:RELATIVE_PATH` for original run-level state/report/readback/wait/finish/
stopped-cleanup JSON. Bind every inventory item, including repeated references;
never empty an original array. Original true human/stop flags are retained as
bound inventory facts as well; archiving them does not lift the old task's
permission/approval scope or allow its replay. Metadata verification is not a native observer.

## Operator sequence

1. Preserve current task/claim/source/config state and actual primary decision.
   If an app-only worker cutover is also planned, keep its current task backup and
   readiness/route prerequisites separate; this reset does not perform cutover.
2. At the assigned Setup boundary, pause this exact worker, stop only its owned
   capacity agent, verify both existing worker/listener locks, and perform the
   approved archive. Keep its old unknown resources, VM, account and permissions.
3. At a safe source deployment boundary, install the qualified service/client
   revision and preserve rollback bytes. No schema/policy activation happens from
   a source commit alone. The worker remains paused and the agent remains stopped.
4. In the existing administrator context, obtain one fresh reset plan. Prepare the
   exact request with the actual archive sidecar, decision and reviewed resource/
   unknown bindings. Helper `prepare` takes hash-bound `--plan`, `--archive`,
   `--policy` (base policy plus reviewed `unknown_binding_resources` indexes),
   `--decision`, a new `--output` and `--agent-stopped`. On the guest it verifies
   the archive once and generates all per-record unknown hashes into the private
   final request; there is no manual retyping of old observations. Do not copy an administrator credential to the guest.
5. On the guest, use `scripts/reset-cli-session.py verify --archive PATH
   --archive-sha256 HASH --policy PATH --policy-sha256 HASH --root ACTIVE_ROOT
   --worker WORKER --agent-stopped`. It reads metadata/bytes and obtains the local
   locks; it neither stops an agent nor infers one stopped. Preserve its output.
6. While pause/agent-stop boundaries remain valid, the existing administrator
   sends that exact reviewed body to the apply API. If this context is already
   local, helper `apply --admin-config ... --request ... --request-sha256 ...`
   additionally verifies/journals locally. Otherwise apply from the existing
   administrator host with the verified guest receipt; never transport its token.
   Inspect actual response and a fresh plan before declaring reset recorded.
7. On the guest use helper `install --worker WORKER --epoch ID --root ACTIVE_ROOT
   --client-config OWN_CLIENT --dispatch-config OWN_DISPATCH`. Its existing worker
   credential reads back `/api/worker/session` using the explicitly selected epoch.
   It saves private before bytes, adopts the epoch into both configs, and leaves
   admission/event startup disabled. Interruption is recoverable using the same
   epoch. No account/token/model is changed; no agent or model is started.
8. The fresh active root is `ACTIVE_ROOT/epochs/ID`; claims and approval journals
   use their own `epochs/ID` subdirectories. Historical records/approvals remain
   preserved and are not automatically replayed. Establish current instruction
   adoption and fresh source/epoch-bound readiness here; reuse valid exact-context
   OS access without demanding another Allow response.
9. The normal source/CLI route, if still authorized, uses its existing readiness,
   dispatch enrollment, unpause and reporter/listener flow, with explicitly scoped
   tasks and actual image/video inspection, own cleanup, finish/API/ACK checks.
   When the separately approved app-only cutover is verified, do not start a CLI
   worker runtime. The epoch protocol is transport-independent: the app-backed
   worker uses the same exact worker epoch on its authenticated service requests.

## Stale clients, upgrade and rollback

Worker mutations require `X-Nova-Session-Epoch` after reset. Clients cache the
explicit local value; they never discover/adopt a new epoch automatically. Old
clients/reporters fail closed. Read-only state remains available. A fresh local
client and dispatcher must agree on epoch; readiness manifests include it.
Model writes are limited to the new task workspace, own run and fresh claims;
old broad Migration/native-volume write roots are not used for reset-epoch jobs.
This is a stale-protocol/claim fence, not rotation of the unchanged queue credential
or OS isolation against a malicious user holding that credential.

Before the first reset, old clients retain their existing protocol. An upgrade
does not reset any worker by itself. After a reset, do not roll back to code that
ignores epochs and then reactivate the worker. Keep it paused/unenrolled and its
owned agent stopped until epoch-aware source is restored. Preserve the epoch,
quarantine, request and archive rows; never delete them or set an earlier epoch.
Old wake/claim tokens remain fenced by quarantined state even during a stopped
rollback. Restore local configs only from captured private bytes at a quiet
boundary; a mismatched local/client epoch refuses activation. Rollback does not
restore an old active namespace, model session, approval replay or RAM state.
