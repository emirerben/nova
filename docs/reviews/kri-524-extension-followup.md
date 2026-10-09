# KRI-524: extension follow-up

This follow-up addresses the failed request to extend a saved opening clip and
hold its final word longer. It does not claim general reliability for every
editing request. Raw conversation, video identifiers, and provider captures stay
outside the repository.

## Confirmed failures

1. The production turn ended after 40.041 seconds with a provider outcome unknown.
   There was no model response, plan, or execution to apply. The durable worker
   now gives the durable editing call up to 120 seconds, bounded by the shared
   turn deadline with a 20-second post-processing reserve. The original call
   still had the separate 40-second provider limit.
   The reconstructed prompt is 106,307 characters with high reasoning. These
   are possible latency contributors; the provider-side delay remains unknown.
2. A capped live replay returned a correct clip extension and final-word timing,
   plus a request to realign clip labels. Parsing checked that auxiliary request
   against the original snapshot, found labels already aligned, and converted
   the entire bundle to a clarification, discarding valid clip and text siblings.
   This was application logic overriding a correctly understood request.

## Changes

- Durable edit calls receive up to 120 seconds, bounded by the shared turn
  deadline minus a 20-second post-processing reserve. Every retry shares that
  deadline. Stateless calls retain their 40-second cap. Invalid or depleted
  budgets fail before provider contact; reservations are released if budget
  expires while reserving.
- Already-satisfied text operations have a distinct no-effect disposition. They
  no longer erase valid clip or text siblings when label alignment is already
  satisfied. Standalone no-ops explain that nothing changed; genuine selector
  ambiguity still blocks the whole bundle in either operation order. Existing
  timeline rebasing keeps clip labels aligned.
- Existing short opening/closing text bars retain their authored duration during
  timeline changes. The 0.2-second protection only applies to bars that were
  already that long; the authored 0.15-second opening bar remains 0.15 seconds.
  Interior source-time projection retains its existing frame rounding.
- Responses arriving after a final provider timeout produce correlated
  diagnostic metadata only, without prompt, response, or exception-body content.
  They cannot apply an edit, start another request, or settle an unknown cost
  reservation automatically.
- CI runs the extension through parsing, compilation, Save, and a native simulator
  export using synthetic media. It retains separate native artifacts alongside
  the existing creation and trim journeys.

No model, reasoning level, prompt wording, public API, database schema, or native
product behavior changes. Save and revision/approval boundaries remain intact.

## Evidence and limits

### Live

Two calls used the same enforced $0.75 envelope. The first returned in 25.65s and
exposed the parser defect above. The second passed after the parser correction:
38.40s, first clip 3s, all 12 words adjusted, final word 0.7s versus 0.2–0.3s for
other words, all ten slots retained. Both used the captured input and unchanged
Gemini Pro/high-reasoning prompt. The response was compiled locally, not applied
to the creator's production video.

Settled spend: **$0.217652**. Cumulative KRI-524 exposure is **$3.333734 of $5**,
including the earlier unknown reservation. No unmetered judge was used. These
calls do not demonstrate sustained live reliability or prove why the original
provider call exceeded 40 seconds.

### Offline and native

The required backend gate passed **2,318 tests, 6 skipped**. The final focused
gate passed **639 tests**. CI policy passed **35 tests and 93 subtests**.
The current branch's native compound export passed **1 test**, including rendered
text, the five-second output, and source audio. The delayed transport is an
authored deadline threshold, not a 55-second wall-clock wait.
The captured operation shape is replayed with synthetic text/media; production
input is replayed privately. Native export verifies rendered output separately
from model behavior. CI builds the branch's native test binary before export.

Deployment and the creator's actual follow-up remain unverified until this PR
is approved, merged, and the deployed revision is checked.
