# KRI-132: what phone rendering for Talking-Head / Subtitled would require

## TL;DR

Both formats are cloud-only today with no phone compiler. Unlike montage/day_vlog/single_hero, which already had their pure decision/media split done for them by KRI-114 (`GenerativeVariantDecision`), talking_head and subtitled have never been split — their cloud assemblers pick shots AND touch real media (FFmpeg subprocess calls, Whisper transcription) in the same function. Getting either of them onto the phone is not "write a new compiler like the existing two" — it's "do the KRI-114-style decision/media split first, for the first time, AND add a captions schema that doesn't exist yet." The iOS renderer, encouragingly, already has a reusable primitive (`overlay` track + `VisualMediaPlacement`) that's structurally close to what talking_head's audio-spine + B-roll-cutaway shape needs — that part is not greenfield.

All line numbers verified against `origin/main` at commit `88eb0e5` (2026-09-21).

> **Update — talking-to-camera + narrated PR.** This assessment's §4 ("Captions
> schema: confirmed absent") is now stale: `app.pipeline.phone_captions
> .compile_caption_layers` compiles cloud caption cues into native
> `PortableTextLayer`s (no new recipe-schema field was needed after all — it
> reuses the existing `text_layers` lane the same way any other text overlay
> does), and `subtitled` now HAS a phone compiler
> (`app.pipeline.phone_subtitled_plan.compile_phone_subtitled_plan`), landing
> the §6(a)/(c) work this doc scoped as greenfield for `subtitled`, without
> needing §3's `TimelineClip` crop/reframe work at all (the compiler rejects a
> non-portrait source clip instead of cropping it — see that module's
> docstring). `narrated`/`narrated_planned`/`narrated_ready` WITH a recorded
> voiceover also shipped (`app.pipeline.phone_narrated_plan
> .compile_phone_narrated_plan`) via the SAME captions mechanism, reusing the
> existing single-track-per-step timeline rather than talking_head's
> audio-spine + B-roll-cutaway shape. **`talking_head` itself is unaffected by
> this PR** — everything in §1 (no pure decision object splitting
> `select_spine`/`schedule_broll` from real FFmpeg/Whisper work) and §3 (no
> phone-side crop/reframe compiler) remains exactly as described below; a
> narrated item with NO voiceover only reaches the phone through a
> single-clip self-narration exception that can never resolve to
> `talking_head` (2+ clips) — that path fails closed instead.

## 1. talking_head: no pure decision object

`assemble_talking_head()` (`app/pipeline/talking_head_assembler.py:477`) is the cloud entry point. It calls two genuinely pure helpers — `select_spine()` (line 139, ranks candidate clips to pick the spine) and `schedule_broll()` (line 344, computes cutaway windows) — but interleaves them with real media I/O in the same function body:

- probes durations (~548)
- runs an optional silence-cut FFmpeg/Whisper pipeline (~554-689)
- calls `reframe_and_export()` on the spine — a real FFmpeg subprocess (~591-641)
- reframes every B-roll clip individually, one FFmpeg call each (~690-716)
- builds and runs a composite FFmpeg command via `subprocess.run(cmd, ...)` (~730-735)

There is no intermediate object emitted between "decided what to do" and "did it" — contrast with the montage family, where `GenerativeVariantDecision` (`app/pipeline/generative_decision.py:135`) is explicitly the pure boundary: its module docstring (lines 1-11) describes exactly this split having already been done for montage — `_decide_generative_variant` (pure) → `_process_generative_variant` (assembly through upload). Nothing equivalent exists for talking_head. The Celery-side caller, `_render_talking_head_variant` (`app/tasks/generative_build.py`, ~17226), is similarly monolithic — it doesn't call out to a separate decision function first.

## 2. subtitled: same interleaving

`_render_subtitled_variant()` (`app/tasks/generative_build.py`, ~20032) imports `reframe_and_export`, `transcribe_whisper[_cached]`, `burn_captions_on_video`, and `generate_ass_from_cues` inline and runs them sequentially — reframe, then transcribe, then caption-burn — inside one function (its own docstring, ~20052-20073, describes this as one pipeline). No pure decision object precedes it either.

## 3. `TimelineClip` crop/reframe: schema stub exists, nothing uses it

`TimelineClip` (`app/kria/recipes.py:80`) has a `transform: MediaTransform` field (line 87, default-constructed) — the on-device analog of pan/zoom/position, not a literal crop rectangle. Neither `phone_montage_plan.py` nor `phone_guided_plan.py` ever populates it (confirmed by grep for `transform=`/`MediaTransform` in both — zero hits). Worse, `phone_guided_plan.py` actively **rejects** any plan carrying a crop: `moment.source_crop is not None` raises `UnsupportedPhonePlan` at both line 176 (stills) and line 189 (video moments), with the comment (181-182) *"The recipe has no crop or retime for story footage; dropping either silently would render something the creator didn't approve."*

Cloud-side, `reframe.py::reframe_and_export` does real face-tracked crop/scale (`output_fit="crop"` is its default), and `talking_head_assembler.py` calls it on every spine and B-roll clip. **This has no phone equivalent today.** The schema field exists as a stub; the compiler logic to populate and honor it does not — this is genuinely greenfield work, not "wire up an existing thing."

## 4. Captions schema: confirmed absent

Grepped case-insensitively for "caption" across all of `app/kria/`. Two hits, neither is a real schema:

- `app/kria/recipes.py:171` — `"captions"` is one entry in the `MediaCapability` Literal, immediately preceded by a comment (~165-170) explaining this Literal exists to NAME lanes the schema has no fields for yet, and that naming a capability does not enable it.
- `app/kria/recipes_v2.py:105` — `"caption-pop"` is a generic stylized-text **animation effect** name (one of several `TextTreatment.effect` values), unrelated to timed subtitle cues.

There is no timed-cue data structure (start/end/text/speaker) anywhere in the on-device recipe contract. This is the actual hard blocker for Subtitled specifically, and a prerequisite for any Talking-Head phone work that wants captions too (talking_head today doesn't necessarily burn captions, but a reasonable v1 might want them, and Subtitled can't exist on-device without this regardless).

## 5. `Composition.swift`: a reusable seam already exists

`Composition.swift` (`src/apps/ios/Packages/KriaMediaEngine/Sources/KriaMediaEngine/Composition.swift`) builds an `AVMutableComposition` from `recipe.tracks`, iterating multiple `TimelineTrack`s of kind `video`/`overlay`/`audio` (the per-track loop starts ~82). Critically, a clip's own audio is only added when `clip.visualPlacement == nil` (~214-215 guard) — i.e. a clip that carries a `VisualMediaPlacement` (the `overlay`-track mechanism used today for fullscreen cutaways, per CLAUDE.md's `FULLSCREEN_CUTAWAYS_ENABLED`) renders visually but contributes **no** audio of its own.

`VisualMediaPlacement` (`app/kria/recipes.py` — the Python-side schema `portable_visual.py` mirrors it, `window_start`/`window_end` fields, `width_fraction` up to `1.0` i.e. full-canvas capable within a bounded window) already supports exactly the shape talking_head needs: **one continuous audio-spine track playing underneath, with visual-only B-roll clips swapped in and out on top for bounded windows, muted.** This is not a new Swift capability to build — it's the existing fullscreen-cutaway mechanism, already shipping.

The gap is not the renderer, it's the **phone compiler**: nothing produces a talking_head-shaped recipe today. `editor_media_overlays`/media-card-style lanes are currently in `phone_guided_plan.py`'s reject list (`_UNSUPPORTED_PHONE_LANE_CAPABILITY`, line 51-60), so no compiler emits this pattern yet even though the primitive underneath it works.

## 6. What each piece actually requires

| Component | Status | Evidence |
|---|---|---|
| (a) Pure decision object (audio-spine + B-roll swaps, talking_head/subtitled analog of `GenerativeVariantDecision`) | **Greenfield.** `select_spine`/`schedule_broll` are already pure and could seed one, but nothing packages them into a phone-compilable object today. | §1 |
| (b) `TimelineClip` crop/reframe support | **Partial seam, mostly greenfield.** Schema field (`transform`) exists but unused; the guided compiler actively rejects any crop rather than silently dropping it. Needs a phone-side crop compiler AND semantics validation. | §3 |
| (c) Captions schema addition | **Greenfield.** No cue/timing structure exists anywhere in the recipe contract; only an inert capability-name placeholder. | §4 |
| (d) iOS `Composition.swift` changes | **Reusable seam already exists — likely NOT greenfield.** The `overlay` track + `VisualMediaPlacement` primitive (audio-silent, full-width-capable, windowed) already ships for fullscreen cutaways. Wiring talking_head's spine+B-roll into `video` track (spine) + `overlay` track (B-roll, `VisualMediaPlacement`) looks plausible with new Python compiler logic and little to no new Swift work. | §5 |

## Recommended slice order and rough size

1. **Captions schema (c) first, independent of talking_head/subtitled entirely.** It's a pure schema addition (`recipes.py`/`recipes_v2.py` + the Swift-side decode), has zero dependency on the decision-object work, and unblocks Subtitled's on-device story on its own. Rough size: small-to-medium (schema + one new Swift render path for burning/showing timed text — no FFmpeg equivalent needed since it's compositional, not baked-in).
2. **Talking-head pure decision object (a), reusing `select_spine`/`schedule_broll` as-is.** This is the same shape of work KRI-114 already did once for montage — follow that precedent directly rather than inventing a new pattern. Medium size: mostly extracting existing pure logic into a `TalkingHeadVariantDecision`-equivalent Pydantic model, with the actual FFmpeg assembly staying in the cloud path unchanged (no regression risk to the existing cloud renderer).
3. **Phone compiler for talking_head (`phone_talking_head_plan.py` or similar), targeting the `video` spine + `overlay` B-roll pattern from §5.** Depends on (1) for any captions want and (2) for the decision object to compile from. Medium-to-large: this is genuinely new compiler code, though it can follow `phone_guided_plan.py`'s and `phone_montage_plan.py`'s existing structure (reject-map pattern, `UnsupportedPhonePlan` fail-closed defaults) closely.
4. **Crop/reframe support (b) — defer.** Nothing above strictly requires it (talking_head's spine/B-roll windows don't inherently need cropping if source footage is already portrait), and it's the most architecturally unclear piece (the guided compiler's explicit rejection with a "didn't approve this" comment suggests this was a deliberate product decision, not just unfinished work — worth a product conversation before investing engineering time here).
5. **Subtitled phone compiler — after (1) and after talking_head's decision-object pattern is proven in production**, since subtitled's assembler has the same "everything in one function" shape and can reuse whatever `TalkingHeadVariantDecision`-equivalent pattern gets established, rather than inventing its own.

Total rough sizing for a minimally-shippable talking_head phone path (steps 1-3, skipping crop and deferring subtitled): **medium-large**, comparable to or somewhat larger than the original montage-family phone work (KRI-114), since that work had the benefit of the decision/media split already being done — this doesn't.
