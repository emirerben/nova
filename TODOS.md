---
type: concept
title: Todos
ingested_at: '2026-06-25T17:00:05.493Z'
source_kind: put_page
ingested_via: put_page
---

# Nova — Deferred Work

## Editor virtual preview — follow-ups (from v0.7.30.1)

### T-VPM-1 — Music preview shorter than the edited cut ends silently
**What:** In the clip-edit virtual preview, `mapVirtualTimeToMusicTime` maps virtual time to `preview_start_s + t` with no wrap or clamp against the preview clip's duration — when the track's preview audio is shorter than the edited cut, music reaches native `ended` mid-preview and the tail plays silent.
**Why:** v0.7.30.1 fixed music dying entirely on clip edits (deck mute + URL refresh); this residual case still under-represents the final render, which loops/fits the full track section.
**Context:** `src/apps/web/src/app/plan/items/[id]/_editor/virtual-timeline.ts` (`mapVirtualTimeToMusicTime`), `useVirtualPreview.ts` music sync. Needs the track's preview duration plumbed into the mapping to wrap (or clamp + restart) sensibly. Related: the Sound-lane toggle routes through `patchMixLevel(0)` on mix-capable variants, which the virtual music element ignores.
**Effort:** S (CC: ~30 min)

## Plan 010 — SFX/overlay lanes on caption archetypes (follow-ups)

### T-CAPFX-1 — Caption keep-out hint in the overlay placement UI
**What:** Shade the caption zone (subtitled lower band / narrated centered word-pop area) in the overlay editor so users see where captions live before dropping a PiP card or fullscreen takeover on top of them.
**Why:** On caption archetypes the captions ARE the content; manual placement freedom (kept for montage parity, decision D13/OV-6 in plan 010) means users can bury their own captions with no warning today.
**Pros:** Prevents accidental occlusion without blocking deliberate creative choices (fullscreen cutaways over speech are a real idiom).
**Cons:** Caption geometry varies (font, cue length, margin_v); the hint band is an approximation, and it's FE work on the shared placement UI used by all archetypes.
**Context:** `SUBTITLED_CAPTION_MARGIN_V` in `app/pipeline/captions.py` defines the subtitled band; narrated word-pop is centered. The placement UI lives in `src/apps/web/src/app/plan/_components/` (OverlayLane/OverlayCardPopover). Decision trail: plans/010-subtitled-sfx-overlay-lanes.md (OV-6).
**Depends on:** plan 010 shipping (lanes enabled on caption archetypes).
**Effort:** S-M (CC: ~30 min)

### T-CAPFX-2 — Evaluate AI overlay-suggestion quality on speech content
**What:** Run the overlay suggester against a sample set of subtitled/narrated renders and grade anchor quality (does it propose cutaways at sensible speech moments? does it avoid fullscreen takeovers mid-sentence?), then decide whether to lift the `caption_archetype` suggestions gate.
**Why:** Plan 010 deliberately kept AI suggestions OFF on caption archetypes (decision D12/OV-5) because the suggester has only been evaluated on montage content; enabling it blind risks fullscreen cards hiding captions and the speaker's face.
**Pros:** Unlocks the suggestions rail (the main overlay-discovery surface) for the speech formats once quality is proven.
**Cons:** Needs a labeled sample set and judge criteria; Gemini eval cost.
**Context:** Gate added in `_editor_capabilities` (`suggestions_reason = "caption_archetype"`) + the suggest-overlays route guard in `routes/plan_items.py`. Eval harness pattern: `src/apps/api/tests/evals/`. Decision trail: plans/010-subtitled-sfx-overlay-lanes.md (OV-5).
**Depends on:** plan 010 shipping; sample subtitled/narrated jobs with ready assets.
**Effort:** M (CC: ~1-2 h incl. eval fixtures)

## Plan 011 — Smart Captions v2 follow-up

### T-SMART-COMP-1 — Consolidate Smart Captions visual rendering into one compositor pass
**What:** Replace the current caption, title, boundary, media, and final-caption sequence with one renderer-owned visual compositor after v2 canary data proves where the extra encode cost and generational loss occur.
**Why:** Plan 011 deliberately ships through existing proven renderers first. A single pass is a larger renderer rewrite and should be justified by measured v2 latency or image-quality regressions, not assumed upfront.
**Trigger:** V2 canary P95 exceeds the v1 comparison threshold or sampled outputs show measurable generational-quality loss attributable to the extra passes.
**Depends on:** Plan 011 canary receipts and at least 20 successful internal v2 renders.
**Priority:** P3

### T-SMART-REVIEW — v0.11.0.0 pre-merge review deferrals (2026-07-20)
Deferred with recommendations from the 8-pass /review of `feat/smart-captions-v2-trust-canary-2026-07-20`; fingerprints in the review log.
- **P2** Reveal-schedule re-association keys on `(text, round(start_s,3))` (`_text_element_burn_dicts`) — identical-text titles crosswire and edited titles desync typewriter ticks from visuals; key by element id and regenerate tick placements on title edits.
- **P2** User caption-font edits on Smart variants measure with the wrong typeface (`render_geometry._FONT_FILES` maps only 3 families; falls back to TikTokSans-Bold) while libass burns the chosen font under WrapStyle:2 — wide fonts risk edge clipping; resolve measurement through the font registry.
- **P2** `voiceover_caption_style="word"` switch on a `smart_captions_applied` variant bypasses the pinned Smart policy and protected-box geometry — either decline like caption-language or teach word-pop the policy.
- **P2** v2 `_render_subtitled_variant` orchestration error-recovery (retry_without_camera, sc_apply_failed replan, preset fail-open receipts) lacks an end-to-end test harness; happy path covered piecewise only.
- **P3** Job-status responses expose internal receipts to non-admins (ffmpeg error text, curated GCS keys, matcher rationale in `smart_validation_receipts`/`smart_music_treatment`) — whitelist variant fields for non-admin reads.
- **P3** Editor caption preview sits ~10% off on v2 renders: persisted `caption_margin_v` (default 384) ≠ policy `y_frac` 0.705 until the first user edit — persist policy-derived values or have the FE prefer `smart_caption_policy`.
- **P3** Typewriter tick SFX can starve later effects at the 48-placement cap in `resolve_sfx_placements` (chapter-heavy videos) — give ticks their own budget.
- **P3** 24h music-treatment Redis cache keys only on `job:variant` — preset audio-policy changes within the TTL are ignored on re-render; include an intents hash in the key.
- **P3** Editor shows persisted-but-unapplied media cards (D4 manifest `media_overlays_applied_ids` has no FE consumer yet) — surface applied/pending state in the overlay lane.
**Priority:** P2/P3 as marked

## Generative photos — re-plan (PR #476 closed 2026-07-11)

### Photo support in generative edits (re-plan against current stack)
**What:** Let users include photos (stills) in generative edits — pacing, Ken-Burns-style motion or static holds, and slot assembly for mixed photo/video clip pools.
**Why:** Real creator footage is photo-heavy; today photos are rejected or mishandled by the clip pipeline. PR #476 (June 7) built this against a pre-format-aware stack and drifted 140 commits behind — closed as unlandable, but the demand stands.
**How:** Re-plan rather than rebase: the agent stack has since gained format-aware edit intents, editorial sequences, and clip_metadata parse-threading (new fields must be threaded into parse() explicitly). Use branch `feat/generative-photos-2026-06-07` as the reference implementation for the photo_pacing agent shape and upload handling; re-derive the assembler integration from `docs/pipelines/generative.md` as it exists now.
**Effort:** L (human: ~1w / CC+Codex: ~1-2 days)
**Priority:** P2
**Depends on:** nothing hard; benefits from the format-aware Lane D dispatch work if it lands first.


## Landscape-fit — Follow-ups (from PR landscape-fit-2026-06-26, v0.5.3.0)

### ~~T-LANDSCAPE-1 — Show Fit/Fill toggle even after first render~~ ✓ SHIPPED
Landed as a read-only applied-fit display (`variants.length > 0` gate).
Re-renders still inherit from `all_candidates` (editable-post-render deferred by design).

### ~~T-LANDSCAPE-2 — Jest tests for Fit/Fill toggle~~ ✓ SHIPPED
6-test suite in `src/apps/web/src/__tests__/plan/plan-item-landscape-fit.test.tsx`.

### T-LANDSCAPE-3 — Landscape support for still photos (image_to_video path)
**What:** `image_to_video.py` does not pass through `output_fit`, so landscape still images are not letterboxed even when `landscape_fit='fit'`. Scope this separately once photo uploads become common.
**Effort:** S
**Priority:** P4

## CSS Motion System — Follow-ups (from transitions.dev slice, PR #TODO)

These were deliberately deferred from the initial slice to keep scope tight.

### T-MOTION-1 — Extract `useNextFrameCallback` hook
**What:** The `requestAnimationFrame(() => setState(...)) / return () => cancelAnimationFrame(raf)` pattern is copy-pasted in `OnboardingShell.tsx` (StepSlide) and `VariantRenderCard.tsx` (a third copy in `TemplatePreviewModal.tsx` was deleted with the dead `/template` route, v0.7.8.2). Extract into `src/apps/web/src/lib/hooks.ts` as `useNextFrameCallback(fn, deps)`.
**Effort:** XS (CC: ~10 min)

### T-MOTION-2 — Wire `t-stagger` exit via IntersectionObserver
**What:** `.t-stagger.is-hiding` CSS is defined but `is-hiding` is never applied. Add an IntersectionObserver on the hero `<section>` to apply/remove `is-shown`/`is-hiding` as it enters/leaves the viewport.
**Effort:** S (CC: ~20 min)

### T-MOTION-3 — Extend motion to remaining surfaces (t-tabs, t-accordion, spinner)
**What:** The skill ships `t-tabs` (tab-switch slide), `t-accordion` (height expand), and `t-success-check` (checkmark draw). Apply where appropriate once UX direction is confirmed.
**Effort:** M per surface (3 surfaces)

## Narrated Walkthrough — frontend slice (backend shipped 2026-06-22)

Backend is complete and tested (narrated_alignment, narrated_assembler, _render_narrated_variant, dispatch). Kill switch `narrated_archetype_enabled=False` (default). These are the remaining frontend tasks before flipping the switch.

### T1 — Script block UI (step spine)
**What:** Step rows on the plan-item page: faint Fraunces step numeral, editable spoken line (Inter body), lime `~3.2s` duration pill, zinc `timing estimated` / `voice differs` state pills. Reuses existing `ShotSlotUploader` clip wells per step. No new design tokens.
**Effort:** M (CC: ~45 min)

