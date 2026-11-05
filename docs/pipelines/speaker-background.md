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

## Chat: the creator strategy field (S18, S19)

The Main Creator can name a background in the creator strategy: `background_media`
`{"media_id": "<manifest id>", "source_start_s": 0.0}` (`source_start_s` only for a video; omitted
from the stored strategy while unused, so every other strategy hashes as before). The field stays out of
every JSON schema the model is served except the Main Creator wire contract (`_creator_wire.MODEL_HIDDEN`),
like `ending_clip`.

| Step | Where | What it does |
| --- | --- | --- |
| Advertise | `resolve_creator_manifest` | `capabilities.speaker_background` is present only on a phone Talking manifest while the media lanes and `speaker_background_supported()` hold. Closed: no key, no prompt text, identical hashes. |
| Teach | `main_creator._SPEAKER_BACKGROUND_PROMPT_SECTION` | Appended only when the manifest advertises the lane (`MAIN_CREATOR_PROMPT_VERSION` v113 at the time of writing; the version string in `agents/main_creator.py` is the truth). |
| Repair | `creator_capabilities.repair_creator_speaker_background`, run by `compile_strategy_to_plan` | Keeps an id that is a photo or video of THIS item's pool, in the manifest's canonical `asset-<id>` spelling, drops anything else (another item's, the speaker's own clip, made up, a kind the device cannot draw, a manifest that cannot show it) with a plain `Couldn't ...` notice, and takes the photo out of every reaction beat and the closing shot. The camera pick fills in a strategy the agent left without one. |
| Resolve | `_run_phone_subtitled_job` (`_resolve_strategy_background`) | Looks the id up in the job's ready pool, builds `SubtitledBackground`, runs `strip_unsupported_background`, and binds it like a lane-request background. A hand-written lane request's background wins (`lane_request_background_wins`). A miss is a `phone_lane_receipt.dropped` entry (`not_in_ready_pool`, `missing_generation`, `multi_clip_talking`), never a failed job. |
| Reserve | same function | The resolved photo is excluded from the generic card / full-screen grounding (`used_media_ids`). A closed gate reserves nothing: the render equals one for a strategy that never asked. |

## Chat: copilot ops (S18, S19, S20)

`set_background {asset_id, source_start_s?}` and `remove_background` (lane
`app/agents/editor_ops_v2/background.py`, prompt `prompts/edit_copilot_ops/speaker_background.txt`,
`EDIT_COPILOT_PROMPT_VERSION` v109 at the time of writing; see `agents/edit_copilot.py`). They compile into the editor Save's `speaker_background` section.

| Piece | Rule |
| --- | --- |
| Snapshot | `snapshot["background"]`: `{"available": true, "current": {...} \| null, "pool": [{id, kind, label?, duration_s?, context?}]}` while the lane is open, `{"available": false, "reason": "..."}` for a Talking edit that cannot take one (cloud, customised timeline, gate closed). Absent, with `allowed_op_families` and every prompt byte-identical, while `SPEAKER_BACKGROUND_ENABLED` is off or the variant is not a Talking edit. |
| Pool | The item's own `ready` photos and videos (`planner._background_pool_rows`, one scoped query). `set_background` accepts only an id in it; an empty pool fails closed. It is a section of its own, never `overlays.asset_pool`, which would also open `add_overlay` on a Talking edit. |
| Apply | Both chat paths (async copilot edit, approval task) call `pin_background_for_commit[_sync]` before `prepare_editor_commit`: the same pin the HTTP Save route does, only for a Save the validator can accept, and the plan is restored if the Save is refused. |
| Draft | A cleared background is an explicit `speaker_background: null`; `editor_payload_dump` keeps it through the draft's None filter and `merge_editor_draft`. Untouched means absent. |
| Receipts | A refused Save answers `Couldn't ...` with the reason in English and Turkish (`BACKGROUND_REFUSALS`); a gate the model was told about answers as an unmet request with the snapshot's reason; an op the family never offered is `capability_unavailable` ("Not done: put that behind you ..."). |
| Parity | Group `speaker_background` in `kria/editor_capability_parity.py` maps `setSpeakerBackground` and `removeSpeakerBackground`. The scanner counts only `func` declarations, so the session's computed properties and the `static` `backgroundChoices(from:)` need no row. |

