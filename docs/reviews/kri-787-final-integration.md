# Final Merope T1 replay: landed-parent audit

Private integration only; #1723 must merge last after actual child fixes. Fresh main1bf6d14a6 plus original23bd2653 replay, preserving the original request/fixture and every child acceptance condition. The selector conflict retained both landed order-dispatch and final T1 journey mappings.

On actual landed main, original unchanged controls produced13failures,11passes,11strictxfails. Exactly7 fail instances were historical bad-behavior pins made obsolete by landed #1727/#1733: dispatch lost answer (twojobs), worker refused it (twojobs), runtime approved dispatch refusal, word-for-word old chat and missing-caption receipt. Exactly6 fail instances were strictXPASS acceptance cases for those same fixes: approved answer retained/talking route (twojobs each), real runtime reaches Talking and caption spellings appear.

Retired only those five historical pin functions (seven parameterized cases) and removed only the six already passing acceptance xfail markers. Their full outcome assertions are unchanged and now mandatory. Original request data and the11 still failing unmerged-child acceptance cases are untouched. Replay now17passed/11strictxfails. This is deliberate retirement of reproduced obsolete incident behavior, not changing expectations to implementation or claiming unmerged/fulloutput success.

Remaining final integration must consume actual landed trim, timedtext, namedcut, reanswer/refusal children; promote their acceptance only after independently proving the actual combined behavior. Source includes recorded model transports and mixed production/local word timings, not a newly run model or independently observed phone/export. Physical fulfillment stays open and belongs to the authorized device owner. No paid/model/device/production action was performed.

## Actual #1724 acceptance promotion

After main049bdd0a6 (actual #1724) merged, exactly the old r7 exact-text-miss pin failed and its corresponding strict acceptance XPASSed:2failures,16passes,10strictxfails. The actual new receipt says it will use the first spoken occurrence, explicitly cannot identify the speaker and may use someone else. Retired that one obsolete pin and promoted only the already passing acceptance with its original “placed on her word OR honestly explained” assertion intact. Replay now17passes/10strictxfails. This is honest partial speaker support, not a claim Caro's precise utterance or full original output is verified. Original provider output and fixture words were not rewritten.
