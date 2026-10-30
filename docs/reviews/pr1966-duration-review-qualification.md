# PR1966 duration review qualification

The isolated branch starts at duration-review commit `6f1b98fa6463c249bbb7bda279c5eca5445c0ccd`. It incorporates Yasin's exact PR1966 commit `09026e996254d20603850a2079e39ed1be0f33c0`, with original author and commit message preserved in local commit `e69383725`. The upstream parser change removes the broad `for/lasting N seconds` recognizer. It is not our parser implementation.

Our additional review guard requires clarification when an exact approved total disagrees with the structured global authority. For an `at_most` ceiling, the existing draft sanitizer permits a shorter selected plan. The audit therefore allows an approved target at or below the ceiling, requires dispatch to retain that selected target, and checks the provenance stamp and contract against the ceiling. A valid selected15/stamp20/contract20/recipe15 is consistent; a downstream selected15→3 change is still flagged. That disagreement cannot authorize a restore candidate. The original useful case remains supported: approved plan20, global brief20 and final settings3 produce the bounded restore20 proposal. No proposal applies itself or claims the exported video is fixed.

## Actual factory qualification

`test_dispatch_duration_pr1966_qualification.py` calls the real `_dispatch_item_render` through the existing KRI-788 harness, which invokes the real `build_generative_job` and reconstructs the binding contract. No recognizer success response is substituted. The harness mocks DB queries and enqueue; no provider runs.

The full M5 request, scoped3/global20, scoped2/global15 and quoted `for 3 seconds`/global20 retain the correct final strategy target and creator-duration stamp. The rebuilt contract and real unified-montage plan agree on the total. The two fixture videos have distinct fingerprints and the test checks that both source paths survive dispatch. Trace JSON records the frozen brief, approved plan, final settings, contract and planned cuts.

This fixture qualifies total duration, not M5's original media, sound, crop, ordering or sheet readability. In particular its video-scoped hold survives in the frozen brief but the planner allocates the first video ten seconds in the twenty-second example. Each synthetic original is 68.37 seconds, so a three-second first source plus seventeen-second second source is within the available source durations. The trace includes both upload receipts, source durations, scoped brief and actual planned cuts. The observed ten-second allocation does not demonstrate an unavoidable footage-capacity limit. Scoped-shot fulfillment is explicitly unverified. Any future full acceptance must preserve that hold or explain the limit and seek clarification; total-duration agreement cannot waive it. Yasin's separate upstream M5 photo fixture tests its own approved photo plan. Neither fixture is a live phone export.

## Negative and ambiguity controls

The existing bad-recognizer injection still forces 3 or None at the actual factory boundary and must be caught after dispatch. It is retained independently of PR1966's corrected implementation.

A wrongly global-filed shot3 conflicting with global20 is refused by the real dispatch. A sole wrongly global-filed shot3 contradicting approved plan20 now requires clarification with no correction candidate. A correctly clip-scoped `For this shot, make it 3 seconds` provides no global repair authority, even though the remaining recognizer stamps3.

PR1966 does not remove all textual ambiguity. `Show the text "make it 3 seconds"; make it 20 seconds` still stamps3; the late review detects disagreement with the correctly structured global20. If extractor, approved plan and final settings all incorrectly agree on3, this deterministic consistency audit cannot infer the creator meant one shot. The test records that limitation instead of claiming semantic review.

## Remaining boundaries

This module is still unhooked. Production dispatch admission, post-repair native recipe admission, same-execution freshness/CAS, provider visual evidence and priced admission, corrected approval, measured export and native UI qualification remain pending. The tests invoke neither OpenAI nor Gemini. No UI, simulator, browser or customer project was used.