Not covered by chat in v1: a background chosen in the editor but not yet Saved reaches a turn only
through the editor's `client_state` if the app sends a `speaker_background` lane (the server reads a
draft's `speaker_background`; `EditorStateIn.lanes` carries no such lane yet), and the brief-receipt
ledger (`brief_checks`) does not judge a background the worker later drops; that drop is on
`phone_lane_receipt.dropped`.

## Client contract: camera pick

The camera picks the background before the take exists as a job, so the pick is thread state,
not a strategy field the app writes. The app uploads the photo or video through the normal
Visuals path (`uploads.enqueue(role: .visual, itemID:, recordID:)`), which reserves a pool row
at once (`status: "preparing"`, `client_upload_id: "ios-<recordID>"`), then states the pick.
The call may be made at enqueue time: the upload does not have to be finished.

```
PUT /creation-threads/{thread_id}/speaker-background
X-Kria-Client-Protocol: 27        (27 or higher; required when setting)
Content-Type: application/json

Set, by the upload the app reserved (what the camera has at enqueue time):
{"client_upload_id": "ios-<recordID>",
 "client_event_id": "<unique per call, 1..160 chars, no slash, backslash or "..">",
 "expected_revision": <thread.revision>}

Set, by the pool row id (the reservation id, or "asset-<id>"):
{"media_id": "<pool row id>", "client_event_id": "...", "expected_revision": N}

Clear (no reference at all; also what a background cleared before "Use take" needs: nothing):
{"client_event_id": "...", "expected_revision": N}
```

Fields: `client_upload_id` string 1..128, `media_id` string 1..160, `client_event_id` string 1..160,
`expected_revision` integer >= 0. Name the background by `client_upload_id` OR `media_id`, never both
(422 validation error). Unknown fields are refused. The row must belong to THIS thread's item and
owner, be a photo or video, and be `preparing`, `uploaded`, `queued`, `analyzing` or `ready`.

Response `200`: the usual `CreationThreadOut`. After a set, `state.speaker_background` is
`{"media_id": "asset-<pool row id>", "kind": "image" | "video"}`; after a clear the key is absent. The
revision bumps once and a replay of the same `client_event_id` with the same body returns the
same thread without a second bump (a different body under the same id is `409`).

Errors (`RuntimeFailure` problem body, `code` is the machine value):

| Status | `code` | When |
| --- | --- | --- |
| 404 | `speaker_background_unavailable` | Setting while the kill switch is off, `speakerBackground` is not verified, or the header protocol is below 27. Hide the picker with `capabilities.speaker_background` instead. Clearing is never gated. |
| 409 | `speaker_background_needs_item` | The thread has no plan item yet: attach the video first. |
| 409 | (plain detail) | `expected_revision` is stale, the thread is archived, or the idempotency key was reused with another body. |
| 422 | `speaker_background_media_invalid` | `media_id` is not a pool row id. |
| 422 | `speaker_background_media_not_in_pool` | No such row in this item's pool: another item's or user's, a failed or removed row, a `client_upload_id` that names nothing. |
| 422 | (validation) | Both references given, or an unknown field. |

What the server does with it: the manifest hands the stored pick to the Main Creator, and
the repair writes it into the strategy as `background_media` when the agent left that out. A pick
whose upload never reached `ready`, or whose row was removed, is dropped at plan time with
`Couldn't use the background you picked in the camera ...` in the plan notices (never silently).
The recorded take stays raw; the background is applied at render. A later chat or editor change
replaces what the render uses; the pick stays on the thread until cleared. The app never edits the
plan itself. State key: `app.services.creator_capabilities.SPEAKER_BACKGROUND_STATE_KEY`.

