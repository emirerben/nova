# Text-behind-subject pipeline — internals

Reference doc for deep pipeline internals. CLAUDE.md carries the design contract
(flag name, one-line behavior, rollback command); this file carries the mechanics.

See also: `docs/pipelines/generative.md` for the variant/reburn machinery this
feature plugs into, `agents/VIDEO_CONTEXT.md` for FFmpeg subprocess patterns.

## What it is

The CapCut/IG "text behind object" effect: a hero-intro overlay renders as if
the clip's subject stands IN FRONT of the text, instead of the text always
sitting on top. It is an occlusion flag (`behind_subject: true` on a burn
dict), not an animation — it composes with whatever `effect`/`layout` the
overlay already has.

## Architecture: alpha-multiply compositing, not subject-cutout-on-top

The naive approach — cut the subject out of the frame and re-composite it
above the burned text — would need a second full-frame FFmpeg overlay pass per
occluded overlay and a matte with a hard, cutout-quality edge. Instead:

- A per-frame **grayscale matte** (`app/pipeline/subject_matte.py`) gives a
  solid-object person mask, one small (270×480) frame per rendered output
  tick. Segmentation samples time-aligned at (up to) every source frame, then
  gets the v3 treatment at compute time: trailing 3-frame temporal median →
  hard cut at 0.40 confidence → tiny-fragment drop (< 0.2% of frame:
  background passers-by, speckle) → thin edge feather. The stored matte
  already carries the treatment, so readers and both renderers stay
  treatment-agnostic.
- The Skia text renderer (`app/pipeline/text_overlay_skia.py`) draws each
  occluded overlay's glyphs as a straight-alpha RGBA frame, then multiplies the
  **alpha channel** by `(1 - mask)` before PNG-encoding it
  (`_apply_subject_mask`). Where the mask says "subject", text alpha drops
  toward 0; everywhere else it's untouched. A partial matte hides only the
  glyph pixels it intersects — with one exception, the anti-strobe
  **visibility policy** (see its section below), which hides the whole layer
  when the occlusion is near-total or heavy-AND-strobing.
- The masked PNG sequence then burns into the video exactly like any other
  Skia overlay sequence — no second overlay pass, no separate subject layer,
  no compositing order to get wrong. The subject was always the top pixel
  layer (it's the video itself); the text is masked away only where the subject
  intersects it.

This keeps the renderer's existing PNG-sequence → `overlay` FFmpeg filter
pipeline completely unchanged; `behind_subject` only changes what gets drawn
into the PNG.

## Frozen module interface: `subject_matte.py`

`app/pipeline/text_overlay_skia.py` never imports `subject_matte` — it
consumes a `SubjectMatteProvider` duck-typed `Protocol` (`mask_at(t_abs) ->
np.ndarray | None`) defined locally. This was a deliberate build-order
decoupling between the two lanes that shipped this feature; keep it that way
when touching either module — the renderer must stay segmentation-model
agnostic.

Public surface of `subject_matte.py`:

- `compute_subject_matte(video_path, windows, out_path, cut_boundaries_s=None)
  -> MatteStats | None` — segments the given `MatteWindow`s with the
  **RobustVideoMatting backbone** (rvm_mobilenetv3, onnxruntime CPU,
  `downsample_ratio=0.25`, recurrent — temporally stable by construction;
  model asset `assets/models/rvm_mobilenetv3_fp32.onnx`, GPL-3.0 weights used
  server-side only). When `MATTE_RVM_ENABLED=false` or onnxruntime/the model
  is unavailable it falls back to MediaPipe's `ImageSegmenter` (selfie
  segmenter, stateless `IMAGE` running mode — VIDEO mode's internal temporal
  filter balloons on busy footage; the selfie segmenter's confidence also
  oscillates en masse on person-adjacent textures — beach sand/rock — which
  is what shipped the prod-job `add80a9c` glitch and motivated the RVM swap).
  `cut_boundaries_s` (output-timeline hard-cut times, best-effort) resets the
  temporal median AND the RVM recurrent state at each montage slot join —
  without the reset, ~2 frames of the previous clip's silhouette occlude text
  at every cut — and excludes boundary-crossing pairs from the stability
  stats. Sampling is
  time-aligned to the source fps (`CAP_PROP_FPS`; a tick only advances the
  capture as far as real time has advanced — never sequential half-rate
  reads); the v3 mask treatment applies to both backbones, and the output is
  a grayscale H.264 mp4 + sidecar JSON. On the **mediapipe path only**: when
  the full-frame pass detects only a
  small subject region (union bbox < 25% of frame), a second pass re-segments
  a zoomed crop around it (2x-padded, min 20% side) — a distant person is a
  handful of pixels in the model's ~256px input and confidence flaps
  0.0→1.0→0.0 full-frame, but fills the input and holds 1.00 when zoomed
  (`_small_subject_roi` + the `roi_frac` path; log event
  `subject_matte_roi_refined`). The RVM path skips the ROI pass — recurrence
  + downsample keeps distant subjects stable full-frame, and a window-wide
  crop would zero any clip of a multi-clip montage whose subject sits outside
  it. Best-effort:
  every failure mode (missing model, unreadable video, mediapipe not
  installed, wall-clock budget blown) returns `None` and never raises. A
  third backbone — depth-based, for scenes with no person to segment — is
  attempted after RVM/MediaPipe find nothing; see "Non-person occlusion:
  depth backbone" below.
