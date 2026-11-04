# Lessons from failures

| Symptom | Learning | Response |
| --- | --- | --- |
| OS disk full; workspace has space | Separate 50 GB boot disk constrained OS/runtime installs | Prefer sized single guest disk/shared APFS; migrate after clean shutdown, backup and verification |
| External volume absent/renamed | Path text is not storage identity | Verify UUID, mount and resolved image; stop instead of falling back locally |
| One checkout builds; another fails | Conversion changed workspace UUID | Rebind/test every active checkout's private storage config |
| Xcode works interactively, script fails | Hardcoded developer path differed | Resolve override/alias and test real worker context |
| Simulator exit zero, app unusable | Boot/migration semantics missed | Require migration, SpringBoard and app launch; retract false pass |
| SSH/heartbeat succeed, Browser denied | Independent app access/connection | Supported guest settings action, then same-chat nonce check |
| GitHub lists the key, SSH rejects it | Registration metadata is not authentication; an earlier rejection can later clear without changes | Follow [GitHub access diagnosis](github-access.md); compare actual identity/pair/route, bound checks, preserve evidence and avoid unjustified rotation |
| MCP initialize succeeds, catalog fails | Signed peer ancestry rejected | Current official launcher and tools/list; preserve signature enforcement |
| Bridge waits/long JSON fail | Synthetic turn metadata or PTY truncation | Actual thread/turn IDs and complete framing; reconcile before replay |
| Permission green, capture fails | Wrong Bridge/local actor or desktop | Check selected source, identity and real capture route |
| Guest requests access again | Host/Terminal grants do not transfer | Exact guest actor's consent; stable runner identity |
| Target validation refuses input | Notification Center/overlay blocks hit test | Resolve within scope; retain refusal and input budgets |
| Signing hangs/interaction prohibited | Keychain state varies by context/time | Human authentication or scoped existing helper; actual signing check |
| Signed app's vault fails | Entitlement/profile mismatch | Compare identifier/access groups; retain functional acceptance |
| VM start keypair/Security Server error | Host login keychain unavailable | Normal host login/keychain availability; no empty-keychain shortcut |
| Old worker returns after migration | Reporter/job/schedule still active | Reconcile exact startup/owner state; avoid duplicate identity |
| Setup done, work fails | Install/connection/consent conflated | Separate capabilities, rerun affected real checks |

Inspect external state before retrying unknown UI, signing, dispatch or publication outcomes. Preserve evidence and correct the failing layer. Two identical preparation failures call for consolidated diagnosis rather than more successor packets, broader permissions or longer timeouts.
