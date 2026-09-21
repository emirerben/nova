# KRI-132: phone-render format & feature matrix

## Summary (plain English)

- **Only three of nine video types have an iPhone render path today: Montage, Day-Vlog, and Single-Hero — and only when they came from an approved guided-story plan.** These three are the "guided" formats; the on-device app compiles a pre-approved edit plan into a native recipe and never touches raw pixels on the server.
- **In the app, only Montage is actually reachable.** The chat format picker never offers Day-Vlog or Single-Hero to any account (`_available_formats()`, `app/routes/creation_threads.py:607-615`), so after the phone filter the picker shows exactly one format. Day-Vlog/Single-Hero reach the phone only if the planner picks them from a free-text request.
- **Talking-Head, Subtitled, and all three Narrated variants never render on the phone.** They're cloud-only today — no phone compiler exists for them at all, and the chat planner is explicitly blocked from proposing them on a phone account (it fails with a clear in-chat message, not a crash or a silent cloud render).
- **Slides (mixed photo/video posts) can't even be requested through the chat planner on any account, phone or web** — it's not a bug specific to phone rendering, slides simply isn't part of that flow yet.
- **A live bug, confirmed by reading both the Python compiler and the Swift renderer: phone-rendered background music never fades in or out.** The intended fade-envelope code path in the iOS app is wired to a field the phone compiler never sets, so music jumps to full volume on frame one and cuts hard at the end. See "musicBed fade envelope" below.
- **First fixes to prioritize:** (1) the silent music-fade bug above (cheap, isolated, high visibility), (2) deciding whether a voiceover attached to a guided-format item should be blocked earlier — today it slips past the approval gate and only fails once the worker picks the job up, wasting a dispatch, (3) a captions data contract is the actual blocker for Subtitled/Talking-Head phone support, not device performance — nothing else can start until that schema exists.

The backend and the iOS sources were traced separately; both are in the User Journey section below.

All line numbers were verified against `origin/main` at commit `88eb0e5` (2026-09-21) by grep, not copied from memory.

---

## (a) Format table