- `matte_is_sane(stats) -> bool` — the sanity gate (see below).
- `SubjectMatteProvider.open(matte_path) -> SubjectMatteProvider | None` —
  reads the mp4 + sidecar once, serves per-timestamp masks from memory
  (`mask_at`), upscaled to the portrait 1080×1920 raster with nearest-frame
  lookup by window + offset. Landscape renders draw on a 1920×1080 canvas —
  the renderer resizes the mask to the frame it's masking
  (`_mask_for_shape` in `text_overlay_skia.py`, logs
  `text_behind_subject_mask_resized` once per shape pair). The matte is
  stored portrait-raster regardless of source orientation, so the
  anisotropic resize is the geometrically correct registration. Before this
  fix (#661 regression) the shape mismatch failed open and behind_subject
  was a silent no-op on every landscape variant.

The matte mp4 encodes **lossless** (`-qp 0`, still `ultrafast` — it's an
intermediate artifact): default-CRF x264 rings along the hard silhouette
edge differently per frame, an edge shimmer the occlusion multiply makes
visible.

`mediapipe` is imported lazily inside `_compute_subject_matte_inner` so the
module — and the structural eval-CI job, which has no libEGL/GPU — can import
`subject_matte` without `mediapipe` installed. See `structural-evals-no-skia`
lesson in memory; the same "keep heavy pipeline deps lazy" discipline applies
here.

## Matte lifecycle

1. **Compute on base.** First render of an `agent_text` montage variant with
   at least one `behind_subject: true` overlay: `compute_subject_matte` runs
   over the union of padded (`±0.25s`), duration-clamped windows for every
   occluded overlay (`_behind_subject_windows` in `generative_build.py`,
   merges overlapping windows so no span computes twice).
2. **GCS cache next to `base_video_path`.** The matte mp4 + sidecar upload to
   `{base_gcs_path}.matte.v3.mp4` (+ `.json`) — same key prefix as the
   text-free audio-mixed base, so it lives and dies with that variant's base
   artifact. The GCS key persists on the variant as `subject_matte_path`.
   The `.v3` segment is a **cache version** (`_MATTE_CACHE_SUFFIX`) —
   but healthy `.matte.v2.mp4` BLOBS stay accepted as cache hits
   (`_MATTE_ACCEPTED_CACHE_SUFFIXES`): a v2 blob only ever exists where the
   person pass found a real subject, and the person path's write contract
   is unchanged in v3, so recomputing it would be pure churn. A persisted
   path matching neither accepted suffix — pre-beach-glitch-fix (v1), or an
   old-suffix unstable sentinel — is treated as a cache miss, so the
   resolver recomputes under the current v3 key and best-effort deletes the
   stale blob + sidecar after a successful upload. A failed migration
   returns the original (possibly stale) path unchanged for TRANSIENT
   failures (burn falls back to plain text this once, retry next burn), and
   likewise when a real previous-version BLOB exists but the recompute's
   rejection is non-conclusive (`matte insane but retryable (keeping stale
   cache)`) — a circumstantial rejection never destroys a working matte. A DEFINITIVE
   failure — stats computed, `matte_is_sane` False, AND the rejection is
   conclusive per `matte_rejection_is_retryable` (a person was actually
   found, or the depth pass ran to completion and was itself gate-rejected)
   — persists the `.matte.v3.unstable` sentinel (`_MATTE_UNSTABLE_SUFFIX`,
   a path-shaped marker with no GCS object): reburns reuse the same base
   video, so the gate would reject the same way on every text edit while
   burning the full matte budget each time; the sentinel short-circuits
   straight to plain text (trace outcomes `unstable_rejected` /
   `cached_unstable`). A no-person rejection where the depth pass never got
   a conclusive look (flag off during dark ship, model missing, inference
   budget, mid-flight crash) mints the RETRYABLE
   `.matte.v3.nodepth.unstable` sentinel (`_MATTE_NODEPTH_UNSTABLE_SUFFIX`,
   trace outcome `unstable_rejected_retryable`) instead: it short-circuits
   exactly like the permanent sentinel while `MATTE_DEPTH_OCCLUDER_ENABLED`
   stays off (no per-text-edit recompute tax), but once the flag flips on
   it no longer short-circuits and falls through the stale-suffix branch
   into one depth-eligible recompute — the permanent sentinel there would
   have locked the base out of depth occlusion before the flag ever
   flipped. Because
   the sentinel itself is version-suffixed, an old `.matte.v2.unstable`
   sentinel recorded before the depth backbone existed no longer matches
   `_MATTE_UNSTABLE_SUFFIX` — it falls through the same stale-suffix path as
   any other old cache key and gets one real recompute attempt, now with the
   depth backbone eligible. Full re-renders reset `subject_matte_path` to
   None, so new footage retries naturally. Migration deletes are
   prefix-guarded by `_matte_delete_allowed` (job-scoped
   `generative-jobs/*.matte.*` only — curated assets share the bucket).
3. **Reuse on reburn.** Any fast-reburn (font/text/size edit, style change)
   downloads the cached matte and opens it via `SubjectMatteProvider.open` —
   no recompute. This is the "steady state" path and is why matte compute
   only costs once per variant, not once per edit.
4. **Compute-on-toggle for old variants.** A variant with no
   `subject_matte_path` (never rendered with occlusion before, or predates
   this feature) that gets `behind_subject` turned on computes a fresh matte
   at reburn time, exactly like a first render.

The shared resolver for both paths is `_resolve_subject_matte_for_burn` in
`generative_build.py`: cache-hit (current-suffix path) → download + open,
never recompute; cache-miss (or a stale-suffix path — v1, v2, or an old-suffix
unstable sentinel) → compute + sanity-gate + upload + open. Montage call sites
pass `cut_boundaries_s` derived from the variant
timeline (`_variant_slot_boundaries`: user_timeline wins over ai_timeline,
removed slots skipped, collage presets → None) or, on first render, from the
resolved assembly plans (`_cut_boundaries_from_durations`); subtitled passes
None (single clip). Window starts are snapped down to the 1/30 frame grid in
`_behind_subject_windows` — the raw 0.25s pad is 7.5 frames, and a constant
half-frame offset made `mask_at`'s rounding repeat/skip mask indices every
~3 frames (a 15fps judder of the occlusion edge). **Any** step failing
(download, compute, sanity check, upload, provider open) strips
`behind_subject` from every overlay about to burn and logs
`text_behind_subject_fallback` — the render always finishes as plain text,
never fails. A bad recompute never clobbers a previously-good cached path
(`matte_gcs_path` only advances on success). Every resolution outcome also
records a `subject_matte_resolved` pipeline-trace event
(`source: cache|computed`, or `outcome: fallback_stripped` + error) so the
admin job-debug view shows whether/why a matte was used — prod job
`1e768d5b` (behind_subject silently ignored) had zero matte visibility. The
event also carries `backbone` (`"rvm"` / `"mediapipe"` / `"depth"`) when it's
knowable — read from the freshly computed `MatteStats` on a compute, or
peeked from the cached sidecar's top-level `"backbone"` key on a cache hit;
omitted (not stored as `null`) whenever it can't be determined, e.g. an old
sidecar that predates the field. See "Non-person occlusion: depth backbone"
below for what the field's values mean.

### Subtitled variants

`_compose_subtitled_final` (the sole compositor for
`resolved_archetype == "subtitled"` — first render, text reburn, caption
reburn, camera rerender, re-transcribe) calls the same resolver internally
for its authored-text underlay burn and returns
`(final_path, subject_matte_path)`; **every call site persists the returned
matte path** into its variant patch (the plumbing args are required
keyword-only so a future call site can't silently reintroduce the
no-matte no-op this path originally shipped with). The matte caches under
`{variant_..._base.mp4}.matte.mp4` next to the caption-free base. Captions
themselves burn through libass afterwards and are **never occluded** — only
Skia text elements are. Camera-effect rerenders (and first renders with
camera moves) recompute against the warped substrate under a camera-scoped
key and do NOT persist it: the variant's cached matte stays registered to
the clean base later reburns actually use.

## Prod runtime dependency: libgles2

`import mediapipe` succeeds without libGLESv2, but **ImageSegmenter creation
fails** — and because the matte engine is best-effort, the effect silently
degrades to plain pasted-on-top text. The prod Dockerfile installs `libgles2`
and `.github/workflows/docker-build.yml` creates a real IMAGE-mode segmenter
(+ one inference) inside the built image on every PR so this can't regress.

## Sanity gate

`matte_is_sane(stats)`:

```python
stats.max_coverage >= 0.01 and stats.mean_coverage <= 0.85
```

Rejects degenerate and unstable mattes: the segmenter never found anyone at
all (`max_coverage < 1%`), the mask swallowed essentially the whole frame
(`mean_coverage > 85%` — the text would end up almost entirely hidden), or
detection is unstable — the treated mask flips between present and absent
more than 2 times AND faster than 0.75 flips/s (`presence_flips` /
`presence_flips_per_s` on `MatteStats`, counted within windows). Instability
means the segmenter can't reliably see the subject (small/distant people,
low light); occlusion that blinks on/off is worse than plain text, so the
effect falls back. Anchors: Argentina montage scene cut = 1 flip @ 0.29/s
(kept); beach wide shot with dropouts = 5 flips @ 1.56/s (rejected).
There is deliberately **no lower bound on mean coverage**: a small/distant
subject (~0.8% of frame on a beach wide shot) is a legitimate occluder and
must keep the effect. Coverage stats are computed on the post-treatment
masks (what actually multiplies text alpha). Either failure falls back to plain text via the same
`text_behind_subject_fallback` path as a hard compute error.

