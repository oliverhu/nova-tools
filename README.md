# Nova coordination service

A private Kubernetes task queue and status dashboard for `oliver-mac` and
`oliver-mini`. Both have the same permissions and pull from the service. They
never contact one another. The service queues requests, tracks results, routes heavy work to the mini
and permits only one publish flow at a time.

## Deployed endpoints

- Cluster API and dashboard: `https://192.168.2.13:31866`
- Optional local client gateway: `http://127.0.0.1:18766`
- In-cluster DNS: `nova-coordinator.nova-coordinator.svc.cluster.local:443`
- Kubernetes namespace / deployment: `nova-coordinator`

The cluster dashboard opens directly on the LAN without a login or access-token
prompt. Its certificate is signed by the Nova Internal Coordination CA, whose
public root is installed in both clients for verified service connections.
Browser trust is verified on the mini; the Mac still needs its local macOS
trust confirmation (its worker is already connected securely). Machine identity tokens
remain automatic for worker claims and administrative actions. There is no
public ingress or tunnel.

## Worker behavior

Submit a bug fix or feature through either chat or the dashboard. It enters
the shared queue. Each existing Mac session checks every **15 minutes**,
continues its current claim, and immediately pulls again after task completion.
One session implements and tests a routine task in one stage. A publish stage
is added only when authorized and claimed only when ready. Both Macs can
develop concurrently while the service serializes publishing. There are no
peer grants, mandatory plan/review handoffs, or coordinator approval stages.
An empty queue ends the scheduled check quietly.


Ordinary tasks can be claimed by either available worker. Heavy tasks prefer
the mini while it is healthy and meets the task's requirements, including when
it is busy. They may fall back to the Mac if the mini is offline or unsuitable.
Hard memory, CPU, disk and capability requirements are never relaxed. The client
reports total memory/CPU and free build-disk space after a 50 GiB reserve. It
does not claim to measure every other process's memory or CPU reservation.

The lightweight background agent reports capacity and polls a local inbox. It
does not start a language model, use API credits, or execute tasks unattended.
Chats execute the steps they claim, with long commands supervised by the
client. The installed `nova-coordination` skill teaches future chats how to
submit requests, pull work and record results. Existing active chats must adopt
the new workflow; old in-flight work is not silently imported or cancelled.

## Connect a Mac

Both Macs are configured and connected. To reinstall a client, run the following
on **oliver-mac** (use `--worker oliver-mini` on the mini). Each Mac needs its own
SSH access to the cluster host. The cluster is the distribution point:

```sh
ssh pi@192.168.2.13 'cat /home/pi/.local/share/nova-coordinator/bootstrap.py' > /tmp/nova-coordinator-bootstrap.py
python3 /tmp/nova-coordinator-bootstrap.py --worker oliver-mac --build-path /Volumes/NovaEngineBuild --trust-internal-ca
```

Choose its actual build volume if different. The installer places the client in
`~/.local/share/nova-coordinator`, its private configuration in
`~/.config/nova-coordinator`, a command at `~/.local/bin/nova-coord`, the Codex
skill in `~/.codex/skills/nova-coordination`, and two login agents for capacity
reporting and the local dashboard. It does not install the coordination server
on either Mac. The server and authoritative database run only in Kubernetes.

## Submit and execute

```sh
~/.local/bin/nova-coord add 'Build and verify the next native engine' --compute heavy --key engine-request-001
~/.local/bin/nova-coord status
~/.local/bin/nova-coord next
~/.local/bin/nova-coord show TASK_ID
~/.local/bin/nova-coord heartbeat TASK_ID --progress 'Running the acceptance suite'
~/.local/bin/nova-coord run TASK_ID -- python3 scripts/your-check.py
~/.local/bin/nova-coord finish TASK_ID --receipt /absolute/receipt.json
```

For richer requests, use `add --file task.json`:

```json
{
  "request_key": "stable-key-reused-on-retry",
  "title": "Prepare an engine candidate",
  "description": "Build the exact requested revision and record its acceptance evidence.",
  "compute": "heavy",
  "priority": 50,
  "requirements": {"memory_gb": 20, "disk_gb": 80},
  "dependencies": [],
  "steps": [
    {"name": "Build", "kind": "work", "instructions": "Use the repository's build-storage policy. Record the exact revision and artifact."},
    {"name": "Verify", "kind": "work", "instructions": "Verify the required behavior and record evidence for that exact artifact."}
  ]
}
```

Routine tasks default to one `work` stage covering implementation and testing.
`publish` takes the single global publishing slot. Older `integrate`/`desktop`
labels remain supported as ordinary work; existing in-flight locks settle with
their owners. A later stage stays with the same healthy, eligible worker so its
checkout stays local; an offline owner can be replaced using recorded evidence.
Each completed stage needs a receipt with a nonempty `summary`
and `execution_stopped: true`, plus useful evidence and artifact references.
Successful publication additionally needs `publication_ref`.

