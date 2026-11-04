# Set up a new Nova Mac

For a host with Tart guests, start with the [portable VM setup skill](../skills/mac-vm-setup/SKILL.md).
Read [current agent roles](agent-coordination.md) before worker enrollment: developers
validate and report directly to Coordinator; Dispatcher handles eligible idle wakes.

`scripts/setup-new-mac.py` prepares an Apple Silicon development Mac. It installs
the toolchain dependencies, obtains the browser source, configures the selected
APFS build storage, fetches and validates Nova provisioning files, installs the
coordinator client, requests Peekaboo permissions and prepares the five stable
Nova permission runners. It never starts a feature test, publishes a release or
uploads to Apple.

**Implementation status:** authored and statically reviewed; not executed or
tested on a new Mac. The request was to prepare the script without testing it now.
The optional credential-sync phase has separate isolated tests with temporary
homes/stores and synthetic files; those do not qualify the complete new-Mac setup.

## Before running

1. Install full Xcode at `/Applications/Xcode.app`, open it to finish installation,
   accept its license and install the iOS platform. Sign into the existing Apple
   Developer team `2243E5YVZ9` in Xcode. Use Python 3.9 or newer supplied by Xcode or
   your existing toolchain to start the script.
2. Enroll this Mac's own SSH public key for GitHub and the selected trusted
   provisioning source/cluster host. Verify their host-key fingerprints through
   your existing trusted device. The script requires strict known-host checking
   for private transfers; it never copies private SSH keys or auto-trusts a host.
   `nova-signing-source` is a new, normal SSH alias you configure for the source
   Mac. Do not repurpose the restricted `nova-artifacts-mac` key: that route is
   deliberately limited to reference artifacts.
3. Import the existing password-protected developer-account backup through
   Xcode Settings > Accounts, or the existing signing `.p12` through Keychain
   Access. The setup checks for Apple Development, Apple Distribution and
   Developer ID Application identities for Nova's team. Downloaded profiles do
   not contain the private keys. No certificate is created or revoked by this
   script. Keep backup passwords and signing assets outside this repository.
4. Mount the APFS build volume. Obtain its actual UUID with
   `diskutil info -plist /Volumes/NovaBuild`. Use a dedicated artifact directory
   outside source. This creates Nova's existing schema-1 host storage record;
   a physical replacement of a volume named `NovaEngineBuild` instead needs the
   schema-2 migration procedure in the browser's `BUILD_STORAGE.md`.

## Prepare the profiles on an existing Mac

First download current profiles in Xcode Settings > Accounts, or from the Apple
Developer account. The exporter selects unexpired, explicit Nova application
profiles for team `2243E5YVZ9` from both Xcode cache locations and the browser's
`.build/provisioning` directory. It does not export signing keys, account data,
other teams or wildcard profiles. The new Mac requires both macOS and iOS profiles.

```sh
python3 scripts/export-mac-provisioning.py \
  --browser "$HOME/Documents/ChatGPT/browser" \
  --output "$HOME/.local/share/nova-provisioning-export/nova-profiles.tar.gz" \
  --apply
```

The bundle is private (`0600`, parent `0700`) and contains profiles plus a checksum
manifest. Put the reported SHA-256 and absolute source path in the new Mac's
configuration. Each export uses a new filename if the selected profiles change;
existing different files are preserved. Provisioning profiles can contain device
identifiers, so keep the bundle off Git and use the authenticated SSH transfer.

## Configure and run on the new Mac

Clone `git@github.com:oliverhu/nova-tools.git`, then:

```sh
mkdir -p "$HOME/.config/nova-setup"
chmod 700 "$HOME/.config/nova-setup"
cp setup/new-mac.example.json "$HOME/.config/nova-setup/new-mac.json"
chmod 600 "$HOME/.config/nova-setup/new-mac.json"
# Edit the volume UUID, paths, source SSH alias and profile bundle SHA-256.
python3 scripts/setup-new-mac.py --config "$HOME/.config/nova-setup/new-mac.json"
python3 scripts/setup-new-mac.py --config "$HOME/.config/nova-setup/new-mac.json" --apply
```

Without `--apply`, the script only prints its plan. Run `--apply` from Terminal
in the logged-in desktop, as yourself, not with `sudo` or through SSH. Homebrew's
official installer can request administrator authentication. Existing Homebrew
packages are retained; there is no blanket upgrade. Peekaboo must come from its
signed `openclaw/tap/peekaboo` distribution. Existing browser branches and staged
work are preserved; setup fetches but never resets or pulls an existing checkout.

