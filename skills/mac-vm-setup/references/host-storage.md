# Host and storage

## Inventory and size

The reference setup used an Apple Silicon Studio and two Tart guests, each with 8 vCPUs, 24 GiB memory and a 1920×1080 display. These were workload choices, not minimum requirements. Leave capacity for the host, concurrent builds, both guests and simulator processes; measure memory pressure instead of assigning every core or byte.

Inspect `uname -m`, `sw_vers`, `sysctl hw.memsize hw.logicalcpu`, `diskutil list`, `diskutil apfs list`, and free bytes. Distinguish:

| Layer | Meaning |
| --- | --- |
| Physical SSD | Actual capacity, connection, writable state and physical identity |
| Host APFS volume | Mount, UUID, reserve/quota and real remaining container capacity |
| Tart virtual disk | Guest-visible logical size; sparse allocation and clones complicate physical usage |
| Guest APFS container/volumes | Capacity available to macOS, Xcode, runtimes and workspace |

The final layout had a separate 1 TB host APFS allocation per guest and one 990 GB virtual disk inside each. The guest's macOS and `NovaWork` APFS volumes share the same container. Avoid the earlier 50 GB boot disk plus large separate workspace: OS updates and simulator runtime installation can fill the boot disk while workspace capacity sits unused. Two guest volumes in one container are not two virtual disks.

A quota is a limit, not extra SSD space. Account for base-image cache, rollback copies, sparse-file growth, APFS snapshots and host build payloads. Ten GB of nominal allocation headroom was a historical choice, not a general backup or free-space guarantee. Choose a larger margin when needed.

APFS reserved/quota space can make capacity reports look contradictory. During this skill's inventory check, `diskutil info` reported zero container free bytes while `statvfs` reported substantial space available to the reserved guest volume. Reconcile `diskutil apfs list`, volume reserve/quota and filesystem-available bytes before concluding the SSD is full or that all reported capacity is globally available.

## Install and locate Tart

Use the official package or a verified release archive. With manual installation, retain the app bundle and execute `tart.app/Contents/MacOS/tart`; do not detach the executable from its provisioning context. [Tart installation](https://tart.run/quick-start/).

Resolve the executable in the new environment. Check `--version`, `clone --help`, `set --help` and `run --help` before generating version-specific commands. Select a compatible macOS image, record its resolved digest, and use an immutable image for reproducibility. Do not freeze an old `latest` tag or Xcode version as a universal requirement.

Configure storage before a large download. Each guest can have its own `TART_HOME`; our shared cache was external too. Specify the same home for clone, list, set, run and IP discovery. The old wrapper set `TART_NO_AUTO_PRUNE=1` to preserve evidence; verify support and capacity implications before using it. Pruning does not authorize removing retained originals.

Example for **a new guest**, after validating its mount and ensuring its name is unused:

```sh
export TART_HOME="$VM_TART_HOME"
"$TART_BIN" clone "$PINNED_IMAGE" "$VM_NAME"
"$TART_BIN" set "$VM_NAME" --cpu "$VM_CPUS" --memory "$VM_MEMORY_MIB" --display 1920x1080
"$TART_BIN" set "$VM_NAME" --disk-size "$VM_DISK_GB"
"$TART_BIN" run "$VM_NAME"
```

Supply all variables from the new plan. Check disk-size units; our 990 produced 990,000,000,000 bytes. Growing a virtual disk does not alone prove the guest APFS container expanded. Discover current devices and verify guest capacity before creating a workspace volume. Never paste historical device identifiers or UUIDs into a disk mutation.

## Mount guards and startup

Before launch, validate the canonical configured mount with `diskutil info -plist`: actual mount point, UUID, APFS, writable state, and external/physical identity where required. Check that the Tart home and image resolve beneath that volume. Missing storage must stop launch, not create a same-named local directory. A display name such as `Untitled` is not identity.

Use an ordinary logged-in account with a working login keychain. Tart documents host keychain-related VM-start failures; diagnose that layer before changing the guest. [Tart FAQ](https://tart.run/faq/).

After a successful manual run, configure a user LaunchAgent only when persistent startup is requested. Use absolute executable/config/log paths and explicit environment: LaunchAgents do not inherit shell PATH or expand shell variables in `ProgramArguments`. Use the same guarded wrapper. Do not introduce broad passwordless sudo or an empty keychain. Pause the exact startup job during maintenance so KeepAlive cannot race a disk switch, then restore its recorded state.

SSH or a running Tart process does not prove desktop readiness. Each guest needs a usable logged-in graphical session for interactive tests. Reboot qualification includes host mounts, guest boot, desktop, keychain behavior and actual worker connection.

## Existing-disk migration

Record work and startup state, settle owned operations, stop the guest cleanly and verify image handles closed before copying/resizing. Preserve original image, NVRAM, VM config and launch config. An actively written disk copy is not a consistent backup.

The tested conversion enlarged the boot disk, expanded guest APFS, created a workspace volume in the same container, copied the old workspace attached read-only, then verified bytes, permissions, symlinks and applicable metadata before switching. Originals remained detached for rollback. This outline is not a blind disk script: plan from actual inventory.

A new guest volume has a new UUID. Update and test **every** affected checkout/worktree's private storage record. Preserve source/profile/session placement under project policy. A guest disk can be external-SSD-backed while reporting internal inside the VM; host and guest guards validate different layers.

For Nova, read current `BUILD_STORAGE.md`: ordinary explicit host configuration uses exact schema-1 fields (`schema`, `hostname`, `volume`, `volume_uuid`, `artifact_root`), current owner and mode 0600. Physical `NovaEngineBuild` replacement has a separate schema-2 process. Do not transplant UUIDs or infer internal fallback. Run the existing storage helper's noncreating check in each active checkout before builds.
