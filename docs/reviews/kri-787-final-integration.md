# Final Merope T1 replay: landed-parent audit

Private integration only; #1723 must merge last after actual child fixes. Fresh main1bf6d14a6 plus original23bd2653 replay, preserving the original request/fixture and every child acceptance condition. The selector conflict retained both landed order-dispatch and final T1 journey mappings.

On actual landed main, original unchanged controls produced13failures,11passes,11strictxfails. Exactly7 fail instances were historical bad-behavior pins made obsolete by landed #1727/#1733: dispatch lost answer (twojobs), worker refused it (twojobs), runtime approved dispatch refusal, word-for-word old chat and missing-caption receipt. Exactly6 fail instances were strictXPASS acceptance cases for those same fixes: approved answer retained/talking route (twojobs each), real runtime reaches Talking and caption spellings appear.

Retired only those five historical pin functions (seven parameterized cases) and removed only the six already passing acceptance xfail markers. Their full outcome assertions are unchanged and now mandatory. Original request data and the11 still failing unmerged-child acceptance cases are untouched. Replay now17passed/11strictxfails. This is deliberate retirement of reproduced obsolete incident behavior, not changing expectations to implementation or claiming unmerged/fulloutput success.

Remaining final integration must consume actual landed trim, timedtext, namedcut, reanswer/refusal children; promote their acceptance only after independently proving the actual combined behavior. Source includes recorded model transports and mixed production/local word timings, not a newly run model or independently observed phone/export. Physical fulfillment stays open and belongs to the authorized device owner. No paid/model/device/production action was performed.

## Actual #1724 acceptance promotion

After main049bdd0a6 (actual #1724) merged, exactly the old r7 exact-text-miss pin failed and its corresponding strict acceptance XPASSed:2failures,16passes,10strictxfails. The actual new receipt says it will use the first spoken occurrence, explicitly cannot identify the speaker and may use someone else. Retired that one obsolete pin and promoted only the already passing acceptance with its original “placed on her word OR honestly explained” assertion intact. Replay now17passes/10strictxfails. This is honest partial speaker support, not a claim Caro's precise utterance or full original output is verified. Original provider output and fixture words were not rewritten.

## Private complete sibling candidate, not yet landed or published

Normal private merges consume named-cut/trim producer c733227e0, typed reanswer42846c363 and quoted-window consumer a2b79fb0. The original source request and provider fixture are unchanged. Before edits: fifteen failures, nine passes and three strict expected failures. Eight failures were old tests asserting the incident's incorrect behavior; seven were strict XPASSes. The remaining refusal-card cases revealed that the generic device database double provided no original thread history.

The replay adapter now supplies the fixture's original question/history rows to the real observer, without stubbing its decision. It does not establish the source-event race behavior; the independent real-Postgres KRI-794/KRI-795 controls own that proof. With the adapter, two more unchanged acceptance cases pass.

Automatic approval review rejected a broad scripted cleanup because it could remove more coverage than justified. The safer applied change deletes no test functions and changes no outcome assertions: eight explicitly named acceptance functions (nine parameterized cases) first passed with --runxfail, then had only their expected-failure decorators removed. All nine now pass as ordinary hard checks. The obsolete production-bug pins and one historical one-take order-answer expectation remain for explicit individual review; the private full suite is therefore deliberately not claimed green. No remote push or merge of #1723 occurred.

Hard-check proof: /private/tmp/nova-1723-nine-hard-acceptance-controls.log. Candidate-only proof must be refreshed/qualified against actual merged parents before final publication. Full original request, actual device output and export acceptance remain pending.

## Explicit final historical-pin review

Current published parents2923 question-batch, b7d0e audible-lines and6c4822 source-affinity were normally merged into the private replay. Common gate objects were identical, all unique gates retained; canonical Main Creator69/Brief Extractor16 prompt/hash assertions are preserved. Both cloud unresolved-text and source-group helpers remain imported. No provider fixture or source word timing changed.

Before this pass, all unchanged current cases ran:8 failed,18 passed,1 strict expected failure. Each failure asserted the original incorrect behavior: two misleading phone-failure cards, one ignored typed answer, an invented one-take order question, a whole-take length explanation, two falsely completed/omitted named-cut limitations and the untrimmed whole take. The full review was written before changes.

Every current function and parameter case remains in this pass. The eight pins now retain original bad values in the fixture while requiring the corresponding corrected current boundary. The final one-take expected-failure marker is replaced by a hard check: recognize the typed unordered selection, then require no invented order contract for a timing-only take. Actual multi-clip replacement/closed/forged/stale-render protections remain in the real KRI794/KRI795 suites; they were not weakened or replaced by the one-take case. No new skips.

13 replay cases and14 real-Postgres runtime cases pass individually. Combined with the existing persisted reanswer/refusal and label-reply controls:98 passed. Original independently measured12.0–50.4s window expectations and the source fixture are unchanged; actual kept12.265–50.38 excludes the unrelated intro/tail. A first combined invocation collected no tests because of a nonexistent guessed label-test path; corrected existing module ran all98. Logs /private/tmp/nova-1723-final-parent-before.log, nova-1723-recovery-pin-controls.log, nova-1723-runtime-pin-controls.log and nova-1723-final-recovery-combined.log.

Earlier accepted retirements (before private04f970f04) removed six functions that pinned already-landed bad behavior; they remain in original Git history and immutable fixture. This final pass removed none. Full named phrase removal, speaker identity, actual picture/audio/export and original compound acceptance are still incomplete under KRI829/KRI787. Honest partial receipts are not fulfillment. This PR stays last, after actual parent merges; no publication or merge is claimed yet.
