# Permissions, app access and signing

## Qualify the executing identity

Maintain a matrix per machine with actor path/bundle ID/signature, launch context, capability, observed status, test and pending human action. A green toggle in another account/app/VM is not proof for this actor.

| Layer | Establish |
| --- | --- |
| macOS privacy | Actual app/helper's Accessibility, Screen Recording and action-specific event input grants |
| Files/volumes | Exact process can access source, fixtures and artifacts, including removable-volume prompts |
| Codex app tools | Signed-in guest app and worker chat can discover/use requested Browser, Terminal and other tools |
| SSH/Git | Correct public-key enrollment, verified host trust, actual account authentication and intended repository read under this worker's identity; see [GitHub access](github-access.md) |
| Queue | Unique enrolled worker, scoped credentials, TLS trust and actual heartbeat |
| Signing | Matching certificate **and private key**, usable keychain, profile and app entitlements |

Run consent setup in the logged-in desktop where interaction is needed. SSH does not reproduce a GUI launch context. Request only the missing category for the exact actor; the human completes genuine OS/app consent and authentication. Recheck after required relaunch. TCC copying/edits, broad Full Disk Access, synthetic security-dialog clicks or identity changes to bypass denial are not setup steps.

## Peekaboo and UI runners

Install vendor-signed Peekaboo or independently verify its official signed standalone binary; record the actual installation method. A binary copied into a Homebrew-looking path is not necessarily package-managed. Verify signature, architecture, version and executable path. Do not ad-hoc re-sign it to imitate an approved actor.

Use `peekaboo permissions status --all-sources --json` to distinguish Bridge from local state. Screen Recording and Accessibility are required for the usual UI workflow; Event Synthesizing depends on the action. Current Peekaboo uses native UI APIs, without requiring a generic AppleScript Automation grant. Request missing local access using supported `permissions request` commands and let the human complete consent. Verify real capture/input through the intended execution route. [Peekaboo permissions](https://peekaboo.sh/commands/permissions.html).

Historical Studio verification observed all three grants; it did not grant them to either VM. VM2 later needed its own consent. `--no-remote` is useful for routing diagnosis only when local execution is permitted and correctly consented, never as a denial workaround.

For Nova, use stable runner families and their current setup helper/permission contract. Keep bundle IDs and valid signing requirements stable. A new task/worktree should not invent another permission identity. Permission-only removable-volume preflight is separate from feature execution and category consent. Build through guarded storage, inspect actual setup results and establish cleanup before retrying.

## Codex guest connections and bridge

Install/open the supported app and sign in independently per guest. Connect the actual guest remote host and bind the intended project path. Discover tools and test inside the worker chat. Copied config, running app, SSH success or queue heartbeat cannot prove app-tool access. Settings screenshots support diagnosis; actual tool behavior is decisive.

For denied Browser access, identify the app connection/settings requirement and arrange its supported human action. Refresh/reconnect the appropriate session only after owned operations settle, then run a fresh browser challenge in the same chat. Shell HTTP or another machine's browser does not qualify this Browser.

A historical bridge rejected `untrusted-code-signing-identity` when Python was inserted between signed Codex CLI and signed Node MCP. The unchanged official launcher with accepted ancestry worked. Prefer exposed supported tools and the current signed launcher. Do not transplant socket paths or patch the authorizer. If a current supported manual MCP route needs diagnosis, inherit its current pipe environment and use actual current thread/turn metadata. Initialization may succeed while `tools/list` fails: inspect both. Historical PTY canonical mode truncated long JSON; fix framing before retrying requests whose outcome is uncertain. Old private launcher paths are not a stable API.

## Signing and keychain

Scope signing setup to the workload. Development builds do not automatically require distribution/notarization credentials. Reuse existing accounts/team through supported Xcode/Keychain import/sign-in. Profiles contain no private keys. Do not create/revoke certificates, export private keys or copy whole keychains just because a profile is missing.

For requested transfers, use an authenticated encrypted channel and private files/directories (0600/0700). Validate checksums, CMS metadata, team, exact app ID, expiry and platform/environment. Inspect current Xcode cache locations and project conventions; preserve differing existing files. Keep credentials, profiles, keys and passwords out of this skill and Git.

When using Nova's optional named-credential sync, consult the current repository's
`docs/credential-sync.md`. Local installation prepares a distinct key and empty
private store; it does not enroll the peer, enable Remote Login or transfer any
credential. Enroll and transfer only the explicitly named items through that
supported workflow. Neither queue credentials nor OS consent are implicitly cloned.

Check keychain search list and identity availability without dumping secrets. Test an authorized signing operation against an owned disposable artifact in guarded storage. An identity listing does not prove private-key access, and Terminal success does not prove unattended worker access. Cancelled signing consent is a stop, not permission to replay the prompt.

The VM2 repair ran an explicitly configured, pinned signing-preparation helper immediately before **each** codesign operation because prior keychain state did not suffice later. If the current project supports it, inspect and reuse its current implementation/configuration within scope. Do not hardcode the old helper or invent a blanket password-unlock script. Never expose passwords in logs, shell history or command arguments.

Verify entitlements as well as signature: signed apps can fail Keychain/vault access due to profile, application-identifier or access-group mismatch. Notarization credential readiness is separate, and neither setup nor signing authorizes release/upload.
