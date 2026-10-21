# Editorial taste learning

Goal: propose ideas the creator would actually publish, using their approved
videos and slide posts as evidence. Execution correctness and creative quality
are separate outcomes. KRI-336 and KRI-731 are related; no Linear writes made.

## Current checkpoint — 21 October 2026

The creator approved twenty finished posts and six scoped preferences, rejected
an advertising-like draft, preferred an explanatory opening, and separately marked
the complete developer editorial baseline Would post. Four real calibration
judgments are preserved privately. The complete positive proposal now reaches the
isolated account generator alongside earlier notes; no proposed rule or render
approval is inferred. Project notes allow 1,500 characters while generation remains
bounded to 8,000. The HTTP journey proves preservation and Forget erasure.

The creator declined more collections. Finish with this development material;
no unseen-footage quality result or fine-tuning-readiness claim follows. App
personalization is implemented behind its existing flag/allowlist, with no
production enrollment. Research still has factual-review and conservative source
refusal limitations; [current research evidence](../camera-roll-recommendations/idea-research.md)
is authoritative for current versions, live cost and quality limits. The sections
below retain earlier implementation milestones, not current pending questions.

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
gates pass 1,044 checks. The initial automatic approval review rejected transmitting private account
examples/scene metadata to Gemini until the creator approved the exact payload,
provider and $0.05 cap. The creator subsequently explicitly consented; the result
is recorded below.

## Frozen evaluation failure catalogue (before implementation)

The evaluation boundary must support real future footage and human judgments;
recorded/synthetic responses verify its mechanics only. No provider, upload or
training call is part of this boundary.

- A seen project, overlapping source fingerprint/group, changed corpus or partial
  source registry is presented as verified held-out evaluation.
- Model/arm labels influence the creator's choice, or a different shuffle changes
  the meaning of a recorded A/B vote. Freeze assignments and output hashes.
- Unknown/repeated source IDs, invalid durations or malformed output enter review.
- Failed model calls disappear from the denominator or silently get rerun until a
  desirable answer appears. Preserve failure slots and count pending/neither/ties.
- A changed output, stale review, another owner or repeated vote replaces the exact
  proposal that the creator graded. Immutable writes; identical retries add no vote.
- Preference is mistaken for factuality, willingness to publish, or execution
  correctness. Record these separately; do not declare quality from parse checks.
- Tiny, correlated development samples become a claimed generalization or
  fine-tuning-readiness result. Report scope and uncertainty, and never auto-start
  paid training from a count threshold.

Gate: real private CLI preregistration -> frozen two-arm outputs (or explicit
failure) -> blinded cards -> actual human judgment -> aggregate report, including
contamination, owner, tamper, stale-output and duplicate controls. Test content is
synthetic; account data remains outside Git.


## Final-source and comparison continuation

The account importer now captures only the creator-selected ready variant (or the
single unambiguous ready variant), rejecting unfinished/ambiguous examples. A
finished lip-sync or atmosphere post needs no saved generated text. Its saved form
is retained in retrieval context without claiming that the source footage contains
no visible text. Two HTTP journeys were added first and reproduced the old text-free
rejection and unfinished-positive acceptance; all six account journeys now pass.
The private replay now uses the exact saved job/slide records and original Untitled
item names: all twenty pass, including four lip-sync posts, twelve montages, three
slide posts and one subtitled post. Zero provider calls or production writes.

The provider-free frozen comparison CLI now implements preregistration, complete
source-registry checks, footage/outing-group separation, stable blinded A/B cards,
immutable output/failure slots and exact creator judgments. Reports retain pending,
failed, tied and neither outcomes and separate preference, willingness and factuality.
Changed corpora invalidate the frozen comparison. Independent evaluation requires
an approved profile and preregistered bars; operator-supplied source coverage still
requires an actual account/source audit. No synthetic judgment can qualify as a
real quality win. Four real CLI journeys pass after the missing-command red run.
Combined account/CLI/free replay regression: 2,082 passed, 15 skips, seven expected
failures. The narrower selection differs from the earlier 2,139-check execution run.

The exact pushed-head iOS-contracts CI check passed on 87271fc6370d5ac99fd1cab8d127f83fd4fd8af0. No production activation occurred.

## First consented development preview

The creator explicitly approved one Gemini 2.5 Flash request containing the frozen
eight cached Troya descriptions, four confirmed rules and up to three relevant
approved-post examples, capped at $0.05 with no video uploads. One attempt at prompt
version `2026-10-21.1` settled $0.003633 in the development spend ledger. Its response
failed the unknown/repeated moment-ID guard and was rejected before offering,
building or rendering a project. Sensitive raw output is intentionally absent from
general traces; the exact wrong references and wording were not retained. This is
a failed development sample, not evidence of creative improvement. No paid retry,
fallback or repair was run.

