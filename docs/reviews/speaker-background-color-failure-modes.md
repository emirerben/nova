# Solid-colour green screen: a plain colour behind the speaker — failure modes (written before the code)

Linear: KRI-1173 (the feature). Project `Talk-to-camera green-screen backgrounds` (P-KRI-21).
Extends the first cut (`docs/reviews/speaker-background-failure-modes.md`, S1–S26) and the
commentary cut (`docs/reviews/speaker-background-windows-failure-modes.md`, S27–S45); the row
numbers here continue (S46+).

The creator's need (Emir, 2026-11-03): "sometimes I don't want to show my real background in a
talk-to-camera video. I want a simple background option with a color I can customize, such as fully
white, fully green, or another color."

Scope of this cut: on a device-rendered Talk-to-camera (`subtitled`) edit the FILL background may be a
solid colour instead of a pool photo or video. The person mask, the timed `windows` and the
`speaker_frame` are unchanged: a colour fill may sit under media windows and under a corner speaker.
Entry points: the editor Background tool (swatches + a custom colour), chat (strategy + copilot op).
Out of scope: a colour INSIDE a window (windows stay media), the live camera preview and the camera
pick (both stay media-only; the take is raw and the colour is applied at render), cloud variants,
the web editor, gradients, transparency, blur.

## Contract (the one source of truth for every lane)

### Recipe (V2 wire, `speaker_background`, snake_case; Swift decodes with `.convertFromSnakeCase`)

```
speaker_background: {
  media_kind: "color",                       # NEW literal beside "image" | "video"
  color: "#RRGGBB",                          # REQUIRED iff media_kind == "color"; ABSENT otherwise
  # asset_id is ABSENT for a colour (never null, never ""); REQUIRED for image | video as today
  source_start: 0.0, loop: true, subject_track_id,   # unchanged; a colour has source_start 0
  windows?: [...], speaker_frame?: {...}     # unchanged (KRI-1162); windows are media only
}
```

Rules, enforced identically by `app/kria/portable_speaker_background.py` and
`KriaMediaEngine/SpeakerBackground.swift`, pinned by `tests/fixtures/speaker-background-parity/v3.json`
(v1.json and v2.json stay untouched and must still pass):

- `color`: exactly `#` + six UPPERCASE hex digits (`^#[0-9A-F]{6}$`). Lowercase, `#RGB`, eight digits
  (alpha), named colours and a missing `#` reject the recipe. Normalisation (lowercase → uppercase,
  names → hex) happens at the editor Save, the lane and the chat boundary, never on the recipe wire.
- `media_kind == "color"` ⇔ `color` present ⇔ `asset_id` absent. Any other combination rejects.
  A colour has `source_start == 0` (like a photo).
- A colour fill needs NO `VisualRenderAsset`; every window still names a matching-kind manifest asset.
- Capability: a recipe whose fill is a colour requires **`speakerBackgroundColor`** (new token in
  `app/kria/recipes.py` and the Swift `MediaCapability` mirror; `tests/kria/test_capability_matrix.py`)
  in addition to `speakerBackground` (and `speakerBackgroundWindows` when it also carries windows or a frame).
  Both `EditRecipeV2` (Python) and `effectiveCapabilities` (Swift) derive it.
- A recipe whose fill is a photo or video serialises byte-for-byte as today (digest invariant): `color` is
  omitted while `None`, `asset_id` is omitted only for a colour.

### Protocol and gates

- Client protocol **34** = decodes `media_kind: "color"` + `color` + the `speakerBackgroundColor` capability
  (`KriaClientProtocolContract.version` 33 → 34; 33 is the media-tilt-zoom build; no open PR takes 34 on
  2026-11-03). `KriaTests` / `SuggestionChipsTests` header assertions move with it.
- Server: `speaker_background_color_min_client_protocol: int = 34` (`app/config.py`). Gate
  `speaker_background_color_supported(require_client=...)` in `app/services/phone_rollout.py` =
  `speaker_background_supported()` AND `speakerBackgroundColor` in `phone_render_verified_features` AND
  (inside an app request) protocol >= 34. `speaker_background_color_unavailable_reason()` names the first
  failing check: `disabled`, `token_not_verified`, `color_token_not_verified`, `client_protocol_below_min`.
  No new env flag: `SPEAKER_BACKGROUND_ENABLED` stays the one kill switch; the new verified token is the
  device-parity gate for the new drawing.
- Strip order (`strip_unsupported_background`): first the whole lane as today when the base gate is closed;
  else, when the fill is a colour and the colour gate is closed, the WHOLE lane is dropped (there is no
  media fill to fall back to, and windows cannot exist without a fill) and
  `speaker_background_color_phone_fallback` is traced with `reason`, `color`, `window_count`, `had_frame`;
  else the windows check runs as today. The result equals a request that never asked.
