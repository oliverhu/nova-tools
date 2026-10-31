# Nova task queue

A private Kubernetes task queue for `oliver-mac` and `oliver-mini`. Both workers
claim untaken tasks atomically, implement and test the change, and merge it into
main. Ordinary work **does not publish**. There are no shared publishing,
integration, main, or desktop locks.

Only engine rebuilds are heavy. They prefer Mini; application builds, UI tests,
and ordinary features are standard work available to either qualified Mac.
Explicit memory, disk, CPU, capability and machine requirements still apply.

## Service and workers

- API/dashboard: `https://192.168.2.13:31866`
- Kubernetes namespace/deployment: `nova-coordinator`
- Optional local dashboard gateway: `http://127.0.0.1:18766`
- Client: `~/.local/bin/nova-coord`
- Installed instructions: `~/.codex/skills/nova-coordination/SKILL.md`

The LAN dashboard opens without a login prompt. Automatic worker credentials
protect mutations; internal CA verification protects HTTPS. Browser OS trust is
separate from client trust. No public ingress or tunnel is installed. Never
print client configurations, credentials, or claim tokens.

The existing chat workers check every **15 minutes**, and immediately check
again after completed, stopped or blocked work settles. No eligible task means
a quiet return until the next wake. There are no peer grants or intermediate
coordinator approval stages. The background capacity reporter observes branch
heads and updates the inbox; it does not execute engineering tasks or models.

## Claims and recovery

```sh
nova-coord add 'Rebuild pinned engine' --task-type engine_rebuild --key engine-001
nova-coord add 'Fix a sidebar bug' --key sidebar-001
nova-coord next
nova-coord show TASK_ID
nova-coord heartbeat TASK_ID --progress 'Checking native behavior'
nova-coord run TASK_ID -- python3 scripts/check.py
nova-coord finish TASK_ID --receipt /absolute/receipt.json
```

Task JSON accepts `request_key`, `title`, `description`, optional `task_type`
(`work` or `engine_rebuild`), priority, resource requirements and dependencies.
Default workflow is one Implement and verify step, including merge. Only
`engine_rebuild` receives heavy scheduling; old generic heavy requests normalize
to standard. Manually adding a publish step to ordinary work is rejected.

SQLite transactions prevent two workers from claiming the same task. Opaque
claim tokens and generations fence foreign/stale completions. Heartbeat age is
diagnostic: ownership does not expire, become administratively held, or become
available to another worker when a heartbeat is missed. Supervised commands
still stop on actual service loss/rejection. The worker may resume its claim
once it verifies the operation state.

Completion receipts require a summary and `execution_stopped: true`. Ordinary
workers record merged source/evidence and use non-force pushes, resolving main
movement and rerunning affected checks. Use `--outcome blocked` or `failed` for
incomplete work. Those outcomes free the worker for other tasks. To retry the
same task, its owner supplies verified evidence:

```sh
nova-coord retry TASK_ID --receipt /absolute/stopped-attempt.json
```

The receipt must contain `execution_stopped: true`, `external_state_checked:
true` and `evidence` explaining settlement and a concrete correction. No unknown
input/publication may be replayed. Admin `reconcile` remains for abandoned
legacy tasks; existing holds are not silently reopened by a schema migration.

## Hourly publish tasks

A durable service timer checks each configured repository on the hour. Workers
observe `origin/main` every two minutes using existing host Git access and
`git ls-remote` (no checkout mutation). Stale/failed observations cannot trigger
a release; the service waits for a fresh observation before consuming the hour.
This also detects merges performed outside the queue. Repository configuration
records a verified published baseline, not an assumed current branch tip.

When main differs from the last verified publication, the service creates a
publish task pinned to that exact commit, available to either Mac. A stable
repository+commit key and a transaction prevent duplicate tasks across timer
races/restarts. Outstanding queued, running, blocked or failed publication work
for that repository is not duplicated. New merges wait for a subsequent hourly
check after that work resolves. This is task deduplication, not a publishing
resource lock; there is no lock table used by claims or publishers.

Only the worker that claims the generated task publishes:

```sh
nova-coord publish TASK_ID --receipt /new/publication.json --command python3 publisher.py
```

`publish-permit` verifies ownership of that task; it does not acquire/check a
shared lock. Preserve signing/notarization and actual public-download checks.
Successful receipt fields are `publication_ref`, `published_commit` matching
`release_target.commit`, and normal stopped-execution evidence. If the release
procedure commits version metadata on main, include its verified `release_commit`
to advance the publication baseline without creating a release loop. Do not
include newer feature commits silently. A failed/unknown publication stays an
unresolved task for inspection, without holding other engineering work.

The initial baseline is configured through admin-only
`POST /api/repositories/configure` with repository, branch, published_commit.
Existing configuration cannot silently reset the baseline. Worker-only
`POST /api/repositories/observe` records fresh exact remote branch observations.
No repository credential is added to Kubernetes, and its pod needs no GitHub
egress. `nova-coord observe-main` performs a manual read-only observation.

## Installation and deployment

The service is deployed with `python3 deploy/deploy.py --apply` using existing
cluster SSH access. Source is an immutable ConfigMap; Python is digest-pinned.
SQLite WAL lives on retained local storage, with separate 15-minute backups
retaining96 snapshots. The service is a single durable instance, not automatic
storage failover. Source rollback preserves the database and evidence.

Clients are distributed from the cluster host:

```sh
ssh pi@192.168.2.13 'cat /home/pi/.local/share/nova-coordinator/bootstrap.py' > /tmp/nova-bootstrap.py
python3 /tmp/nova-bootstrap.py --worker oliver-mac --repository-path /actual/nova/checkout
```

Use the actual worker name and checkout. Existing configuration, build path,
claims and credentials are preserved on client upgrades. Do not reinstall OS
trust just to update the client. The optional `--trust-internal-ca` flag is only
for deliberate initial trust setup. The installer manages only its own local
client/reporter/gateway and skill files.

## Verification

Run `python3 -m unittest discover -s tests -v`. Tests exercise atomic claims,
engine-only scheduling, persistent stale-heartbeat ownership, worker retry
fencing, legacy migration, HTTP identity boundaries, hourly no-change/new-change
behavior, duplicate suppression through restart/concurrent ticks, fresh branch
observations, exact published targets, and release metadata loop prevention.

`tests/browser-check.cjs` checks dashboard rendering/submission/details/search
on an isolated service. It creates one test task, so do not use it casually
against a production queue. Production verification should be read-only unless
an explicit task is being submitted.
