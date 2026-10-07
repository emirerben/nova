# KRI-506 engineering review

## Implementation contract

Main Creator treats missing opening or closing copy as a server-owned decision. It
returns a typed `creative_decision` alongside the normal response. An unresolved
request creates an authorship question. A generated candidate creates a second,
wording-specific question. The candidate is never executable until the creator
approves that exact wording and then approves the complete edit plan.

The durable event fold is keyed by the question ID, offered option, candidate digest,
and owned-media dependency digest. A creator-supplied resolution preserves the exact
string and records creator provenance. A generated candidate retains generated
provenance. Replacement questions invalidate older approvals; stale media requires a
fresh question. Cancellation survives as an explicit omitted target, so an
unresolved title cannot fall through to a generic or model-generated fallback.

Copy-bearing v2 turns stay on the planner path. They do not use the editor text
shortcut, which could otherwise stage unrelated text while copy approval is pending.
The unresolved-choice gate injects approved text or `omitted_copy_targets` into the
strategy only after folding durable events. The legacy path retains its existing title
guard and must-not-ask behavior for ordinary creative defaults.

No endpoint, migration, model configuration, or client question-card contract changed.
Existing question cards and plain-text fallback responses remain valid.

## Failure and edge-case evidence

- Unknown or fabricated selections are ignored because no persisted question offers
  them.
- Delegation to generate does not approve wording.
- A stale selection from an older candidate cannot approve a replacement candidate.
- Malformed candidate digests fail closed.
- Exact creator text, including Unicode and spacing, is retained by the resolution
  path.
- Approved copy is reused on continuation; cancellation remains explicit even when
  media changes.
- Ambiguous exact on-screen wording versus spoken or visual treatment is routed to a
  clarification question. Ordinary hook concepts, pacing, music, and treatments keep
  their existing decisive defaults.

## Verification

Required verification completed against the isolated local PostgreSQL test database:

- `make verify-kria`: 1,442 passed, six existing conditional skips; public tool,
  mobile, and iOS OpenAPI snapshots unchanged.
- Main Creator prompt replay corpus: 53 cases, including six new KRI-506 scenarios.
- Focused creator, consent, and legacy route tests, including reversed model option
  order and Turkish consent labels, pass.
- Existing web thread, approval card, and creator panel tests: 73 passed.
- Preship checks cover scoped lint/format, upstream drift, and release-metadata ownership.

The final command outputs and exact commit are recorded in the autoship evidence.
Replay fixtures establish parsing and contract regressions; no paid live model quality
claim is made. Native client code is unchanged, so mobile compatibility is checked
through the existing public schema snapshots and question-card payload contract.

The PostgreSQL suite exposed an existing test fixture that inserted revision 3 without
advancing its thread from revision 2. The fixture now advances the revision, allowing
later sweep tests to observe it without a uniqueness conflict. This is a test-only fix.

## Review decisions and remaining verification

No new endpoint, table, migration, or model/reasoning change. Copy-bearing revision
threads take the Main Creator replan path, which costs an extra planning call relative
to the editor shortcut; this keeps generated rewrites behind wording consent.

Model-generated option labels cannot determine consent. A language code selects fixed
server translations for stable option keys. An incidental phrase in a creator message
is not sufficient title authorization: typed intent metadata or grounded render-intent
evidence is required.

Post-deploy smoke checks remain behind explicit merge/deploy approval: vague hook,
explicit generation, revision, exact wording acceptance, full-plan acceptance, reload,
and cancellation. The existing Fly workflow deploys main; release metadata remains
owned by post-merge automation.