## Timed windows and the speaker frame (KRI-1162, commentary-style green screen)

The same lane can carry several backgrounds in time and draw the speaker small in a corner.
Contract, decisions and failure modes (S27–S45): `docs/reviews/speaker-background-windows-failure-modes.md`.
Capability row: `speakerBackgroundWindows`.

| Where | Shape |
| --- | --- |
| Lane (`SubtitledBackground`) | The first-cut fields stay the FILL. `windows: tuple[SubtitledBackgroundWindow, ...]` (`media_id, gcs_path, generation, kind, source_start_s, start_s, end_s`; sorted, non-overlapping, ≥ 0.5 s, ≤ 40) and `speaker_frame: SubtitledSpeakerFrame | None` (`width_fraction, x_fraction, y_fraction`), both omitted from every dump while unused. Window times are ORIGINAL take time in a lane request (remapped by `remap_lanes_for_cut` like overlay cards; a window inside a removal is dropped and its `media_id` joins the returned dropped-id set) and cut-timeline time in the editor document and Save. |
| Recipe (`SpeakerBackgroundLayer`) | `windows: [{asset_id, media_kind, source_start, start, end}]` and `speaker_frame`, omitted while unused (every first-cut recipe keeps its digest). `EditRecipeV2` requires a matching-kind `VisualRenderAsset` per window, `end` within the recipe, and derives `speakerBackgroundWindows`. Shared fixture: `tests/fixtures/speaker-background-parity/v2.json`. |
| Compile (`_compile_background`) | Pins the fill and every window (`_pin_background_visual`), clips windows to the edit (a window starting at or past the end is dropped), one manifest asset per distinct visual. |
| Editor document / Save | `speaker_background` gains `windows` and `speaker_frame` (keys absent while unused). `EditorCommitSpeakerBackground` accepts them; every window id is pinned from THIS item's pool before validation (`background_pin_requests`). 422 `speaker_background_windows_disabled` (reason), `speaker_background_window_invalid`, and the first-cut per-visual codes; 426 `client_update_required` `feature: "speaker_background_windows"` for a build below 32 that sets them, Saves a variant already carrying them, or polls a pinned recipe using them (`phone_editor.py`, `device_render.py`). Capability `editor_capabilities.speaker_background_windows`. |
| Speaker frame | Written only from a named preset (`SPEAKER_FRAME_PRESETS` in `portable_speaker_background.py`): `small_bottom_right` {0.42, 0.76, 0.80}, `small_bottom_left`, `small_top_right`, `small_top_left`; `full` = absent. The phone masks the full-size speaker first, then scales and places the cut-out. |

### Gates

`speaker_background_windows_supported()` = `speaker_background_supported()` AND `speakerBackgroundWindows` in
`PHONE_RENDER_VERIFIED_FEATURES` AND (inside an app request) protocol ≥ `speaker_background_windows_min_client_protocol`
(32; 29 is unused, 30 is the card-tilt build of KRI-1141, 31 the media-backdrop build of KRI-1139). No new env flag: `SPEAKER_BACKGROUND_ENABLED` stays the one kill
switch. When the base lane is open but this gate is closed, `strip_unsupported_background` keeps the fill and drops the windows
and the frame, tracing `speaker_background_windows_phone_fallback` (`reason`: `windows_token_not_verified` or
`client_protocol_below_min`, plus `window_count`, `had_frame`); the render equals a request for the single background.
`validate_phone_pilot_recipe` refuses an unstripped recipe without the token.

### Chat

