# Creator prompt fidelity

The creator planner can build a visual sequence independently of a recorded
voiceover. This addresses requests such as using every uploaded image and video,
showing grouped 0.3-second photos, and grounding scores and topic labels in the
recording. The source recording remains the audio spine.

## Contract

`CREATOR_PROMPT_FIDELITY_ENABLED` defaults to `false`. When available, a newly
confirmed strategy uses `execution_contract=guided_voiceover_v1`. Existing
narrated jobs keep their original render path. The model receives descriptive
capabilities; source audio paths and generations remain server-owned.

The confirmed plan carries media scope, visual timing and label requirements.
Confirmation hashes bind its strategy, manifest and context. Dispatch snapshots
that identity with the approved visual proposal and the narration's object
path, generation, duration and timed transcript. Dispatch and rerender reject
changed identities instead of falling back to a different renderer. Required
all-media coverage and exact still timing are compiler constraints.

The guided execution plan owns the visual timeline. The original narration
duration is pinned independently; `canonical_narration_duration_s` rounds the
visual duration up to a 30 fps boundary so no final audio samples are cut.
At 30 fps, a 0.3-second still occupies nine frames. Continuous audio
and transcript labels use narration time; participant placeholders belong to
visual source instances. Grounded participant labels require evidence of
single-player focus. Placeholder identities are source-local and editable;
matching people across different uploads is not inferred.

## Sources of lost intent

| Previous constraint | Implemented behavior |
| --- | --- |
| A recorded voiceover selected the native audio-led renderer before visual requirements were considered. | Explicit all-media or mixed-media timing can select the guided voiceover contract. |
| Native selection and separate photo overlays could omit uploads and apply unrelated photo durations. | One approved visual timeline covers the required source identities and frame-exact still holds. |
| Creator instructions were shortened before downstream rendering. | The shared request bound is 12,000 characters across planning and rendering. |
| Narrow score-context matching rejected transcript-supported scores. | Typed annotations require exact transcript evidence and retain accepted/rejected receipts. |
| Player placeholders were applied without reliable single-player focus. | Visual focus evidence controls source-local, editable participant labels. |
| Large source manifests exhausted the planner output budget and entered a generic fallback. | Bound planner output capacity for larger manifests and preserve pinned narration intent in fallback planning. |
| A planner promise could diverge from dispatch or rerender inputs. | Approval and execution bind the strategy, media and narration identities. |

## Remaining capability boundaries

This change does not make the renderer support arbitrary video operations.
Exact montage cadence with recorded voiceover outside the mixed-media contract,
voiceover in the existing `day_vlog`/`single_hero` format paths, disabled effects,
and requests exceeding available source duration still require explicit capability
handling. They must not be represented as successfully executed instructions.

Future extensions should add a typed intent field, advertise its executable
capability, compile it into the approved plan, and verify its rendered receipt.
Preserve the user's requested outcome when reporting an unavailable capability;
ask for a compatible alternative instead of silently substituting a house style.
Add conflict and negative-instruction fixtures alongside each new capability.

For an invalid specialist draft on the mixed-media voiceover path, initial
planning and direction replanning use the same source-bounded recovery allocator.
It keeps the canonical narration frame budget and required all/selected media;
real video windows may exceed the music montage's three-second ceiling. Exact
photo holds stay fixed, and insufficient footage fails rather than shortening
the recording or dropping required sources. The compiler validates recovery
before approval. `tests/services/test_narrated_fallback.py` covers this boundary,
including the 39-source shape and a selected-media subset.

## Rollout

Deploy workers that consume `creator-fidelity-v1` before enabling the capability
on the API. The dedicated queue prevents older workers from interpreting the
new contract. API and worker images must match. No frontend feature flag is
needed; the server advertises supported planning capabilities.

Before enabling, replay the captured request using local-only fixture storage
and an isolated test database. Inspect the rendered output with the creator,
including media coverage, narration continuity, photo timing, label grounding,
and editor preview/rerender parity. Run the affected live agent evals and the
normal preship checks. Production enablement is a separate reviewed operation.

Disable the flag to stop new plans from using the capability. Do not downgrade
already-confirmed or persisted jobs; retain a compatible worker until queued
work has drained. Existing output remains playable.

## Verification

- `tests/services/test_creator_execution_contract.py`: immutable plan bindings.
- `tests/routes/test_plan_item_sync_dispatch.py`: real-DB dispatch, changed audio,
  flag-off refusal, stale attempts and images-only narration.
- `tests/pipeline/test_narration_labels.py`: exact scores, subject focus and
  transcript-grounded topics.
- `tests/pipeline/test_creator_fidelity_coverage.py`: all 39 source identities,
  nine-frame photos, and the continuous narration budget.
- `tests/services/test_guided_narration_labels.py`: runtime label integration.
- `make verify-editor-timeline`: preview, editing and compiler parity.

## Creator acceptance checklist

For the captured case, use the same 19 videos, 20 photos, recorded voiceover and
full conversation instructions. Verify the output against each requested outcome:

- Every uploaded visual source appears; the receipt identifies all 39 sources.
- Photos run in consecutive groups with each photo visible for exactly 0.3 seconds.
- Visual sport groups correspond to the narrated context, and the recording is intact.
- A single-player shot receives an editable placeholder; group shots do not.
- Intro text, sport labels and scores match the request and recorded evidence.
- Renaming or deleting a placeholder survives save, playback and rerender.

A synthetic render proves timing and coverage mechanics, not the creative quality
of this footage. The creator must inspect the actual replay before acceptance.

Replay artifacts contain private media and prompts. Keep them outside Git.

## Original-input replay findings

The isolated production-image replay completed with all 19 videos, all 20 photos,
and the pinned recording. The rendered receipt verifies source coverage and
text visibility; creator acceptance must still judge semantic footage order.
Do not treat a passing structural receipt as proof of creative fidelity.

Large narrated edits also exercise renderer capacity: preserve individual text
receipts, then composite supported caption/label animations into one PNG stream
before FFmpeg. Opening hundreds of PNG decoder inputs can exhaust worker memory.
Narrated edits use their grounded label lane rather than advisory montage copy,
which can otherwise duplicate participant names and collide with captions.

The replay uncovered zero-length transcribed word timestamps and source videos
longer than the music montage's three-second cut ceiling. Preserve point tokens
inside positive-duration caption cues, and validate narrated video windows against
actual source bounds rather than the music-only cadence limit.

Narration currently supplies semantic planning context, captions and the total
frame budget; it does not enforce timestamped chapter anchors for every visual
cut. Exact per-topic source placement therefore remains a creative review item.
A future chapter contract should bind cuts to transcript ranges and explicitly
place unmatched required sources without claiming a timing guarantee prematurely.
