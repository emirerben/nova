# Cabildo named-moment gate

KRI-1052 remains the parent incident. This is a bounded safety fix, not full request fulfillment. No new strategy schema, model/prompt change, provider call, or feature activation is involved.

The creator requested five named moments from one long video, then a final photo. Production's saved server-resolved strategy assigns the first video moment to that photo. The remaining four moments reference the video. The recorded brief does not contain an explicit moment count. Raw Main Creator/extractor outputs are unavailable, so the model-versus-normalization origin of that bad assignment is unknown.

The existing named_moments gate returned None whenever any sequence row had a first/last position, before separating photo rows from video moments. The final photo was pinned last, so the gate never asked for source locations. The saved length-plan windows instead all had reason moment_fill and score0, which means the midpoint of available space, not evidence of the requested actions.

The fix classifies source rows first. A photo's positional pin no longer bypasses localization of video moments. A positional pin on the video itself retains the prior behavior. When an explicit count exists and differs from the video-moment assignments, a capable client receives an actionable question with no draft/render intents. Source assignments are never guessed or rewritten. Unsupported clients retain their existing path; this is not an assertion that their exports now fulfill named actions.

The sanitized recorded brief/strategy-shape test now reaches the existing location card for FOUR unlocated video moments before approval. It deliberately preserves the wrong first-photo assignment. A source-corrected synthetic control reaches all five moments. An explicit five-versus-four count mismatch is a separate synthetic control, not the recorded brief. Tests also cover photo-first/photo-last, ordinary video pins, and legacy clients. The existing 19 KRI-723 authority tests remain green.

## Editor capability, independently checked

The existing editor can save disjoint windows from the same source. The new route integration probe uses real prepare_editor_commit, guided-v2 revision compilation, and device_status. Three cuts select source8–9,0–1,4–5 in that output order. All retain one source ID and distinct cut IDs. An out-of-bounds edit is rejected without mutating the saved assembly plan.

A generated ten-second source contains red0–4, green4–8, blue8–10. The editor-produced recipe was exported by a freshly built production AVFoundationLocalExporter through the existing AudioParityFixtureTests on Mac. At output0.5/1.5/2.5 the observed colors are blue/red/green. A native wrong-content control changes only source windows to0–1,4–5,8–9, preserving IDs/count/durations/output timing; the resulting red/green/blue fails the independent expected-color check. Summary is in cabildo-native-capability-probe.json. This proves synthetic Mac native capability, not physical-iPhone or original-Cabildo semantic acceptance. Fixture fingerprints are synthetic, not production source authentication.

## Validation and remaining work

Focused command: bash scripts/check-api.sh test tests/kria/test_cabildo_named_moment_gate.py tests/kria/test_kri723_moment_authority_m2.py tests/routes/test_repeated_source_capability_probe.py -q

149 passed in the final focused batch, including render-contract controls. No test mocks the moment decision or editor compiler. Existing test scaffolding supplies sanitized media and frozen inputs. Provider outputs and native test media are not claimed as captured customer output.

Remaining KRI-1052 gaps are the first source misbinding, original exact label placement, explicit-order brief normalization, stale none-answer narrative/review, and actual original-media request fulfillment. PR1931 separately owns legacy pin/text none-answer brief binding. This patch does not edit that helper or its choice-folding logic. The exploratory reader-contract branch is parked and not required by this fix. Broader schema work should follow evidence of an actual missing execution contract, not be presumed the incident's root cause.

The follow-through test added during independent review exposed another boundary with the same shape. The worker's `moment_seats` also rejected every first/last row before separating video moments and Visuals. The real worker then placed a photo-first request last, and photo-last order receipts still reported failure despite correct placement. This was reproduced in the actual compiled fast cuts and intent outcomes. It was not inferred from `contract.order_ids`, which can be a non-authoritative basis order when `order_required` is false. The final local verification must include answering the location card and checking that worker artifact and its receipts.

## Why earlier local fixes did not establish this composition

| Prior work | What its source/tests establish | Boundary still missing here |
| --- | --- | --- |
| KRI-779, PR1709 | Select the sole video while preserving `the only video` as non-creator evidence. Its acceptance explicitly deferred locating actions inside the take. | File membership did not prove which source interval depicted the action. |
| KRI-723 moment authority | Named moments can be located, answered, pinned and executed; existing Merope tests include a photo after a middle action. | The global positional-row exclusion bypassed that path for a final-photo pin. |
| KRI-796 moment seating | A photo can sit between named moments in an all-then sequence. | First/last photo pins bypassed seating and its moment-specific receipts. |
| KRI-949, PR1849 | When the resolver offers nothing, preserve legitimate photo choices rather than hide them. | It did not establish that a video-moment referent was bound to the correct source. |
| KRI-950, PR1853 | Reuse checked intent inventories after a picker answer. | It did not establish reconciliation of every derived story/review field after a dropped intent. |
| PR1931, KRI-1025 | Existing separate work binds none answers to legacy brief pins/text. | It does not provide evidence that this entire Cabildo moment/label/story journey is fulfilled. |

These are documented partial coverage and composition boundaries. The evidence does not establish that those PRs newly regressed model capability. The new cross-stage test exposed the worker seating defect after the pre-approval gate was repaired. Independent review then reproduced a two-photo false-positive receipt: counting only video windows could claim an absolute first/last pin despite another photo outside it. Both boundary proof and actual placement require explicit multi-visual controls.


The follow-through repairs now preserve both photo-first and photo-last in the real worker fast cuts, preserve all five creator-pointed video windows, and produce met order receipts for that supported synthetic composition. Explicit boundary groups are stored internally apart from after-N anchors; they lead/trail all slots, including other Visuals. Two-visual wrong-boundary and reversed-group controls reject false receipts. This does not repair the recorded first moment's incorrect photo binding.
