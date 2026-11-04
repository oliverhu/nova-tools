# Functional acceptance and cutover

Maintain a private capability ledger for each host/guest. Record `passed`, `pending`, `failed` or `not requested`, with exact actor/version/context and evidence. Never import historical success flags from example files.

| Capability | Acceptance |
| --- | --- |
| Storage | Correct host/guest UUIDs, canonical paths, writable APFS, physical headroom, all relevant checkout guards passing |
| VM boot | Intended external image actually open, usable guest desktop, rollback disks detached |
| SSH/GitHub | On each development host/guest: [verified trust and intended identity, fresh noninteractive authentication, and actual repository read](github-access.md); record normal push separately as verified or not exercised |
| Toolchain | Correct Xcode/SDK selection; small compile executes from guarded output |
| Simulator if requested | Semantic boot/migration, SpringBoard, app launch/termination, owned-device shutdown |
| UI automation | Real capture and scoped input/readback in owned fixture through actual worker/runner |
| Permission readiness | Workload-specific request inventory reviewed; required actor/category grants and Codex target-app access qualified during onboarding; unused categories recorded as not requested |
| Codex Browser | Discovery, form input/click, fresh rendered nonce, capture and tab cleanup in guest chat |
| Signing if requested | Actual authorized key use; correct signature/profile/entitlements for intended artifact |
| Queue if requested | Correct host/chat/project mapping, heartbeat, self-claimed bounded diagnostic and settled receipt |
| Restart | Scoped reboot/reconnect preserves mounts, toolchain, intended startup and usable session |

## Browser/UI challenge

Serve a small owned HTML form on an ephemeral guest-loopback port. Generate a fresh nonce. Through the worker's actual Browser tool, open the form, read accessibility state, type the nonce, submit and read the rendered confirmation. Capture when required. Close only the owned tab, stop the owned server normally, verify process/listener termination and remove only owned scratch. Preserve source/index before/after.

For desktop input use an equivalent owned fixture with known bounds. Resolve/report overlays such as Notification Center through allowed actions before hit-testing. A blocked target or missing input settlement fails the check; do not weaken assertions. Prove intended input and held keys/buttons/helper operations settled. PID absence alone does not settle an unknown outer tool operation.

An immediate port-rebind can report address-in-use after normal shutdown. Inspect actual server exit, listeners and connection refusal together before killing processes or declaring cleanup failed.

## Worker enrollment

For Nova, read [current agent setup](agent-setup.md): developers perform actual
validation and report directly to Coordinator, which records completion without
additional tests/review. Dispatcher wakes eligible idle sessions; it does not
claim for workers. Preserve claim ownership during completion submission.

Skip if no queue is requested. For Nova, read the currently installed coordination skill and live contract; do not copy historical retry limits, endpoint, worker IDs, role split or schedule policy. Concurrent hosts need unique enrolled identities. Reuse a replaced host identity only after the old owner/reporter settles and the replacement procedure is satisfied.

Install without starting work where supported. Record queue worker ID, remote host ID, chat ID and canonical project path separately. Start the requested reporter after credential/storage/network checks. Reuse existing dispatcher/schedules. Cloned credentials/session maps must not create duplicate owners.

If dispatch testing is authorized, use a bounded diagnostic per worker with stable request key and capability acceptance. Workers claim their own task. Record service task ID, generation, host/owner, actual report and cleanup. Dispatch success qualifies dispatch only, not Browser or simulator. Keep onboarding separate from ordinary feature and release work.

## Migration cutover and final report

Preserve authored/staged source, Git refs/worktrees, private app/session/browser state and signing material according to actual scope. Database migration needs a consistent snapshot or supported backup, not arbitrary live copies. Retire old reporters/schedules at the agreed cutover, preserve rollback originals and verify the new worker's actual role. Do not resume paused legacy schedules because a new host is online.

Deliver the filled machine manifest, ledger, evidence paths, pending human steps and rollback disposition. State unexercised operations, such as physical reboot or actual signing. Interrupted/denied operations remain pending with original evidence. Setup completion is not feature acceptance, integration or release authorization.