- Strategy `background_media` gains `sequence` (`{media_id, source_start_s?, spoken_phrase?, hold_s?}`, ≤ 40), `cadence_s` (2–10,
  default 4) and `speaker_frame` (a preset name), all omitted while unused. Taught only when the manifest advertises
  `capabilities.speaker_background_windows`. Repair keeps only this item's pool photos/videos, drops the rest with a
  `Couldn't …` notice, and drops the frame when the windows capability is closed.
- Worker: entries with a `spoken_phrase` start at the first loose match in the take's caption-cue words; the others fill the
  remaining time in order, `cadence_s` each. **The same photo never comes up twice** (S57–S63, 2026-11-05): nothing cycles,
  the window before a gap holds through it, the last photo holds to the take end, a rotation shows each photo once
  (cadence stretched to cover the take), a photo listed twice keeps one window (`duplicate_media`), and a fill that is one of
  the sequence's photos never shows in a gap. An unheard phrase falls back to the cadence with a `phrase_not_heard` receipt
  note. Window visuals are reserved from cards (`used_media_ids`).
- Copilot ops `set_background_sequence {asset_ids, cadence_s?}`, `set_speaker_frame {preset}`, `clear_speaker_frame`;
  `set_background` stays the single fill, `remove_background` clears everything. Snapshot `background` carries `windows`,
  `speaker_frame` and `windows_available` while open.
- brief_checks (KRI-1161): the brief writer records the ask as facts (`speaker_background`, `background_changes`,
  `speaker_small`; never for background music) and `_check_speaker_background` answers with a named reason (no photo or
  video attached, not an iPhone Talking edit, gate closed, app too old), never the generic Talking refusal.
  `assess_readiness` builds the same Talking facts as the draft turn, so a checked `not_possible` is `unsupported` and
  named in the reply instead of a hidden `unverified`. Layout module: `app/services/phone_background_sequence.py`
  (anchors on `raw_words` in original take time; windows laid out in kept-take time and written back in original time).

### Rollout

`speakerBackgroundWindows` joins `PHONE_RENDER_VERIFIED_FEATURES` only after the physical iPhone check in the failure-mode
doc (three photo windows + corner speaker in preview and export, a silent looping video window, a 60 s ten-window export
within 120 s). Until then every request renders the single fill background exactly as before.

### Names heard when said another way (`BACKGROUND_FOLLOWS_TALK_ENABLED`, Qendresa item 17)

Server only, default off (off = windows and the Main Creator prompt byte-identical). On, every sequence entry's
`spoken_phrase` is also heard when said another way (joined / split words, a final plural "s", a plural with a KRI-961
sound-alike), as a last resort after the exact and KRI-961 matchers, and not inside another entry's longer phrase
("agents" inside "trading agents") while it is heard elsewhere too. Main Creator v123 (only with the flag on) teaches the
planner to write each phrase as said in the take, with the words said only where that visual is introduced. Holding the
last visual to the take end is the no-repeat rule (S57) for everyone, not this flag. Failure modes S64-S69 in
`docs/reviews/speaker-background-windows-failure-modes.md`.

- Apply: `fly secrets set BACKGROUND_FOLLOWS_TALK_ENABLED=true --app nova-video` + restart (api + worker).
- Rollback: `...=false` + restart; never a code revert.
- Before turning it on: one live v123 turn on a screen walkthrough (paid, about $0.10-0.50, not run) to see the planner
  copy the spoken names.

## Solid-colour fill (KRI-1173)

The FILL may be a flat colour instead of a pool photo or video ("fully white, fully green, another colour").
Contract, decisions and failure modes (S46–S64): `docs/reviews/speaker-background-color-failure-modes.md`.
Capability row: `speakerBackgroundColor`. Windows and the speaker frame still apply over a colour; windows stay
photo/video only.

