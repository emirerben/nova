# Slide posts — internals (plans/024)

Reference doc for the mixed-media "slide post" pipeline — an ordered sequence
of images and videos (a TikTok photo-mode post or an Instagram carousel),
distinct from every other archetype, which always renders one continuous
video. CLAUDE.md carries the one-paragraph contract; this file carries the
mechanics.

## Naming

`carousel` was already taken: it means the Blossom card-scroll effect burned
*into* a video (`app/pipeline/carousel/`, `variant["carousel_moment"]`,
`_editor/CarouselPanel.tsx`). The new domain noun is `slides` / "slide post".

## Platform profiles

`app/pipeline/slide_post/profiles.py` is the single source of truth for
every per-platform limit — slide count, allowed kinds, video duration cap,
output canvas:

- **`tiktok_photo`** — images only, 1–35 slides, 9:16 (1080×1920). This is
  TikTok's actual Content Posting API photo-mode behavior, not an arbitrary
  restriction: a TikTok photo post cannot contain a video item at all.
- **`instagram_carousel`** — images + videos, 2–20 slides, videos capped at
  90s, 4:5 (1080×1350).

"Mixed media" as the issue named it is really the Instagram shape; TikTok
photo mode is the images-only degenerate case.

## Data model

- `plan_items.slide_post` (migration 0104) — the reviewable draft.
  `app/schemas/slide_post.py`: `SlidePostDraft{version, platform_profile,
  slides[SlideRef], cover_index, caption, rendered_version, user_edited}`.
  `SlideRef{id, asset_id, kind, alt}` — **references a `PlanItemAsset.id`,
  never copies a raw GCS path.** `version` bumps on every edit;
  `rendered_version` records which version the current render used
  (`slide_post_needs_render` compares them) — same verb shape as
  `edit_proposal`'s draft/approved pair, deliberately a **separate** column
  so a slide draft can never trip `guided_edit_applicable`'s media-sync
  machinery.
- `edit_format = "slides"` on the item. Added to `EditFormat`
  (`app/agents/_schemas/edit_format.py`) but kept **out** of
  `GUIDED_EDIT_FORMATS`/`AUDIO_LED_EDIT_FORMATS` — the content-plan generator
  never proposes it; the user picks it via `SetupPicker`'s "Photo & video
  post" card.

## Dispatch

Unlike every other archetype, a slide post's source of truth is
`PlanItem.slide_post`, not `all_candidates.clip_paths` — it has no "clip"
concept. `_dispatch_item_render` (`app/tasks/content_plan_build.py`)
synthesizes one seed path from the draft's first asset only to satisfy
`build_generative_job`'s generic non-empty-`clip_paths` contract; the worker
never reads that seed.

`_run_generative_job_impl` (`app/tasks/generative_build.py`) branches on
`render_intent_value == "slides"` **before** the `if not clip_paths_gcs:
raise` guard — mirrors the `guided_snapshot` branch immediately above it.
`_run_slide_post_job` does the actual render; `_run_generative_job_impl`
never touches `_resolve_archetype`, music matching, or text agents for a
slides job.

**Deploy fence** (same class of hazard as `day_vlog`/`single_hero`, even
though slides isn't a guided format): `SLIDES_RENDERER_VERSION` is stamped
into `all_candidates` at dispatch (`services/generative_jobs.py`) and
re-checked at job entry (`generative_build.py`). A mixed API/worker deploy
raises `SlidePostPolicyError` rather than silently rendering a montage.

## Render (`app/pipeline/slide_post/build.py` + `_build_slide_post_result`)

Pure filesystem functions — no GCS, no DB; the dispatch task owns
download/upload. Per slide:

1. Resolve the slide's `PlanItemAsset` (ownership-checked: `plan_item_id`,
   `user_id`, `gcs_path` prefix, `media_status == "ready"`). A stale/foreign/
   unreadable reference is **dropped**, not fatal — same best-effort posture
   as a deleted clip elsewhere.