### T2 — VoiceRecorder mount on plan item page
**What:** Mount `VoiceRecorder.tsx` on plan item page, sticky once steps exist. Upload via `POST /plan-items/{id}/generate` voiceover path (NOT `/music-jobs/upload-slot` — that's the generative flow). Show `ProgressTheater tone="light"` during transcribe+align (future: separate `/align` endpoint for pre-generate step durations).
**Effort:** M (CC: ~30 min)

### T3 — narrated sub-mode display branching
**What:** `narrated_ready` shows `PoolUploadCard`; `narrated_planned` shows `ShotSlotUploader` per step. Already partially committed (commit `495e0eee`) — needs wire-up to actual clip paths + generate gate.
**Effort:** S (CC: ~20 min)

### T4 — Timeline editor voice-locked mode
**What:** For narrated variants, `TimelineEditor` exposes only swap-clip / pick-alternate / trim-in-point. Disable cross-step reorder and per-clip length changes. `voice_locked: true` on the variant to signal the editor.
**Effort:** M (CC: ~30 min)

### T5 — Generate gate + flip kill switch
**What:** `N of M steps filled` progress pill; Generate disabled until ≥1 clip per step. Then flip `narrated_archetype_enabled=True` on Fly: `fly secrets set NARRATED_ARCHETYPE_ENABLED=true --app nova-video` + worker restart.
**Effort:** S (CC: ~15 min)

## Plan dogfood fixes — review-deferred items (2026-06-12)

These surfaced in the pre-PR `/review` (footage pool + Ask Nova + conformance branch).
Correctness bugs were fixed in-branch; these are the genuinely bigger or
lower-probability items deferred so the PR stays scoped.

### Pool JSONB concurrent-write safety (row locking)
**What:** `attach_pool_clips`, `rematch_pool_clips`, and `match_pool_clips`'s write-back all do unlocked read-modify-write on `content_plans.pool`. The duplicate-dispatch path is mitigated (no second task while `status=="matching"`), but two interleaved commits can still last-writer-wins on the whole JSONB. **How:** `select(...).with_for_update()` on the ContentPlan row in all three paths, or switch to `jsonb_set` partial updates. **Why deferred:** single-user feature, low collision probability; the cheap dispatch-guard covers the common case. **Priority:** P3.

### Pool `matched_item_id` reconciliation on swap/remove
**What:** Once a pool clip is matched to an item, `matched_item_id` is write-once. If the user later removes/replaces that clip, the pool entry still points at the item, so `pool_matched_count` over-reports and "Match again" skips it forever. **How:** at rematch (or in `_plan_response`), validate `matched_item_id` against the item's live `clip_assignments` and reset stale entries to null. **Priority:** P3.

### Pool clip metadata caching
**What:** every `match_pool_clips` run (incl. "Match again" and incremental uploads) re-downloads + re-runs Gemini `clip_metadata` on all still-unmatched clips. **How:** persist the digest on `pool["clips"][i]` after first analysis; skip re-ingest for entries that already have it. **Priority:** P3 (cost optimization).

### Pool clip storage sweeper
**What:** `users/{uid}/plan-pool/*` persists forever with no sweeper; the feature invites 40-clip dumps, and abandoned pools accumulate (extends the `users/` lifecycle gap already flagged in CLAUDE.md). **How:** revisit with the auth/lifecycle-prefix work. **Priority:** P3.

### Pool matches on shot-list items: Keep/Swap UI
**What:** pool clips attach with `shot_id=None`, so on filming-guide items they land in the "Extra footage" strip as plain chips — the dashed "Matched — keep?" SlotWell branch is only reachable on uninstructed items. The conformance suppression still works (machine_matched), but there's no in-slot Keep affordance there. **How:** surface provisional pool matches in the shot rows or the extra-footage strip with Keep/Swap. **Priority:** P2.

### Test-coverage backlog for the new surfaces
**What:** the review's testing pass flagged ~14 untested new code paths worth covering as a focused sweep: pool routes (prefix-reject, dedup-merge, 4xx branches, `match_pool_clips` behavioral tests), advisor route (kill-switch 404, agent-failure fallback), `set_clip_note` route (404, note cap, machine_matched clear, verdict reset), `/generate` 409 guard, `_ingest_clips` `min_success_fraction` threshold, the three new `/music-tracks` fields, and the React components `FootagePool` / `AskNovaPanel` / `ShotSlotUploader` new branches + the render-register state machine (fake timers). **Why deferred:** the correctness fixes shipped with targeted guards (render_prompt, time-limit, conformance harness); this is the broader belt-and-suspenders sweep. **Priority:** P2.

### Advisor prompt: exclude dismissed/suppressed verdicts
**What:** `_format_conformance` in `plan_item_advisor.py` includes the verdict even when the user dismissed/suppressed it, so Nova may quote a read the user explicitly hid. **How:** skip the conformance block when `dismissed`/`suppressed`. **Priority:** P3.

## Shot-plan guarantee follow-ups (v0.4.112.0)

### PATCH /shots/{shot_id} returns 422 instead of 404 for missing shot
**What:** `edit_shot` raises HTTP 422 UNPROCESSABLE_ENTITY when the shot_id isn't found, but 404 NOT_FOUND is the correct semantic for a missing resource. Low impact (clients only see this on stale shot IDs), but inconsistent with the rest of the route file. **How:** change `HTTP_422_UNPROCESSABLE_ENTITY` to `HTTP_404_NOT_FOUND` in `plan_items.py:714`. **Priority:** P3.

### POST /generate-guide lacks rate limiting
**What:** The LLM-backed `/generate-guide` endpoint has no rate limit — repeated calls can exhaust the Gemini spend cap. The `slowapi` limiter is wired up in `main.py:39` but not applied here. **How:** add `@limiter.limit("10/minute", key_func=get_real_ip)` on `generate_guide`. **Priority:** P3.

### _narrative_pass cursor adjustment on dropped guide clip at cursor position
**What:** Adversarial review found a potential off-by-one in `_narrative_pass` (template_matcher.py) when a guide clip is dropped at the cursor position mid-loop — the cursor adjustment only fires for `cursor > bad_idx`, missing the `cursor == bad_idx` case. Pre-existing code, not introduced on this branch. **Priority:** P2.

### Prose shot labels in unplaced signal
**What:** The "Not in this take" card shows "Shot N" (ordinal) but not the shot's prose description ("morning run"). Surfacing the literal label requires stashing `narrative_shot_labels` on `all_candidates` before `shot_id` is stripped. **Priority:** P3.

## Creator Agent — Style Observation pipeline (PRs 1-4/5 SHIPPED dark; only the flag flip remains)

_Reconciled 2026-07-09: T-STYLE-2 shipped in #564 (v0.5.9.0), T-STYLE-3 in #565 (v0.5.10.0), T-STYLE-4 in #566 (v0.5.11.0) — the entries below were stale._

### T-STYLE-5 — flip tiktok_style_vision_enabled then user_style_enabled (PR 5/5)
**What:** Bake with ingest flag on → watch cost/latency → pass eval gate → flip render flag.
**Effort:** XS (CC: ~15 min)

### Enable USER_STYLE_ENABLED + live-eval validation (pre-flip gate)
**What:** Before setting `USER_STYLE_ENABLED=true` on Fly, run `NOVA_EVAL_MODE=live pytest tests/evals/test_style_derivation_evals.py --eval-mode=live` to capture golden fixtures and validate agent output quality. Check rubric dimensions: persona_alignment ≥ 3.5, parity_safety (no effect in knobs), calibration, instruction_level_correctness.
**Effort:** XS (CC: ~10 min)
**2026-07-09 gate run:** pytest gate passed 2/2 twice with `--with-judge`, but `golden/qbuilder_bold_display_observed` is flaky at the threshold — a per-dimension rerun scored avg 3.25 (<3.5) with parity_safety 3.0 (bar is 5). Run 3-5x and confirm the fixture clears consistently before treating the gate as passed; `travel_lifestyle_creator` clears cleanly every run.

### Frontend StyleCard (M1 UI, after kill switch flip)
**What:** `StyleCard.tsx` in `WorkspaceHome.tsx` near `PersonaCard`. Shows font preview (css_family), set picker (StyleChip grid), position/size/color controls, footage-bias chips, status badge (polls while deriving), Re-derive button. Light editorial design system.
**Why:** Backend ships dark; UI follows once kill switch is on and agent quality is validated.
**Effort:** M (CC: ~45 min)

### M2 — Conversational agent
**What:** Chat endpoint where user says "change my main font to something bolder" → `PATCH /personas/style` → status="edited". Or "I don't want to film running" → `PATCH /personas/{id}` → retune. Or "no instructions" → style.instruction_level="none". See `docs/pipelines/creator-agent.md` for full intent taxonomy.
**Effort:** L (human: ~2d / CC: ~2h)

### M3 — Style-driven plan + filming guide in render
**What:** Planner reads `style.instruction_level` + `preferred_edit_format_mix`; `filming_guide` threaded into `build_generative_job` → `all_candidates["filming_guide"]` → `intro_writer`; `footage_type_bias` biases `_select_archetype`.
**Effort:** L

## Plan dogfood fixes — deferred follow-ups (2026-06-11)

### CJK lyric support (full karaoke for zh/ja/ko tracks)
**What:** Real CJK lyric rendering: word segmentation (jieba or equivalent), a CJK-capable font bundle (~15MB Noto Sans SC), per-character karaoke timing in `lyric_injector.py`/`text_overlay_skia.py`.
**Why:** Chinese/Japanese/Korean tracks currently lose the Lyrics variant entirely — the language gate (`app/pipeline/lyric_support.py`) skips it cleanly and the song picker says "language not supported yet", but the variant itself is the durable fix.
**Depends on:** Docker-image size budget decision (font bundle grows the prod image).
**Effort:** XL
**Priority:** P3

### Persistent media library (supersedes the per-plan footage pool)
**What:** Per-user clip library with notes + metadata, reusable across plans; browsing/search UI. The 2026-06-11 footage pool (`content_plans.pool`, `users/{uid}/plan-pool/{plan_id}/`) is plan-scoped by design.
**Why:** Users will want trip footage to feed NEXT month's plan too, not just the current one.
**Depends on:** auth milestone + `users/{user_id}/` GCS prefix retention decisions (CLAUDE.md storage-retention note).
**Effort:** XL
**Priority:** P3

### Ask Nova thread persistence (v2)
**What:** Server-side conversation storage for `/plan-items/{id}/agent/turn` threads. v1 is deliberately ephemeral (client-held `prior_turns`, lost on reload); contested-verdict outcomes persist via the clip-note PATCH, not chat.
**Why:** A user who reloads mid-conversation loses the advisor's reasoning; support/debugging also can't see what the advisor said.
**Effort:** M (CC: ~1h)
**Priority:** P3

### Clip notes → intro_writer (overlay text uses creator context)
**What:** `all_candidates["clip_notes"]` already rides every plan-item render job (gcs_path → note). Thread it into `intro_writer`'s prompt so overlay/hook text can use facts like "famous vegan restaurant in Buenos Aires".
**Why:** Deferred at eng review: intro_writer is the highest-traffic prompt and the prompt-change rule requires live evals — a rushed bump risks hook quality for a nice-to-have. Data is already plumbed; this is prompt-only work.
**Depends on:** intro_writer live-eval run budget.
**Effort:** S (CC: ~30min + eval run)
**Priority:** P2

## Light editorial system — follow-up work

### "I filmed it" loop-closing + filming-guide peek on the Today card (D22)
**What:** Close the daily loop in the workspace without navigating to the item page. When the user taps "See how to film it" and films, they should be able to mark the day filmed and see the filming guide inline on the Today card.
**Why:** Today the workspace → item page round-trip breaks flow. The Today card is the daily anchor; the filming guide peek and a "I filmed it" action belong right there. Currently the backend has no item-level "filmed" state (only clip-upload presence is a proxy).
**How:** Product decision first: define what "filmed" means (clip-upload detection vs explicit toggle vs new `item_status` value `filmed`). Then: expand TodayCard to show the first 2-3 shots from `filming_guide` + an ink "I filmed it" button. Needs item-page design input.
**Depends on:** PR2 workspace shipped; validate with workspace→item bounce rate data.
**Effort:** M (human: ~1d / CC: ~30min)
**Priority:** P3

## Filming guide (v1 shipped in v0.4.79.0)

### Expose edit_format in PlanItemResponse + item detail UI pill
**What:** `PlanItem.edit_format` is stored but never returned by `plan_item_response()` (read-only, zero consumers so far). A simple `edit_format: str` field on `PlanItemResponse` + a small pill on the item detail page (`/plan/items/[id]`) would show the user what render archetype their idea is heading toward.
**Why:** Helps the user understand why the filming guide has the shape it does (e.g. why a talking_head plan only shows 1–2 shots vs a montage's 3–4).
**How:** Add `edit_format: str` to `PlanItemResponse` + expose in `plan_item_response()`. Add a small `bg-zinc-800 text-zinc-400` pill after the rationale callout on `items/[id]/page.tsx`. No new migration needed — field already exists on the row.
**Effort:** XS (CC: ~5 min)
**Priority:** P3

### User-editable shot list (filming_guide PATCH)
**What:** `filming_guide` is read-only in v1. Let the user add/edit/reorder shots from the item detail page so they can refine the AI-generated guide before filming.
**Why:** The AI guide is a good starting point but some users may want to adjust shots to match their specific location or setup.
**How:** Add `filming_guide` to `PlanItemEdit` PATCH whitelist. Build a shot-list editor component (add/remove/reorder shots, edit what/how/duration_s). Needs structured editing UI — non-trivial.
**Effort:** M (CC: ~1h)
**Priority:** P3

### Narrative clip order for PUBLIC generative jobs
**What:** Plan-item edits now follow the filming guide's shot order (`narrative_order` in `template_matcher.match`, kill switch `NARRATIVE_CLIP_ORDER_ENABLED`). Public generative jobs (no plan item) still use pure greedy matching — upload order is ignored. Decide whether upload order should become a soft narrative signal there, or whether the LLM `clip_router` (`app/agents/clip_router.py`, already has a `sequence_variety` eval rubric in `tests/evals/rubrics/clip_router.md`) should rank a sequence for the public flow.
**Why:** The "edit feels random" complaint applies to public uploads too; users tend to upload in the order they filmed. But upload order is a much weaker signal than an explicit guide, and energy-greedy may genuinely beat it for dump-style uploads — needs a judge-eval before committing.
**How:** Option A: pass `narrative_order=upload-order` for public jobs behind a separate flag. Option B: route public montage through `agentic_match()` with sequence-aware prompting. Either way, run the `edit-quality-review` workflow (`.claude/workflows/edit-quality-review.js`) on before/after renders to judge.
**Effort:** S (CC: ~30 min for A; M ~1.5h for B with eval)
**Priority:** P3
**Depends on:** filming-guide narrative ordering (this PR) proving out in prod

### Time-boxed hook text (judge-workflow finding, 2026-06-10)
**What:** The agent intro line burns verbatim for the FULL video. The `edit-quality-review` judge workflow graded a real guide-ordered render: influencer_readiness 5/10 FAIL, naming static full-duration text "the strongest templated-AI tell". Show the hook only for the first ~2.5-3s, then fade out (or swap to a payoff line when the closing scene lands).
**Why:** It's the top blocker between "correct edit" and "edit an established influencer would post" now that sequence alignment passes (8.5/10).
**How:** Overlay end-time support exists in the burn dict (lyrics use timed overlays); intro path pins end=video duration. Plumb a `hook_window_s` through `_resolve_intro_overlay_params` → both renderers (renderer-parity invariant! extend `test_both_renderers_honor_text_anchor_left` pattern) + fast-reburn base unaffected (text burns on base). Run `make verify-overlays` + judge workflow before/after.
**⚠️ Temporarily reverted (v0.5.1.1, 2026-06-26):** The 3s time-box was shipped in `7758af58` but the browser preview (`LinearIntroTextPreview.tsx` / `ClusterTextPreview.tsx`) was never updated to match — so users saw persistent text on screen but the download cut it off at 3s. Reverted to hold-to-EOF (matching browser) at user request. **When re-implementing:** update the browser preview to also time-box (or fade+swap) so download and preview stay in sync. The `HOOK_WINDOW_S = 3.0` constant and `hook_window_s` param are preserved as opt-in; callers pass `hook_window_s=HOOK_WINDOW_S` to re-enable.
**Effort:** M (CC: ~1-2h incl. parity tests + browser preview update)
**Priority:** P2
**Depends on:** nothing; verify with `.claude/workflows/edit-quality-review.js`

### Hook visible on literal first frame + contrast scrim (judge finding)
**What:** hook_strength 6/10 FAIL: text fades in by ~t=0.4 so the feed-preview frame has NO text; white serif over bright/busy zones crosses faces. Render text from frame 0 and add composition-aware nudge/scrim.
**Why:** The first frame is what feed previews show; hooks that appear late lose the scroll-stop.
**How:** Style-set fade-in start → 0 for the intro overlay; reuse `text_safe_zone` (already computed per clip) for vertical nudge away from face boxes + drop-shadow/scrim when background luminance is high. Parity-sensitive (both renderers).
**Effort:** S-M (CC: ~1h)
**Priority:** P2

### Shot-count hint on the content-plan calendar card
**What:** The calendar card today shows the day's theme and idea. Adding a tiny `3 shots` badge would give the user a sense of filming effort before tapping into the item.
**Why:** Helps plan filming sessions — a 4-shot montage vs a 1-shot talking head are very different commitment levels.
**How:** Include `filming_guide` in the plan-list API response (currently already there via `plan_item_response` reuse in `content_plans.py`). Render `filming_guide.length` as a badge on `PlanCalendar` or the item card.
**Effort:** XS (CC: ~5 min)
**Priority:** P3

## Fonts

### Save the variable-font instancing helper as a committed script
**What:** v0.4.38.1 instanced Montserrat-Regular/Bold from `Montserrat[wght].ttf` using a one-off Python script at `/tmp/instance_montserrat_v2.py` that ran `fontTools.varLib.instancer.instantiateVariableFont` and then patched nameID 1/2/4/6 so the family name stays "Montserrat" instead of the variable font's default-instance name. The script lives only in /tmp. Next time someone needs to add a static weight from a variable-font source (Inter-SemiBold, Poppins-Medium, etc.) they will re-derive the name-table fixup from scratch.
**Why:** The default `instantiateVariableFont` call leaves the family name as the variable font's default instance (e.g. "Montserrat Thin") which causes libass to silently fall back to a system font at render time. The fix is non-obvious and was caught only because v0.4.38.1 added a guard test. The next person without the guard test or this script in front of them will hit the same bug.
**How:** Lift the script from this PR's session into `src/apps/api/scripts/instance_variable_font.py`. Parametrize the family name + weights via CLI args. Add a one-paragraph README section under the font library docs explaining when to use it.
**Effort:** XS (human: ~30 min / CC: ~5 min)
**Priority:** P3


## Agent evals — Phase 2

**Completed in v0.4.6.0 (2026-05-10):**
- ~~Phase 2 evals for the 3 in-pipeline agents (transcript, platform_copy, audio_template)~~ — structural checks + rubrics + test entry points + golden fixtures wired
- ~~Cost cap on live-mode eval runs~~ — `--allow-cost` flag + preflight estimator with $20 cap, replay-skipped, zero-cost-spec warning
- ~~Auto `--shadow` prompt-iteration mode~~ — `--shadow-prompts-dir` flag overlays candidate prompts on prod, side-by-side run + Δ scoring, live-only

**Completed since (on `main`, ~2026-05-14 → 2026-05-18):**
- ~~Phase 2.5 — evals for the unwired-four agents (`text_designer`, `transition_picker`, `clip_router`, `shot_ranker`)~~ — all four now have rubrics + hand-authored golden fixtures + test entry-points in `tests/evals/`. `text_designer` and `clip_router` are pipeline-wired; `transition_picker` and `shot_ranker` have eval coverage waiting for wiring (gated on `clip_router` earning trust per `agentic_matcher.py:12`).
- ~~Per-PR auto-eval on prompt changes~~ — `.github/workflows/agent-evals.yml` now fires on `pull_request` when `src/apps/api/prompts/**`, `src/apps/api/app/agents/**`, `src/apps/api/tests/evals/**`, or the workflow file itself changes. Runs structural-only suite (no secrets, ~30s). Manual `workflow_dispatch` job kept for live + judge runs.

### Re-run creative_direction on 10 templates with under-baked descriptions
**What:** When seeding eval fixtures, the export script's structural validator rejected 10 of 14 templates' `creative_direction` text as below the 50-word floor (some as short as 4 words). These templates either pre-date two-pass mode or the model produced a near-empty output that nobody caught. Re-run two-pass analysis on: `that_one_trip_to`, `morocco`, `just_fine_test2`, `just_fine___sunset_reassurance`, `impressing_myself`, `football_face_hook`, plus 4 others surfaced by `python scripts/export_eval_fixtures.py 2>&1 | grep '\[reject\]'`.
**Why:** A 4-word creative_direction means Pass 2 (`template_recipe`) was running with no editorial guidance, so the recipe quality on those templates is whatever Gemini produced cold. Real user-facing impact: the templates' `copy_tone`, `pacing_style`, `transition_style` are likely generic or wrong.
**How:** Trigger reanalysis from the admin UI for each template, OR add a one-off script that calls `analyze_template_task` with `analysis_mode="two_pass"` for each. Verify by re-running `scripts/export_eval_fixtures.py` — rejected count should drop.
**Effort:** S (human: ~1h / CC: ~10 min)
**Priority:** P2

### Run reanalyze script + re-export `creative_direction/prod_snapshots/`
**What:** Operator-side follow-up to the script that landed in v0.4.6.0. Run `cd src/apps/api && .venv/bin/python scripts/reanalyze_underbaked_templates.py --auto-detect --watch` against prod DB. Then re-run `scripts/export_eval_fixtures.py` so the freshly-baked `creative_direction` text replaces the rejected fixtures.
**Why:** v0.4.6.0 only landed the script. The actual reanalysis requires live DB + Celery worker + Gemini API, so it can't run from a CI-style /ship.
**Effort:** XS (operator-run, ~10 min once Gemini quota is available)
**Priority:** P2

### `--record` mode on agent runtime
**What:** A capture path that writes Gemini's actual JSON response (not a JSON dump of the parsed output) into a fixture file in one shot. Unblocks `clip_metadata/prod_snapshots/` (best_moments aren't persisted in DB) and gives `replay` mode a higher-fidelity canary against `live` mode.
**Why:** Today `scripts/export_eval_fixtures.py` writes `json.dumps(parsed_output)` as `raw_text`. If `parse()` does post-processing the dump skips, replay diverges from live silently. A real captured response closes that gap.
**Effort:** S (human: ~4h / CC: ~30 min)
**Priority:** P3

## UX Cleanup (template-first)

### Smarter poster-frame selection
**What:** `app/services/template_poster.py` always seeks 1.5s into the template. For a few templates this lands inside a fade-in and produces a near-black thumbnail (e.g. "How do you enjoy your life?" backfilled to 3.8KB). Add a brightness/variance check on the extracted JPEG and retry at later seek offsets (3s, 5s, 10s) if the frame is too dark or low-variance. Optional: an admin override field on `VideoTemplate` to pin a specific seek time.
**Why:** A few legacy templates ship with fade-ins; the auto-extracted poster looks broken on the homepage. Gradient fallback only kicks in when `thumbnail_gcs_path IS NULL`, not when the poster is technically present but visually empty.
**Priority:** P3

### Typed admin editor for `required_inputs`
**What:** Build a typed UI under `/admin/templates/[id]` that adds/removes/reorders entries in `video_templates.required_inputs`. v1 admins set this by editing the JSON column directly. Also: extend `PATCH /admin/templates/{id}` to accept `required_inputs` (currently it does not — see `routes/admin.py:705 update_template`). Today every template that needs inputs is set up either via a seed script (Dimples Passport) or a one-off backfill script (`scripts/backfill_that_one_trip_to.py`).
**Priority:** P3

### Reanalysis preserves operator overrides
**What:** When `analyze_template_task` regenerates `recipe_cached` from Gemini (clicking "Reanalyze" in admin UI), it overwrites operator-set fields like `subject_part`, position-tool tunings, and other manual edits. The "That one trip to..." backfill (subject_part tags on "lon"/"don" overlays) would silently regress on next reanalysis.
**Why:** Manual recipe edits are durable intent, not a transient Gemini hint. They must survive reanalysis or admins lose work invisibly.
**How:** Either (a) merge a separate `template_overrides` JSONB on top of the regenerated recipe, (b) preserve overlays where `subject_part` or other override fields are set, or (c) prompt with a diff before reanalysis overwrites overrides.
**Effort:** M (human: ~1d / CC: ~30 min)
**Priority:** P2

### PropertyPanel UI for `subject_part`
**What:** Surface the new `subject_part` field (added 2026-05-09 for "That one trip to..." city slicing) in the admin overlay PropertyPanel as a select: "first_half" / "second_half" / "full" / null. Today the field is invisible in the editor — set only via the backfill script — so admins editing the recipe could accidentally drop it.
**Why:** The TS `RecipeTextOverlay` interface (`recipe-types.ts`) carries the field but no form control writes it.
**How:** Add a Select control in `PropertyPanel.tsx` near the existing role/effect/position controls. Plumb through the existing `UPDATE_OVERLAY_FIELD` action.
**Effort:** XS (human: ~1h / CC: ~10 min)
**Priority:** P3

### Sign-in / auth on the new header
**What:** The Nova header has a placeholder "Sign in" button. Real auth is its own project.
**Priority:** P2

### Decision: bring back `/music`
**What:** The `/music` frontend route is deleted but the backend (`routes/music.py`, `music_jobs.py`, beat-sync orchestrate) is preserved. Revisit when product direction on multi-mode (templates vs music sync) is settled.
**Priority:** P3

### Keyboard accessibility for admin music section bands
**What:** Ranked section bands in `AudioPlayer.tsx` (`/admin/music/[id]`) are SVG `<g>` elements with `onClick` but no `tabIndex` or `onKeyDown` — keyboard-only users can't trigger the new click-to-select feature (or the pre-existing click-to-preview). Add `tabIndex={0}` + Enter/Space handler that runs the same path as `onClick`.
**Why:** Admin users today are mouse-driven, so this isn't a current blocker. But the click-to-select feature shipped in feat/music-section-click-select-2026-05-26 is fundamentally a "skip the typing" affordance — a keyboard user needs it more than a mouse user does. Worth fixing the next time the file is opened, not a separate PR alone.
**How:** Add `tabIndex={0}` and `onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); /* same body as onClick */ } }}` to the section band `<g>` in `AudioPlayer.tsx:260`. Extract the shared body into a local function to avoid duplication. Add one test asserting Enter on a focused band fires onStartChange/onEndChange.
**Effort:** XS (human: ~30 min / CC: ~5 min)
**Priority:** P3

## Visual Overlay Editor (shipped v0.2.0.0, 2026-04-11)

### Overlay Editor Component Tests (Tier 2)
**What:** React component tests for `OverlayPreview`, `PropertyPanel` (overlay selection case), and `OverlayTimeline` using `@testing-library/react`.
**Why:** Pure logic (overlay-constants.ts) is 100% tested. Component render behavior (visible overlays, role colors, selection ring) is untested. Deferred due to missing @testing-library/react setup.
**How:** Install `@testing-library/react` + `@testing-library/jest-dom`. Write 3 tests: OverlayPreview renders visible overlays at correct positions, PropertyPanel shows overlay form when selection.type === "overlay", OverlayTimeline renders bars with correct role colors.
**Effort:** S (human: ~4h / CC: ~15 min)
**Priority:** P2

### Role-Aware Preview Resolution (V2)
**What:** Add `_LABEL_CONFIG`-style routing to the frontend `resolveOverlayPreview()` to prevent false-positive subject substitution for title-cased imperative phrases like "Watch Now", "Check This".
**Why:** The Python backend uses `_LABEL_CONFIG` routing (template_orchestrate.py:1162) to differentiate subject labels from prefix/CTA labels after `_is_subject_placeholder()` returns true. The TS preview port has no equivalent, so the preview incorrectly substitutes the preview subject into these phrases. Preview-only impact (backend renders correctly), but reduces WYSIWYG fidelity.
**How:** Add a `role`-aware check in `resolveOverlayPreview()`: skip subject substitution for title-cased 2-word inputs when the overlay role is not "label". Alternatively, port the `_LABEL_CONFIG` prefix detection ("welcome", "check", "watch" starts) as a negative filter.
**Effort:** XS (human: ~1h / CC: ~5 min)
**Priority:** P3
**Depends on:** Unify Text Handling PR shipped

## Font System (shipped v0.2.2.0, 2026-04-12)

### Refactor Pacifico player-card hardcoding
**What:** `src/apps/api/app/pipeline/text_overlay.py` hardcodes `_registry_font_path("Pacifico")` for player-card name rendering. Replace with `_registry_font_path(_FONT_REGISTRY["style_defaults"]["script"])` so player-card rendering follows the registry's script default. Unblocks future Pacifico deprecation.
**Effort:** XS (CC ~15 min). **Priority:** P3.

### Hard-remove soft-deprecated fonts in ~6 months
**What:** Once we have evidence no live templates reference Outfit / Space Mono / 5 redundant handwriting fonts / Inter Regular / Inter Medium, delete the 9 deprecated registry entries + TTF files + license docs.
**Effort:** XS (CC ~10 min). **Priority:** P3. **Target:** ~2026-11-21.

### Matcher vibe-bias (future opt-in)
**What:** Add optional weighting where templates carry a vibe tag and matcher biases toward fonts in that vibe bucket (e.g. music templates prefer `viral_headlines`). Currently the matcher is vibe-blind; only the admin UI uses vibe for grouping.
**Effort:** M (CC ~2 hr). **Priority:** P3. **Defer until:** we have concrete evidence the current matcher under-picks for a category of templates.

### Font-Cycle Cycling Font Customization
**What:** Let admins pick which fonts appear during the rapid cycling phase (not just the settle font). Currently, cycling fonts are hardcoded from registry fonts with `cycle_role="contrast"`.
**Why:** Different templates may want different cycling character (e.g., all serifs for editorial, all sans for modern). Currently only the settle font is customizable via `font_family`.
**How:** Add a `cycle_fonts` array field to the overlay recipe. In `_resolve_cycle_fonts()`, use the admin's list instead of the registry contrast fonts. Frontend: multi-select font picker for cycling fonts.
**Effort:** S (human: ~4h / CC: ~15 min)
**Priority:** P3
**Depends on:** Font expansion PR shipped

### Effect Animation Preview (PR2)
**What:** CSS/JS animations in the admin editor preview for font-cycle, fade-in, typewriter effects. Currently, the preview shows static text regardless of effect.
**Why:** WYSIWYG gap: admin sees static text but the video has animated text. Hard to tune timing without seeing the animation.
**How:** Map each effect to CSS animations (font-cycle: rapid font-family swaps, fade-in: opacity transition, typewriter: clip-path reveal). Only in OverlayPreview component.
**Effort:** S (human: ~4h / CC: ~15 min)
**Priority:** P2
**Depends on:** Font expansion PR shipped

## Gemini + Template Mode (shipped 2026-03-23)

### Gemini Integration Tests
**What:** Real end-to-end tests hitting the Gemini API: `tests/integration/test_gemini_analyzer.py`
**Why:** Current tests mock all Gemini calls — a model API change or schema drift would only surface in production.
**How:** Requires `GEMINI_API_KEY` in local env. Write 3 tests: upload+poll, analyze_clip, analyze_template against a real test video fixture.
**Effort:** S (human: ~1 day / CC: ~15 min)
**Priority:** P2
**Depends on:** GEMINI_API_KEY in local env

### Gemini API Cost + Rate Limit Guard
**What:** Add per-job Gemini token cost tracking and a per-user/per-day quota guard.
**Why:** 9 parallel analyze_clip calls per job. At scale, costs can grow unbounded and ResourceExhausted errors will increase.
**How:** Track token counts in `Job.probe_metadata`; add a Celery pre-task check against a Redis counter.
**Effort:** M (human: ~3 days / CC: ~30 min)
**Priority:** P2 — add before marketing drives volume
**Depends on:** Usage baseline from real jobs

### Content-aware `_fallback_moments()` (added 2026-05-16)
**What:** Replace the three hardcoded `[0, min(clip_dur, 5/10/15)]` windows in `_fallback_moments()` (`app/tasks/template_orchestrate.py:1359`) with content-aware moments derived from the Whisper transcript — e.g., longest speech bursts, word-density peaks.
**Why:** Current fallback is a last-resort code path that produces near-useless moments. For clips ≤ 5s all three windows collapse to identical `[0, clip_dur]`, leaving the matcher with no real choice. When the matcher picks a fallback clip the rendered slot is structurally weak. Surfaced during the v0.4.22.0 investigation — clip 3 of job `9ec8e5ff-…` was assigned its single collapsed `[0, 5]` fallback moment for a 10s slot, producing the 6s silent-failure output.
**How:** Use the Whisper transcript already computed at `template_orchestrate.py:1321`. Score windows by speech density / longest contiguous phrase. Generate 2-3 candidates of different lengths so the matcher still has options.
**Effort:** S (human: ~3h / CC: ~20 min)
**Priority:** P3 — low-leverage if Gemini reliability stays high (the common case never hits fallback)
**Depends on:** nothing — standalone PR

### Multiple Template Support
**What:** Support different template structures for different content categories (tutorial vs. reaction vs. vlog).
**Why:** v1 validates the single-template UX; after validation, multiple templates unlock more use cases.
**Effort:** M (human: ~1 week / CC: ~30 min)
**Priority:** P2 — after v1 template validated in production

### Librosa-Based Beat Detection
**What:** Replace or augment FFmpeg energy-peak beat detection with librosa's onset detection and beat tracking.
**Why:** FFmpeg `silencedetect`/`astats` catches energy transients (drum hits, bass drops) but misses melodic transitions. librosa provides proper onset detection, tempo estimation, and beat tracking with significantly higher accuracy.
**How:** Add librosa dependency (~50MB with numpy/scipy). Replace `_detect_audio_beats()` in `template_orchestrate.py`. Compare quality against FFmpeg approach on 20+ templates.
**Effort:** S (human: ~1 day / CC: ~15 min)
**Priority:** P3 — upgrade path if FFmpeg+Gemini beat detection proves insufficient
**Depends on:** ~~Beat sync feature shipped~~ ✓ unblocked by v0.3.0.0

### Re-Analyze Existing Templates for Beat Data
**What:** One-time migration task to re-run `analyze_template_task` on all existing templates so they get `beat_timestamps_s` in their cached recipe.
**Why:** Existing templates have `recipe_cached` without `beat_timestamps_s`. They work (backward-compatible default=[]) but don't benefit from beat sync until re-analyzed.
**How:** Admin endpoint or management command that queries all templates with `analysis_status="ready"` and dispatches `analyze_template_task` for each.
**Effort:** XS (human: ~2 hours / CC: ~5 min)
**Priority:** P2 — run after beat sync code ships
**Depends on:** ~~Beat sync feature shipped~~ ✓ unblocked by v0.3.0.0

### TikTok Content API Application
**What:** Submit TikTok Content API application at developer.tiktok.com.
**Why:** 4-8 week approval window. Clock must start now even though platform posting (Phase 2) is deferred. This is a time-gated blocker — every day of delay pushes the TikTok launch date by a day.
**How:** Create TikTok developer account, submit Content API application form (15 min).
**Effort:** XS (15 minutes, one-time form)
**Priority:** P1 — submit by 2026-03-28 at latest
**Depends on:** Nothing

### Platform Posting (Phase 2)
**What:** POST /post endpoint, OAuth token refresh, Instagram/YouTube/TikTok upload integrations.
**Why:** Core monetization path — users want one-click posting, not just clip downloads.
**How:** Separate PR; OAuth infra already in models. See agents/DECISIONS.md.
**Effort:** L (human: ~2 weeks / CC: ~2 hours)
**Priority:** P1 — next sprint after template validation

---

## Template Prompt Improvement (from CEO review 2026-03-24)

### Color Grading Visual Verification
**What:** Post-merge, visually review color grading output on 3-5 templates within 48 hours. Tune the FFmpeg colorbalance/eq values if they look over- or under-processed.
**Why:** The color grade → FFmpeg filter mappings are starting-point values based on theory, not empirical testing. Subtle adjustments may be needed.
**How:** Run template jobs, inspect output videos. Compare warm/cool/vintage/desaturated slots against the original template.
**Effort:** XS (human: ~1 hour / CC: N/A — manual visual review)
**Priority:** P2 — within 48 hours of merge
**Depends on:** Template prompt improvement merged

### A/B Analysis Mode Evaluation
**What:** After running both single-pass and two-pass Gemini analysis on 3-5 templates, compare recipe quality and decide which to keep as default. Remove the losing approach to prevent dead code.
**Why:** Outside voice challenged the two-pass assumption. A/B ships both; need to evaluate and converge.
**How:** Run `analyze_template()` with `analysis_mode="single"` and `"two_pass"` on the same templates. Compare: creative_direction quality, field accuracy (transition_in, color_hint), and overall recipe usefulness.
**Effort:** XS (human: ~2 hours / CC: ~5 min for cleanup)
**Priority:** P2 — within 1 week of merge
**Depends on:** Template prompt improvement merged

### Speed Ramp FFmpeg Implementation
**What:** Implement `setpts` filter for `speed_factor` per slot. Currently schema-only.
**Why:** Speed ramping is a signature TikTok editing move — high visual impact.
**How:** First investigate: does template mode mute clip audio? If yes, simple `setpts=PTS/{speed_factor}` in `-vf` chain. If no, need `atempo` for audio compensation. Template mode mixes clip audio with template music track — need to understand the audio handling before touching video speed.
**Effort:** S (human: ~1 day / CC: ~15 min)
**Priority:** P3 — after audio handling is understood
**Depends on:** Template prompt improvement merged (for speed_factor data)

### ~~Agent text-overlay coverage for word-by-word templates~~ (added 2026-05-16)
**Completed in v0.4.26.0 (2026-05-17):** Solved by carving text extraction into its own agent (`nova.compose.template_text`) instead of tightening the recipe prompt. Dedicated agent + dedicated prompt + dedicated rubric + OCR ground-truth eval. Recipe prompt left alone — the new agent overwrites recipe overlays in `agentic_template_build_task`. See CHANGELOG v0.4.26.0.

---

## Music Beat-Sync (shipped v0.3.0.0, 2026-04-17)

### Auth on POST /music-jobs
**What:** Replace the synthetic `SYNTHETIC_USER_ID` stub in `music_jobs.py` with real user authentication via `get_current_user(db)`.
**Why:** `POST /music-jobs` is currently unauthenticated — any caller can trigger Gemini API calls and GCS reads. Acceptable for internal MVP; must be fixed before public launch. See `src/apps/api/app/routes/music_jobs.py:23`.
**How:** Wire in the existing `get_current_user` dependency (already used by other routes). Add user_id to music job records for attribution.
**Effort:** XS (human: ~1h / CC: ~5 min)
**Priority:** P1 — required before public launch
**Depends on:** Auth infrastructure (already exists in other routes)

---

## P1 — Required before GTM campaigns go live

### UTM Capture
**What:** Add utm_source, utm_medium, utm_campaign columns to `waitlist_signups` table.
**Why:** Organic TikTok (and any future channel) sends tagged URLs — without server-side capture, attribution is lost even if analytics JS fires.
**How:** New Alembic migration adds 3 nullable VARCHAR columns; `POST /api/waitlist` reads them from query params in the request.
**Effort:** S (human: ~1 day / CC: ~5 min)
**Priority:** P1 — add before first TikTok campaign goes live
**Depends on:** Waitlist table must exist (landing page PR must be merged first)

---

### Resend Confirmation Email
**What:** Send a transactional "You're on the Nova waitlist" email on successful signup.
**Why:** Without confirmation, ~15% of collected emails will be typos, disposable addresses, or bots — poisoning the list before launch. A clicked confirmation is a real lead signal.
**How:** Add Resend (or Postmark) as dependency; `POST /api/waitlist` dispatches a Celery task to send the email after insert. Email: subject "You're on the Nova waitlist", body: value prop + "we'll reach out when your spot opens."
**Effort:** S (human: ~1 day / CC: ~10 min)
**Priority:** P1 — add before GTM campaigns drive significant traffic
**Depends on:** Resend API key (free tier: 3k emails/month)

---

## Template Fidelity (from CEO/eng review 2026-03-25)

### Automated Visual Regression Testing
**What:** On every pipeline code change, auto-run 3-5 reference templates, generate eval grids, diff against golden baselines.
**Why:** Catches quality regressions automatically. Currently manual-only via eval harness.
**How:** CI step that runs template jobs on reference videos, generates per-slot eval grids, compares pixel-diff against golden baselines. Fail if delta > threshold.
**Effort:** S (human: ~1 day / CC: ~30 min)
**Priority:** P2
**Depends on:** Eval harness shipped (this PR)

### Converge Overlay Rendering to ASS-Only
**What:** Migrate font-cycle from multi-PNG to ASS rendering.
**Why:** Reduces two overlay code paths (PNG + ASS) in text_overlay.py and reframe.py to one. Lower maintenance burden.
**How:** Replace `_render_font_cycle()` PNG generation with ASS karaoke tags using rapid style switching. Update reframe.py to remove PNG overlay path.
**Effort:** S (human: ~1 day / CC: ~15 min)
**Priority:** P3
**Depends on:** ASS animated overlays shipped and validated (this PR)

---

## clip_metadata: best_moments clustered at clip start (added 2026-05-13)

**What:** `nova.video.clip_metadata` (gemini-2.5-flash) returns 3 best_moments compressed into <0.5 seconds with near-identical energies (range 0.5-1.0) on a meaningful fraction of prod clips.
**Why:** Surfaced by the first run of `pytest tests/evals/test_clip_metadata_evals.py --with-judge` on 5 prod snapshots pulled from the Redis clip_analysis cache — judge avg 2.5/5 vs 3.5 threshold. 3 of 5 fixtures exhibit the pattern (clip_2c750692, clip_04022f9e, clip_1fd09d23). The matcher downstream effectively has one moment to choose from, defeating the point of best_moments.
**How:** Likely cause is Gemini interpreting `start_s`/`end_s` in some normalized unit when given a short segment, or a prompt that doesn't enforce timestamp spread. Investigate the `analyze_clip` prompt template — add an explicit instruction to spread moments across the clip duration and require non-trivial energy variation. Bump `prompt_version` in `ClipMetadataAgent.spec` and re-run live eval.
**Evidence:** `src/apps/api/tests/fixtures/agent_evals/clip_metadata/prod_snapshots/clip_2c750692.json` — all 3 moments span 0.0-0.10s.
**Effort:** S (human: ~3hr / CC: ~30 min — prompt iteration + re-run live eval against the same 5 fixtures)
**Priority:** P1 — affects every job
**Depends on:** none
**Completed:** v0.4.47.11 (2026-05-28) — `_enforce_moment_spread()` deterministic post-filter added in `clip_metadata.py::parse()` enforcing HARD RULE 2 (≥2s spacing) + HARD RULE 3 (≥1s duration). Eval structural rule relaxed to allow 0-5 entries (prompt explicitly permits empty).

---

## ~~Langfuse: only 2 of 6 prod agents are tracing~~ — RESOLVED 2026-05-13 (caching, by design)

**Resolution:** Not a tracing bug. All four "missing" agents do go through `Agent.run()` via the shims in `app/pipeline/agents/gemini_analyzer.py` and would trace correctly *if* they were invoked per-job. They aren't: their outputs are persisted to `VideoTemplate.recipe_cached` / `MusicTrack.recipe_cached` at admin upload time, and every prod job reads the cached row from Postgres (`template_orchestrate.py:478-481`, `music_orchestrate.py:286,561`). `TranscriptAgent` is only called from the legacy 3-clip pipeline (`app/tasks/orchestrate.py:97`), which isn't on the template-mode hot path. Only `clip_metadata` and `platform_copy` have per-job inputs, so they invoke `Agent.run()` on every job — matching the observed 12 + 6 / 10 sessions ratio.

Full write-up in `src/apps/api/OBSERVABILITY.md` under "What the first prod query revealed".

**Optional follow-up (P3):** emit a lightweight Langfuse `client.event(name="template_recipe_cache_hit", ...)` from `_run_template_job` and `_run_music_job` so per-job dashboards show which cached recipe each job used (cost/latency visibility for the cached path). Not needed for correctness — the cached recipe is already in Postgres and the prior `agent_run` trace from admin upload time is still queryable in Langfuse.

**Original report below for context.**

**What:** Querying Langfuse for `source:prod` traces over 24h returns only `clip_metadata` and `platform_copy` — the other 4 prod-wired agents (`template_recipe`, `creative_direction`, `transcript`, `audio_template`) are missing entirely. 18 traces, 10 distinct job sessions, 0 traces from the missing 4 agents.
**Why:** Surfaced via SDK query `client.api.trace.list(tags=["source:prod"], from_timestamp=now-24h)` after Lane A verification on 2026-05-13.
**Effort (was):** S (human: ~2hr / CC: ~30min)
**Priority (was):** P1 — downgraded after root-cause analysis showed expected behavior.

---

## Langfuse: online judge (Loop B) is not scoring any prod traces (added 2026-05-13)

**What:** With `NOVA_ONLINE_EVAL_SAMPLE_RATE=0.05` confirmed set on Fly and 18 source:prod traces over 24h, zero traces have `judge_*` scores attached. Statistically should be ~1 judged trace per 24h at current volume.
**Why:** Surfaced alongside the agent-missing finding above. Loop A scores ARE attaching correctly (verified on the 5 eval traces). So the trace + score_trace machinery works — the gap is specifically in the worker-side dispatch.
**How:** Check three suspects: (1) `ANTHROPIC_API_KEY` may not be reachable on the worker process group on Fly even though `fly secrets` shows it deployed — secrets propagate per process. `fly ssh console --process-group worker -C printenv ANTHROPIC_API_KEY`. (2) Rubric files must be present in the worker container — verify `tests/evals/rubrics/*.md` is included in the Dockerfile COPY. The Dockerfile copies `app/`, `assets/`, `prompts/`, `alembic.ini` per CLAUDE.md — `tests/` is NOT copied. **This is likely the root cause** — the worker can't find the rubric file because the entire `tests/` directory is excluded from the prod image. (3) Confirm `score_trace_async` is registered as a Celery task and a worker is consuming it.
**Effort:** S (human: ~1hr / CC: ~30min — likely a 2-line Dockerfile change plus verification)
**Priority:** P1 — Loop B is what makes online observability actually useful; right now it's posting traces but not scoring them
**Depends on:** none

---

## Yasin's prompt rewrite — follow-ups (added 2026-05-14)

These TODOs were filed when the first wave of Yasin's prompt rewrites shipped (`clip_metadata`, `template_recipe`, `text_designer`, `transition_picker`, all prompt_version="2026-05-14"). `clip_router` and `shot_ranker` were deliberately deferred — see the first two items below.

### ~~Live-eval gate broken on fixture file-URI format~~ — RESOLVED in v0.4.9.0
**Resolved:** Option 3-equivalent shipped at the eval-harness layer (not the agent base class — surgical scope). New `tests/evals/_fixture_uploader.py` downloads bucket-relative paths from GCS and uploads to Gemini Files API at test time, substituting the `files/<id>` URI into the agent input. Mirrors prod's `gemini_upload_and_wait` flow exactly. Per-session cache keeps the upload cost bounded (~$0.10–0.20 per full live-eval run). 23 unit tests fence each leg of the path.

### Vertex AI service-account auth swap (P2 follow-up to v0.4.9.0)
**What:** Replace the v0.4.9.0 inline-upload fix with a Vertex AI auth path on `GeminiClient`. Use `GOOGLE_SERVICE_ACCOUNT_JSON` (already in Fly secrets for GCS), prepend `gs://nova-videos-dev/` to all fixture paths, drop the per-test upload step entirely.
**Why:** v0.4.9.0 works but uploads ~17 fixtures × ~5–50MB each on every live-eval run. Vertex auth removes the cost, unifies dev/prod auth, and enables `gs://` URIs in production too (downstream value: skip the Files API upload step in `template_orchestrate` when the source already lives in GCS).
**How:** Add Vertex AI auth code path to `app/agents/_model_client.py::GeminiClient` keyed on URI shape (`gs://` → Vertex SA, `files/<id>` → Studio key). Reconcile with the existing `_get_client()` cache. Update fixtures to `gs://...` form. Document the new env var hierarchy.
**Blast radius:** changes production GeminiClient call path. Needs careful rollout — feature-flag or staged.
**Effort:** M (human: ~1d / CC: ~1h)
**Priority:** P2

### ~~Eval scaffolding for `clip_router`~~ — RESOLVED in v0.4.10.0
**Resolved:** `tests/evals/test_clip_router_evals.py` + `tests/evals/rubrics/clip_router.md` + 3 hand-authored golden fixtures shipped. Rubric dimensions: slot_type_fit, energy_match, sequence_variety, rationale_quality. Yasin-style prompt rewrite (`prompt_version=2026-05-15`) shipped in the same PR. Live-eval baseline will be established when an operator runs the suite with `--with-judge --eval-mode=live --allow-cost` from an environment with creds.

### ~~Eval scaffolding for `shot_ranker`~~ — RESOLVED in v0.4.10.0
**Resolved:** `tests/evals/test_shot_ranker_evals.py` + `tests/evals/rubrics/shot_ranker.md` + 3 hand-authored golden fixtures shipped. Rubric dimensions: rank_1_hook_strength, set_variety, description_quality, thematic_fit. Yasin-style prompt rewrite (`prompt_version=2026-05-15`) shipped in the same PR. Live-eval baseline will be established when an operator runs the suite with `--with-judge --eval-mode=live --allow-cost` from an environment with creds.

### ~~Retroactive eval scaffolding for `text_designer`~~ — RESOLVED in v0.4.11.0
**Resolved:** Full four-file scaffold shipped (test entry point, rubric with 4 dimensions, structural floor in `check_text_designer`, 4 hand-authored golden fixtures pinning the prompt's documented calibration patterns). Live-eval baseline will be established when an operator runs `pytest tests/evals/test_text_designer_evals.py --with-judge --eval-mode=live --allow-cost` with creds.

### ~~Retroactive eval scaffolding for `transition_picker`~~ — RESOLVED in v0.4.11.0
**Resolved:** Full four-file scaffold shipped. The new structural check also surfaced a latent `parse()` bug — `float(...) or 0.3` silently coerced `duration_s=0.0` to `0.3` (Python treats 0.0 as falsy), overriding every hard-cut/none output the prompt explicitly specified as duration 0.0. Fixed in the same PR.

### ~~Renderer support for `match-cut` as a distinct transition~~ — RESOLVED in v0.4.12.0
**Resolved:** `match-cut` is now a first-class transition. Renders identically to `hard-cut` at the pixel level (mapped to `"none"` in `_GEMINI_TO_INTERNAL`), but the recipe metadata now carries the editorial distinction so transition_picker's rubric can score match-cut discrimination separately. The motion-vector / OpenCV blending approach mentioned in the original TODO is filed as a P4 follow-up — the current "hard-cut at the pixel layer, editorial intent at the metadata layer" implementation is the right scope for the agent loop today.

### ~~Renderer support for `barn-door-open` as a distinct animation~~ — RESOLVED in v0.4.12.0
**Resolved:** New `apply_barn_door_open_head()` + `_generate_barn_door_bars_png_sequence()` in `app/pipeline/interstitials.py`. Same PNG-overlay strategy as curtain-close (post-PR #105), inverse bar trajectory (`bar_h: half_h → 0`), animation lives at the HEAD of slot N+1 rather than the tail of slot N. Orchestrator post-render phase dispatches to the new helper when `prev_inter_type == "barn-door-open"`. 11 unit tests cover PNG generation correctness + the inverse-of-curtain mirror property.

### ~~Promote `speed-ramp` to a first-class transition~~ — RESOLVED in v0.4.12.0
**Resolved:** `speed-ramp` is now a first-class transition value. The cut is instant (mapped to `"none"` in `_GEMINI_TO_INTERNAL`); the visible mechanic lives on the destination slot's existing `speed_factor` field (already plumbed through `reframe._build_video_filter` as `setpts=PTS/factor`). transition_picker's prompt explicitly documents the `speed-ramp` ↔ `speed_factor > 1` pairing. The schema field retirement that the original plan called for is deferred — production recipes still use `speed_factor` directly, and removing it would require a coordinated migration. Filed as a follow-up below if/when that becomes a priority.

### Deprecate `speed_factor` schema field in favor of `speed-ramp` transition (follow-up to v0.4.12.0)
**What:** Once the agents settle on emitting `transition_in: speed-ramp` consistently, retire the standalone `speed_factor` field on the slot schema. The transition value alone should carry the intent; the renderer should derive the actual playback rate from a fixed mapping (e.g., `speed-ramp → speed_factor=1.5`) or from a separate `ramp_intensity` field.
**Why:** Two fields encoding the same effect is a foot-gun. Recipes can drift into inconsistent states (`transition_in=speed-ramp, speed_factor=1.0`) that the renderer silently treats as "no ramp."
**How:** One-off audit script over `video_templates.recipe_cached` to count how many recipes use `speed_factor != 1.0` in slots whose `transition_in` is NOT `speed-ramp`. If the count is small (<10), migrate them and remove the field. If large, coordinate a deprecation window.
**Effort:** M (human: ~1d / CC: ~30 min)
**Priority:** P3

### Slim the recipe-agent prompt: drop text_overlays once manual templates adopt TemplateTextAgent (follow-up to PR #188)
**What:** Remove the text-overlay extraction instructions from `src/apps/api/prompts/analyze_template_single.txt` and `src/apps/api/prompts/analyze_template_pass2.txt` (the two recipe-agent prompt templates used by `TemplateRecipeAgent.render_prompt()`). Update `_validate_slots()` in `src/apps/api/app/agents/template_recipe.py` to stop validating `text_overlays` on slot dicts. Drop the related validators (`_validate_text_bbox`, the `text_overlays` for-loop inside `_validate_slots`, `_dedup_overlays_across_slots`, `_enforce_pct_uniformity`'s overlay leg) and any callsites that read `recipe.slots[*].text_overlays` in the manual-template path.
**Why:** PR #188 (v0.4.26.0) shipped `nova.compose.template_text` as a dedicated text-extraction agent. In the agentic build path (`agentic_template_build_task`), `template_text_extraction._merge_overlays_into_slots` already OVERWRITES `recipe.slots[*].text_overlays` with the text agent's richer output — so the recipe agent's text extraction in that path is wasted tokens. Once the manual-template build path also calls `TemplateTextAgent`, the recipe prompt is duplicating work on every template analysis. Slimming it will improve focus on slot decomposition, interstitials, transitions, and creative direction, and reduce per-call token usage.
**How:** 1) Wire `TemplateTextAgent` into the manual-template build path (separate prerequisite — no plan exists yet). 2) Once that lands and is verified in prod, edit `analyze_template_single.txt` and `analyze_template_pass2.txt` to remove the text-overlay extraction section. 3) Bump `TemplateRecipeAgent.spec.prompt_version` in `src/apps/api/app/agents/template_recipe.py`. 4) Run `pytest tests/evals/test_template_recipe_evals.py -v --with-judge --eval-mode=live` against existing fixtures and compare scores against the prior `prompt_version` run. 5) After a release cycle, drop the `text_overlays` validation logic from `_validate_slots()` and its helper functions (`_validate_text_bbox`, `_dedup_overlays_across_slots`, the overlay leg of `_enforce_pct_uniformity`).
**Effort:** ~half a day once the prerequisite manual-path wiring is done (~30 min prompt edit + schema cleanup + fixture re-export + eval comparison). The unscoped blocker is the manual-path wiring itself.
**Priority:** P2 — quality improvement, not a bug. Blocked.
**Depends on:** Manual-template build path adopting `TemplateTextAgent` (no plan exists yet — separate work).

