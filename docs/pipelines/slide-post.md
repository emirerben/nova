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

- **`tiktok_photo`** — images only, 1–35 slides, 9:16 (1080×1920) by default.
  This is TikTok's actual Content Posting API photo-mode behavior, not an
  arbitrary restriction: a TikTok photo post cannot contain a video item at all.
- **`instagram_carousel`** — images + videos, 2–20 slides, videos capped at
  90s, 4:5 (1080×1350) by default.

"Mixed media" as the issue named it is really the Instagram shape; TikTok
photo mode is the images-only degenerate case.

Every post also carries ONE post-wide size (`SlidePostDraft.aspect`, KRI-606; see
"Post size and photo fit" below). The numbers above are each profile's default.

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
captions, sounds or video-editor media overlays (the only pasted-image overlay
is a sticker, see "Slide stickers" below), and `behind_subject`/`highlight_color`
are not carried. Extra optional fields, all omitted from the serialized element while
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

## Per-slide framing: crop and Vertical/Landscape (KRI-44, flag `slide_post_framing_enabled`, default off)

`SlideEdits.framing = {orientation: "portrait"|"landscape", source_crop?}`.

- **`source_crop`** is a normalized, top-left-origin rectangle in the UPRIGHT (EXIF-applied) frame of the photo, the same space as `NormalizedSourceCrop`, so the iPhone and the cloud agree. The renderer cuts it first (`ffmpeg_crop_filter`), then does the usual cover fit. Math lives in `app/kria/framing.py` (`cover_placement`, `crop_for_focus`) and is pinned for Swift by `tests/fixtures/slide-framing-parity/v1.json`. Image slides only: video slides keep the plain cover fit.
- **`orientation: "landscape"`** makes THAT slide export at 1920x1080 (`slide_canvas`, TikTok photo profile only; `PlatformProfileSpec.allows_landscape`). A post can mix shapes. The Instagram carousel keeps one shape, so `SlidePostDraft` coerces landscape back to portrait there.
- **Preview stays one canvas.** `render_preview_segment(source_canvas=...)` fits and pads a landscape slide into the post's preview canvas, so `concat_preview_segments` still joins same-canvas inputs. The normalized key includes the slide's own canvas. The text band keeps the destination aspect at the 1080 raster width (uniform scale, no stretch).
- **Cache.** `SlideEdits` omits `framing` from its serialized form while it is None, so an unframed slide hashes exactly as before (pinned in `test_slide_post_framing.py`) and no existing derivative re-renders.
- **Save contract (three states).** `framing` key missing (older iOS, the web editor, `edits: null`) keeps the stored value; explicit `null` clears; a value sets (`merge_stored_framing`). New iOS builds always encode the key. While `SLIDE_POST_FRAMING_ENABLED` is off the PUT ignores incoming framing.
- **Version fence.** `SLIDES_RENDERER_VERSION` is 2; the variant stamps `slide_post.renderer_version`, and `_slide_post_export_is_current` treats a framed draft as stale unless it was rendered by a worker at version 2 or later, so an old worker can never serve a crop-less export as current.
- **AI.** `set_slide_framing{slide_indexes|all, orientation?, focus?, zoom?, reset?}` (slides lane). The model never writes rectangles: `focus` (center/top/bottom/left/right) and `zoom` (1-3) resolve against the photo's upright shape (`PlanItemAsset.aspect`, else `analysis.width/height`; both are measured after EXIF rotation by `_analyze_image`). Unknown shape refuses a focus/zoom crop honestly; orientation and reset still work. Changing the shape drops the old crop. `compile_slide_post_ops` carries `framing` across text and look ops.
- **Rollout.** Merge dark. Turn on `SLIDE_POST_FRAMING_ENABLED` (Fly secret on api and worker, then restart both) once a TestFlight build with the slide Crop UI exists. Capability `slide_post_framing` gates the iOS Crop item and the AI op.

## Post size and photo fit (KRI-606, flag `slide_post_sizes_enabled`, default off)

