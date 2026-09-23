# KRI-118: Video-type requirements audit — target state after the L0-L6 train

Linear: [KRI-118](https://linear.app/kria/issue/KRI-118/create-a-list-of-requirements-for-each-edit-type-and-remove-unneeded) — "Create a list of requirements for each edit type and remove unneeded ones." Founder framing (verbatim from the issue): *"We keep failing videos and debugging one by one but still cannot generate a bunch of videos I want... Find unnecessary ones. Can you review which ones can be removed?... Include any set of rules, anything that can cause a video to fail. Focus videos created in the app."*

This is **lane L0** of an 8-lane PR train (L0-L6 implement, this doc scopes). It supersedes the stale, untracked `docs/reviews/edit-type-constraints-audit-2026-09-19.md` referenced in the original task brief — that file is not present in this worktree (it was never committed), so this doc was built fresh from the current `origin/main` tree (verified 2026-09-23) plus the two committed KRI-132 phone-rendering audits (`docs/reviews/kri-132/phone-format-matrix.md`, `.../talking-head-subtitled-assessment.md`), which this doc cross-checked rather than assumed current.

**Classification key** (the KRI-129 standing rule — see `memory: kri-129-creators-prompt-wins.md` and `app/agents/edit_proposal.py` comments citing "KRI-129" throughout):
- **(a) creator's own contract** — an explicit thing the creator asked for or approved; kept as a guard, never silently overridden.
- **(b) genuine render limit** — a real technical/renderer boundary; the correct behavior is to repair deterministically (never hard-reject a whole plan) and tell the user what happened (a persisted, user-visible reason — e.g. `assembly_plan["archetype_fallback"]`), not to fail silently.
- **(c) taste/heuristic** — an AI-quality preference dressed up as a hard requirement; should be removed or downgraded to a soft ranking signal.

Every row below is grounded in a grep/read of the file cited; anywhere the current code doesn't say enough to classify confidently, that is stated rather than guessed. Because lanes L1-L6 of this same train are actively changing many of the rows below, each row carries a `<!-- STATUS: pending L<N> -->` marker (or `<!-- STATUS: current -->` where nothing in this train touches it) so a later lane can flip it mechanically once merged — this doc was written as the **intended target state**, not a guarantee of what has already shipped.

---

## 1. Type catalogue

The vocabulary lives in two orthogonal enums (`app/agents/_schemas/edit_format.py`, `app/agents/_schemas/creator_agent.py`):
- **`EditFormat`** (the archetype/assembler) — `montage | talking_head | day_vlog | single_hero | subtitled | narrated | narrated_planned | narrated_ready | slides` (`edit_format.py:27-37`).
- **`CreativeDirection`** (a rendering-shape modifier, orthogonal to format) — `guided_story | fast_montage | text_explainer | native` (`creator_agent.py:247`). `direction` and `edit_format` are independent fields on `CreativeStrategy` (`creator_agent.py:266-268`); most guided-format edits render as `direction="fast_montage"` (the default) or `"guided_story"`, and `text_explainer` is a heavier-caption guided-story variant the Main Creator Agent's own prompt picks for "tips, review, guide, facts, recommendations, or detailed explanations" (`prompts/edit_guide.txt:54`).

| User-facing name | `edit_format` / direction | Render program | Cloud vs phone | Governing flags | Clip/media limits | Status |
|---|---|---|---|---|---|---|
| Montage (default) | `montage` | `guided` (no voiceover) / `native` (voiceover) — `render_program_for_intent`, `edit_format.py:148-176` | Both. Cloud: `generative_build.py`. Phone: `app/pipeline/phone_montage_plan.py` (no voiceover) or `app/pipeline/phone_guided_plan.py` (guided, no voiceover) — see `phone-format-matrix.md` for which compiler actually runs for which combination. | None to be montage itself (it's the safe default `coerce_edit_format` falls back to, `edit_format.py:133-145`); voiceover needs `phone_narration_rendering_enabled` + verified `narrationAudio` on phone. | Up to `_MAX_CLIPS_PER_ITEM` = 50 (`app/routes/plan_items.py:326`). | <!-- STATUS: current --> |
| Fast montage (`direction=fast_montage`) | `edit_format=montage`, `direction="fast_montage"` | Same as montage | Same as montage | Same as montage | Same as montage | <!-- STATUS: current, minus the L2 taste-rule removal in §3 --> |
| Guided story (`direction=guided_story`) | any of `montage`/`day_vlog`/`single_hero`, `direction="guided_story"` | `guided` | Cloud: `app/pipeline/guided_story.py`. Phone: `app/pipeline/phone_guided_plan.py`. | `guided_edit_conversation_enabled`, `guided_edit_direction_confirmation_enabled`, `guided_auto_design_enabled` (`creator_capabilities.py:75-77`) | Feasibility gated by `feasible_guided_duration_s` / `guided_feasibility_threshold_s` (see §2, §3) | <!-- STATUS: pending L1 (feasibility floor) --> |
| Guided story, voiceover-timed | `montage`/`day_vlog`/`single_hero` + a recorded voiceover, `execution_contract="guided_voiceover_v1"` | `guided`, narration-spined | Cloud: `_mix_pinned_narration`. Phone: `compile_phone_guided_plan`'s narration branch, gated by `phone_guided_narration_supported()` (`app/services/phone_rollout.py`). | `phone_guided_narration_rendering_enabled` AND `phone_narration_rendering_enabled` + verified `narrationAudio` (phone only; cloud always available) | Same as guided story | <!-- STATUS: current (phone) --> |
| Day-vlog | `day_vlog` | `guided` (no voiceover only — `_format_availability`, `creator_capabilities.py:110-127`, forces `native_render_required` with a voiceover) | Cloud + phone (`phone_montage_plan.py`/`phone_guided_plan.py` shared compiler family) | `edit_format_day_vlog_enabled`, `NARRATIVE_CLIP_ORDER_ENABLED` (both required — `creator_capabilities.py:111-121`) | `_DAY_VLOG_MIN_SHOTS = 2` (`app/tasks/generative_build.py:234`); ordered by filming-guide shot order via `_narrative_clip_order`, not true capture time (see §5) | <!-- STATUS: pending L4/L5 (chat-picked "shape" under Montage) --> |
| Single-hero | `single_hero` | `guided` (no voiceover only, same gate shape as day_vlog — `creator_capabilities.py:128-139`) | Cloud + phone | `edit_format_single_hero_enabled` | `_SINGLE_HERO_MIN_CLIPS = 2` (1 hero + >=1 cutaway); hero must dominate >= `_SINGLE_HERO_MIN_HERO_RATIO = 0.60` of runtime, hero source clip must be >= `_SINGLE_HERO_MIN_HERO_DURATION_S = 3.0`s (`generative_build.py:239-242`, `_build_single_hero_recipe` at `generative_build.py:315-396`) | <!-- STATUS: pending L4/L5 (ratio porting decision, see §3) --> |
| Subtitled / "Talking to camera" | `subtitled` | `native` (audio-led, `AUDIO_LED_EDIT_FORMATS`, `edit_format.py:77-79`) | Cloud: inline in `generative_build.py`'s subtitled path. Phone: `app/pipeline/phone_subtitled_plan.py::compile_phone_subtitled_plan`. | `subtitled_archetype_enabled` (+ `phone_subtitled_rendering_enabled` for phone) | **Exactly 1 clip** — `_format_clip_limit`, `app/routes/creation_threads.py:622-625`; re-enforced at the phone dispatch gate (`subtitled_clip_count_unsupported`, `content_plan_build.py:1724-1730`) | <!-- STATUS: current --> |
| Talking-head | `talking_head` | `native` | **Cloud-only.** `app/pipeline/talking_head_assembler.py`. No phone compiler exists (`docs/reviews/kri-132/talking-head-subtitled-assessment.md` §1-6). | `edit_format_talking_head_enabled` | 2+ clips implied by the self-narration router (`_resolve_archetype`, `generative_build.py:14865-` picks `talking_head` only when 2+ clips carry speech) | <!-- STATUS: pending separate follow-up ticket (§4) --> |
| Narrated (self-narration, planned) | `narrated_planned` | `native` | Cloud always; phone requires a recorded voiceover (self-narration on phone only resolves through the 1-clip `subtitled` exception, §1(a) of the talking-head assessment) | `narrated_archetype_enabled`; self-narration additionally needs `NARRATED_SELF_NARRATION_ENABLED` | 1 clip -> routed to `subtitled`; 2+ clips -> `talking_head` (no phone path); no speech -> montage fallback with a persisted, user-visible `archetype_fallback` reason (CLAUDE.md `NARRATED_SELF_NARRATION_ENABLED` entry) | <!-- STATUS: current --> |
| Narrated (ready / has a recorded voiceover) | `narrated` / `narrated_ready` | `native`, narration-spined | Cloud + phone (`app/pipeline/phone_narrated_plan.py::compile_phone_narrated_plan`) | `narrated_archetype_enabled` (+ `phone_narrated_rendering_enabled`, `phone_narration_rendering_enabled` for phone) | Voiceover required, or `NARRATED_SELF_NARRATION_ENABLED` for footage-only speech | <!-- STATUS: current --> |
| Slides (mixed photo/video carousel) | `slides` | Neither guided nor native in the usual sense — a dedicated slide-post drafting flow (`app/routes/plan_items.py`, `slide_post` field), not the Main Creator Agent chat strategy compiler | **Cloud-only; not reachable from the chat planner at all, on any account** — `_format_availability` (`creator_capabilities.py:101-154`) has no `"slides"` branch | `slide_posts_enabled` | Not part of the montage/guided clip-count model — mixed ordered media items | <!-- STATUS: hidden/server-only, see §4 --> |
| Text explainer (`direction=text_explainer`) | `edit_format` unchanged (typically `montage`), `direction="text_explainer"` | `guided` | Cloud + phone, same as guided story | Picked by the Main Creator Agent prompt itself (`prompts/edit_guide.txt:54`, `prompts/main_creator.txt:163,186`) for "tips, review, guide, facts, recommendations" content; slightly stricter `min_moment_s = 1.8` vs 1.4 for ordinary guided_story (`app/routes/creator_agent.py:1243`) | Same feasibility floor as guided_story, at the stricter 1.8s moment minimum | <!-- STATUS: hidden/server-only, see §4 --> |

**Uncertain / not independently re-verified in this pass:** whether `text_explainer` is reachable through the iOS app's own chat surface (it uses the same `app/routes/creator_agent.py` backend as web, but this doc did not trace the iOS chat client's own direction-selection UI to confirm it ever emits `text_explainer` explicitly vs. only the LLM choosing it server-side from free text).

---

## 2. Every constraint/rule that can fail or degrade a video

Grouped by layer, in intake -> planning -> dispatch -> device order. Each row: what it does, file:line, classification (a/b/c), and status.

### Intake boundaries

| Rule | File:line | Class | Notes |
|---|---|---|---|
| Max 50 clips per plan item | `app/routes/plan_items.py:326` (`_MAX_CLIPS_PER_ITEM`), mirrored at `app/routes/creation_threads.py:625`, `app/tasks/creator_clip_metadata.py:25` | (b) | Genuine cost/latency ceiling (worker soft-limit is derived from it, `creator_clip_metadata.py:30`), enforced consistently at upload time. |
| Subtitled capped at exactly 1 clip | `app/routes/creation_threads.py:622-625` (`_format_clip_limit`) | (a) | Product definition of the format (own-audio talk-to-camera), not a renderer accident — re-enforced at the phone dispatch gate too. |
| Visual (Visuals-pool) video duration cap, 1800s | `app/services/phone_visuals.py:35` (`MAX_PHONE_VISUAL_VIDEO_S`) | (b) | Device/codec-composability limit, phone-only. |
| Max 12 selected media refs for the Main Creator Agent | `app/agents/_schemas/creator_policy.py:19` (`MAX_MAIN_CREATOR_SELECTED_MEDIA`) | (b) | Chat-turn-scoped selection cap, distinct from the 50-clip item ceiling. |
| `CreatorLimits` (max_media_refs=50, max_catalog_refs=50, max_commands=4, max_output_duration_s=120) | `app/agents/_schemas/creator_agent.py:53-56,183-195` | (b) | Structural request-size caps checked by both the planner and execution routes. |

<!-- STATUS: current -->

### Planner manifest rules (`resolve_creator_manifest` / `_format_availability`)

| Rule | File:line | Class | Notes |
|---|---|---|---|
| `day_vlog` requires `edit_format_day_vlog_enabled` AND `NARRATIVE_CLIP_ORDER_ENABLED`; unavailable with a voiceover | `app/services/creator_capabilities.py:110-127` | (a)/(b) mixed | The voiceover exclusion is a genuine architecture limit (guided renderer is audio-destructive); the two flags are rollout gates, not taste. |
| `single_hero` requires `edit_format_single_hero_enabled`; unavailable with a voiceover | `creator_capabilities.py:128-139` | same as above | |
| `talking_head` requires `edit_format_talking_head_enabled` | `creator_capabilities.py:142-143` | rollout gate | |
| `subtitled` requires `subtitled_archetype_enabled` | `creator_capabilities.py:144-145` | rollout gate | |
| `narrated`/`narrated_planned`/`narrated_ready` require `narrated_archetype_enabled` AND (a recorded voiceover OR `narrated_self_narration_enabled`) | `creator_capabilities.py:146-150` | (a) | Without either, generation is blocked rather than silently falling back — deliberate per CLAUDE.md's `NARRATED_EDIT_FORMATS` docstring. |
| `slides` has no availability branch at all — unavailable on every account, phone or not | `creator_capabilities.py:101-154` (absence, confirmed by grep) | not a per-account gate, a missing feature | See §4. |
| Phone accounts additionally reject `sound_effects`/`media_overlays`/`visual_blocks`/`motion_scenes`/`wide_looks` at the manifest level | `creator_capabilities.py:86-96` (`_PHONE_UNSUPPORTED_CAPABILITIES`) | (b) | Mirrors `phone_guided_plan.py`'s reject-map so the chat manifest never advertises a capability the phone compiler would later refuse. |

<!-- STATUS: current -->

### Guided-story feasibility / allocator rules (the biggest cluster; see §3 for the ones being removed)

| Rule | File:line | Class | Notes |
|---|---|---|---|
| `feasible_guided_duration_s`: a video contributes its own probed duration only if it clears `GUIDED_STORY_MIN_MOMENT_S` (1.4s); otherwise contributes **zero** (not even the image credit) | `app/tasks/edit_proposal_build.py:498-519`; threshold at `app/schemas/edit_proposal.py:661` | **(c) — being removed, see §3** | Pre-agent planning estimate; the render-time authority is `_allocate_beat_durations` below. |
| `guided_feasibility_threshold_s`: floors the whole-plan minimum footage at `max(MIN_GUIDED_DURATION_S=3, 1.4 * min(3, media_count))` | `edit_proposal_build.py:522-538` | (b), but its inputs are the (c) rule above | Once §3's floor changes, this threshold's effective value changes too — same function, different downstream numbers. |
| `_allocate_beat_durations`: raises `guided_story_duration_impossible` if a beat's declared duration can't fit every approved media's floor length (water-fill) | `app/pipeline/guided_story.py:1089-1122` | (b) | This is the actual, authoritative render-time check `feasible_guided_duration_s` only estimates ahead of. |
| A zero/negative-duration video in a beat raises `guided_story_duration_impossible` ("has no usable duration") | `guided_story.py:1132-1137` | (b) | Real ffprobe failure case, not a taste call. |
| A video with less than `_MIN_RENDERABLE_MOMENT_S` of usable capacity after overlap raises `guided_story_duration_impossible` ("no frames left to show") | `guided_story.py:1138-1143` | (b) | |
| `single_hero`: hero must dominate >= 60% of total runtime (`_SINGLE_HERO_MIN_HERO_RATIO`), hero source clip must be >= 3.0s, at least 1 usable supporting cutaway required | `app/tasks/generative_build.py:239-242,315-396` (`_build_single_hero_recipe`, `SingleHeroPolicyError`) | **(c)? — under review, see §3** | This is a deliberate creative-shape invariant for the dedicated `single_hero` format, not obviously a bug — flagged for the L4/L5 "shapes under Montage" decision rather than unilaterally removed here. |
| `day_vlog` requires >= 2 usable filming-guide clips (`_DAY_VLOG_MIN_SHOTS`) and at least one timeline slot per filming-guide shot | `generative_build.py:234,16818-16825` (`DayVlogPolicyError("insufficient_media", ...)`) | (a) | Structural minimum for a "day in shots" narrative; not obviously removable without changing what day_vlog means. |
| The minimum-beat-count, empty-thought, and distinct-topic rejections for guided_story are **already removed** (KRI-129) — the creator's requested grouping decides beat count/topics now, and an empty thought just renders no caption | `app/agents/edit_proposal.py:2598-2601` (comment documenting the removal) | (c), already removed | Cited as precedent/evidence that this style of cleanup has landed before under KRI-129; not new work for this train. |

<!-- STATUS: pending L1 (feasibility floor rows) -->

### Session/cost budgets

| Rule | File:line | Class | Notes |
|---|---|---|---|
| Per-session render-attempt budget (`render_attempts >= max_render_attempts` -> 409 "Creator render budget exhausted" / "This session has used its render attempts") | `app/routes/creator_agent.py:5013-5017`; `app/routes/creation_threads.py:4091-4097` | (b) | Real cost control; the creator is told plainly, not silently blocked. `max_render_attempts` default observed in call sites as `2` (`creator_agent.py:5013`, `getattr(session, "max_render_attempts", 2)`). |
| Idempotency-key reuse rejected with 409 across nearly every mutating creation-thread/creator-agent route | many call sites, e.g. `app/routes/creation_threads.py:2913,3198,3351`; `app/routes/creator_agent.py:3115,3217,3842` | (a) | Correctness guard against duplicate client retries, not a video-quality rule; listed for completeness since it is one more way a request can be refused. |

<!-- STATUS: current -->

### Chat gates / `/confirm` 409s

| Rule | File:line | Class | Notes |
|---|---|---|---|
| Confirming a plan with `audio_strategy="voiceover"` and no recorded voiceover yet -> 409 "Record a voiceover before confirming this plan" | `app/routes/creator_agent.py:3344-3350` | (a) | Correct — cannot confirm an unrecordable plan. |
| A retry/resume whose restored manifest hash no longer matches the session's approved hash -> 409 "Footage or capabilities changed; review the plan again" | `app/routes/creator_agent.py:3913-3916` | (a) | Optimistic-concurrency safety: never resumes against stale footage/capabilities. |
| A phone-account strategy naming `talking_head`/`subtitled`/`narrated*` fails closed with `phone_format_unavailable` at the policy layer, before a Job exists | `app/agents/_schemas/creator_policy.py:113-114` (`PhoneFormatUnavailableError`), surfaced by `app/routes/creator_agent.py:1519-1524` | (b) | Real device-render limit; user-facing copy names exactly what would work instead ("Choose Montage..."). |
| A phone-account strategy carrying a voiceover fails closed with `phone_voiceover_unavailable` at the same layer | `creator_policy.py:109-112`; copy at `creator_agent.py:1476-1479` | (b) | Same shape as above. |
| A phone-account strategy naming `slides` fails as a generic `edit_format_unavailable` (not phone-specific — slides is unavailable everywhere) | `creator_policy.py:85-88` -> `CreatorCapabilityError` (`creator_capabilities.py:457-463`) -> `creator_agent.py:2480-2496` | not a phone rule, a missing feature | See §4. |
| Confirming with an active render in progress -> 409 "Wait for the current render before confirming" | `app/routes/creator_agent.py:3872-3879` | (a) | Concurrency guard. |

<!-- STATUS: current -->

### Phone dispatch gates (`app/tasks/content_plan_build.py`)

The gate ladder lives in one place — `PHONE_GATE_MESSAGES` (`content_plan_build.py:685-749`) is the single source of truth for user-facing copy per `phone_gate` code, and the branch logic that sets each `phone_gate` value is at `content_plan_build.py:1637-1747`. Every branch fails closed **before** a Job is minted (documented intent: "before a Job is even minted"). All are (b) genuine phone-render limits, each with distinct, accurate copy:

| `phone_gate` value | Trigger | File:line |
|---|---|---|
| `not_enrolled` | Account is not phone-rendering enrolled at all | `content_plan_build.py:1640-1642` |
| `guided_voiceover_unavailable` | Guided-story voiceover requested but `phone_guided_narration_supported()` is false | `content_plan_build.py:1643-1662` |
| `unapproved_guided` | A guided-applicable format with no approved edit proposal | `content_plan_build.py:1663-1666` |
| `narrated_voiceover_unavailable` | Narrated family + recorded voiceover, but the phone-narrated rollout isn't fully satisfied | `content_plan_build.py:1671-1685` |
| `unsupported_format` | Self-narration landing on anything but the 1-clip subtitled exception, `subtitled` with a voiceover attached, or any other unsupported format | `content_plan_build.py:1700-1733` |
| `subtitled_clip_count_unsupported` | `subtitled` item with != 1 clip once phone-supported | `content_plan_build.py:1721-1730` |
| `voiceover_unavailable` | Plain montage-family voiceover without `phone_narration_rendering_enabled` + verified `narrationAudio` | `content_plan_build.py:1741-1746` |

A live gate quirk, still true as of this pass (confirmed by re-reading, not copied from the KRI-132 doc): a voiceover on montage/day_vlog/single_hero flips `guided_edit_applicable()` to `False` (`edit_format.py:148-176`, `has_voiceover` short-circuits to `"native"`), which routes dispatch through the **non-guided** branch instead of the guided one — this branch only checks `phone_render_supported_formats()`, which montage/day_vlog/single_hero are already in, so a voiceover item with **no approved guided proposal** can still dispatch and mint a Job before any worker-side voiceover check runs. Per `docs/reviews/kri-132/phone-format-matrix.md` ("A live gate quirk" section), this was scoped as a KRI-132 follow-up ("close the gate earlier... or document as intentional"), not yet resolved as of this doc.

<!-- STATUS: current for the gate ladder itself; the live-gate-quirk follow-up is unresolved and not part of this train's scope -->

### phone_rollout.py / phone compiler reject-maps

| Rule | File:line | Class |
|---|---|---|
| `_UNSUPPORTED_PHONE_LANE_CAPABILITY`: SFX, media overlays, visual blocks, motion scenes, edit-wide looks (non-`golden_hour`) rejected outright for the guided phone compiler | `app/pipeline/phone_guided_plan.py:51-60` (per `talking-head-subtitled-assessment.md` §5, `phone-format-matrix.md` (b) table) | (b) |
| Crop/reframe (`source_crop is not None`) explicitly rejected rather than silently dropped | `phone_guided_plan.py` (comment: "dropping either silently would render something the creator didn't approve") | (a) — a deliberate product decision to fail rather than silently degrade, per the code's own comment |
| Golden-hour look requires exact-canvas, unrotated source | `phone_montage_plan.py`, `phone_guided_plan.py` (per phone-format-matrix.md §b) | (b) |
| `phone_rollout.validate_phone_pilot_recipe` re-checks capability subset + font/effect qualification (defense-in-depth, second pass) | `app/services/phone_rollout.py:152-208` | (b) |

<!-- STATUS: current -->

### Device-side / iOS client boundaries

Traced and pinned by tests per `docs/reviews/kri-132/phone-format-matrix.md`'s "iOS side of the journey" table (not independently re-walked line-by-line in this pass — cited as current, cross-check that doc's own currency note: it says "verified against origin/main at 88eb0e5 (2026-09-21)", i.e. 2 days stale relative to this doc):

