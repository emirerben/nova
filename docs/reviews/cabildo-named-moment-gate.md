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

29 passed. No test mocks the moment decision or editor compiler. Existing test scaffolding supplies sanitized media and frozen inputs. Provider outputs and native test media are not claimed as captured customer output.

Remaining KRI-1052 gaps are the first source misbinding, original exact label placement, explicit-order brief normalization, stale none-answer narrative/review, and actual original-media request fulfillment. PR1931 separately owns legacy pin/text none-answer brief binding. This patch does not edit that helper or its choice-folding logic. The exploratory reader-contract branch is parked and not required by this fix. Broader schema work should follow evidence of an actual missing execution contract, not be presumed the incident's root cause.
