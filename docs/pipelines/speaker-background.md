# Speaker background (Talk-to-camera green screen) — server lane

A Talk-to-camera (`subtitled`) edit that renders on the phone can show one photo or
looping muted video behind the speaker for the whole edit. The server only names the
asset; `KriaMediaEngine` cuts the speaker out with a Vision person mask and composites
it. Design and failure modes: `docs/reviews/speaker-background-failure-modes.md`
(rows S9, S13, S16, S17, S19, S22 are the server half). Capability row:
`docs/reviews/kri-29/capability-matrix.md` (`speakerBackground`).

## Lane shape

| Where | Shape |
| --- | --- |
| Request, persisted at `assembly_plan["_phone_subtitled_lanes_v1"]["background"]` | `SubtitledBackground {media_id, gcs_path, generation, kind image\|video, source_start_s}` (`app/pipeline/phone_subtitled_lanes.py`). Omitted from every dump while unused. |
| Compile | `compile_phone_subtitled_plan` pins the pool visual as `VisualRenderAsset(id="visual-<media_id>")` and sets `EditRecipeV2.speaker_background` (`subject_track_id="subtitled"`, `loop=true`). It adds no clip or track. |
| Editor document | `speaker_background`: `null` (key absent) or `{"media_id", "kind", "source_start_s"}` on the variant. Paths and generations never leave the server. |
| Editor Save | `EditorCommitRequest.speaker_background`, tri-state: absent leaves it, `null` clears, an object sets. |
| Capability | `editor_capabilities.speaker_background: true` (present only when the gate is open) and the thread capability `speaker_background`. |

The lane is whole-edit: a speech cleanup cut never moves it (`remap_lanes_for_cut`
leaves it untouched) and a video loops on output time from `source_start_s`.

## Gates

`speaker_background_supported()` (`app/services/phone_rollout.py`) needs all of:

1. `SPEAKER_BACKGROUND_ENABLED` (kill switch, default off),
2. `speakerBackground` in `PHONE_RENDER_VERIFIED_FEATURES`,
3. inside an app request, `X-Kria-Client-Protocol` >= `SPEAKER_BACKGROUND_MIN_CLIENT_PROTOCOL` (27).

When any is missing, `strip_unsupported_background` removes the lane BEFORE anything is
bound or compiled. The recipe is byte-identical to one for a request that never asked,
and the job-debug trace carries `speaker_background_phone_fallback` with the reason
(`disabled`, `token_not_verified`, `client_protocol_below_min`). The edit still renders
with the creator's own background.

| Surface | Behaviour |
| --- | --- |
| Worker (`_run_phone_subtitled_job`) | Strips on a closed gate, drops the lane on a missing `stillImages`/`visualVideos` token or an unreadable asset (`phone_lane_receipt.dropped`), then binds and compiles. A video that cannot play from its start fails the `background` lane (`SubtitledLaneError`) and the retry loop drops it. |
| Device-render poll | A pinned recipe carrying the lane answers a build below protocol 27 with 426 `client_update_required` (`speaker_background`). |
| Editor Save from an old build | 426 for a variant that already carries a background; a Save that sets one is 426 too. Nothing is committed. |
| Editor Save, set | 422 `speaker_background_disabled` (gate closed), `..._unsupported` (cloud, other archetype, empty draft, authored timeline), `..._media_not_in_pool`, `..._kind_mismatch`, `..._start_out_of_range`, `..._media_unavailable`. |
| Editor Save, clear | Always allowed on a variant that can carry one, even with the kill switch off. |

## Pool visuals (S19)

A background is always a ready visual of THIS plan item's pool. The Save route pins a
new pool visual before validation (`app/services/phone_speaker_background.py`:
scoped `PlanItemAsset` query, then `bind_phone_visual_assets`, which rechecks owner,
status, pool namespace and hashes the exact generation) and adds the receipt to
`assembly_plan["_phone_visuals_v1"]`. The validator then accepts only an id that
receipt list holds. A chat or worker caller that goes through `prepare_editor_commit`
without the route must call `pin_pool_visual_for_background` first.

## Rollback

Kill switch, never a code revert: `fly secrets set SPEAKER_BACKGROUND_ENABLED=false
--app nova-video` and restart api + worker. New compiles strip the lane; recipes already
pinned keep theirs and old builds keep their 426. Apply order for turning it on:
`PHONE_RENDER_VERIFIED_FEATURES` gains `speakerBackground` first (after a physical
iPhone preview + export check), then the flag, then restart.

## Tests

`tests/kria/test_speaker_background.py` (compile, gates, S13), `tests/tasks/test_speaker_background_worker.py`
(worker), `tests/routes/test_speaker_background_editor.py` (426, Save, capability,
projection), `tests/services/test_phone_speaker_background_pin.py` and
`tests/integration/test_speaker_background_pin_postgres.py` (S19), and the hash guard in
`tests/schemas/test_guided_edit_revision.py`.