**Shape stability:** presence flips can't see a silhouette that never
disappears but wobbles violently frame to frame — occlusion registered to a
shape that won't hold still reads as glitching. `MatteStats` therefore also
carries `shape_stability_iou`: the **median** IoU of the binarized treated
mask across consecutive present-frame pairs (within windows only), computed
when at least 5 pairs exist (`iou_pair_count`; `None` otherwise — old
sidecars and very short mattes are never rejected on a stat they don't
have). The gate rejects below `_MIN_SHAPE_STABILITY_IOU = 0.40`
(conservative: real subjects at 30fps keep adjacent-frame IoU well above
0.7 even in fast motion, and the median is immune to isolated scene cuts —
the Argentina anchor's single cut pair can't drag it down).

**Oscillation (large-jump) gate:** the median is also blind to *periodic*
multi-frame oscillation — prod job `add80a9c` (beach montage) flapped its
mask area 7%↔63% every ~5–9 frames yet kept median IoU 0.927, because ~15
jump pairs hid among 308 stable ones; presence never flipped (the mask never
fully vanished), so that gate was blind too. `MatteStats` therefore also
counts `large_jump_count` / `large_jumps_per_s`: adjacent present-pairs with
IoU < `_LARGE_JUMP_IOU = 0.50`, boundary-crossing pairs at known cuts
excluded. The gate rejects when count > `_MAX_LARGE_JUMPS = 3` AND rate >
`_MAX_LARGE_JUMPS_PER_S = 0.60` (AND-gate, same shape as the presence gate).
Anchors: beach matte ≈ 15 jumps @ 1.34/s (reject); stable footage ≈ 0; a
single in-window whip-pan ≈ 0.1/s (keep). With cut boundaries provided,
legit montage cuts contribute zero jumps.

