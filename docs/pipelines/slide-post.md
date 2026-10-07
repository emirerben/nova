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

Users see it as **"Slider"** (KRI-472; it was "Photo & video post"). Only the
copy changed: code, routes, schemas and `edit_format = "slides"` keep the
`slides` / slide-post names.

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
  never proposes it; the user picks it via `SetupPicker`'s "Slider" card.

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
   `generative-jobs/{job_id}/slides/normalized/{content_fingerprint}_{canvas}_{edits_digest}[_n{SLIDE_IMAGE_NORMALIZER_VERSION}]_wm{SLIDE_WATERMARK_VERSION}.{ext}`
   (the `_n` part is image slides only).
   If it already exists, download and reuse it — skip re-encoding entirely.
   A pure reorder/caption/cover edit therefore re-encodes nothing.
3. Otherwise normalize: `normalize_image_slide`/`normalize_video_slide`
   (cover-fit scale+crop to the profile's canvas, `setsar=1` — required, not
   cosmetic, for the concat filter below), then the Kria watermark (see
   below). **Encoder policy**: these are
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

## Watermark (KRI-472)

Every slide, photo or video, carries the same Kria mark the iOS engine burns
into phone-made videos: the `mist` standard tile, bottom-left, mark bottom
edge 445px above the bottom of a 1080×1920 frame (`brand/social/README.md`
has the placement rationale). It is composited **last**, above any slide
text, so text can never bury it. Because it is in the normalized derivative,
the export bundle, the stitched preview and the cover all carry it.

- **Scale rule:** one scale for the mark and its insets,
  `min(w/1080, h/1920)` — `KriaBranding.tileTransform`'s rule. A 4:5
  carousel slide therefore gets a 0.703× mark in the same relative corner.
- **Server:** `_watermark_filter` / `_overlay_filter_complex` in
  `app/pipeline/slide_post/build.py`. The PNG is
  `src/apps/api/assets/branding/kria-watermark-mist-standard.png`, written by
  `brand/social/build.py` (`RUNTIME_ASSETS`);
  `test_bundled_watermark_matches_the_brand_kit` fails if it drifts.
- **Phone (supported posts rendered on device):**
  `SlidePostOnDeviceRender.renderSlide` draws the bundled PNG at
  `KriaBranding.watermarkTileRect(canvas:tileSize:)` after the text layer. A
  missing PNG fails the export (`MediaEngineError.missingBrandingResource`)
  rather than saving unbranded slides.
  `SlidePostVideoRender` uses that same text raster and mark for carousel MP4s.
- **Re-render of older posts:** `SLIDE_WATERMARK_VERSION` is in every
  normalized key and is stamped on the variant as
  `slide_post.watermark_version`. `_slide_post_export_is_current`
  (`routes/plan_items.py`) reports a render without the current version as
  stale, so the next export re-renders instead of downloading an unbranded
  bundle. Bump it when the mark, its placement, or the composite changes.
- Video slides map one audio track (`0:a:0?`), as ffmpeg's own stream
  selection did before every slide went through a filtergraph.
- The slide editor canvases draw the original media and do not show the
  mark; only rendered output (bundle, preview MP4, cover, on-device JPEGs)
  carries it.

## Phone video and look export (KRI-482)

`SLIDE_POST_EXTENDED_DEVICE_EXPORT_ENABLED` defaults to **false**. The creation
manifest exposes `slide_post_extended_device_export`; missing/false retains the
server path for video slides and graded photos. Neutral image-only exports keep
the existing KRI-463 path. The same switch gates live source-look preview and
removes the “Save to apply” notice only while that preview is enabled. This is
also the rollback switch; no server renderer is removed.

When enabled, supported drafts export their unsaved edits directly from source
media. Photos become JPEGs at 2160×2700 (Instagram) or 2160×3840 (TikTok).
Instagram video slides become 2160×2700 H.264 MP4 Photos assets, retain source
audio, and stop at 90 seconds. Videos download to temporary files rather than
memory buffers. The complete ordered batch must render before Photos writes;
failure, cancellation, or an edited draft aborts and removes temporary output.
Share keeps the files until the share sheet is dismissed. An expired source URL
refreshes once. Unknown looks/fonts, unavailable sources, and unsupported video
profiles retain the server flow; a failure after native rendering starts is
visible and never silently substitutes neutral footage.

Preview and export share `SlidePostLookRenderer`: cover crop, footage-only look,
then editable text and finally the export watermark. Original thumbnails/cache
remain ungraded. Color cubes come from the server's authored filters; five 64³
float cubes add 20 MiB of uncompressed resources. Spatial treatments use Core
Image kernels. Grain is deterministic and frozen across video frames, while
the server's temporal grain varies; Gaussian resampling and 8-bit rounding also
have bounded differences. They are visual ports, not byte-identical FFmpeg.