**What the platforms do** (researched 2026-10-12; third-party guides plus Meta's IG User Media and TikTok's media
transfer docs; the "first slide decides" rule is widely reported but not in Meta's publishing docs):

| | Instagram carousel | TikTok photo post |
|---|---|---|
| Shape | One shape for the whole carousel: the FIRST slide sets it and later slides are cropped to it | Each photo is shown on its own; anything that is not 9:16 is letterboxed, not cropped |
| Sizes | 4:5 (1080×1350), 3:4 (1080×1440, supported in-app since May 2025), 1:1 (1080×1080), 1.91:1 (1080×566); taller than 3:4 is cropped | 9:16 recommended; 1:1, 4:5, 3:4 display with bars |
| API limits (for a later direct publish) | JPEG, aspect 4:5–1.91:1, width ≤ 1440 | JPEG/WebP, ≤ 1080p, ≤ 20 MB each, ≤ 35 |
| Profile grid | Shows a 3:4 centre crop | n/a |

So every slide is rendered at one size. 3:4, 4:5 and 1:1 post cleanly to both apps.

- **`SlidePostDraft.aspect`** (`"9:16"|"4:5"|"3:4"|"1:1"|"1.91:1"`, None = the profile default) resolves through
  `profiles.post_canvas`. TikTok allows 9:16, 3:4, 4:5, 1:1; Instagram 4:5, 3:4, 1:1, 1.91:1. The draft validator
  collapses the default and any size the profile lacks to None, so switching platform falls back to the new
  platform's default. Omitted from the serialized draft while None (existing drafts, binding digests and cache keys are
  byte-identical, pinned in `test_slide_post_sizes.py`). A landscape TikTok slide still exports at 1920×1080.
- **`SlideEdits.fit`** (`"blur"|"black"|"white"`, None = fill): show the WHOLE (cropped) photo with bars instead of
  trimming its edges. A **sibling of `framing`**, not nested in it: the KRI-44 iOS build re-encodes `framing` from its own
  struct and would silently drop an unknown nested key, whereas an unknown sibling key is simply omitted and the
  "missing means keep" merge protects it. Photos only: the validator strips it from video slides. Crop is applied first,
  then Fit (`fit_placement` = `cover_placement` with `min`), so Free crop + Fit exports exactly what was cropped.
- **Render** (`build._frame_filter`): Fill is the historical cover string byte-for-byte. Black/white: `scale=decrease,pad`.
  Blur: one graph with unlabelled first and last chains (so `_overlay_filter_complex`'s `[0:v]{vf}[b0]` wrap and the
  `,`-joined look still work) that overlays the fitted photo on a quarter-size, `gblur=sigma=20`, upscaled cover copy
  (`FIT_BLUR_SIGMA_PX = 80` at full resolution). `overlay` uses `format=auto` like every other overlay here; the default
  would convert Fit slides to limited-range 4:2:0 and shift their colour against Fill ones. Looks grade the whole frame,
  bars included. Text bands (`min(1920, round(1080·ch/cw))`) and the watermark scale (`min(cw/1080, ch/1920)`) already
  handle every size: no formula change, so `SLIDE_WATERMARK_VERSION` / `SLIDE_IMAGE_NORMALIZER_VERSION` are untouched.
- **Save contract (three states, same as framing).** `aspect` / `fit` key missing (web editor, a KRI-44 iOS build,
  `edits: null`) keeps the stored value; explicit `null` clears; a value sets. `merge_stored_edit_key` is the shared
  merge; `merge_legacy_text_edits` carries `fit` only when it was sent (otherwise every web save of a post with rich text
  would clear it). While `SLIDE_POST_SIZES_ENABLED` is off the PUT ignores incoming values. The chat-edit route backfills
  an omitted `aspect`/`fit` from the stored draft and re-validates it (`model_copy` skips validators).
- **Renderer fence.** The job-dispatch stamp `SLIDES_RENDERER_VERSION` stays 2, so a not-yet-updated worker never rejects
  queued jobs during a deploy. The new worker stamps `slide_post.renderer_version = SLIDES_SIZES_RENDERER_VERSION (3)`;
  `_slide_post_export_is_current` treats a sized or Fit draft as stale unless stamped ≥ 3. A default draft is current at
  any version.
