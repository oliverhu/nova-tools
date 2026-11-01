# Named credential sync enrollment

This optional new-Mac setup phase synchronizes only files deliberately added to
a dedicated private store. It never searches Keychains, browser sessions, home
directories, or legacy service credentials. A peer key can read and update **all
named files in the selected store**; use separate stores for different trust
groups. There is no background sync, remote delete, or arbitrary remote path API.

The generalized source derives from the nonsecret `CredentialSync` handoff of
2026-10-31. Its machine-specific installer is not used. Existing installed peers
and their credentials remain unchanged by this source integration.

## 1. Prepare each machine locally

Use an unprivileged account and a trusted Python 3.9+ interpreter. Enable the
`credential_sync` section in your private new-Mac configuration:

```json
{
  "enabled": true,
  "install_dir": "~/.local/lib/nova-credential-sync",
  "data_dir": "~/.local/share/nova-credential-sync",
  "python": "/usr/bin/python3",
  "peer": null
}
```

Keep this section inside the existing schema-1 setup configuration. Run its
read-only plan, then apply only `--phase credential_sync` as described in
[new-Mac setup](new-mac-setup.md). Installation and data directories must be
separate and non-nested; data must be outside Git repositories. Paths may contain
spaces. Canonical paths are required; symlinks, hardlinked files, wrong ownership,
and overly broad private permissions are refused. Only Ed25519 SSH keys are
supported. Pinned-host filenames may not contain SSH expansion characters
(`%`, `$`, backslash, or double quote).

Each machine generates its own private SSH identity under its data directory.
The private key stays there, owner-only. The phase reports the **public** key file,
fingerprint and new store UUID. Retrying the same installation preserves that
identity; an interrupted public-key write is recovered from the existing private
key. It will not adopt an unrelated directory/key, replace different installation
source, or replace a configured peer. Such changes need an explicit migration.
No legacy ElevenLabs file or other credential is automatically imported.

Keep the private data directory off Git, exports, backups shared with others,
build artifacts and chat. Files are 0600 and store directories 0700. Credential
values are private files, not additionally encrypted at rest by this utility;
the operating system and account storage protections remain relevant.

## 2. Review and enroll a public key on the receiver

Through your existing independently trusted device/channel, obtain the sender's
public key and compare its fingerprint with the sender's installation report.
Copy only that public key to a file on the intended receiving machine. From a
reviewed checkout of this tool on the receiver, run:

```sh
python3 scripts/enroll-credential-peer.py \
  --install-dir "$HOME/.local/lib/nova-credential-sync" \
  --public-key-file /absolute/path/to/sender-public-key.pub
```

This prints a reviewable `authorized_keys` line with `restrict` and a forced
command bound to the exact installed receiver and data path. To apply that exact
enrollment, repeat with `--apply`. The target defaults to your own `~/.ssh`, or
use an explicit `--ssh-dir` for the intended SSH account. No SSH shell is used to
install remotely. Configure macOS Remote Login separately if needed; this utility
does not enable it or change its allowed users.

Existing `authorized_keys` entries and normal SSH access are preserved. An exact
existing restricted line is idempotent. The same key with different restrictions,
a duplicate, or any ambiguous occurrence is refused without changing the file;
review it locally instead of adding another line. It never relaxes restrictions
or removes another key. The forced command accepts only `credential-sync-v1`;
`restrict` disables forwarding, agent/X11 forwarding, PTY and user RC execution.
Both sides may initiate sync after independent enrollment in each direction, or
one side may drive both push and pull with a single enrolled key.

## 3. Pin the receiver and its store

Obtain the receiver's Ed25519 **SSH host public key** and SHA-256 fingerprint
from an independently verified source, such as its local console. This host key
is distinct from either client's newly generated key. Never trust a key merely
because `ssh-keyscan` or a first network connection returned it. Obtain its store
UUID from the receiver's installation report or its local command:

```sh
python3 "$HOME/.local/lib/nova-credential-sync/credential_sync.py" \
  --root "$HOME/.local/share/nova-credential-sync" identity
```