- `CapabilityNegotiator.decide` can return `.cloud` for missing capability / thermal / storage / renderer-version / ducking reasons — no silent cloud fallback occurs; the session goes to `needsAttention` instead (`Capabilities.swift:16-31`). (b)
- Crop/speed/looks/Visuals-import are disabled in the on-device editor with an explanation; **Add clip** is disabled with none (`NativeFootagePanel.swift`, per phone-format-matrix.md's journey table, row "Edit + re-render"). (b), with a UI gap.
- The phone compiles only the top-ranked variant; deferred variants are recorded in `assembly_plan["phone_deferred_variants"]` but no iOS surface ever shows this to the creator. (b), silent narrowing — flagged as a known gap, not fixed by this train.

<!-- STATUS: current, per KRI-132; not re-verified against Swift sources in this pass -->

---

## 3. Removed-rules changelog (taste rules this train removes)

| # | Rule | Where it lives today | Why it's taste, not a real limit | Status |
|---|---|---|---|---|
| 1 | **`fast_montage` must use both photos and videos** — `SchemaError("edit_proposal: story must use both photos and videos")` fires whenever both kinds are available in the source pool but the model's chosen cuts only use one kind | `app/agents/edit_proposal.py:2551-2575` (the `available_kinds`/`used_kinds` comparison; the raise is at line 2575) | This is a variety **preference**, not something the renderer requires — the same function already carries a comment two lines above (`edit_proposal.py:2541-2543`) noting "the generic distinct-source floor is a variety preference, not a render requirement (KRI-129): required coverage is still enforced below via `_required_media_ids`." The mixed-kind requirement is the one variety rule KRI-129 did NOT already remove. A creator with 9 videos and 1 barely-usable photo should not be forced to burn the photo in. | <!-- STATUS: pending L2 --> |
| 2 | **`feasible_guided_duration_s` zeroing sub-1.4s videos** — a video contributes nothing (not even the image credit) to the whole-story feasibility estimate unless its own probed duration clears `GUIDED_STORY_MIN_MOMENT_S = 1.4`s | `app/tasks/edit_proposal_build.py:498-519`; threshold defined at `app/schemas/edit_proposal.py:661` | 1.4s is an arbitrary "legible moment" floor for the pre-agent capacity *estimate* — the actual render-time authority is `_allocate_beat_durations` (`guided_story.py:1089-`), which has its own, more precise floor logic per-clip. Using the coarse pre-check to zero out short-but-real footage can make an otherwise-feasible story look infeasible and silently drop the creator's clip from consideration before the agent even sees it. | <!-- STATUS: pending L1 --> |
| 3 | **Legacy quoted-caption heuristic that blanks all other captions** — when the creator quotes ANY specific phrase as on-screen text, every OTHER AI-drafted caption not matching one of the quoted phrases is blanked (`beat.thought = ""`, `repairs.append("blanked_unrequested_thought:...")`) rather than kept as an AI-authored caption | `app/agents/edit_proposal.py:2602-2629` (the `creator_captions` block; the blank + repair-log lines are 2628-2629) | The code's own comment (`2618-2622`) frames this as intentional: "The creator said what the screen should say: that list is complete... whatever the prompt led it to write." But that's an absolutist reading of one quoted example as an exhaustive spec — a creator who quotes ONE line of dialogue almost never means "and blank every other caption in the video." This has been superseded functionally by `_resolved_caption_intents` (line 2610, "strictly more precise... takes over entirely") in the cases where a clip-intent exists; the heuristic below it is the stale fallback path being removed. | <!-- STATUS: pending L2 --> |
| 4 | **Hard-coded fallback title "A few moments"** — used whenever no real opening title/copy is available | `app/routes/creation_threads.py:118` (`_PLACEHOLDER_PROPOSAL_TITLE`); `app/services/proposal_planning.py:91`; `app/tasks/edit_proposal_build.py:2051` | A generic, creatively empty placeholder that ships to users as if it were an authored title. `creation_threads.py:2453`'s own comment says the specialist's internal placeholder should "never surface" — so at least one call site already treats this as a bug to guard against, not a feature. Removing the single hard-coded string (in favor of no title, or a per-context fallback) closes the remaining leak paths. | <!-- STATUS: pending L3 --> |
| 5 | **day_vlog / single_hero 60%-hero-style duration ratios not being ported when they become chat-picked "shapes" under Montage** | `_SINGLE_HERO_MIN_HERO_RATIO = 0.60` and the dominance math in `_build_single_hero_recipe` (`app/tasks/generative_build.py:239-242,315-396`); day_vlog has no equivalent ratio (narrative order only, `_DAY_VLOG_MIN_SHOTS = 2`) | This is the one entry in the changelog that is a **forward-looking non-port**, not a present-day removal: today `single_hero`/`day_vlog` are separate `edit_format` values with their own dedicated policy code (`SingleHeroPolicyError`/`DayVlogPolicyError`). This train's later lanes (L4/L5, per the task brief) plan to fold day_vlog and single_hero into chat-selectable "shapes" of the plain Montage format. The single_hero 60%-hero-dominance invariant is being evaluated for removal/non-porting in that fold rather than carried forward verbatim — it is a deliberate creative-shape rule today (arguably (a), a real format contract), but once single_hero stops being its own `edit_format` and becomes a Montage shape, keeping a hard 60% floor may reintroduce exactly the kind of "video fails for an opaque ratio reason" problem KRI-118 was filed about. **This row is intent, not a confirmed code change** — no L4/L5 lane has landed as of this doc. | <!-- STATUS: pending L4/L5 --> |

None of the five removals above have landed in `origin/main` as of this pass (2026-09-23) — this doc documents the target state for lanes L1-L4 to implement; L0's job is the audit only.

---

## 4. Hidden server-only formats

Three `EditFormat`/direction values exist, are exercised by tests and production code, but are **not reachable from the iOS app's chat/format picker**:

- **`narrated_ready`** — part of `NARRATED_EDIT_FORMATS` (`edit_format.py:65-67`) and fully wired through dispatch/phone gates, but `_available_formats()` (`app/routes/creation_threads.py:611-619`) only ever emits `"narrated"` mapped to `narrated_planned`; `narrated_ready` is an internal state transition (a filming guide becomes "ready"), not a creator-facing pick.
- **`talking_head`** (2+-clip self-narration or an explicit talking_head request) — has no phone compiler at all (`docs/reviews/kri-132/talking-head-subtitled-assessment.md`, entire doc) and is not in `_available_formats()`. **Talking-head-on-iPhone is being filed as a separate follow-up Linear issue** (not created by this lane — noted here per the task brief so a later lane or the founder can file it; this doc does not create it).
- **`text_explainer`** (a `direction`, not an `edit_format`) — chosen by the Main Creator Agent's own prompt for tips/review/guide/facts content (`prompts/edit_guide.txt:54`), never offered as an explicit user-facing choice in any picker UI found in this pass.

`slides` is a fourth near-hidden case, but it's better described as **unshipped-to-chat** than "hidden": it has a real, separate cloud drafting flow (`app/routes/plan_items.py`'s `slide_post` field) but zero presence in `_format_availability` (`creator_capabilities.py:101-154`) — nobody, on any account, can ask the Main Creator Agent chat for a slides edit today. `slide_posts_enabled` gates only the dedicated drafting route, not chat availability.

