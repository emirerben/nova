# Editorial taste learning

Goal: propose ideas the creator would actually publish, using their approved
videos and slide posts as evidence. Execution correctness and creative quality
are separate outcomes. KRI-336 and KRI-731 are related; no Linear writes made.

## First calibration

The creator selected their last ten videos/slide posts and explicitly confirmed
that all ten are good. Treat all as positive examples with equal weight. Do not
infer that every historical chat message, QA instruction, or AI-authored title is
a durable taste rule. Store actual saved words, available source descriptions,
original requests and artifact identity separately from proposed interpretations.
Creation/export is not evidence of social publication or audience performance.

Private account captures, exact wording, review receipts and media stay outside
Git. Tracked examples/tests are synthetic. The first ten are development data;
use future, different footage for the creative-quality evaluation. Do not claim
that resampling these examples establishes generalization.

## Delivery sequence

1. Import the owner-checked examples into a versioned private corpus. Preserve
   provenance, missing media and the creator's explicit positive selection.
2. Present an inferred editorial profile for correction. Only human-confirmed
   rules enter generation context. Keep simple mood posts, practical reviews,
   jokes, lip-sync and personal stories available rather than forcing one form.
3. Compare proposals on the same new footage with A / B / neither, a short
   reason and optional rewrite. Keep model identities hidden during judgment.
   Selection and export approval remain independent.
4. Integrate bounded, account-specific examples and confirmed rules into the
   actual idea-proposal path. Current requests and source evidence take priority;
   examples never provide facts or permission for the current video. Record
   profile/example versions used by each generation and invalidate caches on change.
5. Evaluate old versus new on frozen, different footage. Primary outcomes are
   creator preference and willingness to publish, with separate feasibility,
   factuality and diversity checks. Freeze criteria before paid calls, keep
   examples/footage groups disjoint, count neither and failures, report ties and
   uncertainty. A model judging its own prose is diagnostic only.
6. Consider fine-tuning after retrieval/prompting is measured and reviewed
   comparisons expose a repeatable residual gap. Ten positives are a seed, not
   proof of an adequate fine-tuning dataset. Inspect supported provider tooling,
   pricing, privacy/retention and an explicit spend cap before a paid training run.
   Never upload account data or start training simply because an export exists.

Normal review/merge/deploy approval still applies. The camera-roll PR is currently
separate from main; do not stack a new branch onto an unmerged PR. The reusable
calibration boundary can be implemented from main before connecting that adapter.

## Failure catalogue (before implementation)

* Another account's project, a changed capture, an unknown id, or a duplicated
  example is admitted as the creator's positive evidence.
* An inferred rule is represented as human-approved or raw chat/QA instructions
  become standing preferences.
* A successful export is mislabeled as posted, viewed or creatively good.
* A seen development project can later be relabeled as unseen holdout.
* A stale profile review activates a different revision; concurrent writes lose
  earlier feedback or partially written JSON is read as valid.
* Re-importing the same material duplicates votes; changed material silently
  overwrites reviewed examples.
* Context includes rejected/unreviewed rules, the wrong owner's examples, URLs,
  excessive private transcript, or copies exemplar experiences into new facts.
* A file store accidentally writes private material inside a Git checkout.
* Free structural/replay checks are reported as improved live creative quality.

Verify the private corpus workflow through the real CLI, including tamper,
ownership, stale review and split-contamination controls. No provider calls or
production mutations are needed for this first gate.

## First gate implementation

`app/services/editorial_taste.py` and `scripts/editorial-taste.py` implement private,
versioned positive-example import, proposed/confirmed rules, bounded context,
source-pinned development comparisons, ties/neither with human reasons and an
exposure ledger. [Operator workflow](../../runbooks/editorial-taste-calibration.md).
The corpus is not yet connected to production generation. No creative-quality
improvement, deployment, fine-tuning readiness or independent holdout pass is claimed.

The real CLI journey was written first and failed on the missing CLI; after
implementation it passed. The comparison journey was also written first and
failed on the missing command before implementation. Four journey tests now cover
the happy path and owner, changed source, tamper, stale review, unknown project,
duplicate import, tied judgment, private-path and seen-holdout controls. Raw local
logs and real account captures are outside Git. Ruff lint and format pass.
