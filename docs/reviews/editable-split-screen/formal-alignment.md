# Formal verification boundary

The historical execution below is not current-head admission. The read-only audit at `f00162b1`, against integrated main `a518aed8`, finds 36 drifts among 98 nonempty covered entries and two covered feature changes. See [the current verification record](verification-2026-11-07.md) and local `formal-source-audit-f00162b1c.json`. The shared coordinator owns model, coverage and verifier maintenance; none was changed here.

The unchanged pinned TLC 1.8.0 artifact was recovered from a local cache and independently verified against SHA-256 `fc6878c603f0fdc18b9fe3e5bf51860cb90ab5b1154e3c371660c036d6bdd63e`. `make verify-phone-render` ran through the supported `TLA2TOOLS` and `JAVA` overrides. Its model inputs were unchanged from `44dc5d213`.

Observed result: happy publication, 20 model scenarios, five historical counterexamples and 33 mutation checks passed their expected outcomes; no residual finding. The verifier exited 3 for 27 covered-function drifts (`make` exited 2). No source hash, invariant, bound, timeout, historical model or mutation was changed.

The source audit compares current covered functions with sync commit `b12bed356ff155ede19908dec43ef89b4c0062df`, and distinguishes split-screen changes from upstream behavior. Six Python functions and 21 Swift functions drift. The full local per-function reviews and extracted diffs are retained in `.dev/split-screen/formal-{python,swift}-drift-review.md` and adjacent audit files. Those reviews make no product-bug or all-current-code proof claim.

| Boundary | Alignment finding |
| --- | --- |
| Split Save preflight | Invalid local layouts return before Save pins or API mutation. The existing model represents accepted Saves and excludes contract refusals. This does not prove split geometry or persistence. |
| Split protocol floor 43 | Existing level/required-capability abstractions cover this class of protocol refusal. Direct API tests verify the literal 42/43 behavior. |
| Durable Stop/Resume | Upstream code adds persisted stopped/retry-pending/released or unreadable intent and awaitable retry acknowledgement. The model has no corresponding durable intent states or interleavings. |
| Account ownership | Upstream read/report/publish paths fence owner changes around awaits. Request identity alone does not prove cross-account safety. |
| Coordinator commands and runs | Upstream start, cancel, recovery and execution-window completion use command/run generations. Atomic model actions omit these reentrant boundaries and some moved helpers are outside function hashing. |
| Export quality advisories | Material upstream policy is explicitly outside this lifecycle model. A lifecycle pass cannot establish request or pixel fidelity. |

A global `--resync` would erase meaningful drift without establishing alignment. Resolve the durable-intent, owner and command/run dimensions, or provide a reviewed refinement argument that establishes an accurately bounded claim, before clearing those hashes. Preserve existing invariants and add separately caught mutations for new modeled safety behavior. The feature's two scoped covered-function changes are not justification for clearing unrelated upstream drift.

The comment excluding GET 426 at the top of `PhoneRender.tla` also contradicts its protocol-floor description and `RecGet` action. This documentation discrepancy should be clarified as part of model maintenance; it is not a reason to remove protocol checking.

The unresolved gate is now **source/model alignment**, not installation or a failed checksum. The artifact guard was preserved throughout.
