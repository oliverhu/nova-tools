# GitHub access on every development machine

Check the host and each guest independently through the real worker's shell and
checkout. A copied public key, installed Git, successful host-side API request or
old migration receipt does not establish guest access. Keep these stages distinct:
identity configured, SSH authenticated, repository readable, and normal push
verified. If GitHub is outside the machine's workload, record `not requested`.

## Onboarding checks

1. Inspect the intended repository's fetch/push URLs and effective SSH settings
   (`ssh -G` for its actual host/alias). Check `core.sshCommand`, `GIT_SSH`,
   `GIT_SSH_COMMAND`, URL rewrites and custom push URLs when present. Record the
   selected user, host, port, identity and trust files, without exposing embedded
   credentials. Preserve approved aliases and other host entries.
2. Compare the selected `.pub` fingerprint (`ssh-keygen -lf KEY.pub`) with managed
   enrollment evidence. Check ownership and modes: normally the worker owns
   `.ssh` at `0700` and private key/config at `0600`. Do not borrow another
   worker's key or assume a filename identifies the account. Establish host-key
   trust from an existing verified pin or GitHub's official fingerprints before
   connecting; neither an unverified keyscan nor disabling checking is sufficient.
3. Make one bounded, noninteractive authentication check through that configured
   route. Use BatchMode, strict host checking and an outer timeout that kills and
   reaps owned processes. A direct-key route should qualify without an unrelated
   SSH agent or cached control socket; if an agent is intentionally required,
   record and qualify that dependency instead of silently switching identities.
   For an already enrolled direct-key GitHub.com route, an example is:

   ```sh
   ssh -T -o BatchMode=yes -o IdentitiesOnly=yes -o IdentityAgent=none \
     -o StrictHostKeyChecking=yes -o ControlMaster=no -o ControlPath=none \
     -o ConnectTimeout=8 -o ConnectionAttempts=1 git@github.com
   ```

   Adapt the target to the approved host/alias. Bound the whole process, not only
   connection establishment. Verify the greeting identifies the intended account
   (or expected repository identity for a deploy key). GitHub's successful SSH
   test normally exits **1** because it offers no shell; the greeting and accepted
   identity distinguish this from `Permission denied (publickey)`.
4. From the worker's actual checkout, run a bounded read such as
   `git ls-remote origin refs/heads/main`, using the repository's real default
   branch. Record exit status and observed ref. Inspect differing push URLs
   separately; origin read does not prove a different push destination works.
5. Record push as `not exercised` until the owner performs a normal authorized
   source push. Do not create commits/branches or push through another worker just
   to manufacture onboarding success. API permission metadata and a dry run do
   not prove an actual push. After repair, the task owner reconciles current main,
   runs affected checks and pushes its own accepted changes.

## Diagnose a rejection before changing credentials

Retain the first failure, effective invocation and public fingerprint. Distinguish
missing key, host-trust failure, local signing/agent failure, server rejection of
the offered key, and repository authorization failure. Use existing diagnostic
logs first; bound any new diagnostic and avoid repeated unchanged retries.

Within an authorized credential diagnosis, derive the public key **locally on its
own machine** with `ssh-keygen -y -f KEY` and compare fingerprints to the `.pub`
and registration. Do not print, copy or export the private key. Keep public output
in memory/private evidence and prevent passphrase/UI prompts in unattended runs;
if normal unlock is required, report that concrete dependency. File metadata alone
cannot prove the private/public pair matches.

Public `GET /users/ACCOUNT/keys` can establish that a fingerprint is listed, not
that SSH works. When owner-admin metadata inspection is in scope, use an already
authenticated supported client: confirm its intended account with `GET /user`,
then read the exact `GET /user/keys/KEY_ID`. Do not extract tokens or sign in as a
different account. Record only allowlisted fields such as ID, verified, read_only,
created_at, last_used, returned scope/status and request ID. A field not returned
(for example expiry or suspension) is unknown, not false. Stop if existing scope
is insufficient rather than broadening it implicitly.

A listed, verified key can still be rejected. If a later authorized bounded check
accepts the **same** key and repository read passes without changes, record access
as recovered with cause unresolved; preserve the earlier failure and do not rotate
the key. If account/repository restrictions or explicit revocation are established,
use the supported owner/provider resolution rather than an alternate identity.

When onboarding or repair authorizes a new worker key, create it at a new private
path on that worker and register only its public key through the intended owner's
supported interface. Choose access limited to the authorized repositories. Preserve
old files and registrations unless removal is separately in scope. Back up and
narrowly change only the relevant identity selection; preserve host trust and
unrelated configuration. Qualify the resulting intended route before handing it
back. A metadata-only investigation does not authorize this mutation.

## Evidence and handoff

Keep a private `0700` directory with `0600` receipts: machine/worker, timestamp,
checkout and intended remote/account, public fingerprint/registration ID, effective
route and trust provenance, authentication result, repository ref/read result,
push status, changed paths/backups (or explicitly none), limits and process
settlement. Never include private keys or tokens. Send the result to Coordinator;
the owner retains its claim, local commits and normal integration responsibility.

Official contracts: [SSH connection test](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/testing-your-ssh-connection),
[GitHub host fingerprints](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints),
and [SSH key REST metadata](https://docs.github.com/en/rest/users/keys).
