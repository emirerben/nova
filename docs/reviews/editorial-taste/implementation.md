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

## Account integration failure catalogue (before implementation)

The same camera-roll task branch will own the calibration boundary and its real
proposal adapter. The main-based calibration checkpoint has been brought into
that existing task; no new PR is stacked on the unmerged camera-roll PR.

* Client-supplied owner ids, words or approval flags admit another account's posts.
  Build examples from owned server records; require explicit confirmation of the
  selected projects and exact proposed rules. Do not infer preference from export.
* Retries duplicate examples/feedback, stale revisions overwrite a newer profile,
  or an old network retry revives memory after Forget. Serialize on the owner;
  retain minimal erased operation identities while deleting private bodies.
* Deleting a source project leaves its copied exemplar text in personalization.
  Store source bodies in project-cascading rows and dereference them at generation.
* Feedback on a corrected/rejected story revision grades the wrong proposal.
  Pin the exact offered wording, moments and revision; changed identities conflict.
* Feedback becomes render approval, or a single contextual judgment becomes a
  standing rule. Keep proposed words/approval and explicit preference confirmation
  separate. Both positive and negative judgments remain contextual evidence.
* Changed preferences/feedback, pausing Memory or erasure reuse the old story cache.
  Bind the private context and revision identity into the actual agent input/cache.
* The global rollout flag or account Memory pause is bypassed, private exemplars
  leak into public story receipts, or a private path appears in provider/admin logs.
  Keep context owner-scoped, bounded and under the agent's sensitive-IO boundary.
* Prompt-only changes retain the same version or recorded-response tests are
  reported as creative-quality wins. Bump the prompt version; API/DB tests prove
  wiring, not whether the creator would publish a new live-generated idea.

Gate: real HTTP and PostgreSQL import -> generation input -> feedback -> changed
cache -> pause -> forget, plus ownership, source deletion, stale/idempotency and
old-retry controls. Use a recorded model response only at the provider boundary;
no paid provider call or production mutation is needed for this integration gate.


## Account adapter implementation

The real collection-story route now consumes the account profile, retrieves
examples against the outing descriptions, binds its cache to the private context,
and rejects results if personalization changed during generation. Prompt
`2026-10-21.1` removes the forced audience-choice example and explicitly supports
useful guides, firsthand opinions, concrete observations and simple mood posts.
Opaque receipts pin the exact profile/source hashes without copying exemplar words
into story history. Native controls expose confirmation, contextual judgments,
pause and Forget. Full Memory clear scrubs taste with the feature flag off.

The four real HTTP/PostgreSQL journeys pass, including foreign ownership, stale
confirmation/feedback, idempotency, erase/re-consent/late retry, source deletion,
account pause during a provider call, and old Memory-clear retries. Combined with
existing collection and CLI journeys: 27 passed. Existing memory/replay regressions:
46 passed. The private ten-example metadata replay reaches the actual account API
and context builder with four confirmed rules and the contextual Troya guide choice,
zero paid calls. The native preference/rating/Forget and existing story-correction journeys both
pass (two UI tests, `.dev/editorial-taste-native-4.xcresult`). The full free backend
replay suite plus account journeys passed 1,248 checks (15 skipped). No production rollout,
independent creative-quality win or fine-tuning readiness is claimed.


Current-main integration retains both camera-roll consent revalidation and the
approval-readiness guard. Combined execution/readiness/taste/replay checks pass:
2,139 passed, 15 skipped and seven expected failures. Both native journeys also
pass on the merged branch (`.dev/editorial-taste-merged-native.xcresult`). CI maps
the new account journeys and native preference/rating/Forget test to changed paths.

The creator subsequently authorized the immediately preceding ten eligible posts.
All twenty are positive development examples. Their original four confirmed rules
remain unchanged; no additional inferred rule has been activated. Some posts share
footage and several have no saved on-screen text; twenty projects are not twenty
independent outings or a fine-tuning sufficiency claim. The local authenticated
adapter replay accepts all twenty at zero cost. No production rollout occurred.


CI selection verification passes all 47 checks after registering the new taste UI
journey and the previously unclassified partial-index collection test. Runtime,
mobile JSON and mobile OpenAPI generated contracts all match. Pre-PR mapped incident
gates pass 1,044 checks. The live development preview is paused: automatic approval
review rejected transmitting private account examples/scene metadata to Gemini with
paid usage. A specific payload/provider/$0.05 approval is pending; no provider call
was made and no workaround was attempted.