- `validate_phone_pilot_recipe` refuses a recipe carrying a colour fill without the token.
- Device-render poll and editor Save answer 426 `client_update_required` (`feature: "speaker_background_color"`,
  `minimum_client_protocol: 34`) to a build below 34 for a pinned recipe / stored variant whose fill is a colour.

### Server lane (`app/pipeline/phone_subtitled_lanes.py`)

```
SubtitledBackground {
  kind: "image" | "video" | "color"
  media_id?, gcs_path?, generation?        # REQUIRED for image | video; ABSENT (omitted) for color
  color?: "#RRGGBB"                        # REQUIRED for color; omitted otherwise
  source_start_s, windows, speaker_frame   # unchanged
}
```

A media background dumps exactly as before (`color` omitted). `background_media_refs` / the worker's
pin and `used_media_ids` reservation skip a colour fill and still cover every window.
`remap_lanes_for_cut` is unchanged (the fill never moves).

### Editor document / Save

- Document `speaker_background`: `{kind: "color", color: "#RRGGBB", windows?, speaker_frame?}` for a colour
  (no `media_id`, no `source_start_s`); the media shape is unchanged.
- `EditorCommitSpeakerBackground`: `media_id` becomes optional, `kind` gains `"color"`, `color` is added.
  Exactly one of `media_id` / `color` per the kind; `color` is normalised to uppercase before validation and
  storage; lowercase input is accepted on this boundary. A colour fill is never pinned
  (`pin_pool_visual_for_background` is skipped for it; windows are still pinned per id). Refusal codes:
  `speaker_background_color_disabled` (reason, while the colour gate is closed and the Save SETS a colour),
  `speaker_background_color_invalid` (bad hex, a colour with a `media_id`, a media kind without one),
  plus the 426 above. Clearing is never gated.
- `editor_capabilities.speaker_background_color: true` only while the colour gate is open on a variant that
  may carry a background; absent otherwise (old payloads byte-identical).
- Thread capability `speaker_background_color` beside `speaker_background` / `speaker_background_windows`.

### Chat

- Creator strategy `background_media` (reaction_beats.py `BackgroundMedia`): `media_id` becomes optional
  and `color: "#RRGGBB"` is added (omitted while unused). Exactly one of the two. With `color`, `sequence`
  and `speaker_frame` keep their meaning (windows over the colour). Taught only when the manifest
  advertises `capabilities.speaker_background_color` (`main_creator._SPEAKER_BACKGROUND_PROMPT_SECTION`
  gains a colour paragraph with the named presets below; `MAIN_CREATOR_PROMPT_VERSION` bumps).
- Repair (`creator_capabilities.repair_creator_speaker_background`): a colour is normalised and kept when
  valid and the colour capability is advertised; an invalid value or a closed capability drops the field
  with a plain `Couldn't ...` notice (never a failed turn).
- Worker (`generative_build._resolve_strategy_background`): a colour resolves to
  `SubtitledBackground(kind="color", color=...)` without a pool lookup; `strip_unsupported_background`
  then applies; windows resolve as today.
- Copilot op (`editor_ops_v2/background.py`): `set_background_color {color}` where `color` is a hex
  (any case) or one of the named presets `white #FFFFFF`, `black #000000`, `green #00FF00`,
  `blue #0000FF`, `red #FF0000`, `yellow #FFFF00`, `grey`/`gray #808080` and their Turkish names
  (`beyaz`, `siyah`, `yeşil`, `mavi`, `kırmızı`, `sarı`, `gri`). It sets the fill to the colour and
  keeps `windows` / `speaker_frame` (unlike `set_background`, which replaces the windows: a colour is a fill
  swap, a new photo is a new background); `set_background` (media) replaces a colour fill as it replaces a media
  fill; `remove_background` clears everything. Snapshot `background` gains `color_available: bool` (with
  a `color_reason` while closed) and `current` may be `{kind: "color", color}`. Refusals:
  `BACKGROUND_REFUSALS` gains the colour-invalid and colour-closed wording (EN + TR). Parity group
  `speaker_background` maps the new iOS session function `setSpeakerBackgroundColor`.
- brief_checks (KRI-1161 rules apply): the brief planner records a colour ask as a fact
  (`facts.speaker_background: true` + `facts.background_color: "<hex or name>"`); `_check_speaker_background`
  answers a colour ask `met` ("a white background behind you") when the colour gate is open, and
  `not_possible` with a NAMED reason (gate closed / app too old / not an iPhone Talking edit) otherwise.
  "Attach the photos or videos first" is NEVER the answer to a colour ask (no pool is needed).

### Engine (`KriaMediaEngine`)

