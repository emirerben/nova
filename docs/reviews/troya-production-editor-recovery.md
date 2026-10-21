# Production editor recovery found during Troya verification

PR #1679 deployed through Fly Deploy run 39229674519 on 19 October 2026.
Its exact image/revision and fleet health gate passed. The signed-in iPhone test
then exposed two editor defects before the research/export journey could finish:

- An unused source was present in the server snapshot but absent from the prompt.
  It also lacked duration because durations were read only from active slots.
- A bundle moved a clip first, then changed its duration using its original
  snapshot index. The server applied that duration to its former neighbour.
  The web drawer already resolves these targets by snapshot identity.

The follow-up exposes bounded, path-free unused-source context, joined to the
approved source generation. Canonical source duration is available when adding
unused footage. Clip operations and indexed patch selectors retain snapshot
identity throughout a bundle. Transition boundaries retain their existing meaning.
Adding unused sources remains an all-selector operation; the prompt explains that
newly added clips can be reordered on the next turn. It does not invent a selective
add operation.

Before the fix, three new regressions failed. After the fix, the focused suite
passed 110 tests, and the broader replay/editor suite passed 1,650 with 15 optional
skips. Replaying the captured production operation bundle places the 3-second
horse duration on the horse source and the 4-second layers duration on the layers
source. The previously omitted car appears as the only unused source, with its
canonical 4.32-second duration and approved description. Changed approved source
generations do not contribute descriptions.

These are offline recovery results. The full signed-in research → Sources →
save/reopen → native export journey remains unverified until the follow-up ships
and is retested on the physical phone. The original saved Troya render remains
unchanged; the partial chat edit is a separate unsaved draft.

The first creation re-plan timed out after a schema retry. Its known provider
usage and an estimate for the unknown attempt are counted against the authorized
test budget. Production cost reservations were disabled, so an empty reservation
query was not used as evidence of zero spend; job/plan-scoped agent usage was read
instead. No cost-control flags were changed.