## Publishing and recovery

All publishers using this workflow must run inside a claimed `publish` step:

```sh
~/.local/bin/nova-coord publish TASK_ID --receipt /new/publication-receipt.json --command python3 your-publisher.py
```

The publisher must create the fresh receipt only after verifying the external
result. Existing publishing scripts can call `nova-coord publish-permit`
immediately before mutation; the wrapper sets `NOVA_COORD_TASK_ID`.

**The service serializes participating publishers. It cannot constrain an old
script or administrator that bypasses it and uses independent deployment
credentials.** Both active Mac sessions have adopted the central queue and publish protocol.
Legacy release scripts still need to be invoked through the wrapper; they have
not been rewritten by this standalone service deployment.

Claims and resource locks are committed in one SQLite transaction. Generation
numbers and opaque tokens fence stale updates. A lease expires after 180
seconds without a task heartbeat; the client renews every 25 seconds for a
supervised command. Expired tasks become **held**, and their locks remain held.
Publish failure is also held because a remote side effect may have succeeded.
There is no claim of exactly-once external side effects and no automatic replay
of uncertain work. This deliberately favors safety over unattended recovery.

An administrator can reconcile only with evidence that all owned execution
stopped and the external state was checked:

```json
{
  "action": "retry",
  "execution_stopped": true,
  "external_state_checked": true,
  "evidence": "Describe verified process settlement and the actual external result."
}
```

Use `--config /private/admin.json reconcile TASK_ID --file evidence.json`.
Actions are `retry`, `complete` (advance the reconciled step), or `cancel`.
Never infer settlement merely from an expired heartbeat or a local SSH exit.
No user approvals or existing acceptance criteria are waived by a task claim.

## Deployment and operations

`python3 deploy/deploy.py --apply` deploys owned resources over SSH using the
cluster host's existing kubeconfig. Source is an immutable ConfigMap and the
Python image is pinned by digest. No image build or package download is needed
in the pod. It runs as a non-root user without a service-account token.

State lives on an explicitly local, retained volume (SQLite WAL is unsuitable
for shared NFS storage). A separate NFS volume holds consistent SQLite backup
snapshots every 15 minutes, retaining the most recent 96. This is a single
durable instance, **not automatic failover**: losing its storage node makes the
service unavailable until that node returns or an operator restores a backup.
After restoring a backup, hold/reconcile all possibly active claims before
enabling new work. Check backup logs and storage as the queue grows.

The source deployment can be rolled back with Kubernetes rollout undo; source
ConfigMaps and the database are preserved. Do not delete the namespace/PVCs as
a rollback. Credentials and the TLS private key are in owned Kubernetes Secrets
and ignored, restricted local deployment files. The internal CA key stays in Kubernetes. cert-manager issues 90-day server
certificates and renews them 15 days before expiry; the server reloads renewed
certificates within 30 seconds without a pod restart. A changed root CA still
requires deliberate trust distribution to clients. The trust installation is
SSL-only and does not grant code-signing trust. NodePort ingress admits the two local LAN subnets and K3s node gateway
addresses; pod egress is denied. WAN port 80 and public routes are untouched.

## API and validation

Use a worker bearer token for `POST /api/worker/pulse`,
`POST /api/worker/claim`, `POST /api/tasks/ID/heartbeat`, and
`POST /api/tasks/ID/finish`. Read `GET /api/state` and `GET /api/tasks/ID`.
Only the separate admin identity can `POST /api/tasks/ID/reconcile`.
LAN users can read `/api/state`, `/api/tasks/ID` and submit tasks without
authentication. Worker and reconciliation mutations still require their
automatic identities and current claim tokens. Host, Origin and browser
fetch-site checks reject cross-site requests and unknown service addresses.

Run `python3 -m unittest discover -s tests -v`. The suite covers competing
claims, equal ordinary-work access, heavy-task routing/fallback, requirements,
dependencies, step ordering, duplicate submissions/completions, global
publishing exclusion, stale tokens, fail-closed expiry/restart, HTTP access
boundaries and stopping supervised commands after lost heartbeats.

`tests/browser-check.cjs` uses Playwright against a running dashboard and creates
one explicit deployment-verification task. It checks submission, details,
search, desktop/mobile layout and JavaScript errors. Use an isolated test
service for repeated development runs.

Storage behavior follows the primary [SQLite WAL documentation](https://www.sqlite.org/wal.html)
and [Kubernetes persistent-volume documentation](https://kubernetes.io/docs/concepts/storage/persistent-volumes/).

Internal signing and renewal use the primary [cert-manager CA issuer documentation](https://cert-manager.io/docs/configuration/ca/). Browser trust was verified on the actual service with certificate verification enabled.