| Where | Shape |
| --- | --- |
| Recipe (`SpeakerBackgroundLayer`) | `media_kind: "color"` + `color: "#RRGGBB"` (uppercase only, strict on the wire) and NO `asset_id` (absent, never null); `source_start` 0. A photo/video fill dumps exactly as before (`color` omitted while None, `asset_id` omitted only for a colour), so every earlier digest holds. `EditRecipeV2` needs no manifest asset for a colour and derives `speakerBackground` + `speakerBackgroundColor` (no `stillImages`/`visualVideos`). Shared fixture: `tests/fixtures/speaker-background-parity/v3.json`. |
| Lane (`SubtitledBackground`) | `kind` gains `"color"`; `media_id`/`gcs_path`/`generation` are absent (omitted from dumps) for a colour and required otherwise; `color` is stored UPPERCASE (lowercase is accepted at this boundary only). `background_media_refs` / `background_pins` yield nothing for the fill, so nothing is pinned, bound or reserved for it (windows still are). |
| Compile | `_compile_background` skips `_pin_background_visual` for a colour and builds the layer with `color=` and no asset id. |
| Editor document / Save | Document `{kind: "color", color, windows?, speaker_frame?}` (no `media_id`, no `source_start_s`). `EditorCommitSpeakerBackground`: `media_id` optional, `kind` gains `color`, `color` added (any case in, uppercase stored). A colour is never pinned (`background_pin_requests` yields only its windows). 422 `speaker_background_color_disabled` (reason, while the colour gate is closed and the Save SETS one) and `speaker_background_color_invalid` (bad hex, a colour with a `media_id`, a media kind without one, a colour with a source start); clearing is never gated. 426 `client_update_required` `feature: "speaker_background_color"`, `minimum_client_protocol: 34` for a build below 34 that sets a colour, Saves a variant already carrying one, or polls a pinned recipe using one (`phone_editor.py`, `device_render.py`). `lanes_from_recipe` rebuilds a colour layer (it is never silently dropped). |
| Capability | `editor_capabilities.speaker_background_color: true` only while the colour gate is open on a variant that may carry a background; thread capability `speaker_background_color`. |

### Gates

`speaker_background_color_supported()` = `speaker_background_supported()` AND `speakerBackgroundColor` in
`PHONE_RENDER_VERIFIED_FEATURES` AND (inside an app request) protocol ≥ `speaker_background_color_min_client_protocol`
(34; 33 is the media-tilt-zoom build). No new env flag: `SPEAKER_BACKGROUND_ENABLED` stays the one kill switch.
`speaker_background_color_unavailable_reason()` names the first failing check: `disabled`, `token_not_verified`,
`color_token_not_verified`, `client_protocol_below_min`.

`strip_unsupported_background` order: base gate closed drops the whole lane (as always); else a colour fill with the colour
gate closed drops the WHOLE lane too (there is no media fill to fall back to, and windows cannot exist without a fill) and
traces `speaker_background_color_phone_fallback` (`reason`, `color`, `window_count`, `had_frame`); else the windows check runs
as for a photo fill. The render equals one for a request that never asked: never a wrong colour.
`validate_phone_pilot_recipe` refuses an unstripped colour recipe without the token. The worker skips the
`stillImages`/`visualVideos` check for a colour fill (it needs only `speakerBackgroundColor`, already enforced by the strip).

### Rollout

`speakerBackgroundColor` joins `PHONE_RENDER_VERIFIED_FEATURES` only after the physical iPhone check in the failure-mode doc
(white then green fill in preview and export, a colour under two photo windows, a 60 s export within 120 s). Until then every
colour request is stripped and the creator keeps their own room. The chat entry points (strategy `background_media.color`,
copilot `set_background_color`) are described below.

### Chat