Generate cubes and full server references inside the **production Docker
image** (host FFmpeg differs, particularly the faded mask's limited-range
conversion). Use the locally built production image and a Docker-visible repo
path; the container runs offline and needs no credentials:

```bash
docker run --rm --network none --entrypoint python \
  -v "$PWD:/work" -w /work nova-render-api:local \
  scripts/ios/generate-slide-look-cubes.py \
  --out src/apps/ios/Kria/Resources/Looks \
  --photo src/apps/web/public/landing/raw-story/lisbon.jpg \
  --references-dir .gstack/slide-look-parity
```

The report measures 64³ vs 128³ color interpolation; it does not certify native
spatial parity. On macOS, compile the native fixture runner next to a `Looks/`
copy of the app resources, then pass the server reference directory:

```bash
mkdir -p .gstack/slide-look-parity/Looks
cp src/apps/ios/Kria/Resources/Looks/* .gstack/slide-look-parity/Looks/
swiftc src/apps/ios/Kria/Features/SlidePostLookRenderer.swift \
  scripts/ios/render-slide-look-fixture.swift \
  -o .gstack/slide-look-parity/native-render
.gstack/slide-look-parity/native-render .gstack/slide-look-parity
```

Run the native harness with normal GPU access; a sandbox that denies Core Image
rendering can leave the bitmap empty without reporting a framework error.
The runner writes native PNGs and RGB MAE/P95 metrics, and checks repeatability,
geometry and opaque coverage. Compare visual effects as well as those numbers.
Native real-MP4 tests verify canvas/codec/audio, shared preview
grading and text placement. Look tests verify deterministic pixels, opaque
edges, neutral bypass and the production faded-mask range. The native UI test
`testLooksAppearOnThePhotoBeforeSavingWhenDeviceCapabilityIsEnabled` captures
each active look before saving.

