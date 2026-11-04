# Portable preconfigured Tart images

Yes: Tart exports a VM as a compressed `.tvm` archive and imports it on a
compatible Apple Silicon Mac. The reference host's installed CLI confirms
`tart export <name> [<path>]` and `tart import <path> <name>`. Check the destination's
Tart/macOS/guest compatibility and installed help before use. This is a stopped
disk image, not a live process checkpoint or a promise of instant readiness.

## Separate reusable setup from enrollment

Build a reusable template with macOS, Xcode/selected runtimes, tool dependencies,
stable signed permission-only runners, storage conventions and pinned skills.
Perform the workload's planned [permission onboarding](permission-onboarding.md)
in its actual guest desktop. Preserve its normal OS state when freezing the
whole VM, but verify required grants after import; never extract/edit its TCC
database or treat a template receipt as a grant to a different actor.

That includes the guest's stored Accessibility permission state. It does not
include the host's grants or establish Codex's separate app-access authorization.
An unchanged guest/app may retain its existing access; this has to be checked on
the restored VM, not promised for every clone. macOS uses the app's designated
code requirement to recognize it for privacy access, so keep the valid runner
identity stable across rebuilds. See [Apple's code identity explanation](https://developer.apple.com/documentation/technotes/tn3127-inside-code-signing-requirements).

Keep live queue enrollment/reporters, Codex remote/chat bindings and active work
out of the reusable template. Prepare it before enrolling personal accounts or
credentials instead of trying to scrub a live developer's VM. Stable tool/app
identities can reduce repeat setup; actual OS consent remains context-specific.

After import, securely provision the selected coordinator's **exact GitHub key**
and selected signing/provisioning assets from the private host baseline. This is
the existing shared-key policy, not key regeneration per clone. Enroll distinct
machine/SSH-server/worker identities, establish actual account/keychain access
and connect the intended chat/project before enabling the existing reporter.

A full personalized recovery archive is a different artifact: it includes
private keys, account sessions, profiles, consent state and potentially work.
Keep it encrypted, access-controlled and private; it is not a general distribution
image. Restore it as the same settled worker, or resolve duplicate identities
before another copy can connect. Never publish a personalized image to a public
registry. The reusable template plus private enrollment is the normal choice.

## Freeze and move

1. Arrange a maintenance boundary with the VM owner. Settle owned tests, UI and
   signing operations, preserve source/index and receipts, and pause the exact
   startup/reporter entries so they cannot relaunch. Shut down the guest cleanly
   and verify it is stopped with no image writer. Do not stop an active VM merely
   because someone asked whether images are portable.
2. Verify source/destination storage guards and capacity for the archive and
   imported disk. A sparse 990 GB virtual disk need not create a 990 GB archive,
   but budget from actual allocation and compression; do not promise a size.
   New image/archive payloads belong on verified external storage. Keep the
   small manifest and receipts local/private.
3. Use a new archive name and the correct source `TART_HOME`. Do not overwrite
   the source or an existing backup. The complete Tart export carries its VM
   disk/configuration; separately inventory additional attached disks and host
   directory shares, which must be transferred/rebound explicitly.

```sh
# All values come from the checked plan. The source VM must already be stopped.
TART_HOME="$SOURCE_TART_HOME" "$TART_BIN" export "$TEMPLATE_VM" "$NEW_ARCHIVE"
shasum -a 256 "$NEW_ARCHIVE"
# Securely transfer and compare the archive hash before import.
TART_HOME="$DESTINATION_TART_HOME" "$TART_BIN" import "$NEW_ARCHIVE" "$UNUSED_VM_NAME"
```

Record archive hash, source image/version, macOS build, Xcode/SDK/runtime versions,
Tart version and disk format, skill commit, baseline revision, included disks,
known permission gaps and whether secrets are present. For broad portability,
prefer a standalone image; layered/stacked disks have extra host/version and
base-image requirements. Retain the original until a distinct import qualifies.

## First boot and acceptance

Rebind destination external-volume UUID/Tart home, paths, startup configuration
and workload-appropriate CPU/memory; do not clone Studio's job count to a VM.
Check unique machine name, network identity, SSH server trust and worker bindings
before allowing concurrent operation. Do not assume import has fixed identity
collisions across different hosts. A guest volume may retain its UUID while the
host physical volume differs: verify both layers and each checkout's storage guard.

Apply the pinned skills/baseline and run the affected onboarding checks: boot and
desktop, guarded storage, toolchain, GitHub account/repository access, requested
signing capabilities, selected UI actor/category access and owned cleanup. Preserve
working grants and request only genuinely missing consent in a grouped session.
Keep reporters off until distinct enrollment is settled. Coordinator records
actual readiness; archive creation alone does not mean a clone is ready for work.

Sources: [Tart quick start](https://tart.run/quick-start/),
[export implementation](https://github.com/cirruslabs/tart/blob/main/Sources/tart/Commands/Export.swift),
[import implementation](https://github.com/cirruslabs/tart/blob/main/Sources/tart/Commands/Import.swift).