- **Strategy.** `background_media` takes EXACTLY ONE of `media_id` or `color` (`reaction_beats.BackgroundMedia`; `color` is a loose
  short string so a bad value never fails the turn's schema; no `source_start_s` with a colour). It is taught only while the
  manifest advertises `capabilities.speaker_background_color` (`creator_capabilities.speaker_background_color_open`): a separate
  prompt paragraph (`main_creator._SPEAKER_BACKGROUND_COLOR_PROMPT_SECTION`) so a closed colour gate leaves every creator prompt
  byte-identical. `repair_creator_speaker_background` normalises case, a missing `#` and the named presets (EN + TR, shared with the
  copilot in `reaction_beats.normalise_background_color`) to `#RRGGBB`; an invalid value or a closed capability drops the WHOLE
  background with a plain `Couldn't ...` notice (`SPEAKER_BACKGROUND_NOTICES["color_invalid"|"color_unavailable"]`, Turkish twins
  in `strategy_policy._KNOWN_TR`). The camera pick (a media pick) is never consulted for a colour. A colour keeps its `sequence`,
  `cadence_s` and `speaker_frame` while the windows gate is open (otherwise the windows notice, colour kept); only the window
  photos are taken out of the pop-ins, the colour reserves nothing.
- **Worker.** `_strategy_background_request` yields `{"color": "#RRGGBB", ...}` (no `media_id`); `_resolve_strategy_background`
  builds `SubtitledBackground(kind="color", color=...)` with no pool lookup (a colour with no sequence does not even read the pool)
  and reserves nothing for the fill; `strip_unsupported_background` then applies. `phone_lane_receipt["background"]` gains `color`
  for a colour fill (a media fill's receipt is unchanged).
- **Copilot.** `set_background_color {color}` (hex in any case or a named preset) in the same `background` family as
  `set_background`: it sets `{kind: "color", color}` and keeps the timed `windows` and `speaker_frame`; `set_background` replaces a
  colour fill, `remove_background` clears everything, `set_background_sequence` / `set_speaker_frame` work over a colour. The
  snapshot's `background` section gains `color_available` (and `color_reason` while closed) and `current` may be
  `{kind: "color", color}`. While the lane is open but the colour gate is closed the op answers as an unmet request carrying
  `color_reason` (`capability_unavailable` with that sentence in the reply notes, never the generic "can't change that"). Parity
  group `speaker_background` maps the iOS `setSpeakerBackgroundColor` to `set_background_color`.
- **Receipts.** The brief planner records a colour ask as `facts.speaker_background: true` plus `facts.background_color: "<hex or
  colour as said>"` (sticky; read through facts only, never the creator's words, KRI-667). `_check_speaker_background` answers a
  colour ask `met` ("a plain white background sits behind you ...") when the plan or finished video carries the colour,
  `partial` when it carries a different one, and `not_possible` with a NAMED reason otherwise (not an iPhone Talking edit, token not
  verified, app too old, or "this draft doesn't put a plain colour behind you"). "Attach the photos or videos first" is never the
  answer to a colour ask.

## Full screen first, then the corner (`SPEAKER_FULL_OPENING_ENABLED`, Qendresa item 16)

Her screen walkthroughs open with her full screen in her own room for 3-7 s (V5: 0-7.2 s, switching on "first
one"; V1: 0-2.5 s), then she sits small in the bottom-right corner over the recordings. Contract and failure
modes S70-S82: `docs/reviews/speaker-background-windows-failure-modes.md` (last section).

| Where | Shape |
| --- | --- |
| Recipe (`SpeakerBackgroundLayer`) | `full_screen_windows: [{start, end}]` (output seconds, sorted, disjoint, >= 0.5 s, 1-4, absent while unused). Inside one the phone draws the speaker's own take untouched: no person mask, no background, no `speaker_frame`; decided per frame. Capability `speakerFrameWindows`; shared fixture `tests/fixtures/speaker-background-parity/v4.json`. |
| Lane (`SubtitledBackground`) | `full_screen_windows: tuple[SubtitledFullScreenWindow]` in ORIGINAL take time, moved by `remap_lanes_for_cut` like windows; `strip_unsupported_background` drops only them on a closed gate (trace `speaker_full_opening_phone_fallback`). |
| Strategy | `background_media.full_screen_opening: true` (+ `full_screen_opening_s` 1-15 when a length was named), omitted while unused; taught by Main Creator v125 only while the manifest advertises `speaker_full_opening`. Brief fact `speaker_full_opening` (+ `_s`), Main Creator v125 / Brief extractor v60, only with the flag on. |
| Layout | `phone_background_sequence.full_screen_opening_end`: the stated length, else the first recording's word when heard 2-8 s in, else 5 s, never before the hook title ends; snapped to a gap between kept words (a sentence end within 1 s first), never inside a word, >= 1 s of take on each side. `lay_out(opening_s=X)`: no window starts before X (the lead-in opens the first window at X). |
| Receipts | Finished: `phone_lane_receipt.background.full_screen_opening_s` (read off the compiled recipe), or a `dropped` entry `{lane: background, part: full_screen_opening, reason: app_too_old\|unavailable\|take_too_short}`. `_check_full_screen_opening` (fact route) and `_check_speaker_framing` never answer "fills the frame" for a corner speaker; the editor document wins over the lane receipt. |
| Editor | Document `speaker_background.full_screen_windows: [{start_s, end_s}]` (cut time); Save validates it (422 `speaker_full_opening_disabled` / `_invalid`), 426 `speaker_full_opening` for a build below 38 that sets it or Saves a variant carrying it, and on a device poll of a pinned recipe with it. Capability `editor_capabilities.speaker_full_opening`. iPhone: a "Starts full screen" row with a remove button; every background change keeps it, removing the background clears it. Copilot ops keep it (no new op). |

Gates: `speaker_full_opening_supported()` (request-aware) and `speaker_full_opening_planned_for(protocol)` (the
build the creator acted from: the turn's bound build, then `creator_client_protocol` on the Job). An old build
gets today's corner from 0 s and an honest "update the app" receipt, never a 426 on the whole video.
Rollout: verify `speakerFrameWindows` on a device (opening in preview and export, the switch frame cut out
cleanly, a failed mask at the switch falls back to the plain speaker in the corner), add it to
`PHONE_RENDER_VERIFIED_FEATURES`, then the flag (api + worker, restart). Rollback: the flag off.
Not changed: the corner preset's size (her corner face is 1.5-2x today's `small_bottom_right`).

## Aimed at the readable text (`BACKGROUND_FOCUS_ENABLED`, Qendresa item 18)

A landscape screen recording behind the speaker used to be cover-cropped to its middle (a 16:9 recording shows
31.6 % of its width), so every README line was cut at its start: the frame was full of words, none readable as a
sentence. Her own Q2 videos zoom so the text column fills the width, lines read from their first word, black above
and below. Failure modes F1-F17 and the measured before/after: `docs/reviews/background-focus-failure-modes.md`.

| Where | Shape |
| --- | --- |
| Recipe (`SpeakerBackgroundLayer`) | Optional `focus: {x, y, zoom}` on each `windows[i]` and on a photo/video fill (a colour fill with one is invalid), absent while unused. Meaning = `VisualMediaPlacement`: cover base, drawn scale `max(cover * zoom, contain)`, `x`/`y` align the picture (0 left/top, 0.5 centre, 1 right/bottom); zoom 0.2-4; below cover the rest of the frame is black. Capability `backgroundFocus`; shared fixture `tests/fixtures/speaker-background-parity/v5.json`. |
| Measure (free) | `app/services/background_text_focus.py`: while `bind_phone_visual_assets` has the file on disk (flag on, picture >= 1.25x wider than the canvas aspect) a few frames are reduced to grey, the horizontal steps glyph strokes make are summed per column, the densest contiguous span is the text column and the spacing of text-row starts is the line pitch. `PhoneVisualBinding.text_column` holds it in memory only (excluded from every dump, so receipts are unchanged). No AI call. |
| Auto focus | `focus_for_text_column`: zoom so the column fills the width, but never so far out that a text line is under ~40 px of the 1920 px canvas (her V5 measures 39-44 px) and never above 2.5x; a column wider than what shows is shown from its left edge (lines read from their start), else centred. Applied at compile (`_FocusResolver` in `phone_subtitled_plan.py`) to a fill or window without a creator focus; nothing measurable = today's centre. |
| Lane (`SubtitledBackground`) | `focus: SubtitledBackgroundFocus(x?, y=0.5, zoom?)` on the fill and each window; missing parts are filled at compile from the measure, else 0.5 / 1.0. `remap_lanes_for_cut` keeps it; `strip_unsupported_background` drops every focus on a closed gate (trace `background_focus_phone_fallback`). |
| Strategy | `background_media.focus` (`left`/`center`/`right`) and `focus_zoom` (0.3-3), only when the creator names a side or a zoom; "zoom in so the text is readable" needs neither (the measure does it). Main Creator v126 teaches them only while the manifest advertises `background_focus`; repair keeps them only then (notice for an old build). |
| Chat | Edit Copilot v116 op `set_background_focus {position?, zoom?, zoom_delta?, asset_id?}` (family `background_focus`, offered only while the gate is open); the other background ops keep a recording's focus when its media stays. |
| Editor | Document `speaker_background.focus` / `windows[i].focus` (`{x, y, zoom}`); Save checks it only when set or changed (422 `background_focus_disabled` / `_invalid`, 426 `background_focus` for a build below 39); capability `editor_capabilities.background_focus`. iPhone: Left / Centre / Right and a zoom slider (0.3-3x) on the fill and on each window. |

Gates: `background_focus_supported()` (request-aware) and `background_focus_planned_for(protocol)`; the worker passes
the approving build's stamp to the compile (`background_focus_open`), so a build below 39, a missing stamp, a closed
flag or an unverified token get today's centre crop and a recipe byte-identical to before. Rollout: verify
`backgroundFocus` on a device (aimed recording in preview and export, black bands below cover, the slider and the
three buttons), add it to `PHONE_RENDER_VERIFIED_FEATURES`, then the flag (api + worker, restart). Rollback: the flag
off. Not changed: receipts (an ask to zoom still says it can't be verified), caption and card placement (they do not
read the background picture).

## Rollback

Kill switch, never a code revert: `fly secrets set SPEAKER_BACKGROUND_ENABLED=false
--app nova-video` and restart api + worker. New compiles strip the lane; recipes already
pinned keep theirs and old builds keep their 426. Apply order for turning it on:
`PHONE_RENDER_VERIFIED_FEATURES` gains `speakerBackground` first (after a physical
iPhone preview + export check), then the flag, then restart.

## Tests

`tests/kria/test_speaker_background_chat.py` (strategy field, manifest, repair, prompt),
`tests/tasks/test_speaker_background_strategy_worker.py` (worker resolution, reservation),
`tests/integration/test_speaker_background_camera_pick_postgres.py` (camera pick route),
`tests/kria/test_speaker_background.py` (compile, gates, S13), `tests/tasks/test_speaker_background_worker.py`
(worker), `tests/routes/test_speaker_background_editor.py` (426, Save, capability,
projection), `tests/services/test_phone_speaker_background_pin.py` and
`tests/integration/test_speaker_background_pin_postgres.py` (S19), and the hash guard in
`tests/schemas/test_guided_edit_revision.py`. KRI-1173 colour fill:
`tests/kria/test_speaker_background_color.py` (wire, lane, gates, strip, compile),
`tests/routes/test_speaker_background_color_editor.py` (426, Save, capability, projection) and the colour cases in
`tests/tasks/test_speaker_background_worker.py`.

## Evidence

Device and Mac exports, the camera and matte cost on an iPhone 13 Pro, and the Phase 0 matte
spike are in `docs/reviews/speaker-background-evidence/` (its README names each file).
