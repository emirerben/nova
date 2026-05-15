# Changelog

All notable changes to this project will be documented in this file.

## [0.4.15.3] - 2026-05-15

### Fixed
- **HDR/HLG tonemap: scale moved ABOVE `format=gbrpf32le`, not below it.** PR #152 (v0.4.15.2) inserted the pre-downscale between `format=gbrpf32le` (the heavy 10-bit-YUV → 32-bit-float upconvert) and `tonemap`. That fixed nothing in practice — the second prod test job after deploy (job `343af2dc-56ca-4fbb-bf3a-17e270d79acd` on template `77151144-62f5-477b-b8e6-15d111165222`, 2026-05-15) hit the 600s subprocess timeout again at 10m43s on a 24s 4K HLG clip. The actual bottleneck was the float upconvert running on full 4K frames (~95MB/frame of bandwidth, 6.4× memory expansion vs 10-bit YUV) before any downscale could relieve it. Moving `scale` ABOVE `format=gbrpf32le` so the upconvert only operates on already-downscaled 1080p frames (~24MB/frame) cuts that step's CPU/bandwidth by ~4×. `scale` operates correctly on 10-bit YUV; only `tonemap` requires float input. New chain order: `zscale=t=linear → scale → format=gbrpf32le → zscale=p=bt709 → tonemap → zscale=t=bt709 → format=yuv420p`. Expected wall-clock for a 24s 4K HLG clip on shared-cpu-2x: HEVC decode ~60s + zscale=t=linear ~30s + scale ~20s + format=gbrpf32le ~75s + gamut/tonemap/transfer ~50s + libx264 ultrafast ~30s ≈ 4-5 min total. Test pin updated: `tests/pipeline/test_compositor.py::test_hdr_hlg_pipeline_scales_before_float_upconvert` now anchors order as `linear < scale < format=gbrpf32le < gamut < tonemap`.
- **`reframe_and_export` ffmpeg subprocess timeout raised 600s → 1500s.** Even with the correct filter order, 4K HDR HLG on shared-cpu-2x has narrow head-room — the prior 600s cap left no margin for longer clips, 60fps HDR, or multi-overlay slots. 1500s fits cleanly under `orchestrate_template_job`'s 1740s soft_time_limit while leaving ~240s for orchestration overhead (Gemini calls, beat detection, audio mix, final encodes). Locked by `tests/pipeline/test_compositor.py::test_reframe_subprocess_timeout_matches_celery_soft_limit` so a future "let's lower this back" patch trips immediately. SDR sources continue to land in seconds — this affects only the rare cliff cases.

## [0.4.15.2] - 2026-05-15

### Fixed
- **HDR/HLG tonemap now downscales in linear-light BEFORE the gamut map + tonemap pass.** First prod test job after PR #150 deployed (job `4551074a-e058-4800-9b3c-1564c2da4075` on template `77151144-62f5-477b-b8e6-15d111165222`, 2026-05-15) hit the FFmpeg subprocess's 600s timeout inside `reframe_and_export` on the very first slot — a 24s iPhone 4K HLG clip (2160×3840) being software-tonemapped (`zscale → tonemap=mobius → zscale`) at native resolution before the downstream scale/crop discarded 75% of that work. The orchestrator caught `subprocess.TimeoutExpired` and marked the job failed, ~10m15s after task start. The fix inserts `scale={output_height}:{output_height}:force_original_aspect_ratio=decrease:flags=lanczos` between `zscale=t=linear` (linear-light entry) and the gamut map + tonemap, so an iPhone 4K HDR clip is downscaled to 1080×1920 BEFORE the slow tonemap pass runs — ~4× less pixel work per frame. Scaling in linear-light is also the physically correct order: averaging linear luminance is accurate; averaging gamma-encoded HDR luminance biases toward bright values. lanczos preserves highlight detail across the downscale (bilinear, the FFmpeg default, blurs specular highlights). Expected wall-clock drop on a 24s 4K HLG clip from ~10min (timeout) to ~2-3min on the same shared-cpu-2x Fly worker. SDR sources are unaffected (filter chain is only used when `color_trc in {HLG, HDR10}`). 1080×1920 HDR sources pass through the new scale step unchanged because `force_original_aspect_ratio=decrease` is a strict downscale. tonemap=mobius is kept as-is — the empirical npl=400/mobius tuning against iPhone HLG samples is intentional. `tests/pipeline/test_compositor.py::test_hdr_hlg_pipeline_scales_before_tonemap` pins the filter ORDER so a future refactor cannot quietly move the scale back behind the tonemap.

## [0.4.15.1] - 2026-05-15