The server, bootstrap and installer recognize `oliverhu-mac-studio`,
`oliverhu-mac-studio-vm1` and `oliverhu-mac-studio-vm2`, plus the two retained legacy
IDs `oliver-mac` and `oliver-mini`. Enroll a distinct credential for each worker.
Do not reuse an active identity on a new machine. Legacy replacements require
settled work, a stopped old reporter and `old_worker_stopped: true`. A further
concurrent machine needs supported server/client identity configuration and
credential enrollment; changing only the example JSON does not add a worker.
Onboarding installs with `--no-start` and does not claim tasks, create chats or
start worker schedules. Coordinator/Dispatcher are separate chat roles, not new
worker credentials.
Once the new host is ready, start its existing reporter/dashboard flow explicitly:

```sh
python3 scripts/install-client.py \
  --config "$HOME/.config/nova-coordinator/client.json" \
  --repository-path "$HOME/Documents/ChatGPT/browser" \
  --build-path /Volumes/NovaBuild/DeveloperStorage/ProjectBuilds/browser
```

The optional internal-CA trust operation uses the existing authenticated cluster
bootstrap and login Keychain, and can prompt for consent. It does not install
global trust outside that existing flow.

## Permissions and signing completion

The script requests local Peekaboo Accessibility, Screen Recording and Event
Synthesizing access, opens the appropriate System Settings pane, then reads the
actual permission state. You approve the macOS prompts. There are no synthetic
clicks on permission dialogs, TCC database edits, resets or Full Disk Access
shortcuts. A denied or restart-required permission remains pending.

## Optional named-credential sync

See [credential-sync enrollment](credential-sync.md) for the separate local
installation and peer public-key review. Set `credential_sync.enabled` to `true`
only when deliberately enrolling that machine. It defaults to `false`; existing
schema-1 setup files without this section keep working. This independent phase
does not need toolchain/provisioning/permission phases:

```sh
python3 scripts/setup-new-mac.py --config "$HOME/.config/nova-setup/new-mac.json" \
  --phase credential_sync
python3 scripts/setup-new-mac.py --config "$HOME/.config/nova-setup/new-mac.json" \
  --phase credential_sync --apply
```

The first command remains read-only. Applying the phase creates a distinct local
SSH key and empty private store; it does not connect, enroll a key remotely, copy
credentials, enable Remote Login, or start any background service. Peer enrollment
requires a second explicit local command on the receiving machine. Setup completion
means locally prepared, not verified SSH access or a successful credential transfer.

All five existing Nova runner families are built and signed through the browser's
guarded SSD setup helper, then launched only for their permission-only removable
volume preflight. A failure stops that runner sequence without retrying input.
Those small setup binaries are not feature tests and grant no broader task access.

Install/open Codex and sign in separately. Approve its own first-use app-access
requests inside Codex; permissions granted to Terminal or Peekaboo do not transfer
to Codex, another app, an SSH session or a Bridge host. Product camera/microphone,
Bluetooth/passkey and other per-app capabilities still request their own consent
when actually used. Notarization credentials remain in the existing Keychain
workflow; this script does not copy an entire Keychain or store Apple passwords.

Profiles are checked again after transfer against the bundle hash and manifest,
CMS metadata, Nova team/app scope and expiration before installation in both
Xcode profile directories. Installed profile files use `0600`; an identical
existing profile is tightened to that mode if needed. The Mac release alias is selected only from a profile
that matches an available Developer ID private-key identity and Production
CloudKit. Existing differing files are preserved. Existing release scripts retain
their complete entitlement and signing checks; onboarding is not release validation.

## Resume and reporting

Each applied invocation writes a private report/log directory under
`~/.local/state/nova-setup/`. No credentials appear in the report. Exit `78` means
a named step still needs attention; exit `0` means the selected setup phases
completed, not that every app's access or any feature test is qualified. The
report explicitly keeps `all_app_permissions_verified: false`.

After fixing the reported prerequisite, rerun all steps or select a phase:

```sh
python3 scripts/setup-new-mac.py --config "$HOME/.config/nova-setup/new-mac.json" \
  --phase permissions --apply
```

Selected phases automatically include their prerequisites. The script rechecks
external state, retains identical existing files and never uses an old receipt
as proof of permission. After an interrupted runner, inspect its existing setup
receipt/cleanup before resuming. It does not blindly repeat failed commands.

## References

- [Apple: export and import developer accounts](https://help.apple.com/xcode/mac/current/en.lproj/dev8a2822e0b.html)
- [Apple: download provisioning profiles](https://help.apple.com/xcode/mac/current/en.lproj/deva899b4fe5.html)
- [Apple: control screen recording access](https://support.apple.com/guide/mac-help/control-access-screen-system-audio-recording-mchld6aa7d23/mac)
- [Peekaboo permissions](https://peekaboo.sh/commands/permissions.html)
- [Signed Peekaboo installation](https://peekaboo.sh/install.html)
- [Homebrew installation](https://brew.sh/)