2. **Content-addressed reuse**: the normalized derivative's GCS key is
   `generative-jobs/{job_id}/slides/normalized/{content_fingerprint}_{canvas}.{ext}`.
   If it already exists, download and reuse it — skip re-encoding entirely.
   A pure reorder/caption/cover edit therefore re-encodes nothing.
3. Otherwise normalize: `normalize_image_slide`/`normalize_video_slide`
   (cover-fit scale+crop to the profile's canvas, `setsar=1` — required, not
   cosmetic, for the concat filter below). **Encoder policy**: these are
   FINAL-output bytes (they ship in the export bundle), `preset="fast"` or
   stricter — a direct implementation, not `reframe._encoding_args` (that
   helper is coupled to the main HDR/canvas reframe pipeline this doesn't
   need).
4. Render a silent, capped preview segment per slide (`render_preview_segment`
   — images hold `DEFAULT_IMAGE_HOLD_S` (2.5s), videos are capped at
   `MAX_PREVIEW_VIDEO_SLICE_S` (4s) regardless of their real length) and
   concat every segment (`concat_preview_segments`, concat FILTER not
   demuxer, so independently-encoded segments always join cleanly).
5. Extract the cover from the designated slide (`extract_cover` — a straight
   copy for an image slide, a single ffmpeg frame grab for video).
6. Build `post.json` + `caption.txt` + `bundle.zip` (`build_post_manifest` /
   `build_bundle_zip`, stdlib `zipfile`, index-ordered filenames).

## The variant contract — "give it a preview MP4 so nothing branches"

The rendered variant (`variant_id` literally `"slides"` — never
`"original_text"`/`"song_text"`, which are hardcoded elsewhere as the
clip-timeline-editable set) carries:

```
video_path        → the stitched preview MP4 (so the Hero player, library
                     tiles, and _finalize_job's allowlist keep working
                     unbranched)
poster_path       → the cover
slides            → [{index, kind, asset_id, asset_gcs_path, width, height,
                     duration_s, alt}]
slide_post        → {platform_profile, caption, cover_index,
                     bundle_gcs_path, validation:{ok, errors[], warnings[]}}
resolved_archetype = "slides"
```

`video_path` is written in the **same** upsert/merge call as
`slide_post.bundle_gcs_path` — `video_path` present must imply the bundle
exists too, or the stuck-variant reaper's "ready-implies-complete" invariant
breaks for this archetype.

This bet only holds because four things are done deliberately:

1. **`_finalize_job_decision`'s per-variant field allowlist**
   (`generative_build.py`) is a hardcoded list, not a passthrough — `slides`
   and `slide_post` are explicitly re-listed there (pinned by
   `test_finalize_job_preserves_slide_post`), or the first render loses both
   the moment it finishes.
2. **`require_editable_variant`** (`routes/generative_jobs.py`) 422s any
   attempt to route a `resolved_archetype == "slides"` variant through a
   video editor lane (swap-song, retext, media-overlays, sfx, timeline,
   editor/commit, …) — the single choke point every one of those ~30 route
   call sites already passes through. `_editor_capabilities` mirrors this
   advisory-side, reporting every lane closed with reason
   `"slide_post_edit_unsupported"`.
3. **`tiktok_publishable.resolve_publishable_output`** excludes
   `resolved_archetype == "slides"` from the publishable set and raises
   `"Photo posts are export-only"` on an explicit request — v1 has no direct
   publish, and a slides job's single variant would otherwise be selected as
   `ready[0]`.
4. **`_variants_for_response`** signs each slide's `asset_gcs_path` into
   `preview_url` and the bundle path into `bundle_url`, modeled byte-for-byte
   on the `media_overlays` signing loop (graceful per-item skip — one bad
   sign must not 500 the poll).

## Post-render edit (`rebuild_slide_post_variant`)

Reordering/adding/removing/changing the cover, caption, or profile after the
first render goes through `PUT /plan-items/{id}/slide-post` (draft write) →
`_maybe_rebuild_slide_post` (only fires if a rendered "slides" variant
already exists) → `dispatch_slide_post_edit`
(`routes/generative_jobs.py`) → the `rebuild_slide_post_variant` Celery task.

