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
  toward 0; everywhere else it's untouched. The renderer never derives a
  whole-layer opacity from the overlap: a partial matte can hide only the
  glyph pixels it intersects, while a matte that genuinely covers every glyph
  pixel still produces natural full occlusion.
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

- `compute_subject_matte(video_path, windows, out_path) -> MatteStats |
  None` — runs MediaPipe's `ImageSegmenter` (selfie segmenter, stateless
  `IMAGE` running mode — VIDEO mode's internal temporal filter balloons on
  busy footage) over the given `MatteWindow`s,
  time-aligned to the source fps (`CAP_PROP_FPS`; a tick only advances the
  capture as far as real time has advanced — never sequential half-rate
  reads), applies the v3 mask treatment, and writes a grayscale H.264 mp4 +
  sidecar JSON. When the full-frame pass detects only a
  small subject region (union bbox < 25% of frame), a second pass re-segments
  a zoomed crop around it (2x-padded, min 20% side) — a distant person is a
  handful of pixels in the model's ~256px input and confidence flaps
  0.0→1.0→0.0 full-frame, but fills the input and holds 1.00 when zoomed
  (`_small_subject_roi` + the `roi_frac` path; log event
  `subject_matte_roi_refined`). Best-effort:
  every failure mode (missing model, unreadable video, mediapipe not
  installed, wall-clock budget blown) returns `None` and never raises.
- `matte_is_sane(stats) -> bool` — the sanity gate (see below).
- `SubjectMatteProvider.open(matte_path) -> SubjectMatteProvider | None` —
  reads the mp4 + sidecar once, serves per-timestamp masks from memory
  (`mask_at`), upscaled to 1080×1920 with nearest-frame lookup by window +
  offset.

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
   `{base_gcs_path}.matte.mp4` (+ `.json`) — same key prefix as the text-free
   audio-mixed base, so it lives and dies with that variant's base artifact.
   The GCS key persists on the variant as `subject_matte_path`.
3. **Reuse on reburn.** Any fast-reburn (font/text/size edit, style change)
   downloads the cached matte and opens it via `SubjectMatteProvider.open` —
   no recompute. This is the "steady state" path and is why matte compute
   only costs once per variant, not once per edit.
4. **Compute-on-toggle for old variants.** A variant with no
   `subject_matte_path` (never rendered with occlusion before, or predates
   this feature) that gets `behind_subject` turned on computes a fresh matte
   at reburn time, exactly like a first render.

The shared resolver for both paths is `_resolve_subject_matte_for_burn` in
`generative_build.py`: cache-hit → download + open, never recompute;
cache-miss → compute + sanity-gate + upload + open. **Any** step failing
(download, compute, sanity check, upload, provider open) strips
`behind_subject` from every overlay about to burn and logs
`text_behind_subject_fallback` — the render always finishes as plain text,
never fails. A bad recompute never clobbers a previously-good cached path
(`matte_gcs_path` only advances on success).

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

- **Scope: generative intro + TextElements only.** `behind_subject` is
  supported on the montage `agent_text` intro path
  (`build_persistent_intro_overlays`) and on user-authored `TextElement`
  overlays (`build_overlays_from_text_elements`). It is NOT supported on:
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
- `src/apps/api/app/config.py` — `text_behind_subject_enabled`.
- `src/apps/web/src/components/variant-editor/EditToolbar.tsx`,
  `src/apps/web/src/app/plan/items/[id]/_editor/{EditorTimelineBody,InspectorPanel}.tsx`
  — editor toggle + timeline badge (frontend flag-gated).
