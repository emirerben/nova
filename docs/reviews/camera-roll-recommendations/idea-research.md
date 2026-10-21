# Research before proposing an idea

## Current behavior

The creator wants interesting public facts researched for them, with footage that
illustrates the point where possible. Relevant background can still be useful
when it cannot be pictured. Layers are not counts of distinct civilizations, and
an outing location does not identify every object or building in its footage.

`StoryGenerationBody.research_topic` is optional and explicit. The existing
grounded public-facts agent receives only that public topic, never account
examples, private direction, clip descriptions, filenames or media URLs. The idea
writer selects known fact IDs. Citations, retrieval time and provider attribution
stay with the offered idea and prepared-project record. Source review and factual
wording review run separately before an idea is offered.

The cache includes the input, personalized context and all prompt versions. Cold
cache-only requests cannot acquire work; reviewed cache hits make no new calls.
No eligible moments, missing institutional grounding, forged references or a failed
review offer no researched idea. Calls retain atomic spend reservations, deadlines
and one attempt per agent. Changed collection/owner/profile state refuses a stale
result. Corrections mark the review stale; choosing an idea is separate from its
explicit expiring render approval.

Native research controls and sources are collapsed. The planning adapter retains
research in project metadata; it does not attach citations to native editor text
elements. It supports the existing opening/closing-text project, not generated
narration, music or arbitrary per-shot captions.

## Source check

`StorySourceReviewAgent` (`2026-10-21.4`) receives each beat paired only with its own
source description. It receives no outing query, historical facts, personal
examples or other creative copy that could license an identity guess. It returns
one indexed check per beat, an exact source quote and extracted identity terms.
Missing/duplicate checks, invented quotes and terms that are neither source words
nor proposed claims fail validation. An asserted specific identity absent from its
source blocks approval even when the model marks the beat supported.

The server derives the verdict from the complete checks. A contradictory free-form
summary no longer rejects a fully supported set or overrides a negative beat.
Generic categories such as structure do not assert a particular building identity.
Claim extraction still depends on the model; exact-word matching can also refuse
valid paraphrases. This is a conservative guard, not proof of visual understanding.

## Factual check and limits

`StoryResearchReviewAgent` (`2026-10-21.4`) checks title, copy and overall reasoning
against selected facts, source descriptions and creator-supplied outing context.
It does not rejudge beat purposes. Historical background need not identify a
pictured excavation layer. Unidentified buildings/artifacts cannot acquire a name,
age or provenance from background facts. Qualifications must survive paraphrase.
An overlong diagnostic is bounded without changing its verdict.

Earlier real-proposal controls failed **0/2**: the model accepted an unqualified
count and rejected qualified wording over terminology. A separate preregistered
three-item synthetic known-answer pilot passed 3/3 for $0.003964, using the same
model/prompt/512 thinking budget with an eval-only 8,192-token output ceiling
(product: 1,024). That narrow synthetic pass does not override the failed real
controls. The reviewer is not an independent factual authority. Research remains
default-off and owner-allowlisted; there is no rollout or publication-quality claim.

## Creator feedback and development evidence

Twenty explicitly approved finished posts and six confirmed preferences seed the
private development context. The creator rejected an earlier live draft as generic
and advertising-like, then preferred a plain explanatory opening. They separately
judged the complete developer editorial baseline **Would post**. These scoped real
judgments enter the isolated account's generation context, preserving earlier
feedback. The positive baseline is not an AI output, factual certification or
render approval. Exact personal copy, captures and raw responses stay outside Git.

The creator declined further collections. No more examples are required to finish
this implementation, and reusing these posts is not an unseen-footage evaluation.
No fine-tuning has run or been shown necessary.

The idea prompt is `2026-10-21.5`. A paired development check used the same cached
Troy descriptions/fact, approved feedback, model and prompt, with temporary app
thinking budgets of 512 and 2,048. One sample per arm cost $0.013991 (four calls).
Both produced explanatory copy without an arbitrary exact count; both were blocked
by the then-current source check. This is not a causal quality estimate and changes
no production default or Codex model setting.

Saved-response replay after the complete-check verdict fix accepts the larger-budget
sample and still rejects the earlier unidentified-museum draft. The other sample
remains blocked by an extracted identity absent from its source. A fresh source
review of the saved larger-budget sample cost $0.002090 and declined the word
"landmark" as absent source evidence. The factual stage therefore did not run.
This conservative false refusal remains recorded; no whitelist was broadened to
turn the paid sample green, and no idea was offered, rendered or creator-approved.

All taste/research previews and controls settled **$0.129655 across 34 calls**.
The earlier physical execution pilot is separately $0.031284. Each additional run
had frozen inputs, one attempt per stage and its own ledger cap. No new search,
upload or render occurred after the initial research preview. Raw failures and
original receipts remain unchanged. No unknown charge was observed.

## Verification

- **2,112** free replay/collection/taste HTTP checks pass (15 optional skips, seven
  expected failures). These prove wiring, refusal paths, cache reuse, erasure and
  bounded context, not factual or creative quality.
- The full-project-note journey reproduced the former 500-character refusal before
  the ceiling became 1,500. It preserves previous and full feedback, enforces the
  new bound, reaches generation within 8,000 characters and erases through Forget.
  No protocol shape or migration changes are needed.
- Source regression cases reproduced contradictory-summary and generic-structure
  refusals before implementation; all 22 focused agent checks pass. Existing unknown
  identities, missing terms, duplicate checks and invented quotes remain rejected.
- Five native collection/story/taste simulator journeys and 102 iOS orchestration
  checks passed earlier. The new taste/research adapter has no new physical-device
  proof; previous footage-to-export/editable-reopen evidence covers execution.
- Public manual cross-checking used the official [Troy site entry](https://www.turkishmuseums.com/museum/detail/2020-canakkale-troya-orenyeri/2020/1)
  and [museum entry](https://www.turkishmuseums.com/museum/detail/2021-canakkale-troya-muzesi/2021/1).
  That browser work is separate from provider-owned research receipts and was not
  injected as a fabricated model result.