- **Chat.** `set_post_aspect{aspect}` and `set_slide_fit{slide_indexes|all, fit: fill|fit, background?}`
  (`editor_ops_v2/slides.py`), offered only when `snapshot.post.sizes_enabled` (flags AND `X-Kria-Client-Protocol >= 5`,
  so an older app is never told "it's square now" and then drops the field). A new size drops portrait crops
  ("Size is now 1:1 · crops reset on N slides"); a focus/zoom crop in the same bundle as a size change fails honestly
  ("Change the size first, then crop") because it was resolved against the old shape.
- **iOS.** `SlidePostSize` (Core) mirrors `ASPECT_CANVASES`; `SlidePostSizeSheet` (toolbar "Size" or the status line)
  sets platform, shape and Fit for all photos; the Crop workspace has a per-photo Fill | Fit row. Every render path takes the
  draft's `aspect` explicitly (no default), including `SlidePostExporter`'s callbacks. Pinned by the `sizes`,
  `fit_placements` and `fit_blur_sigma_px` blocks of `tests/fixtures/slide-framing-parity/v1.json`.
- **Known gaps.** An older iOS build that exports a sized post on the PHONE produces the profile's default shape (it
  cannot see `aspect`); bounded, because sizes are only created by protocol-5 builds while the flag is on, the server
  export is always right, and stored values survive an old app's saves. The legacy drawtext size (`ch·0.045`) shrinks on
  short canvases (25 px at 1.91:1); iOS uses rich text. The web editor has no size picker or Fit (its saves keep them).
  An app treats a size or Fit style it does not know as the default, and its next save writes that default
  (clearing the server's value); a later server that adds a size must also raise the client-protocol floor for the
  capability, as this one does. On the phone, Fit clips the photo to its crop region before the bars are drawn
  (`framedBase`, `SlidePostRegionClip`): drawing the whole source would show the cropped-away pixels in the bars.
- **Rollout.** Merge dark and deploy api + worker. Ship a protocol-5 TestFlight build. With
  `SLIDE_POST_FRAMING_ENABLED` already on: `fly secrets set SLIDE_POST_SIZES_ENABLED=true` (api + worker) and restart.
  Capability `slide_post_sizes` = sizes AND framing. **Rollback is the flag only**: once any post has a size or Fit,
  never roll the CODE back, because the draft models drop unknown keys and the next save or render stamp would erase them.

## Slide drawings (KRI-589 PR2, flag `slide_post_drawing_enabled`, default on)

Creators draw on a slide with a finger (pen, marker, highlighter, neon, chalk, spray). The ink is a STATIC
overlay on one slide, saved as strokes, never as a raster.

- **Wire.** `SlideEdits.drawing = SlideDrawing{canvas_aspect (0.2..5.0 exclusive), strokes[DrawingStroke]}`
  (`app/schemas/slide_post.py`). Only `DrawingStroke` is shared with the video drawing lane
  (`agents/_schemas/drawing.py`): no scale, centre, rotation or timing, so a slide drawing is the identity
  transform of a still. Both models are `extra="forbid"`. Caps: at most 60 strokes and 3000 points per slide,
  8000 points per post (`SlidePostDraft` validator). Omitted from the serialized edits while None, so drafts
  without drawings keep their cache and binding digests (pinned in `test_slide_post_sizes.py`).
  Frozen by `tests/fixtures/slide-drawing-parity/v1.json` (valid drawings, invalid cases, caps, geometry),
  asserted by Python and Swift.
- **Geometry (identity).** The authoring canvas is aspect-fit and centred into the slide's output canvas,
  `px = origin + p * fitSize`, stroke width = `width_frac * min(W, H)`. A slide that later changes shape letterboxes
  the ink (no re-projection).
- **Save contract (three states, like `fit`).** `drawing` key missing keeps the stored value, explicit `null`
  clears, a value sets (`merge_stored_edit_key(..., "drawing", accept_incoming=slide_post_drawing_enabled)` in
  the PUT; `merge_legacy_text_edits` carries it only when sent). Flag off: the stored value always wins. The chat-edit
  route backfills an omitted `drawing` (`_backfill_slide_post_sizes`); `compile_slide_post_ops` copies it into the
  rebuilt slide (the one place a chat edit rebuilt edits from scratch and would have dropped it). The copilot only
  sees a read-only `has_drawing` slot hint (present only when true); there is no draw op. `/propose` carries edits
  through. `/compose` (web only; iOS never calls it) rebuilds every slide from scratch and resets ALL edits, ink
  included: existing behaviour for text, size and framing, not introduced here.
- **Capability.** `slide_post_drawing` in `/creation-threads/capabilities`: flag on AND the build declares
  `X-Kria-Client-Protocol >= slide_post_drawing_min_client_protocol` (7). No 426: an older build ignores the unknown
  key and its saves omit it, so the stored drawing survives.
- **Server renderer** (`app/pipeline/slide_post/drawing_png.py`, skia-python). One canvas-sized transparent PNG per
  drawn slide is an extra ffmpeg overlay input after the text PNGs and before the watermark, for photos and video
  slides, independent of `rich_text_active`. Textures are deterministic functions of (seed, arc length), so the
  content-addressed derivative never churns. Pen, marker and neon follow the iOS `NativeDrawingPainter`; **chalk,
  spray and highlighter are approximate ports** (same noise and dot streams through a different rasteriser; the
  highlighter uses the normal blend at its opacity because iOS multiplies against the frame, which a transparent
  overlay cannot express). The phone export paints with `NativeDrawingPainter`.
- **Cache and freshness.** A drawn slide's normalized key gets `_dr{SLIDE_DRAWING_PAINTER_VERSION}` (only then;
  existing keys are unchanged). `SLIDES_DRAWING_RENDERER_VERSION = 4` is stamped on `slide_post.renderer_version`
  when any slide has a drawing (otherwise 3), and `_slide_post_export_is_current` treats a drawn draft as stale
  below 4. The dispatch fence stays 2.