### Fixed
- **`torchvision` CPU wheel now pinned alongside `torch` in the Dockerfile.** PR2 (#150, v0.4.15.0) installed `torch` from the PyTorch CPU-only index but did not list `torchvision` in the same install step. `open-clip-torch` depends transitively on `torchvision`; pip then resolved it from the default PyPI mirror, picked a CUDA-built wheel, and at the worker's first open-clip call the operator `torchvision::nms` failed to register against a CPU-only torch — surfacing as `RuntimeError('operator torchvision::nms does not exist')` in the worker_ready prewarm. CLIP font matching was silently disabled until this fix; `font_default` and `font_alternatives` would never populate. The Dockerfile now installs `torch` and `torchvision` in the same pip invocation against `https://download.pytorch.org/whl/cpu`, so pip picks a mutually-compatible pair from the only index that publishes CPU-only torchvision wheels.
- **`.github/workflows/docker-build.yml` now smoke-checks the CLIP matcher inside the built image.** Previous CI only validated that `docker build` succeeded against the .dockerignore-filtered context. That catches Dockerfile/COPY mismatches but not version-skew bugs that pass install yet explode at first import (exactly the torchvision regression above). The new step `docker run`s the built image with `load: true` and executes a minimal `_ensure_loaded()` + `embed_image()` against an 8×8 PNG. The op-registration failure is exactly what fires at this call, so the bug now fails the PR instead of failing the deploy.

## [0.4.15.0] - 2026-05-15

### Added
- **Font identification CV pipeline + admin "alternatives" picker.** Closes the PR2 half of the font-ID feature on top of PR1's `text_bbox` emission. When the template-recipe agent emits a bbox, the orchestrator now FFmpeg-extracts the sample frame, crops the bbox region, and runs it through a CLIP image-tower embedding to rank the visually-closest registry fonts. Output lands as per-overlay `font_alternatives: [{family, similarity}, ...]` plus a template-level `font_default`, both serialized into `template.recipe_cached`. The admin template editor renders alternatives as click-to-swap PNG preview tiles right under the existing font dropdown, with an "apply to all overlays" shortcut. Font identification is a best-effort enrichment — every failure mode (missing artifact, FFmpeg error, low-confidence match) falls through to the existing Playfair Display default chain.
  - `app/pipeline/font_identification.py` — new orchestrator. Defines the `FontMatcher` Protocol (swappable later for FontCLIP), FontMatch dataclass, FFmpeg single-frame extract, Pillow bbox crop with frame clamping, weighted-vote aggregator (`similarity × overlay_duration` summed over top-3 per overlay), and the registry `.npz` loader with model-version metadata enforcement.
  - `app/services/clip_font_matcher.py` — new lazy-singleton open-clip ViT-B/32 implementation. Loads once per worker process, embeds via `model.encode_image` only (text encoder dropped to save ~75MB), L2-normalizes to match registry shape.
  - `app/worker.py` — Celery `worker_ready` signal handler prewarms the matcher singleton in a background thread, so the first template-analysis after a deploy doesn't pay 3-5s of model-load latency.
  - `app/tasks/template_orchestrate.py` — calls `identify_fonts(recipe, local_path, get_matcher())` after recipe analysis, wrapped in try/except so a font-ID failure can never abort a template job. Writes `font_default` into `recipe_cached`.
  - `scripts/generate_font_embeddings.py` — one-time precompute script. Renders each registry font name in its own face, embeds via CLIP, saves to `assets/fonts/registry-embeddings.npz` with `{model_id, model_version, registry_sha256, generated_at}` sidecar metadata so a future regeneration with a different model can't silently misalign. Also writes 600×120 preview PNGs to `src/apps/web/public/font-previews/<slug>.png` for the admin UI tiles.
  - `assets/fonts/registry-embeddings.npz` — committed precomputed artifact (21 fonts × 512-dim float32, ~44KB).
  - `src/apps/web/public/font-previews/` — 21 PNG tiles (~252KB total) rendered against each font's own face.
  - `src/apps/web/src/app/admin/templates/[id]/components/FontAlternatives.tsx` — new component. Renders alternatives as a 4-up grid of click-to-swap tiles with similarity scores, missing-preview fallback, current-selection highlight, and an "apply to all overlays" shortcut.
  - `src/apps/web/src/app/admin/templates/[id]/components/PropertyPanel.tsx` — mounts `<FontAlternatives>` directly under the existing FontPicker. No mutation to existing dropdown semantics.
  - `src/apps/web/src/app/admin/templates/[id]/components/EditorTab.tsx` + `recipe-types.ts` — adds the `APPLY_FONT_TO_ALL_OVERLAYS` reducer action (atomic: sets `font_default` AND overwrites `font_family` on every overlay in a single dispatch) plus the `FontAlternative` interface and `font_alternatives` / `font_default` fields on the recipe types.
  - `app/pipeline/agents/gemini_analyzer.py` — `TemplateRecipe` dataclass gains an optional `font_default: str = ""` field. Default keeps pre-PR2 cached recipes backward-compatible when rebuilt via `TemplateRecipe(**recipe_dict)`.

### Changed
- **Worker memory bumped 2048MB → 4096MB.** The CLIP image-tower singleton resident set is ~450-600MB (weights + torch CPU runtime + activations). Combined with existing FFmpeg subprocess working sets at 1080×1920 (peak 800MB-1.4GB), the prior 2048MB ceiling left 0-800MB of headroom — tight enough that OOM mid-render would silently orphan jobs at `status=processing`. Bumping now and validating headroom from telemetry is cheaper than chasing orphan incidents later.
- **Dockerfile: torch CPU-only install promoted to its own cached layer.** Installs torch from `https://download.pytorch.org/whl/cpu` BEFORE the pyproject.toml install step so pip sees the dep already satisfied when resolving `open-clip-torch` and never falls back to the default index (which would pull the CUDA wheel, ~2GB). Version constraint is `torch>=2.4,<3`.
- **`assets/fonts/font-registry.json` — duplicate `"Inter"` key renamed to `"Inter Regular"`.** Pre-existing bug: JSON parsers silently drop the first occurrence of a duplicate key, so Inter Regular was invisible to the renderer (only the Bold variant was reachable as `font_family: "Inter"`). The .npz embedding artifact and the runtime resolver now both see all 21 unique fonts.
- **`pyproject.toml`** — adds `numpy>=1.26`, `open-clip-torch>=3.3,<4`. The 3.x floor on open-clip-torch matches what produced the committed `.npz` artifact and prevents a silent 2.x downgrade from changing the preprocess pipeline (which would misalign query embeddings against the registry).

## [0.4.14.1] - 2026-05-15

### Removed
- **Rotted eval fixtures `saygimdan.json` and `saygimdan_v2__gemini.json`** under `tests/fixtures/agent_evals/template_recipe/prod_snapshots/`. Both fixtures reference `templates/a70732f3-9367-4a90-861a-374b311c2427/reference.mp4` in GCS, which returns 404. They worked in replay mode (cached `raw_text`/`output` was self-contained) but failed every `--eval-mode=live` run with a download error. When the saygimdan template's reference video is re-uploaded, regenerate fixtures via `scripts/export_eval_fixtures.py`.

### Changed
- **`tests/evals/conftest.py` lifts per-test timeout to 300s in live mode.** The global pytest timeout of 30s (set in `pyproject.toml [tool.pytest.ini_options]`) is appropriate for fast unit tests but falsely fails every live Gemini call (template_recipe latency is 30-65s/fixture on real reference videos). The bump is applied via `pytest_collection_modifyitems` only when `--eval-mode=live` or `NOVA_EVAL_MODE=live` is set, so unit-test enforcement is unchanged. Items already carrying an explicit `@pytest.mark.timeout(...)` are left alone.

## [0.4.14.0] - 2026-05-15

### Added
- **Template-recipe agent now emits an optional `text_bbox` per text overlay** so a downstream computer-vision step can identify the font used in the reference video. The agent is instructed to emit bboxes ONLY for visible burned-in text (not for text it inferred from voiceover or vibe), with normalized coordinates `(x_norm, y_norm)` as the bbox CENTER, `(w_norm, h_norm)` as size, and `sample_frame_t` seconds relative to slot start. PR1 of two: bbox emission + storage only. PR2 will add the CLIP-style font matcher and admin "font alternatives" picker UI on top of this data.
  - `prompts/analyze_template_schema.txt` — defines the optional `text_bbox` field with shape, normalization, and emission rules.
  - `prompts/analyze_template_single.txt` + `analyze_template_pass2.txt` — add bbox emission guidance to both single-pass and two-pass-Part-2 prompts. Pass-1 (creative-direction prose only) is unchanged.
  - `app/agents/template_recipe.py` — `prompt_version` bump to `2026-05-15`; new `_validate_text_bbox()` helper validates shape, range `[0,1]`, area `> 0`, horizontal/vertical frame containment, and that `sample_frame_t` falls within the overlay's `[start_s, end_s]` window. Invalid bboxes drop to `null` (logged) while the overlay itself survives.
  - `src/apps/web/src/app/admin/templates/[id]/components/recipe-types.ts` — new `TextBbox` interface and optional `text_bbox?: TextBbox | null` on `RecipeTextOverlay`. No UI consumers yet; landing the type alongside the backend keeps the wire shape in sync for PR2.

### Changed
- **Eval harness now validates `text_bbox` shape** at the structural layer so fixture or direct-construction callers cannot bypass the agent's `parse()` cleaning. Mirrors the agent's range/window checks in `tests/evals/runners/structural.py:check_template_recipe`. Defense-in-depth: production always reaches `parse()` first, so this guard kicks in for tests and future direct callers.
- **`text_bbox_plausibility` rubric dimension** added to the LLM judge at `tests/evals/rubrics/template_recipe.md`. Returns `null` (skipped) when no overlay has burned-in text; otherwise scores 1-5 based on bbox tightness, location accuracy, and whether the bbox is absent for vibe-inferred overlays. The judge runner already drops non-numeric scores at parse time so `null` cleanly skips the dimension without breaking the avg calculation.

## [0.4.13.1] - 2026-05-15

### Fixed
- **Live judge eval no longer fails with `400 INVALID_ARGUMENT: Unsupported file URI type` on every fixture.** Gemini's File API now rejects the bare resource-name form `files/<id>` at `Part.from_uri()` call time and requires the full URL form (`https://generativelanguage.googleapis.com/v1beta/files/<id>`). The production agent path (`app/agents/_model_client.py:137`) was already using `file_ref.uri` (full URL) so prod was unaffected. The eval-only fixture uploader at `tests/evals/_fixture_uploader.py:94` was returning `file_ref.name` (bare form), which silently broke `NOVA_EVAL_MODE=live` runs — every fixture errored before reaching the agent, so 10/10 fixtures failed with zero tokens consumed.
  - `tests/evals/_fixture_uploader.py` — `FixtureUploader.normalize()` now returns `file_ref.uri` instead of `file_ref.name`. Docstring updated. Inline comment cross-references the prod path to flag that the two attributes are different shapes.
  - `tests/evals/test_fixture_uploader.py` — `_FakeFileRef` now mirrors the real `google-genai` SDK File-object shape (exposes both `.name` and `.uri`); 6 assertion sites updated to expect the full URL form. New module constant `_GEMINI_FILE_API_BASE` keeps the URL prefix in one place.

  Verified by `NOVA_EVAL_MODE=live pytest -k impressing_myself --eval-mode=live` against a real reference video (~28s, ~$0.02 in Gemini). PASS. Unblocks PR #145's prompt-change gate.

## [0.4.13.0] - 2026-05-14

### Fixed
- **Rule of Thirds template no longer renders Gemini's clip description in place of "The"/"Thirds"** — production job `a1091488-09f6-4ce0-b92e-b1cc52695c9c` shipped with "pilot in cockpit" (from Gemini's `detected_subject` on the cockpit clip) burned into the hook overlay where the title words belong. Empirically verified the regression by diffing rendered frames at 0.7s / 1.2s / 2.0s / 2.9s against the reference clip; "Rule of" survived (it doesn't match the placeholder heuristic), "The" and "Thirds" were replaced and the recipe's red `#E63946` color was lost when the substituted overlay was reclassified as a generic subject label.
  - `scripts/seed_rule_of_thirds.py` — adds `"subject_substitute": False` to the shared `_HOOK_BASE` dict so all three hook overlays opt out of placeholder substitution at the per-template layer.
  - `app/migrations/versions/0017_rot_subject_substitute_flag.py` — backfills the flag into `VideoTemplate.recipe_cached` for the Rule of Thirds template in any environment that has already seeded it. Idempotent, reversible. Chains after PR #135's `0016_template_is_agentic.py`.

### Changed
- **Gemini per-clip metadata can no longer leak into rendered video overlay text** (architectural invariant). Two fallback paths in `app/tasks/template_orchestrate.py` that fed Gemini output into the substitution pipeline are removed:
  - `_consensus_subject(clip_metas)` (majority-voted `detected_subject` across clips) was the fallback when `user_subject` was empty. Replaced with `subject = user_subject` only. Dead function deleted.
  - `clip_meta.hook_text` fallback inside `_resolve_overlay_text` for empty hook overlays. Empty hook overlays now render empty rather than borrowing Gemini's per-clip description.
  - The overlay text substitution surface is now exclusively user-input-driven (`inputs.location` via `_resolve_user_subject`).  Caption text (`copy_writer`) is NOT covered by this invariant and remains a separate trust surface, called out explicitly in `CLAUDE.md`.
  - Defense-in-depth: `_resolve_user_subject` strips leading/trailing whitespace so a `" "` submission can't bypass the empty-subject branch.
- `CLAUDE.md` template pipeline section documents the invariant + per-template flag, and flags the caption surface as a separate trust boundary.

### Added
- **Test coverage layered against the regression** — three sentinels and one parametrized audit:
  - `TestRuleOfThirdsHookLiterals` (4 tests): asserts every literal hook overlay survives `_resolve_overlay_text` even when a subject is provided and `clip_meta.hook_text` is rich. Includes a snapshot test that walks all slots (not just `slot_type=='hook'`) and gates on `_is_subject_placeholder` so future copy moves are caught.
  - `TestNoGeminiTextLeaks` (3 tests): hasattr sentinel for the deleted `_consensus_subject` function, AST-based sentinel that walks `_assemble_clips` and asserts no call on the `subject` assignment's RHS takes `clip_metas` as an argument (rename-proof), and a behavioral assertion that empty hook overlays return empty regardless of clip metadata.
  - `tests/scripts/test_seed_overlays_literal.py`: parametrized audit across every `seed_*.py` and `add_*_intro_overlays.py` script. Any literal text matching `_is_subject_placeholder` without an explicit substitution decision fails the audit. Brazil's `PERU` overlay is the only grandfathered entry, with a comment explaining the intentional implicit-substitution reliance.

## [0.4.12.0] - 2026-05-14

### Added
- **Three new first-class transition / interstitial values.** PR #121's prompts forced these cinematic concepts to collapse onto existing enum values (`match-cut → hard-cut`, `barn-door-open → curtain-close`, `speed-ramp → hard-cut + speed_factor`). They now have their own enum entries and the prompts reflect that.
  - **`match-cut`** — first-class transition. Renders identically to `hard-cut` at the pixel level (same `_GEMINI_TO_INTERNAL` → `"none"` mapping), but the recipe metadata now carries the editorial distinction. transition_picker's rubric can score "match-cut discrimination" (was this picked over hard-cut because the rationale named visual continuity?). Downstream tooling (editorial QA, eval rubrics) can reason about action-match / eye-line-match / shape-match continuity without losing the intent at the prompt boundary.
  - **`barn-door-open`** — first-class interstitial type. Bars start fully closed (covering the frame from top and bottom edges, meeting at center) and animate OPEN, retracting to the edges. Lives at the HEAD of slot N+1 (the slot following the interstitial's black hold), where curtain-close lives at the TAIL of slot N. New `apply_barn_door_open_head()` + `_generate_barn_door_bars_png_sequence()` in `app/pipeline/interstitials.py` mirror the curtain-close call surface — same PNG-overlay strategy, same encoder budget (preset=fast + tune=film + CRF=18 + 1200s subprocess timeout), same 60% max-ratio cap on uncovered footage. The orchestrator's post-render phase detects when the previous slot's interstitial was `barn-door-open` and dispatches the head animation BEFORE the next slot lands in the assembly. A re-raise on failure mirrors the curtain-close treatment — a missing hero animation paired with the existing black hold reads as a hard cut to black, which is worse than a hard failure.
  - **`speed-ramp`** — first-class transition. The cut itself is instant (maps to `"none"` in `_GEMINI_TO_INTERNAL`); the visible mechanic lives on the destination slot's `speed_factor` field (already plumbed through `reframe._build_video_filter` as `setpts=PTS/factor`). transition_picker's prompt now explicitly documents the speed-ramp ↔ speed_factor pairing — the agent emits `speed-ramp` and downstream tooling pairs it with `speed_factor > 1` on the slot whose playback is sped up.
- `transition_picker.py` Literal extended from 6 → 9 transitions. `template_recipe.py::_VALID_TRANSITION_TYPES` extended in lockstep. `_VALID_INTERSTITIAL_TYPES` adds `barn-door-open`. Structural duration envelope (`tests/evals/runners/structural.py::_TRANSITION_DURATION_RANGES`) adds `match-cut` and `speed-ramp` as 0.0/0.0 (same as hard-cut).
- `tests/pipeline/test_barn_door_open.py` (11 tests) — PNG generation correctness (frame 0 fully closed, last frame fully open, monotonic bar shrinkage, frame count scales with duration, minimum 2 frames), inverse-of-curtain-close mirror property (barn-door bar at frame i ≈ curtain-close bar at frame N-1-i, allowing ±1 for int truncation), and orchestrator-wiring canaries (the helper is importable, the orchestrator references both `apply_barn_door_open_head` and the `prev_inter_type == "barn-door-open"` conditional, the interstitial dispatch accepts `"barn-door-open"` as a hold-rendering type).

### Changed
- **`analyze_template_single.txt` + `analyze_template_pass2.txt` vocabulary reconciliation tables updated.** match-cut, barn-door-open, and speed-ramp now emit their own enum values rather than collapsing to existing ones. barn-door-CLOSE still maps to curtain-close (no separate renderer for the close half of the door pair).
- **`transition_picker` prompt** gains decision principles for match-cut (pick only when the rationale can name the continuity element — motion, eye-line, or shape), speed-ramp (pair with `speed_factor > 1` on the dest slot), and the updated duration envelope table (match-cut/speed-ramp both at 0.0). `prompt_version` bumped `2026-05-14` → `2026-05-14b`.

## [0.4.11.0] - 2026-05-13

### Added
- **Retroactive eval scaffolding for `text_designer` and `transition_picker`** — closes the regression-coverage gap left when Yasin's prompt rewrites for these two agents shipped in PR #121 (`prompt_version=2026-05-14`) without eval suites. No prompt changes here; the first live eval run will establish the v0.4.8.0 baseline retroactively.
  - `tests/evals/test_text_designer_evals.py` + `tests/evals/rubrics/text_designer.md` — 4 dimensions (hierarchy_fit, slot_position_awareness, timing_accuracy, tone_typography_alignment), threshold ≥3.5. 4 hand-authored golden fixtures pinning the prompt's documented calibration patterns: `subject_first_slot` (signature hook treatment — xxlarge/sans/gold/font-cycle/accel_at_s 8s), `prefix_label_routing` (quiet lead-in — small/serif/white/no-effect), `mid_slot_subject` (one-size-band drop with family consistency), `casual_tone_match` (tone-only mapping with no creative direction override).
  - `tests/evals/test_transition_picker_evals.py` + `tests/evals/rubrics/transition_picker.md` — 4 dimensions (default_fidelity, camera_movement_compatibility, pacing_style_modulation, duration_envelope), threshold ≥3.5. 4 hand-authored golden fixtures: `default_hard_cut` (Rule 0 direct hit), `whip_pan_match` (override when both clips share lateral camera movement), `calm_dissolve` (slow-cinematic pacing pulls dissolve to long end of envelope), `energy_chapter_break` (energy delta 6.5 forces curtain-close override of hard-cut default).
  - `tests/evals/runners/structural.py::check_text_designer` — catches intent-level drift coercion hides: hierarchy inversion (subject coming back at 'small', or prefix at 'xxlarge'), `accel_at_s` set with a non-font-cycle effect, slot-1 subject with font-cycle but no accel_at_s, and `start_s` past 15s (likely confused with absolute clip time).
  - `tests/evals/runners/structural.py::check_transition_picker` — duration outside the canonical envelope for the picked transition (hard-cut/none = 0.0; whip-pan ∈ [0.20, 0.40]; zoom-in ∈ [0.30, 0.50]; dissolve ∈ [0.40, 0.80]; curtain-close ∈ [0.60, 1.00], ±0.15s tolerance), boilerplate rationales, and the explicit prompt-forbidden case of whip-pan between two static cameras.
  - `_build_agent_class_for` dispatch + conftest preflight expanded for both agents.

### Fixed
- **`transition_picker.parse()` silently coerced `duration_s=0.0` to `0.3`.** The expression `float(...) or 0.3` treats Python's falsy 0.0 as missing, so every hard-cut / none transition output (canonically 0.0 per the prompt's duration envelope) was overridden to 0.3 after parse. The bug shipped in v0.4.8.0 with the rest of the picker rewrite; the new `default_hard_cut` golden fixture and `check_transition_picker` duration-envelope check caught it on the first run of the new eval. Fix: explicit `None` check before float coercion.

## [0.4.10.0] - 2026-05-13

### Changed
- **Reworked `clip_router` and `shot_ranker` prompts in Yasin-style calibration-anchored format.** Both agents now have explicit decision-priority tables, anchored vocabulary (hook_score 9-10 = instant curiosity, 7-8 = strong, 5-6 = middle, 3-4 = weak, <3 = drop), a slot-type → candidate-profile mapping table (hook → highest hook_score, punchline → highest energy peak, broll → mid energy + visual variety, reaction → emotional beat, face → talking-head framing, content → narrative support), camera-movement / scene-noun variety rules, and an enforced rationale format that names the dimension that decided each pick. Boilerplate rationales ("best fit", "good match") are explicitly rejected. `prompt_version` bumped from `2026-05-09` → `2026-05-15` on both agents.

### Added
- **Eval scaffolding for `clip_router` and `shot_ranker`** — the two agents that were deferred from PR #121 because their editorial-ordering impact needs regression coverage before drift can ship safely. Same four-file pattern as the existing eval suites:
  - `tests/evals/test_clip_router_evals.py` + `tests/evals/rubrics/clip_router.md` — 4 dimensions (slot_type_fit, energy_match, sequence_variety, rationale_quality), threshold ≥3.5.
  - `tests/evals/test_shot_ranker_evals.py` + `tests/evals/rubrics/shot_ranker.md` — 4 dimensions (rank_1_hook_strength, set_variety, description_quality, thematic_fit), threshold ≥3.5.
  - `tests/evals/runners/structural.py::check_clip_router` and `check_shot_ranker` — structural floors that catch what `parse()` lets through: duplicate-candidate assignment (variety violation), duplicate IDs in ranked output, boilerplate rationales, missing/extra slot assignments, ranks not dense from 1, target_count overrun.
  - 6 hand-authored golden fixtures total. clip_router: `hook_alternation` (hook→broll→punchline alternation), `broll_variety` (5-slot scene-noun variety stress test), `energy_match_easy` (2-slot minimum-viable canary). shot_ranker: `hook_first` (rank-1 calibration table direct hit), `variety_check` (near-duplicate handling — same description, same hook_score, must pick one), `thematic_pull` (creative_direction overrides raw hook_score — the highest hook_score candidate is dropped because tone=calm + introspective brief contradicts a scream-pump-fist clip).
- `_build_agent_class_for` dispatch and conftest preflight expanded to register both new agents.

### Deferred
- `prompt_version=2026-05-15` baselines on both agents will be established when an operator runs `pytest tests/evals/test_clip_router_evals.py tests/evals/test_shot_ranker_evals.py --with-judge --eval-mode=live --allow-cost` from an environment with GCS creds + `GEMINI_API_KEY` + `ANTHROPIC_API_KEY`. The replay-mode evals (no API call) already pass 6/6 on the new fixtures.

## [0.4.9.0] - 2026-05-13

### Fixed
- **Live-eval gate unblocked.** The eval harness now uploads bucket-relative fixture paths (`clips/<id>.mp4`, `templates/<uuid>/reference.mp4`) to Gemini File API at test time and substitutes the resulting `files/<id>` URI into the agent input — mirroring exactly what `template_orchestrate` / `orchestrate` already do in production. v0.4.8.0 documented this as a P1 known-issue: all 17 fixtures had been 400-ing in <8s with `Unsupported file URI type`, so every prompt change after v0.4.7.0 was shipping blind against the model. `pytest tests/evals/<agent>_evals.py --with-judge --eval-mode=live --allow-cost` now actually exercises Gemini end-to-end.

### Added
- `tests/evals/_fixture_uploader.py` — `FixtureUploader` class with bucket-relative path detection (`is_bucket_relative`), per-session upload cache (Files API IDs live ~48hr, plenty for one test run), and `normalize_input()` that returns a fresh dict so the fixture isn't mutated in place. Built from injected `download_fn` + `upload_fn` so the unit tests stay pure.
- `tests/evals/test_fixture_uploader.py` — 23 unit tests fencing each leg of the path: URI shape detection, cassette pass-through for `files/<id>` / `gs://` / `https://`, upload+caching behavior, download/upload error propagation (the cache MUST NOT poison on failure — a retry has to try again), temp-file cleanup on both success and failure paths, and `normalize_input` purity (caller's dict not mutated). These tests fence exactly the silent-failure mode that broke v0.4.8.0's gate.
- `tests/evals/conftest.py` — new session-scoped `live_input_normalizer` fixture, None in replay mode, lazy-constructs the prod GCS + Gemini helpers in live mode.
- `tests/evals/runners/eval_runner.py` — `run_eval` gains an optional `live_input_normalizer` callable that transforms `fixture.input` before `agent.run` when both it and `model_client` are set. Live-only by design; replay mode (cassette) ignores `media_uri` entirely, so the normalizer is a no-op there.

### Deferred
- **Vertex AI service-account auth swap.** Inline upload at the eval-harness layer is the surgical fix; the longer-term play unifies dev/prod auth and removes the per-eval upload cost. Filed as P2 in TODOS.md.

## [0.4.8.0] - 2026-05-13

### Changed
- **Reworked the prompts for four Big-3 agents using Yasin's drafts.** `nova.video.clip_metadata`, `nova.compose.template_recipe`, `nova.layout.text_designer`, and `nova.layout.transition_picker` get longer, structured prompts with calibration anchor tables (hook scores 0–10, energy levels 0–10, transition energy-deltas), a shared hook-type vocabulary (Visual / Verbal / Outcome / Gap / Contrast), explicit decision principles, and "decisions, not suggestions" voice. `prompt_version` bumped to `2026-05-14` on all four agents.
- **`clip_metadata` prompt fuses the new structure with the existing 5 HARD RULES verbatim.** Yasin's draft dropped the span ≥60% / no-overlap-clusters / ≥1s duration / energy variation ≥2.0 / no-generic-labels constraints that were added in v0.4.7.0 to fight the clustered-moments bug. Those rules are preserved exactly; Yasin's hook taxonomy, anchor tables, niche action vocabulary, and "Omit > pad" framing wrap them.
- **`template_recipe` prompts (`analyze_template_single.txt` + `analyze_template_pass2.txt`) carry an explicit Yasin → code-accepted vocabulary reconciliation table.** Yasin's drafts referenced 16 transitions, 8 compound color grades, and aspirational slot types that the code's Pydantic `Literal` types reject. The new prompts list every cinematic concept Yasin mentioned alongside the actual enum to emit (`match-cut → hard-cut`, `barn-door-close → curtain-close`, `teal-orange → warm`, `a24-pastel → desaturated`, etc.) so Gemini cannot drift into invalid output even when reasoning about richer concepts.
- **`text_designer` inline prompt grows from ~1.2KB to ~5KB** with hierarchy-by-placeholder-kind, slot-position awareness (slot 1 = signature hook, mid-slots lighter, final returns to bolder), tone → typography mapping for both current schema tones (`casual, formal, energetic, calm`) and aspirational tones (`chill-lofi, dramatic-cinematic, …`), explicit `start_s` timing rules, and three signature calibration patterns. Agent still runs in shadow mode against `_LABEL_CONFIG` with `max_attempts=1` so cost is bounded.
- **`transition_picker` inline prompt restructured around editor's grammar.** Rule 0 (default fidelity wins when the pair fits), energy-delta as the primary signal (0–1.5 continuation → hard-cut; 1.5–3 step → zoom-in; 3–5 major shift → curtain-close; 5+ chapter break), camera-movement compatibility rules, pacing-style modulation, and a duration envelope table. Vocabulary still strictly limited to the 6 transitions the renderer accepts.

### Deferred
- `clip_router` and `shot_ranker` prompt rewrites deliberately deferred. Editorial-ordering changes need eval scaffolding before they ship — TODOS.md captures the new evals as P2 gates. Yasin's drafts for both agents are preserved in the plan file at `~/.claude/plans/let-s-improve-the-prompts-rosy-floyd.md`.

### Known issue
- **Live-eval gate blocked on test fixture URI format.** CLAUDE.md mandates `pytest tests/evals/<agent>_evals.py --with-judge --eval-mode=live` before merging any prompt change, but every fixture's `file_uri` is a bucket-relative path (`templates/<uuid>/reference.mp4`, `clips/<id>.mp4`) and the current Studio Gemini key cannot resolve those. All 17 fixtures fail with `400 INVALID_ARGUMENT: Unsupported file URI type` in <8s — $0 spent, prompts never reach the model. Replay-mode evals (no API call) still pass 17/17. Three possible fixes filed as P1 TODO: backfill fixtures with Files API IDs, switch eval auth to Vertex service account, or auto-upload bucket paths inside `media_uri()` at call time.

## [0.4.7.2] - 2026-05-13

### Changed
- New `.github/workflows/docker-build.yml` runs `docker build` against the prod Dockerfile on every PR that touches `Dockerfile`, `.dockerignore`, or `src/apps/api/**`. Catches Dockerfile/`.dockerignore` coupling bugs on the PR instead of at deploy-to-Fly time — the same class of bug that broke the v0.4.7.0 deploy (`f156c19`) and required hotfix #119 (`121a6e9`). Uses `docker/build-push-action@v5` with GitHub Actions cache backend so warm runs stay ~30-60s; no Fly tokens or build minutes consumed.
- `CLAUDE.md` gains a "Lessons from prod" section documenting the Dockerfile/`.dockerignore` coupling rule and pointing at the new CI check.

## [0.4.7.0] - 2026-05-13

### Fixed
- **Loop B online judging is now live in prod.** The worker container was missing `tests/evals/rubrics/*.md` because the prod Dockerfile copied `app/`, `assets/`, `prompts/`, `scripts/`, and `alembic.ini` but not `tests/`. `_online_eval.py` resolves the rubric path via `Path(__file__).parent.parent.parent / "tests" / "evals" / "rubrics"`, so the gate `_rubric_path_for(agent_name).exists()` silently returned False on every dice-roll. With `LANGFUSE_*` keys + `ANTHROPIC_API_KEY` + `NOVA_ONLINE_EVAL_SAMPLE_RATE=0.05` already deployed on Fly, this Dockerfile change is the missing piece: ~15 judge calls/day, ~$5/month, scored traces appearing in Langfuse within minutes of deploy.
- **`clip_metadata` agent no longer returns moments compressed into the first 0.5 seconds with identical energies.** Five captured prod fixtures showed `best_moments` clustered at clip start (e.g. all three moments inside a 0.0–0.10s window at energy 7.0), defeating the downstream matcher. The `analyze_clip.txt` prompt now enforces five hard rules with concrete thresholds — moments must span ≥60% of the analyzed segment, adjacent starts must differ by ≥2.0s, each window must be ≥1.0s long, energy range across moments must be ≥2.0, and "moment 1/2/3" labels are forbidden — plus a self-check instruction before emitting JSON. `prompt_version` bumped from `2026-05-09` → `2026-05-13`.

### Added
- `tests/fixtures/agent_evals/clip_metadata/` — 5 prod_snapshots captured from the Redis `clip_analysis` cache (the canonical regression archive for the clustered-moments bug, scoring 2.5/5 against the rubric) plus 2 hand-authored golden fixtures demonstrating compliant output (scoring 4.50/5 and 4.75/5). Replay-mode eval has positive controls for the first time; the prod snapshots stay red until the new prompt rolls out and fresh fixtures get captured.
- `scripts/export_clip_metadata_fixtures.py` — pulls clip_metadata outputs from the Redis cache and emits them as eval fixtures. Necessary because `best_moments` are not persisted in Postgres (only the chosen window per JobClip survives). Bucketed sampling by speech / moment density for diversity. Supports `--stdout-json` for piping out of `fly ssh console`.

### Changed
- `OBSERVABILITY.md` — added a "Verified loops" section documenting the exact env vars confirmed deployed on Fly (date-stamped), the SDK query that proves Loop A traces land in Langfuse with `source:eval` + `judge_*` scores per dimension, and the cross-loop findings surfaced by the activation work (only 2 of 6 prod agents currently appear in traces — caching is the answer, not a bug).
- `TODOS.md` — three new P1 entries: (1) clip_metadata clustered-moments bug (now fixed by this PR, kept for traceability), (2) Langfuse trace gap on 4 of 6 prod agents (resolved as docs-only after investigation — caching by design), (3) Loop B worker rubric path (resolved by this PR).

## [0.4.6.0] - 2026-05-10

### Added
- Phase 2 evals: structural checks, rubrics, and per-fixture pytest gates for the three remaining in-pipeline LLM agents — `transcript` (word-level timestamps + `low_confidence` calibration), `platform_copy` (TikTok/IG/YouTube native voice + placeholder leakage + duplicate-hook detection), and `audio_template` (slot-duration arithmetic + monotonic beat list + non-empty style metadata + valid `color_grade`). Each ships with at least one hand-authored golden fixture; `prod_snapshots/` populates from a live DB export.
- `--shadow-prompts-dir <path>` flag: live-mode-only side-by-side run of prod and a candidate prompt overlay. Per-fixture summary prints `primary_avg=… shadow_avg=… Δ=…`. Implemented via a `prompt_loader._get_raw` overlay context manager so any prompt file not in the candidate dir falls through to prod. Closes the prompt-iteration loop in one pytest run instead of stash-and-diff.
- `--allow-cost` flag + automatic live-mode preflight: estimates token cost per fixture × `AgentSpec.cost_per_1k_*_usd`, refuses to run if total > $20 unless overridden. Heuristic is deliberately pessimistic (chars/3 input + fixed 1500 output). Replay mode skips the gate entirely. Prints a warning when an agent's spec has zero cost so the gate isn't silently bypassed.
- `scripts/reanalyze_underbaked_templates.py` one-off: snapshots `recipe_cached` to `/tmp/`, dispatches `analyze_template_task` (two-pass mode), optionally polls until each template reaches `ready` or `failed`. Auto-detects templates whose `creative_direction` has fewer than 50 words.
- `scripts/export_eval_fixtures.py` extended to walk `Job.transcript`, `JobClip.platform_copy`, and `MusicTrack.recipe_cached` so prod-snapshot fixtures for the new agents can be exported alongside the Phase 1 set.
- Eval dashboard (`scripts/build_eval_dashboard.py`) now surfaces the three new agents with their fixtures (both `prod_snapshots/` and hand-authored `golden/`) in the Agents tab. Per-agent fixture picker labels `golden` cases distinctly so they don't collide with prod exports.
- `.github/workflows/agent-evals.yml` exposes `transcript`, `platform_copy`, and `audio_template` as workflow_dispatch agent choices. Workflow now passes `--allow-cost` so the new gate doesn't block CI runs.

### Changed
- `runners/eval_runner.py`: `_build_agent_class_for` dispatches all six in-pipeline agents. Added a `_RUBRIC_FILENAME_OVERRIDES` map so `nova.audio.template_recipe` (audio_template) and `nova.compose.template_recipe` no longer collide on a shared rubric file.
- `tests/evals/README.md` rewritten: documents all six in-pipeline agents, both prompt-iteration loops (manual stash and auto `--shadow-prompts-dir`), the cost cap, and the limitations of `prod_snapshots/` fixtures (`raw_text` is a JSON dump of the persisted output, not the original Gemini response).

### Tests
- 5 new unit tests for the shadow context manager (round-trip + exception-path restore) and `estimate_live_cost` (per-agent breakdown, zero-cost-spec warning, unknown-agent skip). Coverage of new code paths: 8/13 (62%) by count; load-bearing structural checks at 100%.

## [0.4.5.0] - 2026-05-10

### Added
- Per-agent quality eval harness for the Big 3 LLM agents (`template_recipe`, `clip_metadata`, `creative_direction`). Combines deterministic structural assertions (slot durations sum to total ±5s, energy ∈ [0,10], valid enums, overlay-bounds, football-filter compliance, creative-direction topic coverage) with an opt-in Claude Sonnet 4.6 LLM-as-judge that scores outputs against per-agent rubrics. Sits on top of the existing `app/agents/_runtime` primitives (`ModelClient`, `Agent.run`, `run_with_shadow`) — no new agent abstractions added.
- Replay mode (`pytest tests/evals/`) is the default: 0 cost, no network, runs against recorded `raw_text` cassettes from production templates. Live mode (`--eval-mode=live`) actually re-calls Gemini for prompt-iteration A/B testing. Judge mode (`--with-judge`) adds Claude scoring against the rubric.
- `scripts/export_eval_fixtures.py` ports `VideoTemplate.recipe_cached` and the `creative_direction` text from the local DB into JSON cassettes under `tests/fixtures/agent_evals/`. Validates each fixture against the structural floor before writing — broken DB rows surface as `[reject]` lines instead of breaking CI.
- `scripts/inspect_eval_fixtures.py` pretty-prints what each agent returned for every template (slot tables, interstitials, creative_direction body, raw JSON dumps) for fast CLI inspection.
- `scripts/build_eval_dashboard.py` generates a self-contained HTML dashboard at `.dev/agent-evals-report/dashboard.html` with five tabs (Overview · Agents · Templates · Issues · Tests). The Agents tab shows the full pipeline flow, every agent's spec / prompt files / `render_prompt()` source / Pydantic schemas / per-template returns. `--watch` rebuilds on fixture, prompt, or agent-source change. `--serve` adds an HTTP server with browser auto-reload over a 2s heartbeat.
- `tests/evals/` ships 67 unit tests (27 structural assertions, 11 judge tests, 15 runner tests, 14 parametrized fixture tests). Default CI path runs the structural-only flavor (no secrets needed, ~30s). The `agent-evals.yml` GitHub Actions workflow is `workflow_dispatch`-only for the live + judge run.
- Editorial dark/light dashboard theme — Fraunces serif display + Inter body + JetBrains Mono code, oxblood accent, `prefers-color-scheme` aware. Numbered flow rail diagrams the pipeline, agent cards expand into magazine-style detail panels with syntax-highlighted Python source for `render_prompt()`.

### Changed
- `pyproject.toml` adds `anthropic>=0.40` to dev dependencies for the LLM judge.
- `CLAUDE.md` documents the agent-eval workflow and the prompt-version bump rule (any change to `prompts/*.txt` or `render_prompt()` must bump the agent's `prompt_version` and re-run evals before merge).
- `TODOS.md` records Phase 2 follow-ups (the remaining 8 agents, per-run cost cap, automatic `--shadow` mode) and surfaces 10 production templates whose stored `creative_direction` is below the 50-word floor — a real prompt-quality issue the eval caught.

## [0.4.4.0] - 2026-05-10

### Added
- Admin overlay editor now shows a WYSIWYG preview that matches the exported video. The preview panel renders text overlays as a server-rendered transparent PNG produced by the same Pillow code path the export uses, so shadow, font weight axis, line spacing, stroke, span baseline, and auto-shrink all match the final video pixel-for-pixel. New `POST /admin/overlay-preview` endpoint accepts the slot's overlays plus a time cursor and returns the overlay layer at that moment.
- Editor playback keeps live DOM-rendered text as a fallback so users still see overlays during playback; the server PNG takes over once the cursor settles for ~400ms (debounced, with abort-in-flight + 32-entry blob URL LRU cache).
- Font-cycle preview picks the active frame at the cursor time using a new `_compute_font_cycle_frame_specs` pure helper, so scrubbing across an accelerating font cycle shows the same fonts the export will render.

### Changed
- Refactored `_render_font_cycle` in `text_overlay.py` to share its frame-spec math with the new preview path. Production output is unchanged; pinned by regression tests.

### Fixed
- `reframe.py` no longer crashes local dev jobs on HDR/HLG sources when the local Homebrew ffmpeg lacks libzimg. Detects zscale availability once per process and falls back to a no-tonemap path with a structured warning. Production Docker (which ships with libzimg) uses the proper Mobius tonemap as before.

## [0.4.3.0] - 2026-05-10

### Added
- Reject template jobs whose clips can't fill the template's audio length. The upload UI reads each video's duration on file select and shows a live "Footage total: X.Xs / Y.Ys required" readout that turns amber when short. The submit button disables until the user adds enough footage. The backend enforces the same rule on `POST /template-jobs` and `POST /admin/templates/:id/test-job` via a new `validate_clip_total_duration` check, so clients that bypass the FE still get a clear 422.
- New visual reference doc at `docs/pipeline.html` mapping the full template pipeline (admin-time analysis → job-time render), what Gemini decides versus what hardcoded rules override, and every if/else fork in the path.

## [0.4.2.0] - 2026-05-10

### Added
- Admin templates page now has a "Has intro slot" toggle (Settings tab) that controls whether the upload UI splits into a pinned slot-1 intro asset + action clips for the rest. Replaces the prior name-substring heuristic that gated this behavior on the word "face" appearing in the template name. Renaming a template no longer changes how its upload UI behaves.

### Changed
- Template-name renames are now fully decoupled from runtime behavior. The flag persists in `recipe_cached.has_intro_slot` and is registered in `_ROUTING_ONLY_RECIPE_KEYS` so it survives Gemini re-analysis.
- Seed scripts (`seed_how_do_you_enjoy_your_life.py`, `seed_dimples_passport_brazil.py`) look up templates by pinned UUID instead of name. Renaming a seeded template no longer causes the next re-seed to create a duplicate row.

## [0.4.1.0] - 2026-05-10

### Added
- Homepage tiles now show the actual template playing instead of a flat 2-color gradient. Each tile loads a poster JPEG by default, fades in the muted looping video on hover (desktop) or when most-visible in the viewport (mobile), and pauses the previous tile so only one video plays at a time. Click a tile to open a modal preview with native controls and audio, then either close it or jump straight to the upload flow with a "Use this template" button.
- Backend FFmpeg poster extraction: the template analysis pipeline now generates a 540px-wide JPEG from each template right before `analysis_status` flips to `ready`, uploaded to GCS at `templates/<id>/poster.jpg`. `GET /templates` signs a 7-day URL inline (no per-tile round-trip) and returns it as `thumbnail_url`. Templates without a poster (mid-analysis or extraction failed) still appear in the list — the homepage falls back to the existing gradient.
- `scripts/backfill_template_posters.py` — idempotent management script that walks templates with `analysis_status='ready' AND thumbnail_gcs_path IS NULL` and generates posters. Race-safe via conditional UPDATE, supports `--dry-run` and `--limit`.
- New frontend primitives in `src/apps/web/`: `lib/template-playback.ts` (module-level active-tile coordination via `useSyncExternalStore`, signed-URL cache, reduced-motion + save-data helpers, tab-visibility auto-pause), `app/TemplateTile.tsx` (200ms hover debounce, 4s video-load timeout safety net, IntersectionObserver autoplay on touch devices, gradient fallback, mute icon overlay), `app/TemplatePreviewModal.tsx` (focus trap, ESC, click-outside, focus restore, "Use this template" CTA).

### Changed
- `src/apps/web/src/app/page.tsx` rewired to render `TemplateTile` components and mount the preview modal at the page level. The skeleton loader, error banner, empty state, and grid layout are unchanged.

## [0.4.0.2] - 2026-05-09

### Fixed
- `ClipMetadataAgent.parse()` now raises `SchemaError` when Gemini returns a non-empty `best_moments` list but every entry fails coercion (wrong field types, negative durations, etc.). Previously each malformed entry was silently dropped, parse returned `best_moments=[]` as success, and downstream matching had nothing to work with. The runtime now retries with schema clarification before giving up.
- Orchestrator's `_analyze_clips_parallel` adds defense-in-depth: if `analyze_clip` succeeds but returns 0 `best_moments`, treat it the same as an analysis failure and engage the Whisper fallback (which generates synthetic moments). This protects against any future regression where moments are silently lost in the agent pipeline.

## [0.4.0.1] - 2026-05-09

### Fixed
- Template-job clip matcher no longer rejects perfectly usable clips when Gemini extracts moments whose durations fall outside the slot's ±6s tolerance window. The downstream assembler uses `moment.start_s` and trims to `slot.target_duration_s`, so a 12s moment is fine for a 5s slot. Added a last-resort fallback that accepts any moment when both tight (±2s) and loose (±6s) passes are empty. The only remaining path to `TEMPLATE_CLIP_DURATION_MISMATCH` is a clip with literally zero `best_moments` (Gemini analysis failure). Reproduces the user-reported case where a 12s upload produced "no clip fits slot 2 requiring ~5.0s."

## [0.4.0.0] - 2026-05-09

### Added
- Hand-rolled agent runtime with structured output validation, retry/repair loops, tool-call dispatch, and per-call telemetry
- 12-agent catalog under `app/pipeline/agents/` with shared registry and conftest fixtures
- Agent runtime tests: `tests/agents/test_runtime.py`, `test_output_validator.py`, `test_registry.py`

### Changed
- Refactored `gemini_analyzer.py` and `copy_writer.py` to call agents through the new runtime instead of bespoke per-call wrappers

### Fixed
- Template-job failures from `TemplateMismatchError` (no clip fits a slot) now persist `failure_reason="user_clip_unusable"` instead of `"unknown"`. The frontend's existing `user_clip_unusable` copy and `error_detail` passthrough now surface the actual matcher reason instead of "Processing failed. Please try again."
- Same fix applied to single-video orchestrator path with explicit defense-in-depth handler
- Matcher's "no clip fits" hint no longer renders meaningless `≥0s` for short slots; new copy reads "Upload longer clips — slot N needs a moment about Xs long" with a usable range only when meaningful

## [0.3.3.0] - 2026-04-19

### Added
- Audio-only template creation: admins can create templates directly from music tracks without a reference video
- Gemini audio analysis: `analyze_audio_template()` listens to audio tracks and designs visual recipes with mood-driven transitions, energy-based color grading, and beat-synced slots
- `POST /admin/templates/from-music-track` endpoint creates `audio_only` templates from analyzed tracks
- `merge_audio_recipe()` combines exact FFmpeg beat timing with Gemini visual properties via proportional mapping
- "Create Template" button on music track detail page (visible when track analysis is ready)
- Audio-only template handling in admin UI: amber badge, audio placeholder instead of video player, gcs_path null guards throughout
- Migration `0011_music_track_recipe`: adds `recipe_cached` and `recipe_cached_at` to music_tracks, makes `video_templates.gcs_path` nullable, extends template_type enum with `audio_only`
- Gemini audio analysis prompt template (`prompts/analyze_audio_template.txt`)
- Graceful fallback: if Gemini audio analysis fails, tracks still reach "ready" status with a beat-only recipe
- 16 new tests: 4 Gemini analyzer, 2 merge algorithm, 4 orchestration task, 6 API endpoint

### Fixed
- TypeScript operator precedence: `||` and `??` mixed without parentheses in music track audio player props

## [0.3.2.0] - 2026-04-18

### Added
- Music template variants: parent templates can now have per-song sub-templates with beat-synced timing and the parent's visual recipe merged in
- "Enable Music Variants" toggle in admin template Settings tab switches template_type between standard and music_parent
- Music tab on parent templates: browse children, add songs from published track library, navigate to child detail
- `POST /admin/templates/{id}/children` creates a sub-template by merging parent recipe with a track's beats (proportional slot mapping, overlay timing scaling)
- `GET /admin/templates/{id}/children` lists sub-templates with track info
- `POST /admin/templates/{id}/remerge-children` re-merges all children when parent recipe changes, with skipped-child visibility in response
- `merge_template_with_track()` algorithm: proportional visual mapping, overlay timing scaling, interstitial index remapping
- Track picker modal in Music tab filters to published+ready tracks, excludes already-added songs
- Breadcrumb navigation on child templates links back to parent's Music tab
- Migration `0010_template_music_variants`: adds `template_type`, `parent_template_id`, `music_track_id` columns with FK constraints, unique constraint, and indexes
- Admin template list now filters out music_child templates by default (`?exclude_children=true`)
- Public template gallery now excludes music_child templates (they're accessed via their parent)
- 28 new tests: 7 merge algorithm unit tests, 9 API route tests, 12 existing tests updated

### Fixed
- `TemplateRecipeVersion` check constraint now includes `'remerge'` trigger in both migration and SQLAlchemy model (was only in migration, causing model/DB drift)
- Overlay deep copy in merge function prevents parent recipe mutation via shared nested references
- Tab state gracefully falls back to "recipe" when active tab is removed from visible tabs (e.g., disabling Music Variants while on Music tab)

## [0.3.1.0] - 2026-04-18

### Added
- Interactive audio player on music track detail page — play/pause, play selected section, click-to-seek, click-to-set start/end times on beat waveform
- Direct audio file upload (`POST /admin/music-tracks/upload`) — bypasses yt-dlp for environments where YouTube blocks bot IPs (Fly.io, cloud servers)
- Signed audio URL endpoint (`GET /admin/music-tracks/{id}/audio-url`) — 1-hour GCS signed URL for browser playback
- Upload file tab on admin music page (default) alongside existing URL tab

### Fixed
- Beat detection now works on continuous music — replaced `silencedetect` (finds silence gaps, returns 0 for music) with RMS energy peak detection via `astats` (finds rhythmic peaks)
- Fixed RMS value parsing crash on bare `-` values from FFmpeg `ametadata` output (`could not convert string to float: '-'`)
- Fixed admin proxy dropping query parameters (e.g., `?limit=50&offset=0` silently stripped)
- Fixed admin proxy returning raw 500 when backend is unreachable (now returns 502 with clear error)
- Registered `music_orchestrate` task module in Celery worker (tasks were silently stuck in Redis queue)
- Documented `ADMIN_TOKEN` env var in `.env.example` (required for Next.js admin proxy)

## [0.3.0.0] - 2026-04-17

### Added
- **Music beat-sync template** — users can now browse a music gallery, select a song, upload clips, and receive a video where every cut lands on a detected beat
- `MusicTrack` data model with beat timestamps, track config, publish/archive lifecycle, and GCS audio path
- Database migrations: `0008_create_music_tracks` table and `0009_add_music_to_jobs` FK column
- `POST /admin/music-tracks` — admin uploads a YouTube/SoundCloud URL; audio is downloaded via yt-dlp and beat analysis runs as a background Celery task
- `GET /admin/music-tracks` and `GET /admin/music-tracks/{id}` — admin list and detail views
- `PATCH /admin/music-tracks/{id}` — tune `best_start_s`, `best_end_s`, `slot_every_n_beats`; publish or archive a track; now validates the merged config window (not just the patch payload)
- `POST /admin/music-tracks/{id}/reanalyze` — re-trigger beat detection and config computation
- `GET /music-tracks` — public gallery endpoint returns published, ready tracks with section duration and clip count requirements
- `POST /music-jobs` — submit a beat-sync job; guards validate track state, publication, analysis readiness, audio availability, and per-track clip count limits
- `GET /music-jobs/{job_id}/status` — poll job progress
- `analyze_music_track_task` Celery task: yt-dlp audio download → `_detect_audio_beats` → `_auto_best_section` → config storage
- `orchestrate_music_job` Celery task: load track → `generate_music_recipe` → parallel Gemini clip analysis → `template_matcher.match` → `_assemble_clips` with beat-snap → `_mix_template_audio` with music track audio
- Music gallery page (`/music`) — browse tracks, select, upload clips, submit, and poll for result
- Admin music management pages (`/admin/music`, `/admin/music/[id]`) — upload, monitor analysis, publish/archive, tune config
- Next.js API proxy for admin routes (`/api/admin/[...path]`) — keeps admin token server-side only

### Fixed
- Beat-sync polling interval now correctly clears on terminal job states (was referencing undefined `terminal` variable, causing perpetual polling)
- Tracks with 0 detected beats are now marked `failed` at analysis time with a descriptive error, preventing silent job failures downstream
- `PATCH /admin/music-tracks/{id}` now validates the merged `track_config` window (not just keys present in the request body), preventing inverted `best_start_s`/`best_end_s` from reaching the pipeline

## [0.2.3.1] - 2026-04-14

### Changed
- Parallel slot rendering via ThreadPoolExecutor (max_workers=3), replacing sequential _render_slot loop
- Parallel font-cycle PNG generation via ThreadPoolExecutor (max_workers=4)
- Burn preset changed from `fast` to `ultrafast` for final text overlay encode pass

### Removed
- Dead `_render_slot()` function (replaced by `_plan_slots` + `_render_planned_slot`)

### Added
- `pytest-xdist` and `pytest-timeout` for parallel CI test execution with timeout protection
- Jest `maxWorkers: "50%"` to prevent OOM on CI runners
- CI now runs `pytest -n auto --timeout=60`

## [0.2.3.0] - 2026-04-13

### Added
- Rich inline text spans: per-word font, color, and size within a single overlay line
- `TextSpan` data model (TypeScript + Python Pydantic validation) stored in existing recipe JSONB
- `_draw_spans_png()` renderer with baseline alignment, line wrapping, and overflow scale-down
- SpanEditor segment list in admin template editor with inline preview and "split text" button
- OverlayPreview per-word CSS rendering for WYSIWYG span preview
- Font-cycle spans integration: fixed-font spans stay constant while cycling spans swap fonts
- `_draw_frame()` DRY helper for spans/flat text dispatch in font-cycle rendering
- Per-span text sanitization and length cap to prevent unbounded Pillow measurement
- 10 new backend tests covering all span rendering paths and edge cases

### Fixed
- Timing bypass when spans present but text empty (dead branch removed, proper clamping added)
- Ghost PNG reference when all spans have empty text (file existence guard)
- Overflow scale-down fallback using wrong size key (now correctly scales to "small")
- Font-cycle override font preserved through overflow scale-down via font_variant()

## [0.2.2.0] - 2026-04-12

### Added
- Font expansion: 7 new curated Google Fonts (Space Grotesk, DM Sans, Instrument Serif, Bodoni Moda, Fraunces, Space Mono, Outfit) alongside existing Playfair Display and Montserrat
- Shared font registry (`font-registry.json`) as single source of truth for both Python pipeline and TypeScript admin editor
- Font picker in admin template editor showing real font names instead of abstract style categories ("display", "sans", "serif")
- `font_family` field on overlay recipes, overriding legacy `font_style` when set
- Variable font weight axis support in Pillow rendering for WYSIWYG fidelity with CSS preview
- 31 new tests: 21 registry validation, 10 font_family resolution and regression tests

### Changed
- Merged duplicate `_draw_text_png` / `_draw_text_png_with_font` into a single function (40 lines DRY reduction)
- Font-cycle cache now keyed by `(size, settle_font_name)` to prevent cross-overlay pollution
- Cross-slot overlay merge now compares `font_family` to prevent merging overlays with different fonts

### Fixed
- Production font fallback: replaced all macOS system font paths (`/System/Library/Fonts/...`) with bundled fonts from the registry, fixing degraded rendering on Fly.io (Linux)
- Font-cycle contrast fonts now render at correct weight via `set_variation_by_axes` instead of defaulting to Light/Thin

## [0.2.1.2] - 2026-04-12

### Fixed
- Wired `consolidate_slots()` into the production pipeline to eliminate excessive clip repetition when users upload fewer clips than template slots (e.g., 3 clips + 11 slots no longer repeats each clip 3-4 times)
- Moved `consolidate_slots()` call inside the existing error-handling block for consistent error reporting

### Added
- Regression test (T14) for the 3-clip/11-slot passport vlog scenario

## [0.2.1.1] - 2026-04-12

### Fixed
- FFmpeg concat demuxer timeout on long template jobs: switched intermediate concat to `ultrafast` preset (re-encoded at `fast` in the text overlay pass), bumped timeout from 600s to 1200s
- Celery worker concurrency reduced from 2 to 1 to give each FFmpeg job the full 2 shared CPUs on Fly.io, preventing CPU contention timeouts
- Aligned concurrency setting across all deploy configs (fly.toml, Dockerfile.worker, Procfile)

## [0.2.1.0] - 2026-04-11

### Changed
- Unified text handling in admin template editor: admins now work with a single "Text" field per overlay instead of separate "Text" and "Sample Text" fields
- Overlay preview on the video canvas now shows resolved text (subject substitution applied) instead of raw template text
- Added "Preview Subject" input in the editor header to preview how placeholder text like "PERU" resolves to a real subject like "PUERTO RICO"
- CTA overlays now show "(CTA — auto)" hint instead of generic "(empty)" to explain why CTA text is intentionally blank
- Double-click inline editing and property panel both read/write `sample_text` directly (the backend's source of truth)
- Legacy recipe migration on load: overlays with populated `text` but empty `sample_text` are automatically migrated
- 19 new unit tests for `isSubjectPlaceholder` and `resolveOverlayPreview` (41 total overlay editor tests)

## [0.2.0.0] - 2026-04-11

### Added
- Visual overlay editor on the admin recipe page: click to select overlays directly on the video preview, drag vertically to reposition (snaps to top/center/bottom), double-click to edit text inline
- Overlay timeline strip below the video showing each overlay as a colored bar (color-coded by role: hook/reaction/cta/label), with a live playhead indicator
- Property panel now shows a focused overlay editor when an overlay is selected from the video or timeline, with a Remove button for quick deletion
- New `overlay-constants.ts` module shared across overlay editor components, with full unit test coverage (22 tests)

### Fixed
- Inline text edit no longer commits partial text when the user's focus moves to the timeline bar (onBlur/pointerdown ordering)
- Inline edit commit now guards against stale overlay index when an overlay is deleted externally during an active edit

## [0.1.10.0] - 2026-04-06

### Added
- Minimum-coverage clip rotation ensures all uploaded clips appear in the output video, not just the best-matched ones
- Differentiated label sizing: city/subject names render at 120px (large), prefix text like "Welcome to" renders at 72px (medium)
- First-slot label timing: prefix text appears at second 2, subject text at second 3 (staggered reveal)
- Subject labels now always get font-cycle animation with acceleration at second 8

### Changed
- Curtain-close animation minimum increased from 3.0s to 4.0s for more dramatic effect
- Curtain-close duration clamp changed from 50% to 60% of slot duration (more curtain, still 40% visible footage)
- Label detection is now content-based (matches "PERU", "Welcome to") instead of relying on Gemini's inconsistent role field

### Fixed
- Curtain-derived font-cycle acceleration now correctly uses post-timing-override start time, preventing accel from firing before text appears
- Combined "Welcome to PERU" overlays on later template slots are filtered out, preventing redundant text after the staggered reveal

## [0.1.9.0] - 2026-04-04

### Added
- Location input on template upload page so users can type city/country for overlay text
- Timing overrides (`start_s_override` / `end_s_override`) for recipe-level overlay timing corrections
- Role-based visual overrides for label text: 120px maize/gold Montserrat ExtraBold (replaces unreliable Gemini-returned styles)
- Text exit clamp on curtain-close slots prevents text from lingering on black frames
- Subject validation (`max_length=50`) on template job creation

### Fixed
- Curtain animation safety clamp now ensures at least 50% visible footage (was only triggered when clip was shorter than animation, missing equal-duration case)
- Font-cycle acceleration timestamp now uses the same 50% clamp as the visual curtain, keeping text cycling in sync with closing bars
- Reroll endpoint now preserves the subject field from the original job instead of silently dropping it

### Changed
- Curtain animation minimum increased from 1.0s to 3.0s for more dramatic slow-curtain effect
- 14 new unit tests covering timing overrides, role overrides, curtain sync, and exit clamp

## [0.1.8.0] - 2026-04-04

### Fixed
- White screen after video upload caused by stale `.next` build cache referencing missing webpack chunks from pre-route-move layout
- `next build` failure from invalid named exports (`saveBatchToStorage`, `readBatchFromStorage`, `clearBatchStorage`) in `template/page.tsx` — Next.js App Router only allows default + metadata exports from page files
- Batch storage entries without `saved_at` timestamp now expire correctly instead of bypassing the 30-minute TTL indefinitely
- `readBatchFromStorage` now returns only declared fields (`batch_id`, `template_id`) instead of leaking raw parsed localStorage data

### Added
- Root error boundary (`error.tsx`) catches page-level runtime errors with retry UI
- Global error boundary (`global-error.tsx`) catches root layout crashes with inline-styled fallback (Tailwind unavailable when layout fails)
- Batch storage helpers extracted to `lib/batch-storage.ts` for proper module separation

## [0.1.7.0] - 2026-04-04

### Changed
- Upload UI is now the primary entry point at `/` — users land directly on the upload page instead of the waitlist. The old `/nova-studio` path redirects to `/`.
- Page metadata updated to reflect product positioning: "Turn raw footage into viral clips"

### Fixed
- Server-side MIME normalisation for presigned uploads: `video/quicktime` (common on macOS/iOS) is now normalised to `video/mp4` before GCS signing, preventing signature mismatch errors when the client PUTs the file
- Architecture dashboard module config updated to reference upload UI at new root path

### Added
- 11 presigned upload endpoint tests: MIME normalisation unit tests, integration tests for happy path, quicktime normalisation, AVI passthrough, unsupported type rejection, file size limit, aspect ratio validation, and GCS failure handling

## [0.1.6.0] - 2026-04-03

### Fixed
- Curtain-close animation too fast to perceive: minimum animation duration raised from 0.5s to 1.0s via `MIN_CURTAIN_ANIMATE_S` constant, enforced at both `_assemble_clips` and `_collect_absolute_overlays` call sites
- Font-cycle settle phase interfering with curtain-close sync: when `font_cycle_accel_at_s` is active, settle phase is now skipped entirely so cycling runs to the end, reinforcing the kinetic energy of the closing bars
- Font-cycle frame cap leaving timing gaps: gap-fill PNG now bridges the frame cap boundary to `cycle_end`, preventing silent gaps in the overlay timeline
- Cross-slot text merging: adjacent slots sharing the same text (e.g., "PERU" on slots 1 and 2) now merge into a single continuous overlay instead of dropping duplicates, correctly spanning interstitial hold durations

### Added
- 8 new tests covering all 4 root causes: curtain minimum clamp, settle-phase skip with accel, frame-cap gap-fill, cross-slot merge (same text, different positions, non-adjacent gaps, accel inheritance)

## [0.1.5.0] - 2026-03-30

### Fixed
- Template analysis pipeline leaves `recipe_cached = NULL` on Fly.io when Celery hard `time_limit` sends SIGKILL before DB commit. Added `soft_time_limit` (catchable) before all hard limits so the error handler runs and persists failure state.
- Infinite SIGKILL-requeue loop: tasks killed by hard `time_limit` were requeued forever via `task_acks_late + task_reject_on_worker_lost`. Added Redis-based attempt counter (max 3, 1hr TTL) as a circuit breaker.
- Hardcoded `"gemini-2.5-flash"` in 3 Gemini API calls now uses `settings.gemini_model` config, matching the pattern already used by `analyze_clip` and `transcribe`.
- `float("NaN")` and non-numeric Gemini slot durations now rejected during template analysis instead of silently propagating to FFmpeg commands.
- Redis connections created in admin reanalyze endpoint and Celery tasks are now explicitly closed after use.

### Added
- `error_detail` column on `video_templates` table: stores the failure reason (timeout, refusal, quota) so admins can see why a template failed without reading logs.
- `error_detail` exposed in admin API responses (`GET /admin/templates`, `GET /admin/templates/:id`).
- Reanalyze endpoint clears stale `error_detail` and Redis attempt counter so manual retry always gets a fresh set of attempts.
- Slot semantic validation: Gemini responses with missing/zero `target_duration_s` or missing `slot_type` are rejected with clear error messages instead of producing broken recipes.
- `soft_time_limit` + `SoftTimeLimitExceeded` handlers on all 4 Celery tasks: `analyze_template_task` (840s/900s), `orchestrate_template_job` (1740s/1800s), `orchestrate_job` (1080s/1200s), `render_clip` (540s/600s).
- 12 new tests: timeout handling, error_detail persistence, max attempts guard, settings model config, slot validation, admin error_detail flow.

## [0.1.4.0] - 2026-03-29

### Fixed
- GCS `DefaultCredentialsError` on Fly.io: added `GOOGLE_SERVICE_ACCOUNT_JSON` env var support as tier-2 in the credential chain (file path → JSON string → ADC)
- Clear error messages for misconfigured GCS credentials: malformed JSON and invalid service account key structure both produce actionable `RuntimeError` instead of cryptic library tracebacks
- Whitespace-only `GOOGLE_SERVICE_ACCOUNT_JSON` (trailing newlines from shell piping) now correctly falls through to ADC instead of erroring

### Added
- 6 unit tests for the GCS credential chain covering all tiers, priority ordering, and error paths
- `GOOGLE_SERVICE_ACCOUNT_JSON` documented in `.env.example` and CLAUDE.md Fly secrets list

## [0.1.3.0] - 2026-03-29

### Added
- Batch import progress recovery via localStorage: users can navigate away during a Drive import and return to find progress resumed automatically
- `saveBatchToStorage`, `readBatchFromStorage`, `clearBatchStorage` helpers with SSR guard, type validation, and 30-minute staleness protection
- Extracted `startBatchPolling(batchId, templateId)` reusable polling function from inline handler, supports both new imports and recovery
- Mount-time recovery `useEffect` that reads localStorage and restarts polling on page load
- Retry logic for transient network errors during polling (3 consecutive failures before giving up, exponential backoff)
- Recovery UI indicator: "Resuming Drive import..." message distinguishes recovery from new imports
- 11 tests for batch recovery: localStorage helpers, staleness detection, malformed data handling, overwrite behavior

## [0.1.2.0] - 2026-03-28

### Added
- Interactive architecture dashboard at `/architecture` with react-flow diagram, dark mission-control theme
- Module graph config (`architecture-config.ts`): 5 L1 pipeline modules, 3 data stores, 17 L2 sub-modules with dependencies, file paths, and business context
- Business/Technical view toggle: business view shows user-facing descriptions, metrics, and status; technical view shows file counts, issue badges, and data flow labels
- L1→L2 drill-down: click any pipeline module to expand and see sub-modules
- Module detail slide-out panel: description, file list with GitHub links, recent commits, open issues
- GitHub integration via Next.js API route proxy (`/api/architecture/github`): fetches issues by module label and commits by directory path
- Impact highlighting: right-click any module to highlight direct downstream dependents
- Live job activity overlay: polls active jobs via SWR, maps job status to pipeline modules with emerald pulse animation
- localStorage job tracking: `trackRecentJob()` called on upload and template job creation for live overlay
- 32 tests across 5 suites: architecture config validation, react-flow diagram, detail panel, GitHub route, SWR hooks
- Jest configuration (`jest.config.js`) for Next.js with path aliases
- Production Dockerfile for Fly.io deployment with cached dependency layer (parses pyproject.toml at build time)
- `fly.toml` with api + worker process groups, auto-stop for API, VM sizing, health checks
- Alembic `release_command` in fly.toml for automatic migrations on deploy
- `asyncpg_database_url` property on Settings for Fly Postgres compatibility (translates libpq sslmode to asyncpg ssl)
- `normalize_postgres_scheme` field validator on database_url (postgres:// to postgresql://)
- DejaVu fonts installed in Docker image for font-cycle variety
- `.dockerignore` to exclude media, tests, frontend, and dev files from production image
- Procfile for process group definitions
- 6 new tests for Settings database URL normalization and asyncpg URL translation

### Fixed
- Rate limit check in `ModuleDetailPanel` was dead code (always caught by empty items check first)
- L2 view highlighting now passes through `highlightedIds` and `selectedModuleId` to child nodes
- Migration 0004 `down_revision` pointed to filename instead of revision ID
- TypeScript build errors for Vercel production: explicit Set generic, missing priority field, session.user null guard

### Removed
- Dead code: `hook_scorer.py` (109 LOC, never imported — superseded by `score.py`)
- Stale file reference in architecture config: redis module pointed to non-existent `celery_app.py` (now `worker.py`)

## [0.1.1.0] - 2026-03-27

### Added
- Font-cycle acceleration: cycling interval drops from 0.15s to 0.07s when curtain-close animation starts, syncing font switching speed with closing bars
- `text_color` passthrough for font-cycle overlays, enabling colored text (yellow country names, red highlights) during rapid font switching
- Per-size font caching in `_resolve_cycle_fonts()` so large/medium/small overlays get correctly sized fonts
- `MAX_FONT_CYCLE_FRAMES` safety cap (60) preventing PNG explosion on long overlays
- 14 new tests: 11 for text overlay (color, acceleration, size), 3 for template orchestration (curtain-close wiring)

### Fixed
- Font-cycle timing corruption: removed broken 1:1 timing reassignment in `_burn_text_overlays()` that overwrote multi-PNG font-cycle timestamps with single overlay timestamps
- Curtain-close animation: replaced broken drawbox approach with `geq` pixel expression filter for reliable top/bottom bar closing
- `font_cycle_accel_at_s` clamped to overlay start time to prevent full-speed cycling when `animate_s >= slot_duration`
- Stale `font_cycle_accel_at_s` removed when dedup truncation pushes overlay `end_s` before acceleration timestamp

### Changed
- Pillow added as explicit dependency for text overlay PNG rendering
- Font-cycle font resolution now accepts pixel `size` parameter with per-size dict cache

## [0.1.0.0] - 2026-03-27

### Added
- Interstitial pipeline for compound transitions: curtain-close detection via FFmpeg luminance band analysis, `render_color_hold()` for black/white/flash holds, `apply_curtain_close_tail()` for drawbox animation overlays
- Curtain-close detection classifier (`classify_black_segment_type()`) that samples pre-black-segment frames and compares top/bottom vs middle luminance to distinguish curtain-close from fade-to-black
- Gemini vocabulary translation layer (`translate_transition()`) mapping LLM output types (whip-pan, zoom-in, dissolve) to internal FFmpeg transition types
- Interstitial validation in Gemini analyzer (`_validate_interstitials()`) with schema enforcement
- Playfair Display font bundle (Bold + Regular .ttf) replacing Montserrat for editorial text overlays
- Soft gaussian shadow rendering for text overlays (replaces hard outline/stroke)
- `fontsdir` parameter to ASS subtitle filter for bundled font discovery in `reframe.py`
- Comprehensive prompt updates for curtain-close, barn-door wipe, and interstitial classification in Gemini template analysis (pass1, pass2, schema, single)
- 16 new tests: 10 for interstitial classification, 6 for Playfair Display font rendering

### Fixed
- Beat-snap clock drift after interstitials: `cumulative_s` now accounts for interstitial hold duration
- Transition from interstitial forced to hard-cut: prevents broken xfade from solid color frames
- Pre-existing test failures: 8 tests using real asyncpg connection now use `app.dependency_overrides[get_db]` with mock session, 3 email task tests now patch `httpx.post` at correct module level
- `firebase-debug.log` removed from tracking and added to `.gitignore`

### Changed
- `blackdetect` thresholds lowered: `black_min_duration` 0.3 to 0.15, `pixel_threshold` 0.10 to 0.15 for better detection of short holds
- Default font style changed from "sans" (Montserrat) to "display" (Playfair Display Bold)
- ASS overlay header updated: transparent background, no outline, soft shadow