Set the initiating machine's `credential_sync.peer` to these exact fields, then
rerun the local phase. Example placeholders must be replaced with independently
verified public values:

```json
{
  "host": "receiver.example.net",
  "user": "your_ssh_user",
  "port": 22,
  "host_key": "ssh-ed25519 REPLACE_WITH_VERIFIED_PUBLIC_KEY_BASE64",
  "host_fingerprint": "SHA256:REPLACE_WITH_INDEPENDENTLY_VERIFIED_FINGERPRINT",
  "store_id": "REPLACE_WITH_RECEIVER_STORE_UUID"
}
```

Unknown or mismatched fingerprints fail before installation changes. No network
trust discovery occurs. The client checks the exact private known-hosts file and
its local SSH key fingerprint on every request. SSH uses strict checking, one
dedicated identity, BatchMode, no agent or password fallback, and no inherited
SSH configuration/proxy/local commands. Each request names the expected remote
store UUID; a replaced store cannot receive a write intended for another store.
Baselines include both store UUIDs, host fingerprint, hostname, port, user and
local key identity. A changed peer cannot borrow another peer's history.

## 4. Add and sync only selected files

Use the installed receiver/client with the configured `--root` on either side:

```sh
python3 "$HOME/.local/lib/nova-credential-sync/credential_sync.py" \
  --root "$HOME/.local/share/nova-credential-sync" add service-name \
  --file /absolute/private/path/to/credential.env
python3 "$HOME/.local/lib/nova-credential-sync/credential_sync.py" \
  --root "$HOME/.local/share/nova-credential-sync" sync service-name
```

`add` requires an existing owner-only regular file. API keys, env, JSON and binary
files are accepted from 1 byte to 256 KiB, with at most 1024 names per store. Names
contain lowercase letters, digits, underscores and hyphens. `list` prints only
names. `sync` without a name checks all deliberately enrolled names from both
stores. It never searches for additional credentials or runs automatically.

First-time different values and concurrent changes cause a conflict. Resolve
one named conflict deliberately with `sync NAME --prefer local` or `--prefer remote`.
Remote writes use compare-and-swap; both sides are read back before advancing the
baseline. Disappeared files are restored from the other store, not deleted there.
Failed/unknown transfers retain the old baseline; inspect current state and sync
again only after the earlier operation has settled. Never retry unknown writes
by forcing a preference. Protocol requests/responses are byte- and time-bounded;
errors do not include credential contents or SSH stderr.

Export with `export NAME --file /absolute/private/existing-directory/file.env`.
Existing destinations require `--replace`; exports use 0600. Source inputs and
exports are snapshots: add an updated file again to rotate it, then explicitly
sync and reload the consuming application. There is no Keychain integration.

## Existing installations and migration

No automatic in-place upgrade is performed. The prior client identifies peers
only by an SSH alias and has no store UUID, so reusing its baselines would be
unsafe. Install both generalized endpoints into **new empty directories**, enroll
new dedicated keys and verified host/store bindings, and explicitly export/add
only the names you choose. Preserve old directories and authorized-key entries
until the new pair is verified; remove obsolete access separately and explicitly.
Do not copy the old private identity, `peer.json`, or baselines to a new machine.

The receiver retains the v1 inventory/get/put wire shape (plus store identity),
but the new client intentionally refuses an old receiver lacking that identity.
There is no implied compatibility proof for mixing live legacy and new peers.
If installation was interrupted, rerun the same reviewed source/configuration;
a differing source/config is refused. Inspect any unexplained staging file or
changed enrollment before manual cleanup. Re-running the phase never runs sync.

## Validation scope

Run `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p test_credential_sync.py -v`.
Tests use temporary homes/stores, actual generated disposable keys, the actual
setup phase and forced-command receiver processes. They cover transfer, conflict,
CAS/readback races, path/permission/integrity failures, exact enrollment,
interruption recovery and bounded transport. OpenSSH `ssh -G` verifies the actual
argument parsing, including spaces and strict settings; the network SSH hop is
substituted with a local protocol process. No real Mac enrollment, live SSH
daemon, production credentials or complete new-Mac provisioning run is tested.
