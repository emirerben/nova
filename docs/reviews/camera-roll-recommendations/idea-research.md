# Research before proposing an idea

The creator wants useful, interesting public facts researched for them. Prefer
footage that illustrates the point; relevant background remains useful when the
clips cannot demonstrate it. An archaeological settlement layer is not
automatically a separate civilization. A location does not identify an object.

## Failure cases fixed before implementation

- Cache-only requests, cache hits and collections with no eligible moments must
  perform no new provider calls. Research is optional; existing requests work.
- Public search receives only the explicitly supplied public topic, never the
  account's examples, private direction, clip descriptions, filenames or URLs.
- Absent grounding, noninstitutional sources, forged fact references or a failed
  claim review must not produce an apparently researched idea.
- Facts must carry provider-owned citations, retrieval time and search attribution.
  The proposal writer cannot invent source URLs. Only selected facts are offered.
- General context must not imply an unidentified object or visible structure has
  a specific identity/date. Relevance to an edit is different from visual proof.
- Research cannot silently promise narration, per-shot captions or a longer
  timeline than the existing project adapter can build.
- All three bounded calls use existing cost reservations and deadlines. No retry
  on failure. A changed source, owner/profile state or collection still refuses
  the result. Changed prompt versions invalidate the request cache.
- Corrections invalidate the research review of the wording. Selecting an idea
  still requires separate render approval; its original evidence is retained in
  the prepared project for review, not silently relabeled as fresh research.

## Verification

Before implementation, add an HTTP → PostgreSQL journey with replayed grounded
research, proposal and claim-review boundaries. Prove a cold cache-only refusal,
one successful researched proposal, a no-call cached replay, exact selected source
links, and failed claim review. This proves wiring, not factual or creative quality.
Use public primary sources for a human example; paid live calls need their own
bounded authorization. No production activation is part of this change.

## Implemented and verified, 21 October 2026

`StoryGenerationBody.research_topic` is an optional, explicit public-topic input.
The existing grounded public-facts agent runs before the idea agent; a separate
bounded claim review checks the result. Public search receives only that topic.
The idea agent selects known fact IDs; the server retains their original source
links, retrieval time and provider attribution. All research is background context,
not a claim to identify an object from a vision description.

The same request cache covers the complete pipeline and includes all prompt
versions. Cold cache-only requests cannot acquire work. A reviewed cache hit makes
no calls. Missing institutional evidence or a failed claim review returns an
explicit failure without an unsourced fallback. Calls retain the existing atomic
cost ledger, deadline and per-agent one-attempt limits.

The collection offers a collapsed public-topic control. Ideas keep their facts and
source links in a collapsed research section. A copy/sequence change marks the
review stale. Source evidence also persists in the server's prepared-project
record, across draft approval and dispatch; source display inside the exported
project's native text Sources view is not added by this planning-only adapter.
That view remains available for the existing researched text-edit path.

Verification: 2,072 free replay evals passed (15 optional skips, seven expected
failures), and 28 collection/project HTTP/PostgreSQL checks passed. The research
journey proves cache-only refusal, private/public input separation, source reuse,
claim-review refusal, institutional-source refusal and stale-copy signaling. The
prepared-project journey proves evidence persistence through approval and worker
execution. These are replayed provider boundaries, not live creative-quality proof.

Public manual research used the official [Troy archaeological-site entry](https://www.turkishmuseums.com/museum/detail/2020-canakkale-troya-orenyeri/2020/1)
and [Troy museum entry](https://www.turkishmuseums.com/museum/detail/2021-canakkale-troya-muzesi/2021/1).
The site describes ten settlement layers and more than fifty construction phases.
Those are not counts of separate civilizations; the proposal should preserve this
distinction and avoid claiming a particular pictured structure belongs to a layer.
This browser research is separate from the in-app agent and has not been injected
as a fabricated provider result.

A new live development preview has been frozen privately: up to three Gemini 2.5
Flash calls (grounded search, idea, claim review), at most $0.10 total, no video
uploads, retries or render. Its zero-cost dry run validates code/input hashes and
shows no consent or prior reservation. The two earlier one-call authorizations
are exhausted. This preview remains pending new explicit authorization; no paid
call, production activation or independent held-out evaluation is claimed here.

The final simulator bundle `.dev/editorial-taste-research-final-native.xcresult`
passes all five collection/story/taste UI journeys, including expanding research
and finding its real SwiftUI source-link control. The screenshot was inspected;
search attribution uses a bounded scrollable area. All 102 iOS orchestration checks,
generated contracts and the pre-PR gate (1,044 mapped incidents plus journey
mapping) pass. The physical iPhone was not used for this new adapter.

## Approved live preview and correction

The creator approved the frozen three-call preview, capped at $0.10. It ran once
and all three reservations settled, totaling **$0.047087**. Public research
retrieved institutional grounding; the proposal still used generic wording and
called an unidentified building a museum. The automated reviewer rejected it.
Its explanation exceeded the 500-character limit, so the original pipeline stopped
with a schema error. No researched idea was offered, rendered or saved as a project.
The raw responses, approval and original failed receipt remain private and unchanged.

A synthetic replay reproduced the overlong-rejection failure before the fix. The
parser now bounds only diagnostic text and preserves the boolean verdict. Replaying
the actual saved response still rejects the draft; this is not a new model review.
The idea prompt now requires a concrete fact in the opening itself and excludes
unsubstantiated object/building identities. The reviewer distinguishes overall
creator-supplied outing context from individual object identification. Prompt
versions are `2026-10-21.4` (ideas) and `2026-10-21.2` (review). These wording changes
have **not** been tested with a fresh model response.

Free verification: 2,073 replay evaluations passed (15 optional skips, seven
expected failures), and 38 focused collection/project/agent checks passed. The
three-call authorization is exhausted despite remaining dollar allowance. No retry
or new paid preview ran. There is no new human verdict on this draft, held-out
quality result or fine-tuning-readiness claim.