- `SpeakerBackgroundLayer.assetID` becomes `String?`, `color: String?` is added; `validate()` enforces the
  rules above (`mediaKind == .color` ⇔ `color != nil` ⇔ `assetID == nil`). `VisualMediaKind` gains `.color`
  ONLY if that enum is not shared with window/card decoding; otherwise the layer carries its own
  `SpeakerBackgroundFill` kind. The windows decoder keeps rejecting `color`.
- The fill layer for a colour is a `CIImage(color:)` in sRGB cropped to the canvas, built once, with no
  asset download, no composition track and no sample window; `assetURLs` need no entry for it. Preview
  (`LivePreviewComposition`) and export (`RecipeVideoCompositor.composeBelowText`) share that one builder,
  so both show the same colour. The matte path (S1–S7, S27) is untouched.

### iOS app

- `NativeEditorDocument.speakerBackground` carries `kind: "color"` + `color`; `NativeEditorRenderCompiler`
  emits the recipe shape above; `NativeEditorSession.setSpeakerBackgroundColor(hex:)` writes it (parity op
  `set_background_color`); Save sends `{"kind": "color", "color": "#RRGGBB"}`.
- Background panel (`NativeBackgroundPanel`): a "Colour" section with swatches White, Black, Green, Blue,
  Grey and a `ColorPicker` for a custom colour (`supportsOpacity: false`), shown only while
  `editor_capabilities.speaker_background_color` is true; picking a swatch replaces a media fill and keeps
  windows / the speaker frame; the current colour swatch is marked selected. Pattern:
  `SlidePostTiltPanel`'s "Background colour" picker and `nativeEditorHex(_:)` / `nativeEditorColor(_:)`.
- `KriaClientProtocolContract.version = 34`; `openapi.yaml` `speaker_background` schemas gain the fields.

## Decisions that remove whole classes of bugs

- **A colour is a fill, not a window.** Nothing about timing, remap or the matte changes; the only new
  drawing is one flat layer at the bottom of the stack.
- **No asset, no pin, no download.** A colour never touches the pool, GCS or `assetURLs`; the device cannot
  fail to fetch it and the server cannot leak a path.
- **Strict hex on the wire, forgiving at the edges.** The recipe accepts one spelling so both decoders and
  every digest agree; the Save, the lane and the chat normalise case and names before that boundary.
- **The new token gates the new drawing.** `speakerBackground` alone never lets a colour fill reach a phone;
  `speakerBackgroundColor` is added to `PHONE_RENDER_VERIFIED_FEATURES` only after a physical iPhone
  preview + export check.
- **Closed gate = no lane, never a wrong colour.** There is no media fill to fall back to, so the strip drops
  the whole background and traces why; the creator keeps their own room, exactly as for a request that
  never asked.