Deliberately its **own** small task rather than a branch inside
`regenerate_generative_variant` (a ~30-kwarg function serving every other
archetype) — but it calls the exact same helper functions that task uses,
not a reimplementation of them: `@_with_owned_job_fence` (ownership-epoch
fence), `_claim_creator_craft_generation` + `_creator_craft_generation_heartbeat`
(generation claim/heartbeat), `_update_variant_entry(expected_render_gen_id=…)`
(token-fenced merge write). The route side mints the token, calls
`stamp_variant_attempt`, commits, then enqueues — the same
mint→stamp→commit→publish-after-commit shape `dispatch_retext` uses.
`_finalize_job` is **not** called on rebuild — that allowlist only applies to
a job's first render; a rebuild is a targeted merge onto an already-terminal
job, like every other regenerate path.

## AI composer (`app/agents/slide_post_composer.py` + `app/services/slide_post_compose.py`)

`nova.plan.slide_post_composer` orders the item's ready pool assets, picks a
cover, and writes a caption from each asset's **already-computed**
`PlanItemAsset.analysis` — no new media analysis. The agent may only
reorder/select from the ids it's given; `parse()` treats a dropped, invented,
or duplicated id as a hard `SchemaError` (triggers a clarification retry),
never a silent partial post. `compose_slide_post_draft` is best-effort at the
service layer too: any agent failure (refusal, timeout, quota) falls back to
pool order / first slide as cover / no caption rather than blocking assembly.

## Routes

| Method | Path | Purpose |
|---|---|---|
| `PUT` | `/plan-items/{id}/slide-post` | Save a manually-edited draft (full replacement of the editable fields). Rebuilds the rendered variant if one exists. |
| `POST` | `/plan-items/{id}/slide-post/compose` | AI-propose ordering/cover/caption. Same rebuild trigger. |
| `PUT` | `/plan-items/{id}/variants/{vid}/slides` *(via `require_editable_variant`'s companion `_require_slides_variant`)* | Not a separate route — the two above are the only writers; this is the internal dispatch helper's job. |
| `GET` | `/plan-items/{id}/variants/{vid}/slides/bundle` | Signed export URL. 422s while `slide_post.validation.errors` is non-empty — export is refused, not silently partial. |

## Known v1 gaps (tracked, not silently shipped)

- **No direct publish.** v1 is export-only (`bundle.zip`). TikTok photo
  direct-publish needs `/v2/post/publish/content/init/` with
  `media_type=PHOTO` plus an image variant of the signed `/tiktok/media/…`
  proxy, and would touch `docs/runbooks/tiktok-direct-publishing.md`'s
  pinned three-scope review set on an in-flight app submission — deliberately
  out of scope.
- **Library tiles** (`routes/me.py` `_LibraryPreview`, `LibraryTile.tsx`)
  present a slides post as an ordinary video tile with a Download-MP4
  affordance — cosmetically wrong, not a data-loss risk. Follow-up: surface
  `resolved_archetype` on `LibraryJob` and render a carousel tile.
  `main_creator_agent_capability` context-hash rotation from adding
  `"slides"` to `EditFormat` is a one-time deploy-window blip, not a bug.
- **Uploading a brand-new file from inside the slide-post panel** is
  deferred — `SlidePostPanel`'s "Add" offers already-uploaded, ready pool
  assets only; attach new media via the item's Assets pool first.
- **No dedicated type-poster loop** for the "Photo & video post" SetupPicker
  card yet — it reuses the "Photo wall" style tile as a visual stand-in.

## Verification

```bash
cd src/apps/api && pytest tests/pipeline/test_slide_post_profiles.py tests/pipeline/test_slide_post_build.py tests/tasks/test_slide_post_render.py tests/tasks/test_slide_post_dispatch.py tests/agents/test_slide_post_composer.py -q
cd src/apps/web && npx jest src/__tests__/plan/items/SlidePostPanel.test.tsx src/__tests__/plan/edit-format.test.ts -q
```