---

## 5. Known gaps

- **Talking-head has no phone compiler.** Per §1/§4 and `docs/reviews/kri-132/talking-head-subtitled-assessment.md`, this requires (a) a KRI-114-style pure decision/media split that talking_head's assembler has never had (`select_spine`/`schedule_broll` are pure but interleaved with real FFmpeg/Whisper calls in `talking_head_assembler.py:477-735`), and (b) new phone-compiler code targeting the `video`-spine + `overlay`-track-B-roll pattern the iOS renderer already supports for fullscreen cutaways. Sized as medium-large by that assessment. **Filed as a separate follow-up Linear ticket per the founder's direction, not part of this train.**
- **Day-vlog has no true capture-time ordering.** `_narrative_clip_order` (`app/tasks/content_plan_build.py:1100-1142`) orders clips by the filming-guide's shot sequence (client-declared `shot_id` at attach time), falling back to "pool" order (the order clips were attached in `clip_assignments`) for anything not shot-bound. There is **no `creationDate`/capture-timestamp field anywhere in the iOS media model** searched in this pass (`grep -rn "creationDate" src/apps/ios/Kria/` returned zero hits) — so a day_vlog assembled from clips attached out of the order they were actually filmed in will render in attachment order, not true chronological order. This is a real, unaddressed product gap for the format's core premise ("a day in shots, in order") and is out of scope for this train; flagged here as a known limitation, not something L1-L6 fixes.

---

## 6. Links

- Type-matrix test: `src/apps/api/tests/tasks/test_kri118_type_matrix.py` (lane L7, 2026-09-23) — table-driven `EditFormat` x footage-shape coverage; also `src/apps/api/tests/replay/test_prod_failure_replay.py` (real prod failure-shape replay, see `src/apps/api/tests/replay/README.md`).
- On-device test results doc: `docs/reviews/kri-118/on-device-script.md` (lane L7, 2026-09-23) — a manual test script for a human to run on a physical iPhone; results columns are blank pending an actual device run. `docs/reviews/kri-132/phone-format-matrix.md`'s "How verified" section remains the closest existing AUTOMATED-verification artifact (simulator runs + `test_phone_format_matrix.py`), but predates this train's changes and should not be treated as verifying anything in §3.
- Related, already-committed audits this doc builds on: `docs/reviews/kri-132/phone-format-matrix.md`, `docs/reviews/kri-132/talking-head-subtitled-assessment.md`.
- Standing rule this doc's classification is built on: KRI-129 ("creator's prompt wins" — see repo memory `kri-129-creators-prompt-wins.md` and the KRI-129-tagged comments throughout `app/agents/edit_proposal.py`).