The gate is **backbone-aware**: see "Non-person occlusion: depth backbone"
below for the depth-specific floor. Everything else in this section (shape
stability, oscillation/large-jump gate) is shared unchanged across backbones
— both write treated masks in the same 270×480 grayscale format, so the
downstream stats math doesn't care which segmenter produced them.

## Non-person occlusion: depth backbone

**Motivation.** Prod job `30b717b9` ("ACROPOLIS"/"ATHENS" text,
`behind_subject: true`, plan item `85d1de16-ba11-4533-9290-927a45819cd3`)
rendered fully in front of the landmark on a landscape scenery shot with no
person in frame. Root cause: the matte engine was person-segmentation only
(RVM primary, MediaPipe selfie fallback) — with nobody in the shot,
`MatteStats` came back all-zero, `matte_is_sane` rejected on
`max_coverage < 0.01`, and `_resolve_subject_matte_for_burn` silently
stripped `behind_subject` (trace `subject_matte_resolved` →
`fallback_stripped`). This was a documented capability gap, not a
regression — see the 2026-07-27 entry in `agents/DECISIONS.md`, which
explicitly deferred "behind-arbitrary-objects" — closed by this backbone.

**Selection flow.** `compute_subject_matte` always runs the person pass
first, exactly as before — the depth backbone never replaces it, it only
fills the gap the person pass leaves. A depth pass is attempted only when
BOTH hold: the person pass's `stats.max_coverage` is below the same
`_PERSON_MAX_COVERAGE_FLOOR` (0.01) the sanity gate uses for "nobody found",
AND `_depth_occluder_enabled()` reads true (`MATTE_DEPTH_OCCLUDER_ENABLED`,
default `False` — dark by default, see "Flag + rollback" below). The depth
pass runs to a scratch path under the *same* wall-clock budget the person
pass started against (`MATTE_WALL_CLOCK_BUDGET_S = 90`, not a fresh budget)
— person-less footage pays for both passes, but never more than the
existing per-burn time budget allows. If the depth stats pass
`matte_is_sane`, the scratch matte + sidecar replace the person-pass output
(`os.replace`) and depth stats are returned; if not, the scratch is cleaned
up and the original (all-zero) person stats are returned — the fallback
path this doc already describes is byte-identical either way.