| # | Failure mode | Treatment | Pinned by |
|---|---|---|---|
| S46 | Python and Swift disagree on the colour fields | Shared fixture `tests/fixtures/speaker-background-parity/v3.json` (valid → normalized, invalid rejected) read by both suites; v1 + v2 still pass on both. | `tests/kria/test_speaker_background_color.py::test_shared_fixture_v3`, `SpeakerBackgroundTests.testSharedFixtureV3` |
| S47 | A colour fill with an `asset_id`, a media fill with a `color`, lowercase / short / alpha hex on the wire | Rejected on both sides at decode (fixture `invalid` rows). | S46 fixture rows |
| S48 | A media recipe's digest changes because of the new optional fields | `color` omitted while `None`; `asset_id` omitted only for a colour; the v1/v2 fixtures' `normalized` forms are unchanged. | `tests/kria/test_speaker_background_color.py::test_media_fill_is_byte_identical_to_today`, hash guard `tests/schemas/test_guided_edit_revision.py` |
| S49 | Old app build receives `media_kind: "color"` | Unknown literal below 34 (strict decoder); the compile strips the lane below 34 or without the token; a pinned recipe answers 426 `speaker_background_color` on poll and Save. | `tests/kria/test_speaker_background_color.py::test_protocol_below_34_strips_the_colour_lane`, `tests/routes/test_speaker_background_color_editor.py::test_poll_tells_an_old_build_to_update`, `::test_save_from_old_build_is_426` |
| S50 | Token not verified / kill switch off | Base gate closed: whole lane stripped (S17). Colour token missing: whole lane stripped, trace `speaker_background_color_phone_fallback`; `validate_phone_pilot_recipe` refuses an unstripped recipe. | `tests/kria/test_speaker_background_color.py::test_colour_token_missing_strips_the_lane`, `::test_pilot_validation_refuses_unverified_colour` |
| S51 | Preview and export draw a different colour (colour space, premultiplication) | One builder makes the flat `CIImage` in sRGB for both composers; a composed frame's pixel away from the speaker equals the requested RGB within 2/255 for white and pure green in both paths. Mid-tones (`#336699`) come out lifted by about +7..+10 per channel in BOTH paths (the same video-pipeline lift a photo fill shows, even more); preview and export agree within 3. The device check must confirm the lift is acceptable or pin its cause. | `SpeakerBackgroundCompositionTests.testColourFillShowsTheRequestedColourBehindTheSpeaker`, `SpeakerBackgroundLivePreviewTests` colour case |
| S52 | A colour fill under media windows / a corner speaker | Windows draw over the colour in their span; the colour shows in gaps; the speaker frame scales the cut-out as in S27. | `SpeakerBackgroundCompositionTests.testWindowsDrawOverAColourFill`, `tests/kria/test_speaker_background_color.py::test_colour_fill_with_windows_compiles` |
| S53 | The matte never produced a mask while the fill is a colour (S3 fallback) | Unchanged S3 behaviour, the same as for a photo: without a speaker frame the plain speaker layer (room and all) is drawn and the fill is skipped; with a frame (S27b) the colour is composed and the plain speaker is drawn small. Never a black canvas. | `SpeakerBackgroundCompositionTests.testColourFillWhenNoMaskIsProduced` |
| S54 | Editor Save: a colour that is not a pool visual is run through the pin and refused `media_not_in_pool` | The pin is skipped for a colour fill; windows are still pinned per id; a colour with a `media_id` is 422 `speaker_background_color_invalid`. | `tests/routes/test_speaker_background_color_editor.py::test_save_a_colour_needs_no_pin`, `::test_colour_with_a_media_id_is_refused` |
| S55 | Save sets a colour while the colour gate is closed | 422 `speaker_background_color_disabled` with the reason; clearing stays allowed; a media Save is unaffected. | `tests/routes/test_speaker_background_color_editor.py::test_save_colour_while_closed_is_422` |
| S56 | Editor draft rebase / server projection drops the colour (the KRI-1165 class) | The document and `drafts` projection carry the colour shape through `directKeys` / `_augment_variant_with_phone_subtitled_editor_sections` exactly as a media background. | `tests/kria/test_draft_projects_speaker_background.py` colour case, `SpeakerBackgroundDraftRebaseTests` colour case |
| S57 | Chat: the model writes a named colour or lowercase hex into the strategy | Repair normalises names and case to `#RRGGBB`; anything else is dropped with a notice. | `tests/kria/test_speaker_background_chat.py::test_strategy_colour_is_normalised_or_dropped` |
| S58 | Chat: a colour ask is answered "Attach the photos or videos first" | `_check_speaker_background` reads `facts.background_color` and never requires a pool for it. | `tests/kria/test_brief_checks_speaker_background.py::test_colour_ask_needs_no_pool` |
| S59 | Chat: `set_background_color` on a Talking edit whose colour gate is closed | Refused as `capability_unavailable` carrying the snapshot's `color_reason` as its detail and reply note (never the generic "Not done: put that behind you" sentence); while the base lane is closed the family is absent as today. | `tests/routes/test_speaker_background_chat_ops.py::test_a_closed_colour_gate_is_an_unmet_request_with_the_snapshots_reason` |
| S60 | Worker reservation treats a colour as a media id (`used_media_ids`, `phone_lane_receipt`) | `background_media_refs` yields only windows for a colour fill; the receipt lists `applied: [background]` as today. | `tests/tasks/test_speaker_background_worker.py::test_colour_fill_reserves_nothing` (lane request), `tests/tasks/test_speaker_background_strategy_worker.py::test_strategy_colour_reserves_nothing` (strategy) |
| S61 | Editor parity drift: a new iOS session function without an agent op | Parity group `speaker_background` gains `setSpeakerBackgroundColor`. | `test_ios_editor_actions_have_agent_ops` |
| S62 | Capability drift | `speakerBackgroundColor` in both `MediaCapability` mirrors and the capability-matrix doc. | `tests/kria/test_capability_matrix.py` |
| S63 | Protocol collision | Server's newest is 33 (`media_tilt_zoom_min_client_protocol`), app `version = 33`; no open PR takes 34 (checked 2026-11-03), so this feature takes **34**. Re-check open branches before merge. | `KriaTests` header assertions |
| S64 | Live camera preview shows the room while the plan will paint a colour | The camera pick stays media-only in this cut; the teleprompter shows "Preview" and never claims parity (S24, S44). Documented gap, follow-up issue. | n/a, copy review |

Device tests owed before `speakerBackgroundColor` is verified: on an iPhone, a Talk-to-camera take with a
white fill and then a green fill must show the flat colour (no room, no halo tint) behind the cleanly cut-out
creator in the editor preview and in the export; a colour fill under two photo windows must show the colour
only in the gap; a 60 s export must finish within 120 s without a thermal step-down.