---

## Single-pass OOM follow-ups (added 2026-05-17)

### Drop `base_output_path` on the admin-test / preview path
**What:** When `preview_mode=True` in `POST /admin/templates/{id}/test-job` (`admin.py:1099` → `orchestrate_template_job` → `_assemble_clips` → `run_single_pass`), pass `base_output_path=None`. Eliminates the `split=2` dual-output fork in `single_pass.py` and the second simultaneous libx264 encoder.
**Why:** Admin test surface OOM'd at 1.9 GB anon-RSS on a MINIMAL spec (1 input clip, 0 transitions, 0 PNGs, 2 ASS subs) on 2026-05-17 — job `022a00e4-7926-4d82-b248-16431dec543b`. The 4096 → 6144 MB `fly.toml` bump is the immediate fix; this is the structural one. The overlay-free `[base]` output exists for the production audio-mix step (`_mix_template_audio`), which the preview path doesn't run, so the second encode is wasted work AND the dominant memory consumer.
**How:** Conditional at the `run_single_pass` call site in `_assemble_clips`: `base_output_path=None if preview_mode else base_path`. Verify the post-process `shutil.copy2` branch at `single_pass.py` still works (it's the no-overlays fallback, not the preview path). Existing tests in `tests/pipeline/test_single_pass.py` already cover the single-output path. Add one regression test asserting `preview_mode=True` produces a command with exactly one `-map [vout]` block.
**Effort:** S (human: ~1h / CC: ~15 min)
**Priority:** P2

### Investigate float-buffer working set on single-pass HDR sources
**What:** Profile `_per_clip_filter_chain` on an HDR/HLG source to confirm whether `format=gbrpf32le` in the reframe chain (a 6.4× memory expansion, ~24 MB per 1080×1920 frame) is the dominant memory hog when `split=2` dual-output is active. If yes, move the format conversion AFTER the split so the fork holds 8-bit YUV not 32-bit float.
**Why:** OOM at 1.9 GB on a 1-input spec is high for an SDR source; need to confirm whether clip `files/d22usve26ly4` on job `022a00e4-...` was HDR. Pull metadata via `/admin/jobs/022a00e4-7926-4d82-b248-16431dec543b/debug` (shipped v0.4.22.0). If HDR-driven, the structural fix may eliminate the need for the 6144 MB worker bump.
**How:** (1) Read the clip's `colorspace`/`color_transfer` from the job-debug page. (2) Reproduce locally; run ffmpeg with `-progress`/`-stats` and watch RSS in `top`. (3) If confirmed HDR-driven, restructure so `format=yuv420p` happens before the `split=2` fork — currently it happens earlier in `_per_clip_filter_chain` but the float buffer may persist through the split point.
**Effort:** M (human: ~1d / CC: ~2h)
**Priority:** P2
**Depends on:** "Drop base_output_path on the admin-test / preview path" — that change may resolve the symptom on the admin surface and reduce the urgency.

### Worker memory telemetry
**What:** Wire `process_resident_set_size` (and ideally peak RSS during a render) into the worker's structured logging or Prometheus metrics. The original 2048 → 4096 fly.toml comment ended with "Revisit once `process_resident_set_size` telemetry confirms whether the bump was needed" — that telemetry never landed, so the 4096 → 6144 bump on 2026-05-17 is again "vibes-based capacity planning."
**Why:** Without RSS telemetry, every OOM is a surprise discovered by a user report. With it, we'd see the working-set trend over time and bump RAM (or apply structural fixes) preemptively.
**How:** Wrap `subprocess.run(ffmpeg, ...)` in `single_pass.py` and `reframe.py` with `psutil`-based peak-RSS sampling (one thread polling `proc.memory_info().rss` every 100ms). Log peak alongside the existing `single_pass_start`/`single_pass_done` events. Optional: emit a Prometheus gauge or a Langfuse trace metric.
**Effort:** S (human: ~2h / CC: ~30 min)
**Priority:** P3

## P0 — pre-existing test failures on main (noticed 2026-05-17)

### Fix overlay-constants snapToNearestZone zone-boundary tests
**What:** 5 failing tests in `src/apps/web/src/__tests__/admin/overlay-editor.test.tsx` under the `overlay-constants > snapToNearestZone` block. Expected `"center"` / `"top"` but got `"center-above"` at zone boundaries. Surfaced during `/ship` of the admin music Test tab branch on 2026-05-17.
**Why:** The branch I shipped doesn't touch overlay code, but these failures are on origin/main right now. Whoever introduced `center-above` as a snap zone didn't update the boundary tests. CI will fail for everyone shipping until this is fixed.
**How:** Either (a) update the tests to expect the new zone labels, or (b) revert the snap-zone change. Check `git log -p src/apps/web/src/lib/admin/overlay-constants.ts` to find the introducing commit.
**Effort:** XS (human: ~30 min / CC: ~10 min)
**Priority:** P0

---

## Music-only edits — quality gaps (added 2026-05-17)

Discovered during the `/plan-eng-review` audit for the admin Music Test tab. The
beat-sync path (`_run_music_job`) works today but has these soft edges. The Test
tab itself shipped in the same PR — these items only matter once admins start
producing real music-only edits at volume.

### Authenticate `POST /music-jobs`
**What:** Replace the `SYNTHETIC_USER_ID = 00000000-...-001` constant in `src/apps/api/app/routes/music_jobs.py:31` with `Depends(get_current_user)`. Right now any caller can POST a music job and burn Gemini quota.
**Why:** The endpoint comment already calls this out as a known MVP gap. The admin test-job endpoint (added in this PR) is admin-token-gated, so this only blocks public exposure of `/music-jobs` — but it must land before /music goes back to public users.
**How:** Mirror the `template_jobs.py` auth pattern when that lands. Single dependency swap.
**Effort:** S (human: ~2h / CC: ~15 min)
**Priority:** P2
**Depends on:** "Sign-in / auth on the new header" (above)

### Music-only output eval harness
**What:** Extend `src/apps/api/tests/evals/` with `music_assembly_evals.py`. Structural checks: every produced slot's `cumulative_s` is within 0.05s of the nearest beat in `beat_timestamps_s`; no slot's actual duration deviates from `target_duration_s` by more than 0.1s; audio track length matches video length within 0.5s.
**Why:** The Big-3 eval harness (`template_recipe`, `clip_metadata`, `creative_direction`) covers prompt agents. Music assembly is pure deterministic FFmpeg + math, but beat-snap regressions slip through every refactor of `_assemble_clips` / `_plan_slots`. A 5-min smoke job + ffprobe-based structural checks would catch them.
**How:** Replay-mode fixture is a `(beat_timestamps, track_config, clip_durations)` tuple → assembled-slot ranges. Live mode renders a real video and probes the output. Reuse the `eval_mode` flag from existing harness.
**Effort:** M (human: ~1d / CC: ~45 min)
**Priority:** P2

### End-to-end test for `_run_music_job`
**What:** New pytest under `src/apps/api/tests/tasks/test_music_orchestrate_e2e.py` that walks a real beat-sync flow through `_run_music_job` with a fixture track (mocked Gemini, real FFmpeg, tmp clips). Asserts the job ends in `music_ready` and `assembly_plan.output_url` is a non-empty string.
**Why:** Today the only coverage is route-level validators. A regression in `generate_music_recipe`, `match`, or `_assemble_clips` for the music path would only surface in prod or manual admin testing.
**How:** Generate 3 short tone-clips via `ffmpeg lavfi` in the fixture, mock `_upload_clips_parallel` + `_analyze_clips_parallel` to return canned `clip_metas`, run the orchestrator end-to-end against a tmp GCS bucket.
**Effort:** M (human: ~1d / CC: ~40 min)
**Priority:** P2

### Music recipe: transition vocabulary beyond `cut`
**What:** `generate_music_recipe()` in `src/apps/api/app/pipeline/music_recipe.py:63` hardcodes `"transition_in": "cut"` for every slot. The infrastructure in `transitions.py` already supports whip-pan, flash-cut, zoom-in, dissolve — the recipe just never asks for them.
**Why:** Most viral music edits punch transitions on the beat (whip on the snare, flash on the kick). Cut-only output looks flat next to organic refs.
**How:** Add `transition_style` to `track_config` (one of `cut` | `whip` | `flash` | `mixed`) and let the recipe pick per-slot transitions based on the song's section labels (already on `MusicTrack.best_sections`).
**Effort:** M (human: ~1d / CC: ~45 min)
**Priority:** P3

### Music recipe: speed ramps / slow-mo
**What:** `speed_factor` in `_plan_slots` defaults to 1.0 for music recipes. No way to drop to 0.5x on a drop or 2x on a build.
**Why:** Speed contrast is a core music-edit lever. Beat-sync without speed ramps reads as mechanical.
**How:** Map `MusicTrack.best_sections[*].energy` ("peaks_high" / "high" / "medium" / "low") to a per-slot speed_factor curve in `generate_music_recipe`.
**Effort:** M (human: ~1d / CC: ~45 min)
**Priority:** P3

### `_assemble_clips` Phase 3 short-circuit for music-only
**What:** Add `skip_overlays: bool = False` parameter to `_assemble_clips()` in `src/apps/api/app/tasks/template_orchestrate.py:1663`. When `True`, skip the curtain-close, interstitial-insert, and overlay-merge loops (`music_orchestrate.py:425–426` already passes `interstitials=[]` and `user_subject=""`, so Phase 3 currently runs as a series of no-ops).
**Why:** Marginal CPU win, much cleaner code. Makes the music-only path readable as a path rather than as a sequence of empty branches.
**How:** Wrap lines 1758–1894 in `if not skip_overlays:` and add a fast-path collect of `reframed_paths` + `slot_durations`. Pass `skip_overlays=True` from both `_run_music_job` and `_run_templated_music_job`.
**Effort:** S (human: ~3h / CC: ~25 min)
**Priority:** P3

### Clip-shorter-than-slot policy
**What:** Document and test what happens when a clip is shorter than its assigned slot's `target_duration_s` — does `_plan_slots` loop the clip, freeze on the last frame, or trim the slot? Today this is implicit FFmpeg behavior.
**Why:** Admin testing the Music tab will hit this with short test clips. Silent fallback = bad output without a clear failure.
**How:** Probe each clip's actual duration in `_plan_slots` and either (a) reject with 422 at submit, (b) trim the slot to clip length and compensate elsewhere, or (c) loop the clip. Pick one policy, document it, test it.
**Effort:** S (human: ~3h / CC: ~30 min)
**Priority:** P2

### Tighten `_validate_clip_count` for beat-sync tracks
**What:** Today `_validate_clip_count` in `src/apps/api/app/routes/music_jobs.py:109` defaults `required_clips_min=1`, `required_clips_max=20` for beat-sync tracks. The "correct" count is `beat_count / slot_every_n_beats`, but the validator doesn't enforce it — so admins can submit any 1–20 clips and the assembler will silently truncate or repeat.
**Why:** Wrong clip count → silently wrong output. Should surface at submit time, not playback time.
**How:** During beat analysis, write `required_clips_min == required_clips_max == slot_count` into `MusicTrack.track_config`. Drop the 1/20 default fallback.
**Effort:** S (human: ~2h / CC: ~20 min)
**Priority:** P2

### Cap music-only output at 60s
**What:** Per CLAUDE.md domain context, target output is sub-60s. The beat-sync path doesn't enforce this — if a track's `best_section` is 75s, the output is 75s.
**Why:** Over-spec videos are uploaded to TikTok/Reels and silently rejected or auto-trimmed by the platform. The pipeline should refuse to produce them.
**How:** Clamp `best_end_s - best_start_s ≤ 60` in `_auto_best_section()` (`src/apps/api/app/services/audio_download.py`). Show a warning in the admin Config tab when the saved section exceeds 60s.
**Effort:** S (human: ~2h / CC: ~15 min)
**Priority:** P2

### Programmatic audio-mix QA on music-only output
**What:** After `_mix_template_audio` finishes in `_run_music_job`, run an ffprobe loudness measurement on a 1-second window every ~5 seconds of the output. If any window is silent (< -50 dBFS RMS), mark the job as `music_ready_warning` and surface the gap in the admin Test tab.
**Why:** Silent-failure mode: if `_mix_template_audio` produces a video with the audio track muted, out-of-sync, or only partially looped, there is no automated check today — admins only catch it by ear, and the public viewer would ship a broken video.
**How:** New helper `probe_audio_loudness(path)` in `app/pipeline/audio_qa.py`. Call after the mix step, write `assembly_plan.audio_qa = { peak_dbfs, silent_windows: [...] }`. Render a warning chip in `TestTab.tsx` when `silent_windows` is non-empty.
**Effort:** M (human: ~1d / CC: ~45 min)
**Priority:** P2

### Revisit LRCLIB 429 backoff schedule after 1 week of prod observation
**What:** Re-evaluate `_RETRY_DELAYS_S = (0.5, 1.5, 4.5)` in `src/apps/api/app/services/lrclib_client.py:77`. If `lrclib_rate_limited_retrying` log events show workers consistently being pushed to the 4.5s tier, the initial base delay is too short — bump to `(1.0, 3.0, 7.0)` or larger. If retries almost never fire, the current schedule is fine.
**Why:** LRCLIB is keyless and rate-limits by IP. Our worker pool (4 shared CPUs on Fly) can fire several `analyze_music_track_task` jobs concurrently during admin batch uploads, and a too-aggressive schedule means we burn retry budget unnecessarily while a too-conservative schedule means real songs silently degrade to whisper-only.
**How:** After ~1 week post-deploy (shipped 2026-05-20 in PR #256 → merge commit `475a2e7`):
1. `fly logs -a nova-video | grep lrclib_rate_limited_retrying | jq '.attempt' | sort | uniq -c` — distribution of which retry attempt fired.
2. If `attempt:3` dominates → bump base delays.
3. If retries rarely fire (< 1% of `lrclib_lyrics_fetched` events) → leave alone.
4. Document the observed distribution in this TODO when you close it.
**Depends on:** PR #256 having been in prod for ≥ 7 days with non-trivial music-track upload traffic.
**Effort:** S (human: ~30 min observation + tune / CC: ~10 min once data is collected)
**Priority:** P3

---

## Layer-2 Progressive Reveal — follow-ups (added 2026-05-21)

Captured from Lanes A-G of `feat/layer2-progressive-reveal-2026-05-21` (v0.4.39.0). The progressive word-by-word reveal feature shipped; these are the explicitly-deferred parts of the original plan.

### Frontend overlay editor exposes `text_anchor`
**What:** Add a `text_anchor` dropdown (left / center / right) to the overlay editor at `/admin/templates/[id]` — `PropertyPanel.tsx`. Backend already writes the field through `_overlay_to_recipe_dict`; the editor reads it generically but offers no way to set it.
**Why:** Admins editing overlays today can't switch a static caption to left-anchor for visual testing without editing JSON. The cumulative reveal sets this automatically, but manual overlays can't benefit from the same anchor mode.
**How:** Add to `PropertyPanel` overlay form, wire to `recipe-types.ts`, send via `PATCH /admin/templates/{id}`. The orchestrator already round-trips the field.
**Effort:** S (human: ~1h / CC: ~10 min)
**Priority:** P3

### `_draw_spans_png` honors `text_anchor`
**What:** Extend `app/pipeline/text_overlay.py:_draw_spans_png` to accept the `text_anchor` parameter and apply it to the spans layout (font-cycle, karaoke). Currently the spans renderer silently ignores `text_anchor` — set the field on a spans overlay and nothing changes.
**Why:** Cumulative reveal doesn't use spans, so this didn't matter in Lanes A-G. But a future caller (e.g. a karaoke template that wants left-anchor) would see the field ignored without warning.
**How:** Mirror the `_draw_text_png` anchor logic in the spans path. Center-anchored spans (the existing path) stay unchanged.
**Effort:** XS (human: ~30 min / CC: ~5 min)
**Priority:** P3

### Spatial+temporal OCR clustering for visual-only labels
**What:** Extend line-grouping in `app/pipeline/text_overlay_v2/line_grouping.py` to form groups from spatial+temporal OCR clustering when no transcript match exists. Today: phrases without a transcript word fall through to today's per-phrase emit (no progressive reveal). For visual-only label templates ("PERU", "RULE OF THIRDS"), this means progressive reveal never kicks in.
**Why:** Some templates have on-screen text that builds up word-by-word without spoken audio. The current line-source picker is transcript-only by user choice; expanding to spatial fallback gates on real templates wanting it.
**How:** Add a second grouping pass: cluster atomized phrases by proximity in y (same row) + monotone-rightward x + temporal proximity. Existing transcript-match groups take precedence; spatial groups fill the remainder. Add `LineGroup.source: "transcript" | "spatial"` so debug tab attributes which path each line took.
**Effort:** M (human: ~1 day / CC: ~1h)
**Priority:** P3 — only do this when a real visual-only template needs reveal.

### Hand-label `fdaf3bbc.json` ground truth
**What:** Hand-label `src/apps/api/tests/fixtures/agent_evals/template_text/ground_truth/fdaf3bbc.json` so the eval harness can quantitatively score "Not just luck" instead of just confirming overlays render with the expected shape.
**Why:** Without ground truth, the Layer-2 evals fall back to qualitative inspection on this canary template. The design doc at `~/.gstack/projects/emirerben-nova/template-cache-id-fix-design-text-overlay-strategy-20260520.md` calls this out as Phase 1 of the broader measurement loop.
**How:** Run `scripts/build_text_ground_truth.py` for a tesseract first pass, then hand-correct from the prod video.
**Effort:** S (human: ~2h / CC: not applicable — human in the loop)
**Priority:** P2 — gates honest measurement of progressive-reveal quality on prod templates.

### Stage E `dropped_count` per-reason attribution
**What:** Today the stage_e_summary event reports `phrases_dropped` as a single bucket. Split it by reason: `transcript_unmatched`, `line_count_mismatch_fallback`, `sanitizer_emptied`, `rebuild_failed`. Pure logging change.
**Why:** Future debugging of "why was this phrase dropped at Stage E" requires per-reason logging. The current bucket forces operators to grep raw logs to attribute.
**How:** Add a per-reason counter inside `TextAlignmentAgent.parse()` mirroring Stage G's `drops` dict pattern.
**Effort:** XS (human: ~30 min / CC: ~5 min)
**Priority:** P3 — forward-looking; only do this when leakage debugging actually needs it.

---

## Vercel Frontend Deploy (added 2026-04-06)

All items completed 2026-04-06:
- ~~Regex CORS for preview deployments~~ — done via `allow_origin_regex` in `main.py`
- ~~Connect Vercel GitHub Integration~~ — done via REST API
- ~~Disable Deployment Protection~~ — done, preview-only
- ~~Update Google OAuth Redirect URIs~~ — done in Google Cloud Console
- ~~GCS bucket CORS~~ — done via `gsutil cors set`

## Cache invariants

### Lint check: bump `AgentSpec.prompt_version` when agent code touches prompts/schemas
**What:** The content-hash Layer-2 cache key (`compute_text_overlay_version` in `src/apps/api/app/pipeline/template_cache.py`, added 2026-05-23) is derived from the prompt-file bytes + schema-module bytes + `AgentSpec.prompt_version` strings + a settings dict. **Gap:** edits to an agent's Python file that don't touch its prompt file and don't bump `prompt_version` will NOT invalidate the cache — silent stale recipes in prod. Add a CI lint that fails a PR when `src/apps/api/app/agents/<name>.py` changes without a corresponding bump to that agent's `AgentSpec.prompt_version` value.
**Why:** Documented as a known gap when T1 (Lane A) shipped content-hashed invalidation; the plan's failure-modes registry called this out as the "critical gap" in the narrow-hash approach. Mitigation deferred to this lint check so the wave-1 cache change could ship without scope creep. Without it, the cache claims correctness it can't guarantee whenever an agent's parse/validation logic moves.
**How:** Touched-files check in a new `.github/workflows/require-prompt-version-bump.yml` (or fold into the existing `agent-evals.yml`). When `src/apps/api/app/agents/<agent>.py` is in the PR diff, parse the file's `AgentSpec(...).prompt_version` value at both base and head; fail if equal. Escape hatch: `[skip-prompt-version-check] <reason>` in PR body (parallels the eventual T8 escape hatch). Cover the runtime path (`_runtime.py` itself) too — edits there can change every agent's behavior without touching any single agent file.
**Effort:** S (human: ~1h / CC: ~20 min)
**Priority:** P2
**Depends on:** none (the cache change itself is already merged; this just closes the known gap)

## Lyric pipeline — architectural debt (from PR plans/geli-me-var-ama-hatalar-robust-reddy.md)

### Lyric injection runs on stale pre-snap slot durations
**What:** `inject_lyric_overlays` ([app/tasks/music_orchestrate.py](src/apps/api/app/tasks/music_orchestrate.py): _run_music_job:543, _run_templated_music_job:1074) runs BEFORE `_assemble_clips → _apply_beat_snap`. The splitter (`_inject_line` in [app/pipeline/lyric_injector.py](src/apps/api/app/pipeline/lyric_injector.py)) clamps segments to PRE-snap slot durations. Beat-snap then re-times slot boundaries by up to ±beat-interval/2 (~221 ms on the empirical Bug A job 14ded08a, can hit ~500 ms on slower tracks). The merged-overlay end stays stale and would not touch the next segment's snapped start — handled today by the Layer 1 identity-driven merge band-aid in `_collect_absolute_overlays._consolidate_lyric_segments`.
**Why:** Layer 1 merge is correct but it COMPENSATES for a structural defect (stale data). Layer 2 audible-window finalization also has to derive the post-snap audio window from `_collect_absolute_overlays`'s context for the same reason. Moving injection to AFTER beat-snap eliminates the gap entirely (segments touch with zero drift) and lets Layer 2 receive an exact `AudioWindow` at injection time instead of deriving it post-hoc.
**How:** Extract the post-snap slot-duration computation from `_assemble_clips → _apply_beat_snap` so the orchestrator can call it BEFORE `inject_lyric_overlays`. Either (a) split `_assemble_clips` into prepare + render phases with the snapped durations as a hand-off, or (b) move `inject_lyric_overlays` calls INTO `_assemble_clips` after the snap. **Do NOT delete Layer 1's identity-based merge** — it is semantically correct (same `lyric_line_id` ⇒ one line ⇒ one overlay) and remains the regression detector. Tighten `_LARGE_CONTINUATION_GAP_WARNING_S` from 0.5 → ~0.05 once drift is engineered out.
**Effort:** M (human: ~1 day / CC: ~2-3 hours)
**Priority:** P1

### Karaoke variant beat-snap drift desynchronizes word highlight
**What:** `_inject_karaoke` ([app/pipeline/lyric_injector.py:_inject_karaoke]) does NOT split across slots — one overlay per line, clamped to one slot via `_slot_for_time(line.start_s, windows)`. So the Layer 1 `lyric_line_id` merge does NOTHING for karaoke. BUT beat-snap drift up to one beat-interval shifts the overlay's `abs_start` by the slot drift, while `word_timings` inside the overlay are pinned relative to the overlay's own start. Result: per-word highlight runs N ms early or late against the actual vocal. On a 2.4 BPS track (~250 ms beat interval), 200+ ms drift is a full beat off-sync.
**Why:** Karaoke is shipped today and silently misaligned. The user has been training their visual expectations against the wrong sync. The longer this sits, the more recordings drift before someone notices something feels "off."
**How:** Either (a) re-anchor word timings against post-snap audio positions inside `_collect_absolute_overlays` (small surface, doesn't touch injection), or (b) inject karaoke post-snap as part of TODO 1 (subsumes this). Add a regression test that loads a known karaoke track with intentional ≥200 ms beat-snap drift on the line's containing slot and asserts per-word highlight crossings land within ±50 ms of song-time word boundaries. The test should FAIL today and pass after the fix.
**Effort:** S (human: ~half day / CC: ~1 hour)
**Priority:** P1 — STRICT

### Skia lyric-line lacks fade animation (renderer parity violation)
**What:** `text_overlay_skia.py:_ANIMATED_EFFECTS_SKIA` does NOT include `"lyric-line"`, so the music path (which uses Skia per PR #319) renders lyric-line as a static PNG that hard-cuts in and out. `fade_in_ms` / `fade_out_ms` on the overlay dict are IGNORED. The Pillow path (`_emit_lyric_line_alpha_tags` in `text_overlay.py:807`) honors them via ASS `\alpha` keyframes. Two renderers, two looks for the same overlay dict. Violates the CLAUDE.md renderer-parity invariant. The user's prod render and the lyrics-preview job they liked render visibly differently because of this.
**Why:** Lyric-line fade IS user-visible (250 ms fade-out at the end of every line is part of the YouTube-lyric-video feel PR #287 tuned in). Cutting it on Skia makes lyrics feel abrupt where on Pillow they feel polished. Eventually all rendering goes through Skia; this gap will compound.
**How:** Add `"lyric-line"` to `_ANIMATED_EFFECTS_SKIA`. Implement an alpha animation in `_draw_with_animation` consuming `fade_in_ms` / `fade_out_ms` from the overlay dict (linear or ease-out cubic, match libass's `\alpha` behavior). Lock by a renderer-parity test that runs the same lyric overlay through BOTH renderers and compares per-frame alpha at start, start+fade_in, end-fade_out, end.
**Effort:** S (human: ~half day / CC: ~1 hour)
**Priority:** P2

### Admin UI: snap `best_start_s` / `best_end_s` to lyric-line boundaries
**What:** Layer 2's runtime audible-window guard ([app/pipeline/lyric_injector.py:_finalize_lyric_audible_window]) silently corrects admin-set bounds that don't align with lyric line edges. The runtime fix is correct but suboptimal UX. A "snap to nearest lyric boundary" affordance on the music-track admin (`src/apps/web/src/app/admin/music/[id]/`) would show admins at config time which lines get included.
**Why:** Defense in depth is correct; making misconfiguration unlikely at the source is better. Today admins set bounds blind and runtime decides what survives — surface this earlier.
**How:** In the LyricsTimingPanel (or wherever `best_start_s`/`best_end_s` are edited), add buttons "Snap start to previous line / next line" and "Snap end to previous line / next line." Compute snap targets from `MusicTrack.lyrics_cached.lines` (the same source the runtime guard uses). Show a preview list of which lyric lines will be included with the proposed bounds.
**Effort:** S (human: ~half day / CC: ~1 hour)
**Priority:** P3

### LyricsTimingPanel: per-field dirty tracking + "auto" placeholders for untouched fade fields
**What:** After PR #344 (§F of plans/mirea-we-ve-lost-memoized-shannon.md), the admin Test tab's `LyricsTimingPanel.tsx` fade_in_ms / fade_out_ms sliders only affect solo / last-line fades and the kill-switch-off legacy path. The sliders are now labeled "Fade in (solo / legacy only)" and a small inline note explains the contract, but the form still submits every field on every render (form defaults included). That's how PR #343 was empirically tricked into thinking the operator pinned form defaults as overrides. Today the post-pass ignores these for inter-line transitions, so the bug is dormant — but the UX is still misleading.
**Why:** Per-field dirty tracking removes a whole class of "the operator didn't pin this, the UI did" misunderstandings — both for humans reading the `lyrics_config_effective` on a Job row and for future scheduler code that might re-add cfg-key-based logic. Also shrinks the saved Job blob.
**How:** In `LyricsTimingPanel.tsx`:
  1. Track per-field `dirty` state (initially false, flips true on first user change).
  2. In `onSubmit` and `saveDefaults`, only include fields where `dirty[key] === true`.
  3. Display "auto" placeholder text on untouched fields (faded zinc) instead of the slider's numeric default.
  4. Add a "Reset to auto" affordance that clears dirty state for a field (round-trip back to "auto").
  5. Backend `LyricsConfigOverride` schema in `src/apps/api/app/schemas/lyrics_config_override.py` already accepts Optional fields, so no API change is required.
**Effort:** S (human: ~2-3h / CC: ~30 min)
**Priority:** P3

## Generative edits — render-speed levers (added 2026-06-01)

Surfaced by prod generative job `d30c61fe-dab3-417d-998a-3a81535f7b50`, which sat ~30 min on "Analyzing your clips" before freezing. Root cause: the HDR→SDR pre-tonemap (`_pretonemap_hdr_clips`, `src/apps/api/app/tasks/generative_build.py`) ran ~31× slower than realtime on Fly shared CPUs (an 8-min tonemap for a 15s clip) — consistent with CLAUDE.md's documented 70-123s/slot HDR cost. The bounded-parallel tonemap + fail-fast-on-timeout fix (branch `feat/generative-tonemap-timeout-2026-06-01`) halves wall-clock and stops the silent freeze, but does NOT get heavy-HDR jobs near the ~2-min target (SDR jobs already land ~100s to first variant). These three levers attack the remaining wall. They compound — biggest single win is moving the tonemap off shared CPUs (separate infra item), but these are the in-pipeline levers.

### Tonemap only the footage the variants actually use, not whole clips
**What:** `_pretonemap_hdr_clips` converts the *entire* uploaded clip to an SDR intermediate up front (job `d30c61fe`: ~79s of footage across 7 clips). A montage only uses a fraction of each clip, so we tonemap footage that never reaches the output. Reorder so the per-variant recipes (slot source-time ranges) are computed first, take the union of used ranges per clip, then tonemap only those ranges once.
**Why:** Tonemap cost is per-frame, so tonemapping unused footage is pure waste at ~31× realtime. On clips where used << total this is a direct multiplier on the dominant cost. The current design (tonemap up front, before any recipe) was chosen to share one SDR intermediate across all 3 variants and avoid 3× tonemapping — this keeps that win while dropping the unused-footage tax.
**How:** In `_run_generative_job` (`src/apps/api/app/tasks/generative_build.py`), the pre-tonemap currently runs in Stream A *before* `_resolve_archetype` and variant recipe generation. Move recipe construction (`generate_music_recipe` for song variants, `_build_no_music_recipe` for original/voiceover) ahead of the tonemap, union each clip's used source-time ranges across all variant recipes, then trim-and-tonemap only those ranges (frame-accurate `-ss`/`-to` before the `_ZSCALE_SDR_PIPELINE` `-vf`). The per-slot reframe then seeks within the trimmed SDR intermediate — verify the seek offsets stay correct after trimming. Judgment call: if the 3 variants use largely disjoint ranges, the union can approach the whole clip and the win shrinks — measure used-vs-total on a few real jobs before committing to the refactor.
**Effort:** M (human: ~2-4d / CC: ~1-2h)
**Priority:** P2 — speed is a UX problem, not launch-blocking; do after confirming used<<total.
**Depends on:** none (but see "cap source resolution" below — they touch the same tonemap call site, sequence them).

### Render the 3 generative variants in parallel across workers
**What:** `_render_spec_set` in `_run_generative_job` renders variant specs one at a time in a single Celery task on a single worker. Fan the 3 variants out as independent units (after the shared pre-tonemap completes once) and join in a finalize step.
**Why:** The render phase is ~5-10 min of the budget (per-slot reframe + overlay burn + encode × 3, serial). Parallelizing could cut it toward 1× a single variant. The per-variant persist + resume infra and a `regenerate_generative_variant` task already exist, so the building blocks are there.
**How:** Restructure as: (1) one task does ingest + pre-tonemap + agents + recipes, persists shared state; (2) a Celery `group`/`chord` fans out one render task per variant; (3) a finalize callback runs `_finalize_job`. **Hard dependency:** `fly.toml` runs workers at `--concurrency=1` with a single worker machine ("one task at a time"), so fan-out on today's infra just re-serializes on the same CPU — this only pays off with more worker machines or higher concurrency (and shared-CPU concurrency>1 risks the OOM class from the 2026-05-17 incident). Cross-references the "single-worker bottleneck" noted in the generative speed work. Reuse the variant-resume guard so a redelivered fan-out task doesn't double-render.
**Effort:** M (human: ~3-5d / CC: ~1-2h)
**Priority:** P2 — meaningful only alongside worker-pool scaling.
**Depends on:** worker-pool scaling (more `worker` machines, or `--concurrency>1` on dedicated CPUs) — without it this is inert.

### Cheaper / adaptive HDR tonemap + cap source resolution before zscale
**What:** Reduce the per-frame tonemap cost for HDR sources, via (a) capping source resolution (e.g. scale to a max long-edge) *before* the expensive linear-light stage, and/or (b) an adaptive tonemap that only uses the full anti-banding chain on banding-prone content.
**Why:** Per-frame cost is the fundamental enemy (~1s compute per frame at 4K on shared CPU). 4K HDR is ~4× the pixels of 1080p; the current `_ZSCALE_SDR_PIPELINE` reads full-res float frames. This is the most direct lever on the per-frame number, but it is quality-sensitive.
**How:** `_ZSCALE_SDR_PIPELINE` lives in `src/apps/api/app/pipeline/reframe.py` and is reused verbatim by `_pretonemap_hdr_clips` for color parity. It is a deliberately expensive chain (linear-light lanczos downscale + mobius tonemap + error-diffusion dither, crf16) — the v0.4.45.7 sky-banding fix (see DECISIONS.md). **Do not weaken it blind:** any change MUST be A/B'd against banding-prone footage (sky/gradient) using the local docker render path (zscale isn't in host ffmpeg — see the "reframe encode local A/B test" approach: one-shot `nova-render-api:local` docker run). Option (a) cap-res is the lower-risk half (prepend a cheap `scale` to e.g. 1440p long-edge before the linear-light downscale; at most a small detail loss, the output is 1080-tall anyway) and could ship as S on its own. Option (b) adaptive tonemap (cheap hable/reinhard unless gradient content detected) is the riskier half. Keep crf16 for the dithered generation regardless.
**Effort:** M (human: ~2-4d / CC: ~1-2h) — cap-res alone is S (~half day) if shipped without the adaptive path.
**Priority:** P2 — most direct per-frame win, gated on banding A/B not regressing.
**Depends on:** a tonemap-quality A/B check (local docker render of sky/gradient footage) before merge.

### Fix `test_active_font_renders_with_ass` smoke test on FFmpeg 8.1.1
**Completed:** v0.4.75.3 (2026-06-04) — detected libass-capable ffmpeg-full via `_find_libass_ffmpeg()`; escaped paths with `escape_ffmpeg_filter_path()` matching `single_pass.py`. All 33 font parametrize cases pass.

## Loading progress system — follow-ups (added 2026-06-06)

- [ ] **Author DESIGN.md via /design-consultation** — codify the loading system's reusable rules (D6 truth rules, D13 mood tiers, D14 motion constants, D15 host-owns-surface) right after implementation while decisions are fresh.
- [ ] **SSE for generative job status** — extend the template `/events` SSE pattern + `useJobStream` to generative so variant arrivals and the D12 climax land instantly instead of up to 2s late; sanity-check connection capacity on the 512MB API VM first.
- [ ] **Baseline refresh from real phase data** — extend `scripts/aggregate_phase_timings.py` with a `phase_log` DB reader and refresh `app/services/phase_baselines.py` from prod percentiles once PR2's instrumentation has soaked (~1–2 weeks of generative jobs).

## Landing page design system

### ~~Create DESIGN.md via /design-consultation~~ — RESOLVED v0.4.83.1
**What:** Codify the Nova landing/product design system — cream `#fafaf8` background, lime-600/lime-50/lime-200 accent, olive/ink `#0c0c0e` text, Playfair Display for editorial serifs (`font-display`), `rounded-2xl border border-zinc-200 shadow-sm` card tokens, anti-slop rules (no candy gradients, no rainbow palettes, editorial restraint). Run `/design-consultation` to produce `DESIGN.md` with the full token set, usage rules, and calibration examples.
**Why:** Three consecutive design review sessions have reverse-engineered the same token set from `page.tsx` and memory entries. A DESIGN.md (or equivalent) means future `/plan-design-review` runs calibrate against a stated system, not guesswork.
**How:** Invoke `/design-consultation` with the current landing + product pages as input. Persist the result as `DESIGN.md` (or `docs/DESIGN.md`) at the repo root. Reference from CLAUDE.md.
**Effort:** XS (CC: ~15min)
**Priority:** P3

### Normalize DESIGN.md ledger drifts #2/#5/#6
**What:** Three low-effort cleanup items from the known-deviations ledger in `DESIGN.md §10`: (1) stray product radius values (`rounded`, lone `rounded-2xl`) → `rounded-lg` surfaces / `rounded-full` buttons; (2) remove dead Montserrat 800 `@import` from `globals.css` (dead font download on every page view); (3) collapse 6 eyebrow `letter-spacing` values to 2 canonicals (`tracking-[0.24em]` landing, `tracking-[0.14em]` product micro-labels). Source: `DESIGN.md §10`.
**Why:** Montserrat alone is a free perf win; radius and tracking drift silently copy into new components until the ledger is cleared.
**How:** Grep-and-replace opportunistically during nearby UI work; no isolated PR needed. Pick one drift at a time.
**Effort:** S (CC: ~10min per drift)
**Priority:** P3

## Shot-slot uploader — follow-ups (added 2026-06-08, v0.4.93.0)

### Signed-GET clip thumbnails + persisted duration (TODO-1 / D18)
**What:** Authenticated `GET /plan-items/{id}/clip-thumbnail?gcs_path=…` returning a short-TTL signed URL (or a server-extracted poster frame), so the filled-on-reload state can show a real thumbnail and persisted duration instead of the chip-led row.
**Why:** D9 ships chip-led reload (no image well) because clip GCS paths are private and the local object URL/duration are gone after reload. A signed-GET endpoint closes that gap and makes reload visually identical to fresh-upload.
**How:** New authed `GET /plan-items/{id}/clip-thumbnail?gcs_path=…` in `routes/plan_items.py` using `storage.py` signed-URL helper. Persist duration server-side (probe at attach or extract on demand). Reuse `users/{user_id}/plan/{item_id}/` prefix for path validation. Frontend: ShotSlotUploader post-reload state renders thumbnail + duration from API response.
**Effort:** S (human: ~half day / CC: ~30min)
**Priority:** P3
**Depends on:** `clip_assignments` (shipped v0.4.93.0). See plan file `we-recently-changed-the-lively-hollerith.md` TODO-1.

### Per-shot conformance verdicts (TODO-2 / D19)
**What:** Extend `ConformanceFeedbackAgent` to judge EACH shot-assigned clip against its own shot brief (`what`/`how`), surfacing a per-slot conformance chip rather than one aggregate verdict.
**Why:** This plan ships conformance as a single aggregate verdict from the first shot-assigned clip. With stable `shot_id` + `clip_assignments` now available, per-shot verdicts become possible and give the user actionable, slot-level feedback.
**How:** Pass shot-keyed clip list to `ConformanceFeedbackAgent`. Extend `conformance` JSONB schema to `{per_shot: [{shot_id, verdict, summary}], aggregate: ...}`. Add per-slot conformance chips to `ShotSlotUploader`. Feature flag: `conformance_feedback_enabled` already defaults `False` (dark in prod) — safe to extend behind the same flag.
**Effort:** L (human: ~2 days / CC: ~1h)
**Priority:** P3
**Depends on:** `shot_id` + `clip_assignments` (shipped v0.4.93.0). See plan file `we-recently-changed-the-lively-hollerith.md` TODO-2.

## Word-cluster intro (shipped) — follow-up work

### Narrative caption arc across body clips (the other half of the reference aesthetic)
**What:** The juliakursten reference edit (TikTok 7635975632727379222, analyzed 2026-06-10) pairs the word-cluster intro with a NARRATIVE caption arc over the body clips: small italic one-liners at the bottom of each segment that tell a story across the whole edit ("I don't have a favorite place" → "I have my favorite people" → "that place becomes my favorite"). We shipped the cluster intro; the arc is the remaining piece.
**Why:** The arc is what makes the reference feel authored rather than assembled — it converts a montage into a story. Biggest remaining aesthetic gap vs top travel/lifestyle edits.
**How:** New writer output (per-clip caption list grounded in clip_metadata order, 4-8 words each, italic serif `Instrument Serif`, position "bottom"), injected per slot in `orchestrate_generative_job` (same `text_overlays` mechanics as the cluster — no renderer change). Reuse `intro_cluster`-style deterministic timing (caption spans its slot, 0.3s fade). Needs: an agent (extend intro_writer or a sibling `narrative_writer`), per-slot injection in `_render_generative_variant`, persistence for retext, and a `GENERATIVE_NARRATIVE_CAPTIONS_ENABLED` kill switch. Gemini-text-leak rules apply: captions are LLM-authored from clip understanding (same trust boundary as intro_writer, NOT the forbidden metadata-to-screen path).
**Depends on:** cluster-intro PR merged; eval fixtures for the new writer.
**Effort:** L (CC: ~2h)
**Priority:** P2

### Auto-scrim for editorial text on busy footage (from 2026-06-12 design review, D7)
**What:** Luma/variance check behind each editorial text region; composite a subtle dark gradient scrim only when the region is too busy/bright for white text + shadow.
**Why:** The editorial sequence ships with white text + stronger shadow (reference look). On busy footage (e.g. the restaurant clip on plan item 4a1a7616) thin Playfair Regular and Great Vibes strokes lose contrast in bright/cluttered regions — readability fails even when typography is right.
**How:** Per-scene region sample in the Skia burn path (mean luma + variance over the block bbox); above threshold, draw a radial/linear scrim (≤20% black) under the text before glyphs. Tune thresholds + scrim aesthetics via `make verify-overlays` montages; needs its own design pass to avoid slapped-on-gradient look.
**Effort:** M (CC: ~1h)
**Priority:** P2
**Depends on:** editorial-sequence PR merged (provides the scene/block structure to measure against).

## Media overlay — follow-ups (from instant-preview PR, v0.5.3.0)

### T-OVERLAY-1 — Instant CSS preview for instantEligible variants
**What:** When a variant satisfies `isInstantEditEligible`, the hero renders `LiveEditPreview` (not `Hero`). The `overlayCards`/`localPreviewUrls` state from the instant-preview PR is only passed to `Hero`. Uploading overlay cards on an `agent_text`/`none` variant with `base_video_url` shows no CSS preview. Fix: pass `overlayCards` + `localPreviewUrls` to `LiveEditPreview` and add the same CSS overlay `<div>` stack inside it.
**Why:** Deferred because `media_overlays_enabled` defaults to `false` (feature is dark). Adversarial review (both Codex passes) identified this before it could reach users.
**How:** Extend `LiveEditPreview` props with `overlayCards?: MediaOverlay[]` + `localPreviewUrls?: Record<string,string>`. Render the same `previewableCards.map(card => <div style={{position:'absolute', ...}}>)` stack inside `LiveEditPreview`'s video container.
**Effort:** XS (CC: ~10 min)
**Priority:** P2 — must fix before flipping `media_overlays_enabled=true` in prod

## Sound effects — follow-ups (from SFX deferred-burn / Apply-removal review, 2026-06-29)

### T-SFX-1 — Unit tests for `useSfxPreview` (live-preview audio hook)
**What:** `src/apps/web/src/app/plan/_components/useSfxPreview.ts` has zero tests, yet after the Apply-button removal it is the SOLE feedback path confirming a sound effect "works" before download.
**Why:** A silent regression (wrong offset, effect not playing, not stopping on pause/seek) would make the feature look broken with no automated catch. Deferred here because this PR does not modify the hook itself.
**How:** Mock `HTMLMediaElement` (`play`/`pause`/`currentTime`/`volume`). Assert: each placement's audio is positioned at `video.currentTime - at_s`; plays/pauses/seeks in lockstep with the video element; applies `gain` as volume (clamped 0–2); schedules a future-start via `setTimeout` when the playhead is before `at_s`; clears timeouts + pauses on `pause`/`ended`/unmount. File under `src/apps/web/src/__tests__/plan/`.
**Effort:** S (CC: ~25 min)
**Priority:** P2 — pre-existing gap; raise priority if preview bugs surface.

### T-SFX-2 — Compose SFX with an uncommitted instant-text/caption edit on one Download
**What:** `handleDownload` (`src/apps/web/src/app/plan/items/[id]/page.tsx`) bakes overlay-first, then SFX-only, then the instant-edit commit — each branch `return`s. If a variant has BOTH unbaked SFX changes AND an uncommitted instant-text/caption draft, one Download click bakes SFX and returns; the text draft isn't committed until a second click. Worse, the instant-text re-render has no SFX-reapply hook the way the overlay pass does (`_reapply_persisted_sfx_if_any`), so committing text afterward can drop the SFX layer.
**Why:** The "Unsaved — downloads will include your changes" hint is then optimistic for that co-edit case. Same composition class we solved for overlay+SFX, applied to text+SFX.
**How:** Either (a) order the instant-text commit FIRST and add an SFX-reapply terminal hook to the text/instant re-render path (mirror the overlay pass), or (b) detect the co-edit and chain both in one Download. Pre-existing ordering (not introduced by the Apply-removal PR), narrow (needs SFX + uncommitted instant edit simultaneously).
**Effort:** M (CC: ~45 min) — touches handleDownload + the instant/text render path (backend reapply hook).
**Priority:** P2 — narrow co-edit case; surface only when both lanes are dirty at once.

## TikTok-style variant editor — deferred follow-ups (from `/plan-design-review`, D13–D15, 2026-07-03)

### Full video-templates system
**What:** Whole-variant style-recipe application in one tap (TikTok Templates proper) — apply a complete look (fonts, colors, animation, layout) across a variant in a single action, rather than restyling text alone.
**Why:** Deferred from D11, which ships Styles v1 as restyle-all-text only. TikTok's Templates feature applies a full aesthetic recipe, not just text style — that's a bigger surface than the editor shell's first cut and deserves its own scoping.
**How:** Not designed yet. Needs its own `/plan-design-review` pass once the editor shell + style-set system have shipped and soaked.
**Effort:** L (human: ~2wks / CC: ~2d) — needs its own design review before implementation estimate firms up.
**Priority:** P3
**Depends on:** Editor shell shipped (T1–T11, `our-timeline-editor-should-graceful-comet.md`), existing style-set system (`getGenerativeStyleSets`, `page.tsx:191`).

### Mobile-native timeline editor
**What:** A thumb-first, vertically-designed timeline editor for <1024px — not the desktop 5-column shell squeezed into a phone viewport.
**Why:** D12 ships light-edit mode for <1024px (canvas + transport + tap-text-to-edit sheet only; no timeline/trim/split/zoom UI). Codex's review flagged that a squeezed desktop shell would be unusable on mobile, so a real mobile timeline is intentionally out of scope for the first ship and left as a separate design effort.
**How:** Not designed yet. Trigger: light-edit mode usage data shows real demand for full mobile timeline editing (trim/split/zoom) before investing in a bespoke mobile IA.
**Effort:** Unscoped — gated on usage data; needs its own design pass once triggered.
**Priority:** P3
**Depends on:** Editor shell shipped + light-edit mode (D12) live long enough to gather usage analytics.

### Preset favorites server-side persistence
**What:** Move the text-preset drawer's "Favorite" category from localStorage (v1) to the existing user-prefs endpoint, so favorited presets sync cross-device.
**Why:** D15 confirms no user-prefs model exists yet (`models.py:47`), so Favorites ship as localStorage-only in v1. Flagged during design review as a likely near-term promotion once the preset registry (T7) is stable — eng review may even pull it into v1 if it turns out trivial.
**How:** Extend the user-prefs endpoint (once it exists) to store favorited preset IDs; swap the drawer's localStorage read/write (`_editor/ToolDrawer.tsx`, `lib/text-presets.ts`) for the API call, falling back to localStorage when signed out or offline.
**Effort:** S (human: ~half day / CC: ~20min)
**Priority:** P3
**Depends on:** T7 preset registry (`lib/text-presets.ts`) shipped; a user-prefs model/endpoint existing.

### Editable music-bed trims
**What:** Let creators trim or reposition the song bed independently of clip length in the post-generation editor.
**Why:** Hotfix 2026-07-05 deliberately makes the music bar honestly non-editable: the song auto-fits the cut, and users change where it ends by trimming clips. Real music-bed handles need a product/rendering pass so song, lyric, and beat-sync variants do not drift.
**How:** Design the beat-grid contract first, then expose handles only once the renderer can preserve whole-beat sync and lyric timing through independent song-bed edits.
**Effort:** M
**Priority:** P3
## Plan-item redesign — follow-ups (from /autoship eng review, 2026-07-05)

### T-PLAN-1 — Verify T-SFX-2 doesn't gain a third racing edit type
**What:** Confirm the new post-gen `reburn_narrated_bed_level` (background sound) and `reburn_narrated_captions`/`captions-enabled` (subtitles) reburns don't interact with T-SFX-2's Download-ordering bug once that's fixed.
**Why:** `handleDownload` already races an uncommitted SFX change against an uncommitted instant-text/caption draft (T-SFX-2). The new reburns dispatch via their own Apply/slider actions, not Download, so the risk should be lower — but this needs confirming when T-SFX-2 is actually picked up, not assumed.
**Context:** Background-sound and caption-style/on-off both trigger real Celery reburns outside the Download flow (`POST .../bed-level`, `POST .../captions/apply`), unlike SFX/text which share `handleDownload`. Check whether a user with an in-flight bed-level or caption reburn AND an unbaked SFX change can produce the same "only one lane bakes" surprise.
**Effort:** S (CC: ~20 min) — read-through + a couple of targeted tests once T-SFX-2 is scoped.
**Priority:** P3 — deferred until T-SFX-2 itself is picked up.
**Depends on:** T-SFX-2.

### T-PLAN-2 — Talking-to-camera "talking points" recording helper
**What:** Build a camera+mic capture recording feature (not audio-only) for talking-to-camera (`edit_format="subtitled"`) items, analogous to "Write a script with Nova" for narrated.
**Why:** Users filming a talk-to-camera clip get no scripting/prompting help today — the existing transcript flow can't be reused as-is for this archetype.
**Context:** `TeleprompterRecorder.tsx`/`ReviewStep.tsx` (under `plan/items/[id]/transcript/`) write a recorded take to `item.voiceover_gcs_path`, a field `_render_subtitled_variant` never reads (it transcribes the uploaded clip's OWN audio instead — there is no separate voiceover track for this archetype). A talking-to-camera version needs to record a NEW clip (replacing/becoming `clip_gcs_paths[0]`) and trigger a fresh subtitled render, not write to `voiceover_gcs_path`. The Brief/Questions/Script steps are already archetype-agnostic (no `edit_format` check in their backend routes) — only the Record/Review steps need the different mechanism.
**Effort:** L (human: ~3-4d / CC: ~1d) — new camera-capture UI + a distinct backend trigger path; not a gate change.
**Priority:** P2 — real gap found during the plan-item redesign's User Challenge resolution, not built defensively there.

## Auto-placement — follow-ups (from plan-design-review of plans/005, 2026-07-02)

### T-AUTO-1 — Follow-up plan: generated diagram B-roll
**What:** Plan for Nova to GENERATE the animated dark-UI diagram sequences (the @qbuilder sample's payload visualization) when the asset pool has no match — template-driven motion graphics from transcript claims.
**Why:** The remaining ~30% of sample-video parity deferred at scope decision D1 (2026-07-02). The zero-match wishlist state ("add a screenshot of X") becomes "generate a visual of X" — natural entry point.
**Pros:** True differentiator; wishlist UX already ships in plan 005. **Cons:** Research-grade scope (motion-graphic templates, brand theming, render cost).
**Context:** Reference: tiktok @qbuilder/7651054516120341791. See plans/005-auto-media-overlay-sfx.md "Non-goals".
**Depends on:** plan 005 shipped (wishlist state is the hook).
**Effort:** L (multi-PR). **Priority:** P3.

### T-AUTO-2 — Standalone sound-only SFX suggestions
**What:** Matcher pass proposing SFX at moments with NO visual (emphasis whooshes on key words, risers before reveals).
**Why:** Decision 9A (2026-07-02) made SFX children of overlays — deliberately can't express sound-only moments; the reference genre uses them.
**Pros:** Completes the automated audio layer; rule-based mapping + glossary exist. **Cons:** No visual anchor in the rail — risk of feeling arbitrary.
**Depends on:** plan 005's suggestion rail + lifecycle machinery.
**Effort:** M. **Priority:** P3.

## Full-screen cutaways — follow-ups (from plan-design-review of plans/009, 2026-07-03)

### T-CUT-1 — v1.1: caption cue re-burn over cutaways
**What:** Re-burn ONLY the caption cues whose timing intersects fullscreen windows on top of the composited output (cue-persisting variants: agent_text/none montage + narrated; reuse `_run_fast_reburn`'s persisted cue inputs — never reimplement the burn). The same PR must convert the "Remove all cards" clear path and add the preview layer split (fullscreen below the live caption layer) + a stacking guard test. Ship WHOLE, never partially per-variant.
**Why:** The committed second half of decision D2 (2026-07-03): v1 covers captions with rule (h) minimizing exposure; v1.1 restores the reference grammar (captions ride on top).
**Pros:** Full reference fidelity for muted viewers. **Cons:** New render code in the incident-prone class (renderer-parity #296/#297, lyric-stacking 5a71226e/e72d52e9); song_lyrics stays excluded regardless (PNG sequences).
**Depends on:** plan 009 v1 shipped.
**Effort:** M-L (CC: ~1-2h). **Priority:** P2 — the D2 decision leans on this landing.

### T-CUT-2 — Snap-to-fullscreen resize gesture (desktop)
**What:** Dragging a PiP card's corner resize handle past ~92% frame width shows a full-frame ghost; release snaps `display_mode` to fullscreen (decision D6 cut this from v1).
**Why:** Teaches the mode through the existing resize affordance; ship if v1 data (popover-toggle usage, demote rate) shows discovery friction.
**Pros:** Design rationale captured while fresh. **Cons:** Second write path for display_mode in HeroOverlayEditor — the component absorbing the most fullscreen gating traps; may never be needed.
**Depends on:** T4 of plan 009 (HeroOverlayEditor fullscreen gating) shipped.
**Effort:** M (CC: ~40min). **Priority:** P3.

### T-CUT-3 — "Great for full screen" tag on portrait pool assets
**What:** Quiet "⛶ Great for full screen" tag on AssetPool tiles whose analyzed aspect is portrait/near-9:16 (suppressed until analysis metadata arrives).
**Why:** Enforcement rule (f) makes portrait assets the best fullscreen candidates; users only learn this when a suggestion appears. Seeding the mental model at upload time compounds suggestion quality (outside-voice finding, 2026-07-03).
**Pros:** Cheap; nudges users to stock the pool with takeover-ready material. **Cons:** One more UI element; label noise if over-applied.
**Depends on:** plan 009 v1 shipped (rule f active).
**Effort:** S (CC: ~15min). **Priority:** P3.

## AI auto-placement × editor — follow-ups (from the reintegration PR, 2026-07-07)

### "Placed by AI" receipt chip in the editor
**What:** Surface `overlay_apply_receipt` (the zero-click demote receipt) as a small provenance chip on editor overlay cards that were auto-applied before the session opened — today they seed as indistinguishable normal cards.
**Why:** Zero-click applies visuals the user never saw being placed; a quiet "✦ placed by AI" cue builds trust and explains where cards came from.
**How:** `_variants_for_response` already exposes the receipt; map applied-card ids → chip in `EditorCanvas`/inspector. No new persistence.
**Effort:** S (human: ~3h / CC: ~20 min)
**Priority:** P3

### Retire the item-page SuggestionRail/AssetPool once the editor is GA
**What:** Remove `SuggestionRail.tsx`, `HeroOverlayEditor.tsx`, and the item-page AssetPool mount after `NEXT_PUBLIC_TIKTOK_EDITOR_ENABLED` defaults on; the editor's Overlays drawer is the single suggestion surface.
**Why:** Two review surfaces for the same envelopes will drift (copy, styling, semantics). Editor accept-→-commit and rail apply-→-render are already different write paths.
**How:** Separate cleanup PR (kill-switch/file-deletion rule: never delete the fallback in the enabling PR). Keep `useOverlaySuggestions.ts` (shared state) until the rail goes.
**Effort:** M (human: ~1d / CC: ~45 min)
**Priority:** P3
**Depends on:** editor flag default-on in Vercel.

### Wishlist → pool-upload deep link
**What:** Each wishlist line in the editor's suggestion section ("a diagram of X would fit 0:42") gets an Upload button that opens the pool uploader pre-tagged with that wish; after analysis, auto-rerun matching.
**Why:** The wishlist is the agent telling the user exactly what asset would improve the edit — today it's dead text; closing the loop turns it into the pool-stocking mechanism (compounds suggestion quality, same insight as T-CUT-3).
**How:** Thread `wishlist` entries into the pool strip's upload CTA; on the tagged asset reaching `ready`, call `suggestVariantOverlays` once.
**Effort:** S (human: ~4h / CC: ~25 min)
**Priority:** P3
**Depends on:** this PR's editor suggestion section.

### Bottom-sheet overlay editor
**What:** If phone usage shows the inline overlay popover is clumsy, rebuild overlay editing on touch as a `dvh`-capped bottom sheet with safe-area padding, a sticky action row, and `t-modal` motion tokens.
**Why:** Decision 6A from the 2026-07-11 plan-design-review chose the lighter popover path for now; a sheet is the next move only if user feedback or session recordings show touch users abandoning the popover.
**How:** Keep the desktop popover. On touch/editor-light layouts, mount the same controls in a bottom sheet capped to the viewport, with `env(safe-area-inset-bottom)` padding and sticky Apply/Close actions.
**Effort:** M
**Priority:** P3
**Depends on:** user feedback or session recordings showing popover abandonment on touch.

## Review follow-ups — informational findings (from /review of plans 005-009, 2026-07-03)

The /review army (31 agents) confirmed 20 criticals — all fixed inline or by the
R1-R4 decisions. These 32 informational findings were batched here (decision R4-A).
Grouped by area; none block ship.

### Backend correctness / safety (informational)
- **T-REV-1 (P2)** — `_WHISPER_WALL_CLOCK_S = 90` in `transcript_source.py` is defined + documented as bounding ASR but NEVER referenced; `transcribe_variant_video` runs Whisper unbounded. A slow/local backend can exceed the match task's soft_time_limit=240. Wire the wall-clock into `transcribe_whisper` or drop the dead constant.
- **T-REV-2 (P2)** — `/uploads/relay` (`routes/uploads.py`) streams up to 2GB per request through the prod API VM (1 shared CPU / 512MB), holds an httpx connection up to 600s, no concurrency bound, available to every authenticated user. Now the pool-upload FALLBACK (post-review), so hit less, but still an unbounded resource path. Consider a concurrency semaphore + smaller relay cap. Also: the relay cap comment claims it "matches the presigned clip cap" but the presigned caps are 4GB (uploads.py / plan_items.py `_MAX_BYTES_PER_FILE`) — comment is wrong.
- **T-REV-3 (P3)** — `/uploads/relay` uses `CurrentUserOrSynthetic`: an unauthenticated caller (no X-User-Id) gets the synthetic user and can relay-PUT to `dev-user/*`, `slot-uploads/*`. Confirm the synthetic-user path is acceptable for the relay in prod or require a real user.
- **T-REV-4 (P3)** — receipt reason vocab drift: `SuggestionRail.receiptLines` special-cases reason `"hook"`/`"intro"` for the "shown smaller to protect your intro" copy, but the backend demote reasons are structural (`cap`/`no_dims`/`panorama`/`low_res`/`gap`/`window_too_short`) — so that nicer copy never fires (falls to generic "shown smaller"). Either map a structural reason → "intro" when the demote was intro-driven, or drop the dead FE branch.

### Data-migration (informational)
- **T-REV-5 (P2)** — `migrations/0063_plan_item_assets.py`: `created_at` is `nullable=True` but the ORM `PlanItemAsset.created_at: Mapped[datetime]` implies NOT NULL, and every prior table-creating migration in the chain declares it NOT NULL with a server default. Align (server_default=now(), nullable=False) to avoid a NULL created_at slipping in.
- **T-REV-6 (P3)** — `plan_item_assets.user_id` FK has no supporting index and no `ondelete` (defaults NO ACTION), unlike `plan_item_id` (CASCADE + composite index). CLAUDE.md says to revisit lifecycle when auth/user-delete lands — add the index + ondelete then.

### Maintainability (informational)
- **T-REV-7 (P3)** — DRY: `isUnavailableError` duplicated verbatim in SuggestionRail + AssetPool (both key on a backend 404 detail string — rewording it silently breaks feature-off detection); the pool-cap COUNT query triplicated across 3 pool routes; the fullscreen-constraint call site (variant lookup + ValueError→422) duplicated in generative_jobs.py + plan_items.py; FE fullscreen warning thresholds (720, 2.5) hand-duplicate backend constants with no sync note. Consolidate + add cross-file sync comments.
- **T-REV-8 (P3)** — stale PR0-era docstrings contradict shipped code (register_pool_asset "dispatch lands in PR1a" but it dispatches now; delete_pool_asset "PR0 has no suggestions yet"). `_persist_variant_fields` docstring promises a return all 4 callers ignore. Refresh.

### Frontend / design (informational)
- **T-REV-10 (P3)** — pre-existing (flagged because the lane is now the suggestion showcase): manual pip chips cycle a rainbow TRACK_COLORS palette starting violet #8B5CF6 (AI-slop signal per DESIGN.md); consider a calmer palette.
- **T-REV-11 (P3)** — lost-update asymmetry: the autoplace TASK takes row locks for assembly_plan writes, but the 4 new suggestion ROUTES do read-modify-write without with_for_update (a concurrent manual edit + task write can lose one). Low frequency; add row locks to the route writers if it surfaces.
- **T-REV-12 (P3)** — a transiently failed pool-asset analysis (one GCS blip / Gemini 500) marks status="failed" permanently; the matcher backfill only re-touches stale-but-ready video assets, so a failed asset is a dead end with no retry path. Add a re-analyze affordance or a bounded auto-retry.
- **T-REV-13 (P3)** — autoplace tasks land on the default "celery" queue drained by prod's ONE concurrency=1 worker alongside 30-min renders (generate-first-week enqueues 7 at once), so match_overlay_suggestions can queue behind a long render. Consider a dedicated autoplace queue/process group in prod (local dev already uses a separate queue).

### Completed
- **T-REV-9 (P3)** — touch targets: several new secondary controls (× remove-sound strip, etc.) fell below the 44px floor; NumField focus outline was a weak zinc border shift.
  **Completed:** v0.7.13.0 (2026-07-11)