**Sky-epsilon threshold on normalized disparity.** The depth backbone (Depth
Anything V2 Small, fp16 ONNX, ~47.3MB, Apache-2.0; see `agents/DECISIONS.md`
2026-08-19) infers a per-frame relative-disparity map — nearer pixels get
higher raw values. An **Otsu threshold was tried first and rejected**:
validated against the real Acropolis footage, raw disparity is heavily
skewed (sky/far-background clusters near ~0, near-foreground dominates the
rest of the histogram), so Otsu's bimodal split misclassified the landmark
itself as background. The implemented rule instead: per sampled frame,
robust-normalize disparity by clipping to its own [p1, p99] percentile range
and rescaling to `[0, 1]` (frames sampled at `_DEPTH_INFER_FPS = 10`,
disparity held between samples — cheaper than a full-fps inference pass);
occluder = `normalized_disparity > _DEPTH_SKY_EPS` (0.05) — i.e. anything
measurably nearer than the far-background mode occludes, rather than
splitting the histogram in two. A scene with no identifiable far layer (sky,
horizon, background wall) pushes normalized disparity uniformly high,
over-occludes, and gets caught by the `mean_coverage <= 0.85` sanity-gate
ceiling below rather than silently producing a bad mask. The binarized mask
then goes through the *same* v3 solid-object treatment already described
above (trailing 3-frame temporal median → hard cut → tiny-fragment drop →
thin edge feather) and the same stats/write tail as the person path — the
depth backbone is a second mask *source*, not a second treatment or a second
file format. Benchmarked at ~194ms/frame CPU at the shipped 518×518 input
(`_DEPTH_INPUT_SIZE`, the model's native ViT-14 patch multiple) on the
worker VM class; a reduced patch-multiple 266×476 input measured ~87ms/frame
and remains a future perf lever (not adopted — E2E occlusion verification
ran at 518×518).

**Backbone-aware sanity gate.** `matte_is_sane` reads `stats.backbone` and
applies a different floor on the depth branch: `mean_coverage >= 0.02`
(`_DEPTH_MIN_MEAN_COVERAGE`) instead of the person path's "any max_coverage
above 1% is fine, no mean floor" rule — a depth split that only nicks 2% of
the frame on average is more likely sky-epsilon noise (JPEG-ish disparity
jitter near the threshold) than a real occluder, since depth has no "this
pixel is definitely a subject" prior the way a person-segmentation
confidence score does. The upper-bound (`mean_coverage <= 0.85`) doubles as
the degenerate-scene catch described above; shape-stability
(`_MIN_SHAPE_STABILITY_IOU`) and the oscillation/large-jump gates are
unchanged and apply identically to both backbones.

**Sidecar + trace.** The matte sidecar JSON gains a top-level `"backbone"`
key (`"rvm"` / `"mediapipe"` / `"depth"`) — observational only, the renderer
(`SubjectMatteProvider`, `text_overlay_skia.py`) stays backbone-agnostic and
never reads it. `generative_build.py`'s `subject_matte_resolved` trace event
surfaces the same value (see above) so the admin job-debug view shows which
backbone actually produced a given occlusion without downloading the
sidecar by hand.

**Cache-version bump.** New computes upload under `.matte.v3.*`, and the
UNSTABLE sentinel suffix bumped v2→v3 — but healthy v2 blobs stay accepted
as hits (`_MATTE_ACCEPTED_CACHE_SUFFIXES`, see "Matte lifecycle"), so the
fleet pays no recompute churn for mattes that already work. The practical
effect: any variant whose `behind_subject` was previously person-REJECTED
under v2 (a `.matte.v2.unstable` sentinel, or nothing persisted) gets a
fresh recompute attempt on its next burn, this time with the depth backbone
eligible — the fix reaches existing person-less renders without a data
migration. And because a no-person rejection without a conclusive depth
verdict mints only the RETRYABLE `.nodepth.unstable` sentinel (see "Matte
lifecycle"), burns that happen while the depth flag is still off
short-circuit cheaply but get one depth-eligible recompute after the flag
flips — never re-poisoning the cache before the rollout.

**Flag + rollback.** `MATTE_DEPTH_OCCLUDER_ENABLED` (`app/config.py`
`matte_depth_occluder_enabled`, default `False`), read lazily per compute.
Off: the depth pass never runs and `compute_subject_matte` behaves exactly
as it did before this feature (person pass only). On config-import failure
the accessor defaults to `False` — same fail-closed posture as the other
matte flags in this doc. Rollback:
`fly secrets set MATTE_DEPTH_OCCLUDER_ENABLED=false --app nova-video` + `fly
machine restart <id>` (worker only — the depth pass is worker-side compute,
no API-side behavior to roll back). Rollback also covers mattes ALREADY in
the cache: on a cache hit whose sidecar says `backbone: "depth"` while the
flag is off, the resolver EVICTS it (`text_behind_subject_depth_cache_
evicted`) — the recompute finds no person, mints the retryable
`.nodepth.unstable` sentinel, frees the depth blob, and later burns
short-circuit cheaply to plain text until the flag returns. No `NEXT_PUBLIC_` twin: this is a
render-time backbone choice, not a user-facing toggle — the existing
`behind_subject` UI/flag surface (`TEXT_BEHIND_SUBJECT_ENABLED`) is
unchanged and still the single kill switch for the feature as a whole.

## Visibility policy (anti-strobe hide)

Per-pixel occlusion alone reads as glitching when a crowd or large object
covers MOST of the text through mask gaps that open and close every frame:
what survives is a strobe of shredded fragments (#651 measured 12
visible-alpha jumps/s on the Argentina crowd scene). #651 shipped a
whole-layer hide above 70% occlusion; #670 deleted it because a *smooth*
partial occlusion (one subject sweeping across) was fading the whole layer —
reintroducing the strobe. Both requirements now hold via a strobe detector
(`_behind_visibility_scales` in `text_overlay_skia.py`, a sequential
pre-pass before the thread-pool render):

- **Strobe events:** frame-to-frame `|Δ visible alpha| / text alpha` above
  `BEHIND_STROBE_JUMP_FRAC = 0.15` counts as one event, tracked over a
  trailing `15`-frame window (0.5s). Measured: a smooth sweep at 95%
  occlusion jumps ≤ 0.09 per frame and produces at most 2 events ever
  (entry + exit); gap-strobing jumps ~0.24 on every frame.
- **Hide** when occlusion > `BEHIND_FULL_OCCLUSION_FRAC = 0.98`
  (near-total: the ≤2% surviving shreds are below what the detector can
  see), OR when occlusion > `BEHIND_HIDE_OCCLUSION_FRAC = 0.70` AND ≥ 3
  events are in the window. **Reveal** only below
  `BEHIND_SHOW_OCCLUSION_FRAC = 0.50` (hysteresis — jitter around one
  threshold can't flap the state). Transitions ramp over 3 frames
  (`_BEHIND_VISIBILITY_FADE_FRAMES`) so they read as the subject wiping the
  text away, not a pop. Engagement logs
  `text_behind_subject_visibility_policy_engaged`.
- The #670 requirement is pinned by
  `test_partial_sweep_only_occludes_intersecting_text_pixels` (unchanged);
  the strobe/hysteresis behavior by the `test_visibility_policy_*` suite in
  `tests/pipeline/test_text_behind_subject_render.py`.

## AI decision path: `overlay_format_matcher.behind_subject`

`OverlayFormatMatcherAgent` (prompt: `prompts/match_overlay_format.txt`)
returns a `behind_subject: bool` field alongside `effect`/`position`/`layout`.
Guidance baked into the prompt (see "Text behind subject" section there):
set `true` only for a single, clearly-framed person occupying a meaningful
but not overwhelming part of the frame; default to `false` when unsure, on
multi-person/no-subject/landscape scenes, extreme close-ups, or busy/cluttered
frames — a wrong `true` produces illegible text, a wrong `false` just renders
normally.

Resolution precedence (mirrors the existing `layout` pattern), in
`_resolve_intro_overlay_params`:

1. `behind_subject_override` (explicit task kwarg, e.g. from the editor
   toggle) — wins when not `None`.
2. `agent_form.get("behind_subject")` — the AI's first-render decision, or
   the caller-folded persisted value on a no-LLM reburn (`_resolve_regen_text`
   threads `persisted_behind_subject` into a reconstructed `agent_form` on its
   no-LLM branches only — a fresh matcher run must not be clobbered by a stale
   persisted value).

The resolved value is gated a second time before it ever reaches a burn dict:
`params["behind_subject"] = resolved AND settings.text_behind_subject_enabled`
— the single chokepoint where the kill switch forces every source (AI, user
toggle, persisted) to `False`. The *pre-gate* decision is separately stashed
under the private `params["_bs_pregate"]` key so it can persist onto
`variant["intro_behind_subject"]` even while the flag is off (flipping the
flag back on later doesn't require re-deciding anything) — every caller of
`_resolve_intro_overlay_params` MUST `pop()` `_bs_pregate` before spreading
`params` into a builder function; neither `inject_persistent_intro` nor
`build_persistent_intro_overlays` accepts that key.

## Flag semantics + rollback

- **Backend:** `TEXT_BEHIND_SUBJECT_ENABLED` (`app/config.py`
  `text_behind_subject_enabled`, default `False`). Off: no matte is ever
  computed, no burn dict carries `behind_subject`, no extra GCS object is
  written. `_resolve_intro_overlay_params` is the single chokepoint that ANDs
  the resolved decision with this flag, so flipping it off mid-flight degrades
  every in-flight job to plain text instead of failing it.
- **Frontend:** `NEXT_PUBLIC_TEXT_BEHIND_SUBJECT_ENABLED` — gates the editor's
  "Behind subject" toggle (`EditToolbar.tsx`, `InspectorPanel.tsx`) and the
  timeline bar badge (`EditorTimelineBody.tsx`). Same dual-flag shape as
  SFX/media-overlays/fullscreen-cutaways: keep Fly + Vercel in sync.
- **Rollback:** `fly secrets set TEXT_BEHIND_SUBJECT_ENABLED=false --app
  nova-video` + `fly machine restart <id>` (api + worker).
- **Version-skew trap (same class as `FULLSCREEN_CUTAWAYS_ENABLED`, see
  CLAUDE.md):** `EditVariantRequest` (`routes/generative_jobs.py`) is a
  Pydantic model with `extra="ignore"` — a NEW web client sending the
  behind-subject toggle against an OLD api that doesn't declare that field
  yet has it silently dropped: the request still returns 200 OK, but renders
  with no occlusion, no error surfaced anywhere. Keep the Vercel
  (`NEXT_PUBLIC_TEXT_BEHIND_SUBJECT_ENABLED`) flag OFF until the Fly api
  deploy carrying the field is live, then flip Fly, then Vercel.

## Frame ceiling: `BEHIND_SUBJECT_FRAME_CEILING`

`behind_subject` overlays render as a per-frame PNG sequence (the hold-frame
hard-link economy other long-running effects use is disabled — the subject
mask can change even when the glyphs don't). Generative intro overlays can be
hold-to-EOF (`effect="static"`, `end_s` spanning nearly the whole clip — see
`_HOLD_TO_END_S` in `generative_overlays.py`); a plain static overlay that
long would take the `-loop 1` single-PNG path and just persist forever, but a
`behind_subject` overlay can't — every frame needs its own masked PNG, so the
sequence needs an explicit frame-count ceiling to bound worst-case scratch
disk on the encode worker.

`text_overlay_skia.py` gives `behind_subject` its own, larger ceiling —
`BEHIND_SUBJECT_FRAME_CEILING = FPS * 120` (3600 frames / 120s) — instead of
the tighter `LONG_RUNNING_TEXT_FRAME_CEILING` (30s/900 frames) other
long-running effects (lyric-line, karaoke-line, sequence overlays) use. 120s
was chosen to equal `SEQUENCE_COMPOSITE_FRAME_CEILING`: 2x Nova's sub-60s
output target. **Text truncates past the ceiling** — a window longer than
120s renders only its first 3600 frames and logs
`skia_long_running_text_duration_clamped` (`clamped_to=3600`); the overlay's
`between(t, start, end)` FFmpeg enable still gates it off at `end_s`, so past
the truncation point the text simply stops appearing for the remainder of the
window instead of erroring. See `tests/pipeline/test_text_behind_subject_render.py`
for the 45s-not-clamped / 150s-clamped-with-warning pins.

## Known limits (v1)

- **Person-only segmentation** was the original hard limit: with no depth
  backbone, footage with no person in frame always fell back to plain text
  regardless of what else was there to occlude behind (see prod job
  `30b717b9` above). This is now **backbone-selectable behind
  `MATTE_DEPTH_OCCLUDER_ENABLED`** (default off) — a person-less scene can
  occlude against a depth-estimated foreground/background split. Still not
  solved: **arbitrary-layer control.** There's no way to say "occlude
  behind THIS object, not that one" — the depth rule is a single
  sky-epsilon threshold per window (see above), which assumes the scene has
  an identifiable far/background depth layer (sky, horizon, back wall)
  sitting near the low end of the normalized range. A cluttered scene with
  several objects at similar depths, or one with no clear far layer at all
  (everything close, or a continuous depth gradient with no background
  mode), either produces a mask that doesn't isolate the intended subject
  or over-occludes past the `mean_coverage <= 0.85` gate and falls back to
  plain text — the same best-effort contract as every other rejection path
  in this doc.
- **Depth window cap: ~30s.** `_DEPTH_MAX_INFERENCES` (300, at the 10fps
  sampling stride) skips the depth pass up front for `behind_subject`
  window totals beyond ~30s — a hold-to-EOF overlay on a long clip falls
  back to plain text via the retryable sentinel (never the permanent one:
  a text-timing edit can shrink the windows back under the cap). While the
  flag is ON such a base re-checks eligibility (person pass, budget-bound)
  on each burn; with the flag off the sentinel short-circuits. Raising the
  cap needs the reduced-input perf lever (see `_DEPTH_INPUT_SIZE`) or an
  adaptive sampling stride; deliberately out of v1 scope.
- **Scope: generative intro + TextElements only.** `behind_subject` is
  supported on the montage `agent_text` intro path
  (`build_persistent_intro_overlays`) and on user-authored `TextElement`
  overlays (`build_overlays_from_text_elements`) — including on **subtitled
  variants**, whose authored-text underlay burns through
  `_compose_subtitled_final` (see "Subtitled variants" above; captions
  themselves are never occluded). It is NOT supported on:
  - **`role="generative_sequence"` overlays** — the transcript-synced /
    rhythm-mode editorial sequence always routes through
    `_render_sequence_composite` once there are ≥2 overlays, which has no
    matte hook. `_strip_behind_subject_for_sequence_role` strips the key
    (logs `text_behind_subject_unsupported_for_sequence_role`) rather than
    raising.
  - **Masonry/collage board-motion burns** — `burn_masonry_text_overlays`
    rides the text with the moving collage wall via its own overlay
    expression; both the first-render and reburn masonry branches strip
    `behind_subject` defensively before calling it, since a half-applied
    effect is worse than a clean fallback to plain text.
  - **Curtain-close tail.** Not part of the intro-overlay burn path this
    feature touches; unaffected either way.
  - **`talking_head` archetype has no reburn / occlusion rendering in v1.**
    A talking_head variant resolves and *persists* an `intro_behind_subject`
    decision (so the UI/editor state is consistent), but the archetype has no
    fast-reburn path and its intro burn call never passes a `matte=`
    provider — so a `behind_subject: true` overlay silently falls back to
    plain text (`text_behind_subject_no_matte_fallback` warning), by the same
    "no matte → render plain" contract the Skia renderer uses everywhere.
- **Editor preview shows a badge, not real occlusion.** The virtual/local
  preview has no matte to composite against — the timeline text bar just
  renders a small "⧉" badge (`title="Behind subject"`) when the flag is on,
  so editors know the toggle is set without a live preview of the effect.
  The real occlusion only appears in the rendered output after a save/reburn.
- **`opencv-contrib` coexistence.** `mediapipe`'s own metadata declares
  `opencv-contrib-python` (GUI-linked build) as a hard dependency — not
  `opencv-python-headless`, which this project deliberately uses (slim
  image, no X11). Both distributions ship the same top-level `cv2/` package
  directory, and pip has no "provides" concept across
  opencv-python/-headless/-contrib/-contrib-headless, so both get installed
  side by side and whichever lands last physically overwrites the shared
  `cv2/` directory on disk — non-deterministic across rebuilds. Verified
  locally (2026-07-17) that both `import cv2` and `import mediapipe` work
  afterward (the prod Dockerfile already installs libgl1/libegl1/
  libglib2.0-0 for skia-python, which happen to satisfy
  opencv-contrib-python's runtime needs too) — risk today is image-size
  bloat (+~55MB) and non-determinism, not a hard crash. See the comment on
  the `mediapipe` dependency in `pyproject.toml` for the escape hatch
  (`--no-deps` + explicit transitive deps) if a future pass wants a
  headless-only image. `docker-build.yml`'s smoke step
  (`import cv2, skia, mediapipe`) runs against the actual built image —
  not the dev venv — so a resolution conflict surfaces on the PR instead
  of at Fly deploy time.

## Key files

- `src/apps/api/app/pipeline/subject_matte.py` — matte compute + provider
  (frozen module interface; MediaPipe consumer).
- `src/apps/api/app/pipeline/text_overlay_skia.py` — `SubjectMatteProvider`
  protocol, `_apply_subject_mask`, `_generate_overlay_sequence` matte
  plumbing.
- `src/apps/api/app/pipeline/generative_overlays.py` — `behind_subject` kwarg
  threaded through `build_intro_overlay` / `build_persistent_intro_overlays` /
  `build_overlays_from_text_elements`.
- `src/apps/api/app/tasks/generative_build.py` — `_resolve_intro_overlay_params`
  (decision precedence + gate), `_resolve_subject_matte_for_burn` +
  `_behind_subject_windows` (lifecycle), `regenerate_generative_variant` /
  `_reburn_text_on_base` (task kwarg threading).
- `src/apps/api/app/agents/overlay_format_matcher.py` +
  `src/apps/api/prompts/match_overlay_format.txt` — the AI decision.
- `src/apps/api/app/config.py` — `text_behind_subject_enabled`,
  `matte_depth_occluder_enabled`.
- `src/apps/web/src/components/variant-editor/EditToolbar.tsx`,
  `src/apps/web/src/app/plan/items/[id]/_editor/{EditorTimelineBody,InspectorPanel}.tsx`
  — editor toggle + timeline badge (frontend flag-gated).