- **Deploy order.** Deploy the API and worker FIRST, then ship the app build with protocol 7: an older worker that
  re-dumps a drawn draft through its older `SlideEdits` (the render stamp) drops the drawing.
- **Painter fixes.** `SLIDE_DRAWING_PAINTER_VERSION` only changes the cache key; to refresh old exports after a painter
  fix, bump `SLIDES_DRAWING_RENDERER_VERSION` as well.
- **`DrawingStroke` is persisted twice** (video lane and every stored slide draft), and `parse_slide_post` fails closed
  on an invalid stroke. Change it only additively with defaults (`test_the_stroke_model_stays_the_persisted_wire`
  pins the field set); tightening a bound would blank every stored draft that has ink.
- **Known cost.** Spray is capped per stroke, not per slide; an adversarial 60-stroke spray drawing took ~12 s to
  render one slide locally (chalk about 1 s). Tracked as a follow-up, not a correctness issue.
- **Rollback.** Flip `SLIDE_POST_DRAWING_ENABLED=false` (api + worker) and restart; stored drawings still render.
  Never roll the CODE back once drawings exist: draft models drop unknown keys and `DrawingStroke` is
  `extra="forbid"`, so `parse_slide_post` would fail closed to "no draft".
- Tests: `tests/pipeline/test_slide_post_drawing.py` (fixture, geometry against the real painter, determinism,
  real ffmpeg composite), `tests/routes/test_slide_post_drawing_routes.py` (merge, gating, freshness, chat-edit
  copy-through, propose), `tests/tasks/test_slide_post_render.py` (stamp and key).

## Slide stickers (KRI-647 / KRI-460, flag `slide_post_stickers_enabled`, default on)

A creator pastes an image (usually a transparent PNG, e.g. a Strava run sticker) onto one slide. It is composited
over the slide's photo in the server render exactly like text and ink.

- **Wire.** `SlideEdits.stickers: list[SlideSticker] | None` (max `MAX_SLIDE_STICKERS = 4`, list order = z-order, last
  on top). `SlideSticker{id (<=64, unique per slide), asset_id (a PlanItemAsset IMAGE id on the same item),
  x_frac/y_frac in [0,1] (the sticker CENTRE as a fraction of the slide OUTPUT canvas), width_frac in [0.05,1]
  (displayed width / canvas width; height follows the image's own aspect, never distorted), rotation_deg in
  [-180,180] (clockwise about the centre, default 0)}` (`extra="forbid"`). Omitted from the serialized edits while
  None or empty, so slides without stickers keep byte-identical drafts, cache digests and binding snapshots.
  Frozen by `tests/fixtures/slide-sticker-parity/v1.json` (geometry cases + a `validation` section), asserted by
  `tests/pipeline/test_slide_post_stickers.py` through `sticker_geometry()` (`app/pipeline/slide_post/stickers.py`)
  and by the iOS tests against the same file.