Keep the switch off until the [physical-device performance gate](../runbooks/ios-development.md#performance-gate)
passes on an iPhone 13 and a current iPhone, with real-photo/video visual approval
and preview/export parity. Simulator timing is not rollout evidence.

## Rich per-slide text (KRI-298 / KRI-299)

Flag `slide_post_rich_text_enabled` (default `false`; capability
`slide_post_rich_text` in `CreationCapabilitiesOut`). `SlideEdits.texts` is a
list (max 4) of `SlideTextElement`: `id`, `text` (1-120), `role`
(`text|label`), `label_source` (`place|capture_time`), `edited`,
`font_family` (must be in `text_element._ALLOWED_FONTS`, default
`Inter-Bold`), `color` (#RRGGBB), `size_px` (in 1920-canvas px),
`alignment`, `position` (`top|center|bottom|custom`), `x_frac`/`y_frac`
(fractions of the SLIDE canvas, read when `custom`), `max_width_frac`,
`stroke_width` (0-20), `shadow_enabled`, `background` (`none|box`).
`size_px` is 8-200 (int or float).

**Text-tool parity (video-editor vocabulary).** The text style vocabulary is
shared with the video editor's Text tool (`agents/_schemas/text_element.TextElement`;
validators are reused, not copied). Slides still have NO timing, animation,
captions, sounds or overlays, and `behind_subject`/`highlight_color` are not
carried. Extra optional fields, all omitted from the serialized element while
unset (so pre-parity elements serialize and hash identically): `rotation_deg`
(-360..360, clockwise), `stroke_color`, `shadow_color` (#RRGGBB),
`shadow_opacity` (0..1), `background_color` (#RRGGBB; wins over the legacy
`background` box, which keeps its black@.45 look when no color is set),
`editor_preset` (`Simple|Bold|Highlight`, informational), `text_case`
(`none|upper|lower|title`, applied to the rendered string only; stored `text`
keeps its casing), `letter_spacing` (em, -0.05..0.5), `line_spacing`
(multiplier, 0.5..3.0). Rendered by `build.render_text_element_png` via
`text_overlay._authored_pillow_paint` (same mapping as the video Pillow path);
rotation is applied on the 1080x1920 raster (pivot = text anchor) BEFORE the 4:5
band crop. `_draw_text_png` gained opt-in `letter_spacing`/`line_spacing`
(per-glyph tracking via `_SpacedDraw`; None = historical pixels). The chat-edit
compiler keeps these fields on existing elements (it rebuilds from
`base.model_dump()`) but the copilot cannot read or set them.

- **Legacy mirror.** Whenever `texts` is not None, `SlideEdits.text` is forced
  to a mirror of `texts[0]` (None when empty), so old clients and the flag-off
  renderer keep working. `effective_texts()` lifts a legacy-only `text` into
  one boxed element.
- **PUT merge** (`merge_legacy_text_edits`, `put_slide_post_draft`): a client
  that omits `texts` (checked via `model_fields_set`) keeps the stored texts;
  a changed legacy `text` is folded into `texts[0]` (cleared text drops it).
  Explicit `texts` from a new client is taken as sent.
- **Renderer.** Flag on and `texts` set: each element goes through
  `text_overlay._draw_text_png` into a transparent PNG, composited with
  `-filter_complex overlay` after scale/crop and the look preset. 4:5 canvases
  remap `y' = (285 + y*1350)/1920` on the 1080x1920 raster then crop the band.
  Legacy-only drafts (`texts is None`) always take the byte-identical drawtext
  path, flag on or off.
- **Cache key.** `edits_cache_digest` hashes the full edits; legacy-only edits
  hash as before the field existed, and a `texts` slide gets a path-specific
  suffix so flipping the flag never reuses the other path's derivative.
- Tests: `tests/pipeline/test_slide_post_build.py` (real ffmpeg; drawtext
  cases need an ffmpeg with the `drawtext` filter, e.g. `ffmpeg-full`),
  `tests/test_slide_post_schema.py`, `tests/routes/test_slide_post_native_routes.py`.

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
- **No dedicated type-poster loop** for the "Slider" SetupPicker
  card yet — it reuses the "Photo wall" style tile as a visual stand-in.

## Chat editing (KRI-301, flag `slide_post_chat_edit_enabled`, default off)

**Requires `slide_post_rich_text_enabled` too:** chat edits round-trip styled
`SlideEdits.texts`, so with rich text off the route 404s and the
`slide_post_chat_edit` capability is false (both flags needed). Limits: 20/min per IP
plus 30/hour per user (`x-user-id` key, like the edit-guide route); `turns` entries are
`{role: user|assistant, content<=2000, applied/rejected<=20}`.

`POST /plan-items/{id}/slide-post/chat-edit` runs the video **edit copilot** over a
slide post instead of a second composer. Read-only: it never writes the DB; the
client stages the returned draft and saves with the normal versioned PUT.

- **Request:** `{message, expected_version?, draft?, turns<=12, client_request_id?}`.
  The editor's local `draft` is the newest truth: when sent, the edit is built from
  IT (asset ownership still validated, else 422) and `expected_version` is
  informational, so there is **no 409 / conflict step**. Without `draft` the stored
  draft is used (409 `slide_post_no_draft` if none).
- **Response:** `{outcome: edited|clarification|unsupported|no_effect|failed, reply,
  draft|null, base_version, changes[], suggestions[]}`. `base_version` is the
  SERVER's current version: quote it as `expected_version` on the next PUT.
- **Pipeline** (`services/slide_post_chat_edit.py`): `build_slide_post_snapshot`
  presents slide i as a 1-second slot (window `[i, i+1)`, `media_id` = slide id,
  `surface: "slide_post"`, `editor_ops_version: 2`) with text bars (a place/time label
  is `clip-label-media-{slide.id}`; free texts get `kria-` ids). Parsed ops run
  through `kria_editor_ops.apply_text_lane_ops` (shared handlers, no Job) and
  `compile_slide_post_ops` projects them back onto a `SlidePostDraft`: spanning texts
  are copied per slide with fresh ids, the cover follows its slide id, per-slide
  limits (4 texts, 120 chars) raise `KriaEditorOpError` => honest `failed`.
- **Surface gating** (`edit_copilot._family_allowed`): only `SLIDE_POST_OPS` are
  accepted on `surface == "slide_post"`; `set_slide_cover` / `set_post_caption`
  (`editor_ops_v2/slides.py`) are refused on video. The slides prompt fragment
  (`prompts/edit_copilot_ops/slides.txt`) is appended ONLY for that surface, so video
  prompts stay byte-identical (guard: `tests/services/test_slide_post_chat_edit.py`).
- **Facts:** `_slide_facts(asset)` reads capture/analysis facts (empty when none);
  KRI-300 swaps in `clip_facts.slide_asset_facts(asset)` here. Facts only reach the
  copilot for accounts where `settings.clip_facts_for(user_id)` is true.
- **Evals:** goldens `tests/fixtures/agent_evals/edit_copilot/golden/slide_*.json`.

## Verification

```bash
cd src/apps/api && pytest tests/pipeline/test_slide_post_profiles.py tests/pipeline/test_slide_post_build.py tests/tasks/test_slide_post_render.py tests/tasks/test_slide_post_dispatch.py tests/agents/test_slide_post_composer.py -q
cd src/apps/web && npx jest src/__tests__/plan/items/SlidePostPanel.test.tsx src/__tests__/plan/edit-format.test.ts -q
cd src/apps/api && pytest tests/services/test_slide_post_chat_edit.py tests/routes/test_slide_post_chat_edit_route.py -q
```
