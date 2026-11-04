# Learning provenance

Compiled November 3, 2026 (America/Los_Angeles) from Studio migration artifacts, local setup docs and a targeted delivery-chat review. This synthesizes available evidence; it is not a claim of exhaustive review of every chat/path. No historical approval, private endpoint, token, key, machine UUID or active authority is bundled.

Original evidence root: `~/Migration/studio-20261102/`. These are optional audit pointers on the original machine, not new-machine dependencies.

| Source | Learning |
| --- | --- |
| `prepare-vms.py`, `vm-preparation.json` | Base cloning, historical compute choices, preparation distinct from readiness |
| `external-vm-config.json`, installed `~/.local/bin/nova-vm` | Per-VM Tart home/UUID guard, optional extra disk, initial hashes historical |
| `single-disk-20261103/completed.json`, `final-verification.json`, `host_ops.py`, `guest_health.py` | Final single-disk layout, detached rollback, verified copy, all checkout guards refreshed |
| `install-vm-xcode.py`, `vm-compiler-qualification.json`, `vm-local-source-and-xcode-alias-qualification.json` | Staged Xcode/signature checks, compiler smoke, checkout/path compatibility |
| `studio-ui-tool-all-permissions-verified.json`; browser `NOVA_CHAT_ROLES.md` | Actor-specific consent and stable runners; historical role policy needs reconciliation |
| `dispatcher-cutover/bridge-repair-receipt.json` | Signed ancestry, dynamic pipe, real turn metadata, PTY framing |
| `dispatcher-cutover/fresh-chat-test-final-receipt.json` | Dispatch success did not qualify Browser |
| `dispatcher-cutover/capability-gap-repair/vm2-browser-repair-generation4-result.json` | Real nonce input/click/readback/capture and cleanup |
| Same repair directory, `vm2-simulator-repaired-final-report.json` | False qualification retracted; exact alternate runtime functionally tested |
| Same repair directory, `both-vms-capability-completion-receipt.json` | Later completion superseded old pending snapshots while retaining failures |
| `~/Migration/nova-projects-20261103/sequential-head-tests-publish-*/vm2-peekaboo-gen1/installation.json` | Verified standalone vendor binary, not package-manager-owned merely by path |
| Same directory, `vm1-signing-readiness-resume-receipt.json`, `vm2-signing-preparation-main-landing.json` | Signing cancellation/readiness scope, per-operation helper verified in actual builds |
| `~/Documents/ChatGPT/nova-tools/docs/new-mac-setup.md` | Helper scope, profile handling, explicit untested-on-new-machine limitation |
| Browser `BUILD_STORAGE.md` and parent `AGENTS.md` | Guarded payloads, ignored host config, preserve source/private data |
| “Nova — Delivery Coordinator”, recent November 3 turns | Roles/authority evolved; don't fossilize old holds or schedules into setup |

Official references checked:

- [Tart quick start](https://tart.run/quick-start/): bundle installation and VM configuration concepts.
- [Tart FAQ](https://tart.run/faq/): host keychain dependence and storage/network troubleshooting.
- [Peekaboo permissions](https://peekaboo.sh/commands/permissions.html): routing, categories and supported requests.
- [OpenAI skill format](https://learn.chatgpt.com/docs/build-skills): portable folder and optional resources.

Historical 8 CPU/24 GiB/990 GB dimensions and iOS 26.2 success are observations, not permanent recommendations. Select compatible software and size the new machine. Old disk-switch scripts are deliberately not bundled: hardcoded disk/account/UUID/authority assumptions are unsafe to replay elsewhere.