- **Stacking.** photo -> look -> text -> drawing ink -> stickers -> Kria watermark (stickers sit above both text and ink; the mark stays last). Stickers are
  clipped by the canvas edge, never moved; alpha is never flattened.
- **Save contract (three states, like `drawing`).** `stickers` key missing keeps the stored value, explicit `null`
  clears, a value sets (`merge_stored_edit_key(..., "stickers", accept_incoming=slide_post_stickers_enabled)` in the
  PUT; `merge_legacy_text_edits`, `_backfill_slide_post_sizes` and `compile_slide_post_ops` carry it; the copilot
  sees a read-only `has_stickers` slot hint). Flag off: the stored value always wins and stored stickers still render.
- **Gating.** Capability `slide_post_stickers` = flag AND `X-Kria-Client-Protocol >= slide_post_sticker_min_client_protocol`
  (8). Unlike drawings, a PUT carrying stickers from a build below 8 (or with no protocol header) is refused with
  `426 {"code": "client_update_required", "feature": "slide_stickers"}`. A NEW or CHANGED sticker must point at a ready
  image `PlanItemAsset` of this item and user (422 `slide_sticker_invalid` otherwise); stickers already stored are not
  re-checked, so a deleted pool image never makes the slide un-saveable.
- **A sticker's asset is never a slide.** It lives in the item's pool (`PlanItemAsset`) but: the PUT 422s a draft whose
  slide uses an asset any slide uses as a sticker; `compose`/`propose` drop sticker assets from the default pool and
  422 an explicit request for one (`_without_sticker_assets`); the creation-thread `visuals.device_ready` count (the
  app's "continue with N visuals") excludes them for slide items (`visuals.current` stays the upload QUOTA and still
  counts them: they take a pool slot). A client building a draft from the pool must skip assets referenced by stickers.
- **Render** (`stickers.py`, `build.py`). The dispatch task resolves each slide's sticker assets in the same
  ownership-checked read as the slides (best effort: a stale, foreign, unready, non-image or missing asset, a failed
  download, or an undecodable file DROPS that sticker, never the render) and downloads the originals; `build.py` stays
  pure filesystem. Each original is decoded by Pillow (EXIF-upright, HEIC via `pillow_heif`, ICC to sRGB, capped at
  2048 px) to an RGBA PNG, then ffmpeg `format=rgba,scale -> rotate (c=none, ow/oh from rotw/roth) -> overlay` places
  its centre. The sticker inputs sit after the text/ink PNGs and BEFORE the watermark input; encoder settings are the
  unchanged final-output ones.
- **Cache and freshness.** A slide with stickers gets `_st{SLIDE_STICKER_COMPOSITOR_VERSION}` appended to its
  normalized key (only then; existing keys are unchanged), and `edits_cache_digest` hashes each sticker's asset
  content fingerprint (`missing` for a dropped one) beside the geometry already in the dumped edits, so moving or
  replacing a sticker re-renders and an unchanged slide reuses its derivative. `SLIDES_STICKER_RENDERER_VERSION = 5` is
  stamped on `slide_post.renderer_version` when any slide has stickers, and `_slide_post_export_is_current` treats a
  stickered draft as stale below 5. The dispatch fence stays 2.
- **Deploy order.** API and worker FIRST, then the protocol-8 app: an older worker that re-dumps a stickered draft
  through its older `SlideEdits` drops the stickers.
- **Rollback.** `SLIDE_POST_STICKERS_ENABLED=false` (api + worker) + restart; stored stickers still render. Never roll
  the CODE back once stickers exist (draft models drop unknown keys).
- Tests: `tests/pipeline/test_slide_post_stickers.py` (fixture parity, digest, real ffmpeg composite pixel asserts),
  `tests/routes/test_slide_post_sticker_routes.py` (merge, 426, validation, never-a-slide, freshness, chat-edit),
  `tests/tasks/test_slide_post_render.py` (dispatch download/drop, key, stamp).

## Per-slide tilt (KRI-1138, gate `MEDIA_TILT_ENABLED`, default off)

