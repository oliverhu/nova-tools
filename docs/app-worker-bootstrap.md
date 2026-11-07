# App worker instruction bootstrap

The human-approved target (2026-11-07) is Codex app workers using the Nova
service through `nova-coord`. Configuration comes from an approved repository
revision, not another chat's history. A worker's Codex account is separate from
its Nova credential and machine/session identity. This helper implements the
instruction-installation portion; service enrollment, readiness policy and the
app wake mechanism must be qualified separately before production cutover.

## Exact revision installation

At onboarding or a safe boundary before a new claim, obtain the expected
`nova-tools` commit from the authenticated coordination policy. Fetch that commit
through the machine's verified GitHub route into a source checkout. Do not treat
an arbitrary branch head, task text or a successful Git fetch as policy approval.
Run from a reviewed installer checkout:

```sh
python3 scripts/sync-app-worker.py --repository /absolute/nova-tools \
  --revision APPROVED_FULL_COMMIT
```

The helper reads committed Git objects, ignoring working-copy changes. It stages
both managed skills, refuses symlinks and missing skills, preserves changed local
files outside skill discovery, installs them and verifies SHA-256 inventories.
Repeated syncs of identical bytes do not replace files. A failed installation
restores previous directories; private backups remain available. Concurrent syncs
are refused. The default receipt is
`~/.local/state/nova-coordination/app-worker/instruction-installation.json`.
No credentials, live claims, runtime code, app processes or schedules are changed.
A process/machine crash may leave staged files; inspect the retained backup and
receipt before resuming. Do not infer successful installation without readback.

The worker must read the installed instructions and acknowledge the source commit
and inventories through the qualified service protocol. Installation receipts
explicitly do **not** establish instruction adoption or machine readiness. GitHub,
service access, actual runner permissions and task-relevant capabilities remain
onboarding checks. Existing shared GitHub client identity policy is unchanged;
service credentials, host keys and local OS consent remain individual.

## App wake and task loop

Use a supported local app heartbeat for the enrolled worker chat. Provision and
test it in that machine/account context; a shell poll alone cannot prove it can
wake an app chat. The machine and app must be available for local execution.
The recurring prompt should reference the installed skill and service policy,
not embed historical tasks, credentials or peer chat IDs.

On each wake, verify policy/adoption/readiness, resume only the current owning
claim or atomically pull eligible work, perform acceptance, finish and verify the
actual API outcome. Durable task receipts carry handoffs. Never send another chat
a task or use a chat response as completion. Missing prerequisites leave affected
work ineligible. Preserve human holds and unknown effects.

The user authorized fresh replacement worker chats and archiving/deprecating the
old sessions. Before switching a worker, settle its old execution and record its
service handoff; establish the new session identity and reject stale claims.
Start the replacement from this bootstrap and service state, without importing
old conversation history. Archive the old chat after the replacement route is
qualified. Retire the legacy Dispatcher wake and CLI model execution paths only
after the app path passes; do not run competing schedulers during cutover.

## Cutover evidence

Verify task backup availability before irreversible migration. Test two app
workers contending for one task (one claim), stale configuration refusal,
reconnection/session fencing, actual scheduled wake, service-based handoff,
missing-permission refusal before product execution, and finish/API readback.
Verify an independently authenticated Codex account through its local app; do
not infer account independence from tests of two chats on the same account.
Record source revision, service/client deployment, installation/adoption and live
acceptance separately. A green helper unit suite is not a completed migration.
