---
name: mac-vm-setup
description: Set up or migrate an Apple Silicon Mac host and Tart macOS development VMs, including APFS storage, Xcode, SSH, Codex connections, UI permissions, signing readiness, and verified worker onboarding. Use for a new machine or repairing this host-and-guest setup; not for generic cloud VM provisioning or app release delivery.
---

# Mac host and VM setup

Turn a new Mac into a verified development host with independent macOS guests. This skill distills the Studio/VM1/VM2 migration and repairs of November 2–3, 2026. Historical dimensions, versions, identities and approvals are examples, never defaults to copy blindly. This is an assisted setup procedure, not an unattended provisioning program.

## Start with the target machine

Read applicable `AGENTS.md` and build-storage rules before creating compiler output or app payloads. Inspect existing work, mounts, guests and startup jobs before changes. Keep source, private state and build payload placement explicit. Do not overwrite existing machine configuration with the example.

Run the read-only inventory helper from this skill's directory:

```sh
python3 scripts/inspect_machine.py
python3 scripts/inspect_machine.py --volume /Volumes/ChosenVMVolume
```

It reports selected local facts only; it does not establish permission, signing or worker readiness. Exit zero means inventory ran, even if an individual observation is missing or failed. Add `--toolchain` for Xcode/SDK version queries. It does not start guests, request permissions, unlock keychains or contact the queue. Copy [assets/machine.example.json](assets/machine.example.json) to private setup state and fill it with observed values. It is a planning/receipt template, not a Tart or Nova configuration file.

Determine roles, VM count, workload, available memory, chosen storage and migration scope from existing context. Ask only for consequential missing choices. Continue independent preparation while human login or consent is outstanding. Ordinary scoped setup does not require repeated confirmation; destructive disk operations, removal of originals or expanded credential access need authorization for the actual action.

## Workflow

1. **Host and storage:** [references/host-storage.md](references/host-storage.md). Budget memory and real disk headroom, verify storage identities, install Tart and establish a recoverable layout and stable launch behavior.
2. **Guest and toolchain:** [references/guest-toolchain.md](references/guest-toolchain.md). Give each guest distinct machine/worker identities, SSH host trust, source checkout, Xcode and build-storage records. Use the selected shared-host GitHub/signing policy from [the host baseline](references/host-baseline.md); machine identity and client credentials are separate. Qualify the compiler and requested simulator separately.
3. **Access and signing:** [references/permissions-signing.md](references/permissions-signing.md). Use [permission onboarding](references/permission-onboarding.md) to scrutinize test requests and group only necessary first-use consent before development. Qualify the actual execution actor on each machine. For development hosts and guests, run the [GitHub access checks](references/github-access.md) under each worker's own identity before declaring source access ready. macOS privacy, Codex app access, filesystem access, SSH, queue authorization and signing are different layers.
4. **Nova agent roles:** [references/agent-setup.md](references/agent-setup.md). Map developers, Coordinator and Dispatcher, enroll unique workers, and retain claim-compatible completion without Coordinator retesting.
5. **Acceptance and cutover:** [references/verification.md](references/verification.md). Demonstrate requested capabilities through the real worker session, including fresh browser/UI input and cleanup. Keep missing capabilities pending.

For a reusable preconfigured VM, follow [frozen images](references/frozen-images.md):
freeze a stopped template, export a versioned archive and qualify a distinct
import before cutover. Preserve the shared credential policy while enrolling
machine-specific workers and checking local permissions after import.
6. **A failure:** use [references/troubleshooting.md](references/troubleshooting.md). Prefer a cause-specific correction over repeating provisioning or widening access.

Preserve original approvals, denials and unknown operation outcomes during migration. Reuse authorization valid for the present machine/actor/scope; do not invent new approval stages. A historical consent receipt or copied settings file is not a grant on a new guest. Never answer security dialogs synthetically, edit TCC databases, disable signature checks or add blanket Full Disk Access as a shortcut.

Clone a base image before enrolling accounts, signing keys, SSH identities and queue workers. Apply only the selected host-derived credentials after cloning; keep SSH host keys and queue credentials distinct. Give each worker its own checkout and private profile. Host and guest foregrounds are separate resources. Existing queue policies and user stops remain authoritative; this skill does not itself claim work or create schedules, chats or releases.

## Finish with evidence

Report machine/VM identities and paths, exact tested toolchain/runtime versions, storage checks, capability results, remaining human actions and rollback location. Separate `installed`, `connected`, `permission verified`, `functional check passed` and `cutover complete`. A heartbeat or successful shell command cannot establish the whole VM's readiness.

For provenance and evidence limits, read [references/learning-sources.md](references/learning-sources.md). The procedures are self-contained; old machine records are optional audit material, not dependencies.

## Portability

Copy this entire `mac-vm-setup` directory into the destination Codex user skill directory. Here that is `${CODEX_HOME:-$HOME/.codex}/skills`; use the destination installation's documented location if different. Invoke `$mac-vm-setup` in a new chat, or provide the absolute path to this `SKILL.md`. No credentials, consent records or VM payloads belong in the bundle. Folder-based packaging follows the [official skill format](https://learn.chatgpt.com/docs/build-skills).