A creator turns a slide's photo OR video to any angle. Part of the "Tilt videos and photos" train: the engine contract
(`ClipBackdrop`, clockwise-positive rotation) is PR A (#2014); the main-timeline editor and the other AI lanes are
separate. This section is slide posts only.

- **Wire.** `SlideEdits.tilt = {rotation_deg (-180..180, clockwise), backdrop {kind: blank|color|blur, color?}}`
  (`SlideTilt`, `app/schemas/slide_post.py`). A SIBLING of `framing` (an older build re-encodes `framing` from its own
  struct and drops a nested key). `color` is present for `color` only; `extra="forbid"`. Omitted from the serialized
  form while None, and 0 degrees collapses to None, so an unchanged slide hashes and serializes exactly as before
  (digests pinned in `test_slide_post_tilt.py`). Frozen with Swift by `tests/fixtures/slide-tilt-parity/v1.json`.
- **Geometry** (`app/pipeline/slide_post/tilt.py`, `SlidePostTiltGeometry`). The MEDIA is the (cropped) region scaled the
  way the slide frames it, cover for Fill and whole for Fit, centred on the canvas, then turned clockwise about the
  CANVAS centre. It is not trimmed to the canvas first, so for Fill the pixels the photo has beyond the frame rotate into
  view (the engine's clip tilt behaves the same). Only the media turns: text, ink, stickers and the watermark are
  overlaid after it and stay upright. Videos turn too (cover-fit frame, no crop, no Fit).
- **Tilt and Fit.** A tilt REPLACES the Fit bars with its own backdrop; Fit still decides whether the media is the whole
  photo or the cover-fit one. Tilt and Crop combine (crop first, as everywhere). Looks grade the whole frame, backdrop
  included. The Instagram carousel canvases (4:5, 3:4, 1:1, 1.91:1) and the TikTok landscape slide all turn about their
  own centre.
- **Backdrop.** `blank` is black, `color` a `#RRGGBB`, `blur` a blurred cover-fit copy of the same media at the Fit
  blur's strength (`FIT_BLUR_SIGMA_PX = 80` at 1080 wide; the main-timeline clip tilt uses a lighter 24.9, a separate
  contract).
- **Cloud** (`build._tilt_frame_filter`). `scale` (cover or whole) -> `format=rgba,rotate=<rad>:ow=rotw:oh=roth:c=none`
  -> `overlay` centred on a `color` source (black or the colour; 30 fps with `shortest=1` for video) or on the blurred
  copy. Same unlabelled first and last chains as the Fit blur graph, so the overlay wrap and the look still work. A
  tilted slide's normalized key gets `_tl{SLIDE_TILT_COMPOSITOR_VERSION}` (only then).
- **Device.** Photos: `SlidePostOnDeviceRender.framedBase` -> `tiltedBase` / `turned` (Core Image, angle negated for its
  y-up axis). Videos: `SlidePostVideoRender` turns each frame (`tiltedCover` -> `turned`) before the look. Preview:
  `SlidePostCachedImage` (an instant SwiftUI stand-in while the exact Core Image pass computes) and
  `SlidePostVideoPicture` (the player laid out at its own cover size, rotated; the poster beneath draws the backdrop).
- **Save contract (three states, like `fit`).** `tilt` key missing (older iOS, the web editor, `edits: null`) keeps the
  stored value; explicit `null` clears; a value sets (`merge_stored_edit_key(..., "tilt", accept_incoming=media_tilt_enabled)`;
  `merge_legacy_text_edits`, `_backfill_slide_post_sizes` and `compile_slide_post_ops` carry it). Flag off: the stored
  value always wins and stored tilts still render. A PUT carrying a tilt from a build below
  `media_tilt_min_client_protocol` (28, or no header) is refused with `426 {"code": "client_update_required", "feature":
  "slide_tilt"}`.
- **Capability.** `slide_post_tilt` in `/creation-threads/capabilities` = `MEDIA_TILT_ENABLED` AND protocol >= 28. No
  `clipTilt` verified token: that token gates the recipe path; slide posts export through their own renderer.
- **Version fence.** `SLIDES_TILT_RENDERER_VERSION = 6` (stickers took 5, drawings 4, sizes 3). The worker stamps
  `slide_post.renderer_version` with it when any slide is tilted, and `_slide_post_export_is_current` treats a tilted draft
  rendered below 6 as stale, so an older worker's upright export never reads as current. The dispatch stamp
  `SLIDES_RENDERER_VERSION` stays 2.
- **UI** (`SlidePostTiltPanel.swift`, dock item "Tilt" next to Crop, capability-gated). A dial from -180 to 180 with
  detents (and haptics) at 0, +-90 and 180, a 1 degree nudge, Turn 90 and Reset, and a Background row (Blank, Colour,
  Blur). A dial drag is one undo step. The panel is a view over (`tilt`, `onChange`) so a shared Tilt sheet can replace
  it without touching the session. Fills not borders, Inter, 44pt targets, no animation.
- **AI.** `set_slide_tilt{slide_indexes|all, rotation_deg?, backdrop?, color?, reset?}` (slides lane,
  `editor_ops_v2/slides.py`) is a SIBLING op rather than two more fields on `set_slide_framing`: framing is photo-only and
  gated on `framing_enabled`, while a tilt turns videos too, is gated on `post.tilt_enabled` and persists in its own key.
  `rotation_deg` is the angle the slide should END at (the prompt resolves relative asks against the current angle that
  CURRENT POST lists); out-of-range angles fold into -180..180; a backdrop alone on an upright slide is refused (it would
  draw nothing). The gate is `MEDIA_TILT_ENABLED` and a protocol-28 build (`run_slide_post_chat_edit(tilt_enabled=...)`).
  Closed, the snapshot and CURRENT POST carry no tilt and the op is refused with `capability_unavailable`.
  `compile_slide_post_ops` carries the tilt across text, look, framing and fit changes. Replay goldens:
  `tests/fixtures/agent_evals/edit_copilot/golden/slide_tilt_*.json`. Prompt version `2026-11-01-v98`.
- **Rollout.** Merge dark and deploy api + worker FIRST (an older worker that re-dumps a tilted draft through its older
  `SlideEdits` would drop the tilt). Then `fly secrets set MEDIA_TILT_ENABLED=true` (api + worker) + restart, with a
  protocol-28 TestFlight build out. **Rollback is the flag only**: never roll the CODE back once a tilt exists (draft
  models drop unknown keys and `SlideTilt` is `extra="forbid"`).
- Tests: `tests/pipeline/test_slide_post_tilt.py` (fixture, digests, real ffmpeg against the shared geometry),
  `tests/routes/test_slide_post_tilt_routes.py` (merge, 426, capability, freshness, backfill),
  `tests/services/test_slide_post_tilt_chat.py` (op, gate, sign, carry-through), `tests/tasks/test_slide_post_render.py`
  (stamp and key); iOS `SlidePostTiltTests` (fixture, save semantics, real render against the same geometry),
  `SlidePostVideoRenderTests`, UI `testTiltTurnsThePhotoLiveAndSurvivesSave`.

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
- **Size and Fit ops (KRI-606):** `set_post_aspect` and `set_slide_fit`; see "Post size and photo fit".

## Verification

```bash
cd src/apps/api && pytest tests/pipeline/test_slide_post_profiles.py tests/pipeline/test_slide_post_build.py tests/tasks/test_slide_post_render.py tests/tasks/test_slide_post_dispatch.py tests/agents/test_slide_post_composer.py -q
cd src/apps/web && npx jest src/__tests__/plan/items/SlidePostPanel.test.tsx src/__tests__/plan/edit-format.test.ts -q
cd src/apps/api && pytest tests/services/test_slide_post_chat_edit.py tests/routes/test_slide_post_chat_edit_route.py -q
# Post size + Fit (KRI-606): real ffmpeg, needs one with drawtext + gblur (e.g. ffmpeg-full)
cd src/apps/api && pytest tests/pipeline/test_slide_post_sizes.py tests/pipeline/test_slide_post_framing.py tests/routes/test_slide_post_native_routes.py -q
# Tilt (KRI-1138): real ffmpeg, no drawtext needed
cd src/apps/api && pytest tests/pipeline/test_slide_post_tilt.py tests/routes/test_slide_post_tilt_routes.py tests/services/test_slide_post_tilt_chat.py -q
```
