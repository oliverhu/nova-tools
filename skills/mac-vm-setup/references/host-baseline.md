# Host-derived onboarding and drift checks

The Nova user selected the coordinator host as the source of GitHub and Apple
provisioning identity, including the **same exact GitHub private key** on both
VMs. Capture a private, versioned baseline of approved source assets once, apply
only missing/different setup on each destination, and verify actual capabilities
before dispatch. Do not regenerate certificates or credentials for each VM.

## Identity contract

| Shared from the selected host | Remains specific to each machine |
| --- | --- |
| Exact selected GitHub client key and intended GitHub account/repositories | SSH server host keys, machine name, filesystem and storage identities |
| Apple team and selected signing certificate/private-key identities | Keychain access context, Xcode login/session and app/OS consent |
| Required macOS/iOS provisioning profiles and named notarization credential | Queue worker credential, chat/remote-host binding and owned checkout |

Export only explicitly selected signing assets through the existing supported
encrypted backup/import workflow. A provisioning profile contains no private
key. Do not transplant a whole keychain, account token store or consent database.
Configure the same selected notarization credential through the supported local
tool on each machine; copying its profile name is not provisioning the credential.
The optional credential-sync utility transports only deliberately enrolled files;
its own peer keys remain independent and do not change this GitHub policy.

Measure compute availability on each destination and follow the current
coordination resource policy. A host's job count is not a provisioning identity;
do not copy Studio's concurrency to a smaller VM.

## Capture one nonsecret baseline

Use `scripts/identity_baseline.py` from this skill. Source profiles must be an
explicit, canonical directory of UUID-named, unexpired profiles for the selected
team, such as the validated output of the existing provisioning exporter. Keep
the baseline and assets outside Git in private directories.

```sh
python3 /path/to/mac-vm-setup/scripts/identity_baseline.py capture \
  --public-key /private/path/to/selected-host-key.pub \
  --profiles /private/path/to/validated-profiles \
  --team TEAM_ID --account GITHUB_ACCOUNT \
  --repository git@github.com:OWNER/REPOSITORY.git \
  --notary-profile PROFILE_NAME --output /private/setup/baseline.json
```

The revision hashes the baseline contents. It records public fingerprints,
team-scoped signing identities, profile hashes/UUIDs/expiry/platform/certificate
bindings and the expected notary profile name. It contains no private key,
password, token, device list or exported account session. Source profile/identity
gaps are retained: a host with stale profiles must not become a false ready
template. A source gap needs correction on the source before a new revision.

## Apply at a safe boundary

Coordinate a brief Git boundary with each active VM owner; preserve their claim,
commits, tests and unrelated runtime work. Never stop all workers to edit one
client identity. Existing read-only reporters can see either complete config
version; do not rewrite a partially populated config file in place.

For the shared GitHub key, use the established authenticated host-to-guest channel
(for Tart, host `nova-vm N exec -i` stdin is already available). Transfer the exact
selected private/public pair directly from source memory/stdin to a **new** guest
private path, never command arguments, chat, logs, source, or a shared staging
directory. Require 0700 parent/0600 files and preserve any differing existing file.
Verify source-byte equality in memory and derive the destination public fingerprint
locally. Preserve old keys and remote registrations. Back up the existing SSH
config privately, compare it again before changing it, then atomically replace
only the exact GitHub IdentityFile selection. Preserve other host entries and
verified host trust. Validate the proposed effective config first. On failed
qualification, restore the saved selection only if no concurrent edit occurred;
retain the failure and new key privately for diagnosis. Do not retry or rotate
blindly. The owner resumes normal integration after the actual account/repository
check; onboarding does not push its work.

For profiles, transfer only the baseline's named files and check their hashes.
The helper plans by default; `--apply` preflights every profile and destination,
then installs identical/missing files at both Xcode cache locations. Different
existing bytes are preserved and reported; a repeat is idempotent.

```sh
python3 /path/to/mac-vm-setup/scripts/identity_baseline.py apply-profiles \
  --baseline /private/setup/baseline.json --source /private/setup/profiles \
  --output /private/setup/profile-plan.json
# Add --apply and a new output receipt path to install the reviewed selection.
```

Signing private-key import remains a separate supported operation. Required
checkout-specific profile aliases still follow the repository's signing setup;
the helper does not overwrite a live checkout alias.

## Verify and track drift

```sh
python3 /path/to/mac-vm-setup/scripts/identity_baseline.py check \
  --baseline /private/setup/baseline.json \
  --public-key /private/path/to/selected-guest-key.pub \
  --output /private/setup/identity-check.json
```

By default this is local metadata only: selected SSH route/fingerprint, exact
signing identity presence, every installed profile and its certificate coverage.
Record both supported cache paths, requiring each profile in at least one;
absence in the legacy cache alone is not drift. Differing existing bytes still
fail. On the live host, legacy-cache copies disappeared while the current Xcode
cache retained the exact profiles; do not create an endless recopy loop.
Add `--network` for one bounded intended-account authentication and repository
read (no fetch, push or profile mutation); optional `--checkout` includes that
checkout's Git configuration. The checker rejects URL rewrites to another route.
Exit 78 means differences or a failed prerequisite, with private evidence; exit
zero is **not** full release readiness. Run the network check once after enrollment
or a material access change, not on every queue poll.

For development, require the needed identity/toolchain/input capabilities. For
release workers, also require actual supported Xcode account enrollment, authorized
signing-key use, selected notarization-profile verification, correct profile aliases
and deployment access, using the owners' current evidence. The metadata checker
explicitly leaves `signing_use`, `xcode_account`, `notarization` and `normal_push`
unexercised. It does not inspect account tokens, invoke notarization, create apps,
upload or fabricate a successful capability from installed assets. A missing
release capability need not prevent unrelated development tasks.

Coordinator owns the expected baseline revision and per-host receipt ledger,
including changed paths/backups, actual capability results, remaining gaps and
current skill revision. After onboarding, source asset renewal, VM restore or a
reported access failure, compare against that revision through existing activity.
Repair the differing component and rerun affected checks only. Dispatcher uses
the capability evidence needed by the task; no new timer, generic approval gate,
repeated product validation or credential sync daemon is introduced. A baseline
hash detects drift, not authenticity: obtain it through the trusted source route.

Keep `prepared`, `identity matched`, `functional check passed`, `release ready`
and `push verified` separate. Preserve failed historical receipts even when a
later unchanged route succeeds.