A new replay oracle first fails on exposed long source/moment UUIDs, then verifies
request-local `m1` references resolve to exact immutable moments. Unknown references,
source references, repeats and mixed alias/legacy duplicates still fail. The prompt
uses only these short references and is bumped to `2026-10-21.2`; the public story
contract remains exact source IDs. This reduces selection ambiguity but is not yet
validated by a fresh provider response. A subsequent paid request needs its own
explicitly bounded authorization; the one-call preview authorization is exhausted.

The follow-up free regression run passes 2,094 checks (15 skipped, seven expected
failures), including the actual account/collection and full free agent-replay
suites. The migration-chain guard now explicitly includes additive migration 0124
and passes all 36 schema checks. Current CI also exposed an Xcode 26.3 type-checking
timeout in the collection screen's large SwiftUI body. Splitting its scroll and
group expressions leaves behavior intact; all four existing collection/story/taste
simulator UI journeys pass locally on Xcode 26.6
(`.dev/editorial-taste-ci-native.xcresult`). Remote 26.3 confirmation is still needed.
The corrected-export rejection in the earlier backend CI shard is still under
investigation; the Mac and CI-font reproductions pass and no verifier threshold
has been relaxed.

A separate Linux reproduction found deterministic missing tail pictures in the
synthetic fixture: its container/audio lasted 4.6s but its video lasted 3s (90 frames).
Explicit `fps=30` before `tpad` produces 4.6s/138 frames. The fixture now checks actual
video-stream duration before using its tail as an oracle. All 51 actual-export and
publication checks pass both locally and in the same Linux two-worker/two-CPU
reproduction (49 passed/two partial failures before, 51 passed after). Both correctly
ending text and text leaking into the real tail retain their original verdicts.
No product verifier or threshold changed. The earlier CI corrected-export refusal
was not reproduced by this separate fixture finding and is not claimed fixed by it.

After integrating `fb516fb26` from main, the combined backend selection passes
2,257 checks (15 skipped, seven expected failures), including editor-save contracts,
all free agent replays and the export/publication matrix. All four collection,
story and taste simulator UI journeys and five editor-save code-catalog checks
pass (`.dev/editorial-taste-final-merged-native.xcresult`, Xcode 26.6). Generated
runtime/mobile/OpenAPI contracts match and the mapped pre-PR gates pass. Remote
CI confirmation remains separate; the original corrected-export refusal remains
an explicitly unresolved historical failure rather than a claimed fixture fix.

## Second consented development preview

The creator explicitly approved one additional request on the same frozen private
input, capped at $0.05 with a private diagnostic response capture. Prompt version
`2026-10-21.2` returned one structurally valid 16.58s proposal in one attempt, settling
$0.002911. Both taste-preview attempts total $0.006544. No video upload, extra call,
project build, render, production activation or training occurred.

The short references resolved to known unique source moments with complete beat
reasons. This demonstrates the reference boundary on one development sample only.
The assistant assessment still finds generic tourism wording, little day-trip
utility and an unsupported inference identifying a modern building as a museum.
Those are assistant observations, not creator feedback or independently verified
factuality labels. The creator's Would publish / Needs work / Not for me verdict is
pending. No quality improvement or fine-tuning-readiness claim is made.

Remote iPhone validation and iOS contracts pass on `a9c2178c9`. API shard0 passes;
shard1 still reports partial evidence for the two tail cases and a silent-source
case despite their local reproduction passing. Existing failure assertions now
show only status and nonpassing checks, so CI's compact diagnostics retain the
missing-evidence reason instead of truncating it behind full recipe identities.
This is diagnostics only; no verdict, retry policy or threshold changes.

The creator subsequently rejected that exact second preview as too generic and
advertising-like. The exact reply is pinned privately to the immutable input and
output hashes, without treating the assistant's earlier critique as human labels.
A private CLI journey first reproduced the inability to review a single proposal
without inventing another option. Development calibration now accepts one to four
ideas; an empty selection rejects the single idea or chooses neither in a
comparison. The independent two-arm evaluation workflow is unchanged. All nine
private CLI/comparison journeys pass.

The actual reply is now a source-pinned reviewed development calibration. Private
corpus revision 10 retains 20 positive examples and the original four confirmed
rules, with two actual reviewed calibrations. The local account API separately
records the reply as contextual Troya feedback in the existing project note; its
actual generation context includes the exact rejection and preserves all 20
examples and four rules. These are private/local replays, not production activation
or weight training. No additional provider call or spend occurred. A useful
firsthand observation for the guide is the next creator input.

## Public research as a creative input

The creator's next calibration asks Kria to research interesting public facts and
prefer footage that illustrates them, while still offering relevant background
when it cannot be pictured. The actual statement is saved privately as contextual
Troya direction, alongside the existing approved examples and rejected proposal;
it is not treated as evidence of any historical count. The private corpus is now
revision 12; the isolated account replay is revision 5 with twenty examples and
five creator-provided preferences. Production memory remains unchanged.

The optional research-to-idea adapter and its limitations are recorded in
[idea research](../camera-roll-recommendations/idea-research.md). Its passing
structural/HTTP checks do not establish that the next generated idea matches the
creator's taste. That still requires the real output and creator judgment.