| EditFormat | Phone status | Compile path | Gating evidence |
|---|---|---|---|
| `montage` | **Works**, guided-approved only | `app/pipeline/phone_guided_plan.py::compile_phone_guided_plan` via `_run_phone_guided_job` (see "Which compiler actually runs" below) | In `GUIDED_EDIT_FORMATS` and `PHONE_RENDER_SUPPORTED_FORMATS` (`app/agents/_schemas/edit_format.py:75, 92`). Dispatch gate at `app/tasks/content_plan_build.py:1467-1471` requires an approved guided proposal. Worker fork `app/tasks/generative_build.py:2172-2180`. |
| `day_vlog` | **Works**, guided-approved only; not offered by the chat picker | same as montage | Same gates; additionally requires `NARRATIVE_CLIP_ORDER_ENABLED` and `edit_format_day_vlog_enabled` upstream at the planning layer (`app/services/creator_capabilities.py:104-121`), not re-checked at the phone dispatch gate itself. |
| `single_hero` | **Works**, guided-approved only; not offered by the chat picker | same as montage | Same gates; requires `edit_format_single_hero_enabled` upstream (`creator_capabilities.py:122-133`). |
| `talking_head` | **Blocked** | none — cloud-only `app/pipeline/talking_head_assembler.py` | Not in `GUIDED_EDIT_FORMATS`; dispatch gate rejects with `unsupported_format` (`content_plan_build.py:1473-1476`). Chat planner fails with `PhoneFormatUnavailableError` (`creator_policy.py:113-114`). |
| `subtitled` | **Blocked** | none — cloud-only (assembled inline in `app/tasks/generative_build.py`'s subtitled path) | Same dispatch-gate rejection as `talking_head`. |
| `narrated` | **Blocked** | none | Same. Additionally requires a recorded voiceover or `NARRATED_SELF_NARRATION_ENABLED`, neither of which changes the phone outcome. |
| `narrated_planned` | **Blocked** | none | Same. |
| `narrated_ready` | **Blocked** | none | Same. |
| `slides` | **Blocked** | none on phone; cloud path is a dedicated slide-post drafting flow (`app/routes/plan_items.py`, `slide_post` field), not the Main Creator Agent chat strategy | `_format_availability` (`app/services/creator_capabilities.py:101-148`) has no branch for `"slides"` at all — it's unavailable to the chat planner on every account, not just phone. Falls through to the dispatch gate's generic `unsupported_format` if ever attempted with clip paths. |

**Conditional, not in the table above:** montage/day_vlog/single_hero **with a voiceover attached** dispatch successfully (the dispatch gate's non-guided fallback branch lets them through with no approved proposal at all — see "A live gate quirk" below) but the worker always rejects them once picked up (`app/tasks/generative_build.py:3906-3907`, `"Phone rendering does not yet support voiceover edits"`). This is recorded as **not working** — see `works_on_phone` in `src/apps/api/tests/tasks/_phone_format_expectations.py`.

### Which compiler actually runs

There are two phone compilers, and the dispatch gate decides between them purely on `guided_edit_applicable()` (`app/tasks/content_plan_build.py:1106-1109`):

- **No voiceover** → `guided_applicable` is always `True` for montage/day_vlog/single_hero → the phone gate requires an approved proposal (`content_plan_build.py:1468-1471`) → the worker sees `guided_edit` in the snapshot and runs `_run_phone_guided_job` → `compile_phone_guided_plan` (`app/tasks/generative_build.py:2172`).
- **Voiceover attached** → `guided_applicable` flips to `False` → the non-guided branch only checks `PHONE_RENDER_SUPPORTED_FORMATS` → the worker runs `_run_phone_montage_job` → `compile_phone_montage_plan`... which rejects every voiceover on its first content check.

So today `compile_phone_montage_plan` (KRI-114 P1-2/P1-3, including its music-bed track) has **no production entry that can succeed**: its only real entry is the voiceover case it refuses. `test_phone_gate_allowlist_extension_binds_without_approval` documents the no-approval dispatch as the intended extension point. Voiceover-on-phone (the KRI-132 follow-up PR) is what makes this compiler live; until then the music-bed findings below describe code that is compiled and unit-tested but not exercised by a real phone render.

### A live gate quirk: voiceover bypasses guided approval

Read together, `guided_edit_applicable()` (`app/agents/_schemas/edit_format.py`, `render_program_for_intent`) and the dispatch gate (`app/tasks/content_plan_build.py:1467-1476`) produce a counter-intuitive path: attaching a voiceover to a `montage`/`day_vlog`/`single_hero` item makes `guided_edit_applicable()` return `False` (voiceover always forces the "native" render program, checked before the format lookup). That flips the dispatch gate from its **guided branch** (requires an approved proposal) to its **non-guided branch** (only checks `PHONE_RENDER_SUPPORTED_FORMATS`, which montage/day_vlog/single_hero are already in) — so a voiceover item dispatches with **zero approval**, binds phone sources, and mints a Job. The worker then rejects it at `generative_build.py:3906-3907`. Net effect: this is a wasted round-trip (ingest is never reached — the voiceover check is the worker's very first content check, before ingest), not a rendering failure a user would silently miss, but it means the dispatch-time "did this succeed" signal lies for this one combination. Confirmed empirically; see `test_dispatch_outcome_matches_table` + `test_worker_rejects_voiceover_even_when_dispatch_let_it_through` in `src/apps/api/tests/tasks/test_phone_format_matrix.py`.

---

## (b) Feature table

| Feature | Status | Evidence |
|---|---|---|
| Footage (bound clips) | Works | Always allowed regardless of lane (`app/services/edit_proposals.py:427`, `phone_renderable_media`). |
| Photos (`stillImages`) | Gated by flag | Verified only when `"stillImages"` is in `PHONE_RENDER_VERIFIED_FEATURES` (`edit_proposals.py:418-423`, `phone_destination.py:48-52`). |
| Visual videos (`visualVideos`) | Gated by flag + codec/duration | Verified only when `"visualVideos"` is in `PHONE_RENDER_VERIFIED_FEATURES`, additionally rejected past `MAX_PHONE_VISUAL_VIDEO_S` or an uncomposable codec/pix_fmt (`edit_proposals.py:433-437`, `phone_composable_video` in `app/services/phone_visuals.py`). |
| Original audio | Works | `preserve_source_audio` compiles through `AudioMixRecipe` (`phone_guided_plan.py:454`); `phone_montage_plan.py` original-audio variant likewise preserved. |
| Music bed | **Blocked on device** — compiles, but the exporter cannot open the downloaded track (simulator-verified); also no fade. See the two sections below | `phone_montage_plan.py:262-305` emits the track; `Composition.swift` plays it flat-volume with no fade in/out. |
| Voiceover / narration audio | Blocked | `narration` is in `_UNSUPPORTED_PHONE_LANE_CAPABILITY` (`phone_guided_plan.py:51-60`); montage-family voiceover is rejected at the worker (`generative_build.py:3906-3907`); the planner itself blocks voiceover on phone before any of that (`creator_policy.py:109-112`, `PhoneFormatUnavailableError(voiceover=True)`). |
| Static/authored text (agent intro overlay) | Works | `phone_montage_plan.py:208-258` (`build_persistent_intro_overlays`); `phone_guided_plan.py:396-433` compiles `text_elements` via `build_overlays_from_text_elements`. |
| Animated/effect text | Gated, per-instance | `phone_guided_plan.py:413-420` allowlists 5 sequence effects (`fade-in`, `static`, `none`, `handwriting`, `ink-reveal`); `app/services/phone_rollout.py:validate_phone_pilot_recipe` does a SECOND, defense-in-depth font/effect qualification pass (`phone_rollout.py:152-208`) gated by `PHONE_FONT_QUALIFICATION_STRICT`. |
| Speech captions | **Blocked, no schema** | No caption/cue field exists anywhere in `app/kria/recipes.py` / `recipes_v2.py` (confirmed by grep — the only hit is an inert `"captions"` entry in the `MediaCapability` Literal, `recipes.py:171`, explicitly commented as "not enabled by being named"). |
| Lyrics | Blocked | `lyrics_rendered`/`text_mode == "lyrics"` explicitly rejected (`phone_montage_plan.py:87-90`, capability `musicBed`). |
| Transitions | Works, allowlisted | Crossfade/dip-to-black/flash/whip-pan-family supported; anything else raises `UnsupportedPhonePlan` (`phone_montage_plan.py:168-171`, `phone_guided_plan.py:114-118`). |
| Speed changes | Works | `rate` compiled per clip, `variableSpeed` capability added when any clip's rate ≠ 1 (`phone_montage_plan.py:140-142, 196, 321`). Guided-story moments with a non-default `playback_rate` are rejected instead (`phone_guided_plan.py:177, 190`) — speed is supported in the montage family only. |
| Crop / reframe | **Blocked** | `phone_guided_plan.py` explicitly rejects any moment/still with `source_crop is not None` (lines 176, 189) with the comment *"The recipe has no crop or retime for story footage; dropping either silently would render something the creator didn't approve."* `phone_montage_plan.py`'s input schema (`GenerativeVariantDecision`) has no crop field to reject in the first place — cropping simply never reaches it. |
| Looks / color grade | Gated, allowlisted | Only `none` and `golden_hour` allowed, and `golden_hour` additionally requires an exact-canvas, unrotated source (`phone_montage_plan.py:119-121, 160-164`; `phone_guided_plan.py:187, 194-198`). |
| SFX | Blocked | `licensed_sfx_intent`/`editor_sound_effects` in the guided lane-reject dict (`phone_guided_plan.py:51-60`); never referenced at all in `phone_montage_plan.py` (absent from the montage-family decision schema, not merely rejected). |
| Audio ducking | Blocked | `duck_original_during_music` explicitly rejected (`phone_montage_plan.py:99-102`, capability `audioDucking`). |
| Face-aware text placement | **Cloud-only** | `_apply_guided_text_face_placement` (`app/pipeline/guided_story.py:4272`) is called only from the cloud burn pipeline (`guided_story.py:4568-4571`, gated by `GUIDED_TEXT_FACE_PLACEMENT_ENABLED`), operating on a real decoded video file. Zero references to it, `sample_face_regions`, or `choose_guided_text_y_frac` exist in either phone compiler — confirmed by grep. Phone-compiled text uses its authored `y_frac` as-is with no face avoidance. |
| Media overlays / visual blocks / motion scenes | Blocked | All three are in the guided lane-reject dict (`editor_media_overlays`→`mediaCards`, `editor_visual_blocks`→`visualBlocks`, `editor_motion_scenes`→`motionScenes`; `phone_guided_plan.py:51-60`) and additionally re-blocked defense-in-depth in `phone_rollout.py:168-183`. Never referenced in `phone_montage_plan.py` at all. |

### Music bed cannot be opened on the device — confirmed on the simulator

`DeviceMontageRenderE2ETests` (new in this PR; fixtures from `scripts/ios/phone-montage-render-e2e.py`) renders real `compile_phone_montage_plan` recipes through the production resolver and exporter on an iOS 26.5 simulator. Plain cuts with original audio and a text intro render correctly (1080x1920 H.264 + AAC), and so does a crossfade. The **music-bed case fails**: `AVFoundationErrorDomain -11828 "Cannot Open… This media format is not supported"`. `AuthorizedDeviceSourceResolver.resolve` (`src/apps/ios/Kria/Core/DeviceRendering.swift:205-250`) hands library assets to the compositor straight from `RenderLibraryCache`'s extensionless `<sha256>-<byteCount>` path; Visuals videos get a playable name from `VisualVideoFile.prepare` "because AVFoundation chooses its reader from the extension", library audio gets nothing. The test pins this with a strict `XCTExpectFailure`, so the fix has to remove it. Any downloaded audio asset (a voiceover included) hits the same wall.

### musicBed fade envelope — confirmed broken, not "possibly stale"

Traced end to end across both languages:

1. `compile_phone_montage_plan` emits the music bed as a `TimelineTrack(id="music", kind="audio", ...)` (`app/pipeline/phone_montage_plan.py:284-296`) and separately sets `audio = AudioMixRecipe(music_volume=1.0, original_volume=0.0)` (`phone_montage_plan.py:305`) — **`AudioMixRecipe.music_asset_id` is never passed**, so it stays `None` (field default, `app/kria/recipes.py:129`).
2. In `Composition.swift`, a track with `kind == .audio` is played through the generic per-clip path: `addAudio(asset:clip:gain:)` (`Composition.swift:88-92`, called with `gain: 1`), which applies `applyAudioGain` (`VisualBlocks.swift:153-158`) — a **hard step function** driven only by `AudioMuteWindow`s (instant on/off, no ramp).
3. The DEDICATED fade-envelope code — `if let musicID = recipe.audio.musicAssetID { ... fadeIn/fadeOut AVMutableAudioMixInputParameters ramps ... }` (`Composition.swift:288-303`) — only runs when `recipe.audio.musicAssetID` (Swift camelCase of `music_asset_id`) is set. Since step 1 never sets it, **this code path never executes for a phone montage's music bed.**

Net effect: on a phone-rendered montage/day_vlog/single_hero with a matched track, the music plays at a flat volume from frame one (no fade-in) and cuts instantly at the end (no fade-out) — the `fade_in`/`fade_out` fields on `AudioMixRecipe` exist and default to `0` regardless, so even setting `music_asset_id` today would fade by 0 seconds; both the wiring AND the value would need fixing. This is a real, verified bug, not a hypothesis — worth its own follow-up ticket independent of KRI-132.

---

## (c) Prod flag prerequisites

All four default off/empty (`app/config.py:16-18, 33`):

| Flag | Field | Default | Gates |
|---|---|---|---|
| `PHONE_RENDERING_ENABLED` | `phone_rendering_enabled: bool` | `False` | Global on/off switch, consumed by `Settings.phone_rendering_for()` (`app/config.py:41-45`) — the enrollment check every phone code path starts from. |
| `PHONE_RENDER_USER_IDS` | `phone_render_user_ids: list[UUID]` | `[]` | Cohort allowlist; empty list means **every** enrolled-and-enabled account qualifies (never use an empty cohort for a real pilot — `app/config.py:41-45`). |
| `PHONE_RENDER_VERIFIED_FEATURES` | `phone_render_verified_features: list[str]` | `[]` | Feature-capability bitset checked at `phone_rollout.py:200-201` (`recipe.required_capabilities` must be a subset) and used to gate `stillImages`/`visualVideos` (`phone_destination.py:48-52`, `edit_proposals.py:418-423`). |
| `PHONE_FONT_QUALIFICATION_STRICT` | `phone_font_qualification_strict: bool` | `False` | Selects strict (2 pinned font/effect instances only) vs default (any bundled font on any of 17 native-supported text effects) qualification in `phone_rollout.py:152-164`. |

---

## (d) Blockers to file

0. **Day-Vlog and Single-Hero are unreachable from the chat format picker** (`_available_formats()` never emits them) — product decision: offer them, or state that they are planner-chosen only.
0. **Phone-gate refusals are a dead end for the creator** — reason is logged, never shown; Retry cannot succeed. Fix rides with the voiceover PR.
0. **`needsAttention` Retry loops for `unsupported_recipe`**, **Add clip is disabled with no explanation**, **deferred variants are never mentioned**, and **there is no phone-render UI test** — iOS follow-ups.
1. **Library audio cannot be opened by the exporter** (extensionless cache path) — blocks every music bed and any downloaded voiceover; small fix next to `VisualVideoFile.prepare`.
1. **musicBed fade envelope is broken** (see above) — isolated fix, `phone_montage_plan.py:305` + `AudioMixRecipe` fade defaults; independently actionable today.
2. **Voiceover-on-guided-format bypasses the approval gate** (see "A live gate quirk") — either close the gate earlier (reject at dispatch instead of wasting a Job) or explicitly document it as intentional defense-in-depth; currently looks accidental.
3. **Slides** — no phone compiler, and not even reachable through the chat planner on any account. Needs its own drafting-flow-to-phone-recipe design; nothing here is shared with the guided/montage compilers.
4. **Speech captions** — no schema field exists anywhere in the on-device recipe contract (`app/kria/recipes.py`/`recipes_v2.py`). This blocks Subtitled entirely and is a prerequisite for any Talking-Head phone work too.
5. **SFX** — entirely unrepresented in the montage-family decision schema (not merely rejected, never referenced) and explicitly blocked in the guided lane; needs its own catalog/asset wiring before it can be considered.
6. **Audio ducking** — explicitly rejected, straightforward once the underlying mix envelope primitive (see the musicBed finding above) is trustworthy.
7. **Crop / reframe** — the guided compiler actively rejects any cropped moment rather than silently dropping the crop; the montage-family schema has no crop field to even carry one. Blocks any footage that was reframed off-canvas during cloud editing from ever qualifying for phone rendering.
8. **Face-aware text placement on phone** — cloud-only (`guided_story.py::_apply_guided_text_face_placement`); phone-compiled text can overlap a face today. No tracking/geometry primitive has been ported to either phone compiler.
9. **`hevcDecode`/`hdr` capabilities** — declared in the `MediaCapability` Literal (`app/kria/recipes.py:160-161`) but never derived by any recipe field in either compiler. Confirmed by grep — these are unused vocabulary, not a wired-but-broken feature like the music fade bug.
10. **`narrated`/`talking_head`/`subtitled`** — see the dedicated assessment, `docs/reviews/kri-132/talking-head-subtitled-assessment.md`.

---

## User journey on a phone-rendering account

The backend subsections trace what the FastAPI/Celery layer does and the copy it emits; the iOS table traces what the app shows. Both are from reading `origin/main`; rows pinned by an automated test say so.

### iOS side of the journey (traced in the Swift sources, `origin/main` @ `88eb0e5`)

| Step | What the creator sees on a phone-rendering account | Evidence | Verdict |
|---|---|---|---|
| Format picker | Only **Montage**. `FormatStage` is fed by `GET /capabilities` (`ChatWorkspaceView.swift:527`, `803-816`), whose phone filter intersects `_available_formats()` = {montage, narrated_planned, subtitled, slides} with `PHONE_RENDER_SUPPORTED_FORMATS`. | `creation_threads.py:607-615`, `2661-2668` | Works, but Day-Vlog / Single-Hero are unreachable from the picker |
| Add footage | `.phone`: a small analysis copy uploads, originals stay on the iPhone. | `CreationFlow.swift` `ProjectUploadDestination.resolve`; `DeviceRenderSessionTests.testFootageOnPhoneAccountsAlwaysRendersOnTheIPhone` | Works |
| Add Visuals (photos / videos) | `.phoneVisuals(kinds)` per verified feature; otherwise "Visuals aren't available yet for videos rendered on this iPhone…" | `CreationFlow.swift:208-235`; `testPhoneAccountsKeepVisualsOnTheIPhone` | Restricted with clear message |
| Add voiceover | "Voiceover isn't available yet for videos rendered on iPhone. Your project is saved." The recorder is not rendered at all. | `CreationFlow.swift:200, 232`; `CreationAttachments.swift`; `testVoiceoverNeverMovesAPhoneAccountToTheCloud` | Restricted with clear message (being lifted by the voiceover PR) |
| Mixed sources | "This project has sources from different rendering destinations. Keep the project and reconnect its original footage before continuing." No control is attached to the message. | `CreationFlow.swift:198` | Restricted, recovery step unclear |
| Generate, when the dispatch gate refuses | Chat: "I couldn't start that render. Your creative plan is still saved." (code `execution_failed`) with a **Retry generation** button that can never succeed; the manual Generate route says "Your clips couldn't be validated — re-upload them and try again". The real reason (`not_enrolled` / `unapproved_guided` / `unsupported_format`) is only logged. | `content_plan_build.py:1565-1572`; `creator_agent.py:3688, 3762-3782`; `plan_items.py:2863-2866`; `CreationConfirmationStage.swift` | **Dead end** — being fixed with the voiceover PR |
| On-device render | preparing → rendering → localReady → syncing → synced. When `CapabilityNegotiator.decide` returns `.cloud` (missing capability, thermal, storage, renderer version, ducking) the session goes to `needsAttention`; it never uploads for a cloud render. | `Capabilities.swift:16-31`; `DeviceRenderSessions.swift:113-121`; `DeviceRenderPanel.swift:208-249`; `testDisabledGateNeverStartsExport` | Works — **no silent cloud fallback** |
| Retry on `needsAttention` | "Try again" is offered for every reason, including `unsupported_recipe` ("This edit isn't supported for iPhone rendering yet."), where the negotiator deterministically refuses the same recipe again. | `DeviceRenderPanel.swift:212` | Loop with no way forward for structural reasons |
| Backgrounding / app kill | `DeviceRenderCoordinator.recover()` restarts an interrupted encode and re-publishes an intact MP4 on the next `reconcile()`. No app-lifecycle test. | `DeviceRenderCoordinator.swift`; `DeviceRenderCoordinatorTests` | Works at unit level; lifecycle unverified |
| Preview / save / share | Play, Save to Photos and Share work from the local file at `localReady`; `synced` adds "Available in your Gallery and on your other devices." | `DeviceRenderPanel.swift` | Works |
| Edit + re-render | Text, trims, reorder, split, remove, transitions re-render on the phone (`rendersOnDevice` persists per variant). Crop / speed / looks and Visuals import are disabled **with** an explanation; **Add clip** is disabled with none. `capabilityReason(_:)` is plumbed but never displayed. | `NativeEditorSession.swift:663, 2310, 3611`; `NativeFootagePanel.swift`; `NativeEditorMediaViews.swift:1044, 1126` | Works; one unexplained disabled control |
| Variants | The phone compiles only the top-ranked variant; the rest are recorded in `assembly_plan["phone_deferred_variants"]`. No iOS source references it, so the creator is never told. | `generative_build.py` `_run_phone_montage_job`; runbook | Silent narrowing (montage-compiler path only) |

**Test coverage of the journey:** the upload-destination matrix, negotiator decisions, coordinator lifecycle, and editor fences are unit-tested. **No `KriaUITests` file touches phone rendering at all**, so none of the copy above is exercised through real views, and nothing tests app-lifecycle recovery or the phone-gate reason reaching the user.


### Can the planner choose a non-phone format from chat even though `/capabilities` hides it from the picker?

**Yes — the picker filter and the planner's own compile-time gate are two separate, independently-enforced things.**

`GET /capabilities` (`app/routes/creation_threads.py:2658-2711`) only trims the format LIST it returns to a native client on an enrolled phone account:

```python
formats = _available_formats()
if phone_enabled and native_client:
    formats = {key: value for key, value in formats.items() if value in PHONE_RENDER_SUPPORTED_FORMATS}
```
(`creation_threads.py:2661-2668`, comment: *"the app on a pilot account renders every project on the iPhone, and only these formats can; offering the others would end in a refusal after the creator has already uploaded footage. The web keeps them all."*)

This only shapes what the picker UI OFFERS. It does not touch the Main Creator Agent's chat strategy compiler at all — a creator can still type a free-text request like "make this a talking-head video" and the agent can still propose `edit_format: "talking_head"` in its `CreativeStrategy`. That strategy is compiled by `normalize_creator_strategy_media` → `effective_render_program` (`app/agents/_schemas/creator_policy.py:75-148`), which is called from the actual chat-turn handling in `app/routes/creator_agent.py` (call sites at lines 2274, 2391, 2404, 2437, 2554) — independent of, and downstream of, whatever `/capabilities` advertised.

**What the user sees when this happens:** the request fails CLOSED, never silently, and never as an unhandled crash — with different copy depending on why:

- **talking_head / subtitled / narrated / narrated_planned / narrated_ready** on a phone account: `effective_render_program` reaches its phone-specific guided-format check and raises `PhoneFormatUnavailableError("phone sources require a guided edit format")` (`creator_policy.py:113-114`). The route's handler (`_record_media_unavailable`, `creator_agent.py:1505-1524`) maps this to chat error code **`phone_format_unavailable`** and shows:
  > "Only Montage videos can render on your iPhone right now, not talking or narrated ones. Choose Montage to render on this iPhone. No fallback edit was rendered." (`creator_agent.py:1519-1524`)
- **A voiceover** on any format, phone account, is a *different* branch of the same function (`creator_policy.py:109-112`, `voiceover=True`), mapped to chat error code **`phone_voiceover_unavailable`** with the message defined at `creator_agent.py:1476-1479`:
  > "A voiceover can't render on your iPhone yet, and this project's videos render on this iPhone. Ask for this edit without a voiceover. No fallback edit was rendered."
- **slides** takes a different path entirely: `_format_availability` (`app/services/creator_capabilities.py:101-148`) has no branch for `"slides"` on ANY account, so it's not a phone-specific rejection at all — `effective_render_program` raises a plain `ValueError` for the missing/unavailable format capability (`creator_policy.py:85-88`), which `compile_strategy_to_plan` reclassifies into `CreatorCapabilityError` (code `edit_format_unavailable`, `creator_capabilities.py:457-463`), handled at `creator_agent.py:2480-2496` with a generic "{format} is not available for this Creator rollout" message. Both are still typed, both still fail the turn instead of rendering the wrong thing — slides just isn't gated by phone status specifically.

Both code paths are pinned by `test_planner_chosen_unsupported_format_fails_closed_on_phone` and `test_planner_chosen_slides_fails_closed_but_not_phone_specific` in `src/apps/api/tests/tasks/test_phone_format_matrix.py`.

### Step-by-step table

| Journey step | Guided formats (montage/day_vlog/single_hero) | Talking-Head / Subtitled / Narrated* | Slides |
|---|---|---|---|
| **Planner picks format from chat** | Works — offered by `/capabilities` on phone, compiles cleanly (`creator_policy.py:75-148`). | **Restricted with clear message.** Compiles, but fails closed with `phone_format_unavailable` copy above if the account is a phone account. Works fine on a non-phone account. | **Restricted with generic message.** `edit_format_unavailable`, not phone-specific — blocked on every account. |
| **Add footage** | Works — `phone_renderable_media` always allows clip-lane media (`edit_proposals.py:427`). | Works (this step doesn't depend on format). | N/A (slides never reaches this compiler). |
| **Add photos (Visuals)** | Works, gated by `PHONE_RENDER_VERIFIED_FEATURES` containing `"stillImages"`. | N/A — item never compiles far enough to reach media binding on a phone account. | N/A |
| **Add Visuals videos** | Works, gated by `"visualVideos"` + codec/duration checks (`edit_proposals.py:433-437`). | N/A | N/A |
| **Add voiceover** | **Restricted with clear message** at the planner (`phone_voiceover_unavailable` copy above) — OR, if it slips through (see "A live gate quirk"), dispatches then **silently wastes a Job** (worker rejects it, no user-facing signal beyond the eventual item failure state). | N/A (item never compiles). | N/A |
| **Add music** | Works, but see the confirmed fade-envelope bug above — **silent quality defect**, not a failure. | N/A | N/A |
| **Plan + approve (guided-story proposal)** | Works — this IS the guided-story approval flow; `validate_approved_proposal_media_sync` (`content_plan_build.py:1206-1213`) is the gate. | N/A — these formats are never guided-applicable, so no proposal step exists for them at all (`guided_edit_applicable()` is `False` unconditionally, `edit_format.py`). | N/A |
| **Dispatch** | Works once approved — `unapproved_guided` phone_gate rejects an unapproved attempt (`content_plan_build.py:1470-1471`), but this is a SERVER-INTERNAL dispatch (the activation loop, `content_plan_build.py:2408-2418`) that silently skips the item on any non-`dispatched` outcome — **no user-facing message from this path**. The separate manual "Generate" HTTP route (`app/routes/plan_items.py:4281` → `_respond_to_dispatch_result`) DOES surface a message for `invalid_clips`, but it's a **generic, inaccurate one**: "Your clips couldn't be validated — re-upload them and try again" (`plan_items.py:2861-2865`) — this is shown even when the real reason is `unapproved_guided` or `unsupported_format`, neither of which is a clip-validation problem. This is a real UX gap worth its own ticket. | Rejected with `unsupported_format` — same generic/misleading message via the manual route, or silent skip via activation. | Same. |
| **On-device render** (negotiator `.cloud` decisions, backgrounding, retry) | Backend evidence only: `app/services/device_render.py` defines the retry contract (`retry_device_render`, line 131) and user-facing failure copy for `export_failed`/`thermal`/`unknown` (lines 22-27, e.g. *"Your device paused rendering to cool down. Open the project to retry."*). Negotiator `.cloud`-fallback decisions and backgrounding behavior are iOS-side — not independently verified here. | N/A | N/A |
| **Preview / save / share** | Backend evidence: job status polling (`GET /generative-jobs/{id}/status`, per CLAUDE.md) re-signs ready variant URLs on read (`_variants_for_response`). Not independently traced further for this doc. | N/A | N/A |
| **Edit + re-render / variants** | Backend evidence: swap-song/retext async per-variant re-renders exist (`app/routes/generative_jobs.py`, per CLAUDE.md); not independently re-verified against the phone path for this doc. | N/A | N/A |

### How verified

- **Rendered on the iOS simulator (2026-09-21, iPhone Air, iOS 26.5):** montage-compiler plain cuts + text intro, crossfade, and the music-bed failure — `DeviceMontageRenderE2ETests` (opt-in via `KRIA_E2E_DIR`, not part of CI's unit lane).
- **Pinned by automated test:** dispatch-outcome-vs-table for all 27 (format × scenario) combinations, the worker voiceover/non-guided-format guards, `PHONE_RENDER_SUPPORTED_FORMATS` vs the table, and the planner fail-closed behavior — all in `src/apps/api/tests/tasks/test_phone_format_matrix.py` (`test_dispatch_outcome_matches_table`, `test_worker_rejects_voiceover_even_when_dispatch_let_it_through`, `test_worker_rejects_non_guided_format_directly`, `test_phone_render_supported_formats_matches_table`, `test_planner_chosen_unsupported_format_fails_closed_on_phone`, `test_planner_chosen_slides_fails_closed_but_not_phone_specific`).
- **Static code reading only (not pinned by a new test in this PR):** the feature table in section (b), the musicBed fade-envelope trace (spans Python + Swift; no cross-language test exists), the activation-loop silent-skip behavior, the manual-route generic-message behavior, and everything in the "On-device render" / "Preview save share" / "Edit + re-render" journey rows.
