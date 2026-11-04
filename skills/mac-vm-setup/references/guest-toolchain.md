# Guest identity and toolchain

## Independent bootstrap

Set distinct host/computer names, guest account and VM name. Discover the actual home directory: login name need not match its basename, as happened on the reference host. Never assume old usernames, Homebrew prefixes or Python paths exist elsewhere.

Log in through the guest desktop and finish supported first-launch setup. Change template/default credentials through normal account settings before exposing the guest beyond its intended network. Clone the unpersonalized base before enrolling keys/accounts. For an already personalized image, explicitly resolve duplicated host, SSH and worker identities.

Discover IPs using the correct Tart home and `tart ip`; DHCP changes. Enable only requested remote access. Keep SSH server host keys distinct. Select client credentials through the [host baseline](host-baseline.md): the Nova user chose the coordinator host's exact GitHub private key for both VMs; other SSH purposes retain their separately authorized identities. Pin host keys using independently trusted fingerprints; `ssh-keyscan` alone is not authentication. Use `IdentitiesOnly=yes`, `BatchMode=yes` and strict checking for transfers. Reconcile changed pins rather than disable checking.

GitHub, host-to-guest SSH, queue access and Codex remote connection are separate checks. A forced-command read-only artifact alias is not a general shell/provisioning route. Test fresh noninteractive access without depending on old control sockets or forwarded agents.

Keep test HTTP fixtures on guest loopback when the browser runs in the guest. Host `127.0.0.1` is not guest loopback. A guest-to-host service needs a reachable host interface and narrowly scoped listener/firewall, not public exposure.

## Source and storage

Create a guest-local source checkout bound to the actual project/chat path. Give workers separate checkouts. Preserve branches, index, authored work and Git metadata. Copying a folder does not update project bindings. Record canonical path, repository and commit.

For Nova, optional helpers live in `nova-tools`: `docs/new-mac-setup.md`, `scripts/setup-new-mac.py` and `setup/new-mac.example.json`. The examined docs explicitly called the monolithic script authored/statically reviewed, **not tested on a new Mac**. Do not present it as proven unattended provisioning. Current repository code supports the Studio host and two VM workers plus retained legacy IDs; use [agent setup](agent-setup.md) for the role mapping and enrollment boundaries.

Configure a guarded artifact root outside source for each checkout. Read build-storage rules before compilation, archives, SDK extraction or copying app payloads. Keep host config ignored. Verify ownership against current UID, not the migrated account's old numerical UID.

## Xcode

Use full Xcode for Apple-platform development. Stage copied bundles on selected toolchain storage, preserve metadata, verify signatures and activate only after complete transfer.

Reference guests stored Xcode on `/Volumes/NovaWork/Toolchains/Xcode.app`; some project tools expected `/Applications/Xcode.app`. Use the tools' supported developer-directory override or an intentional verified alias without replacing an unrelated app. Recheck paths and mount availability after reboot.

Select the actual `Contents/Developer`, complete normal license/first-launch requirements with appropriate user/admin interaction, then check:

```sh
xcode-select -p
xcodebuild -version
xcodebuild -checkFirstLaunchStatus
xcrun --sdk macosx --show-sdk-version
xcrun --sdk iphoneos --show-sdk-version
xcrun simctl list runtimes -j
```

An SDK is not a simulator runtime. Run one small compile-and-execute smoke check through guarded output; record exact Xcode/compiler build. Do not silently use source directories or default DerivedData as storage fallback.

## Simulator

Select a supported runtime satisfying the requested target, create one named owned device, boot with a bounded wait, inspect semantic boot/migration completion, confirm SpringBoard, launch a built-in app, verify its PID/result, terminate and shut down the device. Leave unrelated devices untouched.

The migration misclassified a simulator as ready from command success despite migration failure. The claimed iOS 27.2 qualification was retracted; official iOS 26.2 build 23C54 was separately functionally qualified. This is a validation lesson, not a permanent version recommendation. An alternate runtime qualifies only itself and must satisfy acceptance; it does not validate the failed runtime.

Distinguish delayed metadata flush from genuine migration failure using boot/migration logs and functional launch. Avoid premature success or indefinite timeout extensions. Record cleanup and unresolved old runtimes separately.
