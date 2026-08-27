# Shared motion runtime

Nova's motion lane uses one TypeScript frame evaluator and one CanvasKit draw
implementation in both the browser editor and the export worker. It is separate
from authored text and visual-block rendering:

```text
clean video → visual blocks → motion presets → authored text → captions → media overlays
```

This preserves Nova's existing media-overlay ordering, keeps motion below
authored text, and lets a text-only edit reuse the cached motion base. Caption,
lyric, text-reburn, and retranscription paths all rebuild from a stable clean
base and reapply motion before their text layer.

## Contract

- The public payload is `motion_scenes`: at most twelve versioned preset
  instances. It accepts preset IDs, integer 30 fps frame windows, two palette
  colors, intensity, and preset-specific parameters. Each instance is at most
  eight seconds and the union of all active windows is at most twelve seconds;
  blocks may be placed anywhere in the video. The guided editor accepts at
  most 120 timeline slots. These limits are one shared contract in
  `motion-limits.json`, consumed by the browser, API, and renderer.
- `creator-blocks.catalog.json` is authoritative for the nine Creator Blocks,
  their immutable IDs, preset versions, typed controls, timing phases, safe
  ranges, asset requirements, AI exposure, and complexity weights. The strict
  `motion-scene.schema.json` and compact `creator-blocks.ai.json` are generated
  from it; `npm run check:contract` fails on drift. The existing `route_trace`
  preset remains a separate legacy preset.
- Media blocks persist only `{asset_id, gcs_path}` pairs from the plan item's
  ready image pool. Save and export revalidate ownership, exact path, status,
  image kind, dimensions, and storage prefix; URLs and video assets are never
  accepted as scene parameters.
- Copilot-facing catalog defaults and parameters use canonical `asset_ids`. The
  editor/runtime adapter resolves those IDs to the validated `{asset_id,
  gcs_path}` pairs above; runtime-only `assets` conversion stays behind that
  adapter and is not part of the Creator Block contract.
- Raw SVG, arbitrary paths, shader source, scripts, and user-authored scene
  graphs are not accepted. Trusted SVG assets are compiled into immutable path
  data in `src/packages/motion-runtime/src/presets.ts`.
- `MOTION_RUNTIME_HASH` binds the evaluator, preset version, CanvasKit version,
  and CanvasKit payload hashes. The editor sends it with every dirty motion
  section. The API rejects a mismatched runtime before changing variant state.
  The legacy v1 hash is accepted only for persisted `route_trace` scenes. The
  known persisted Creator runtimes v2, v3, v4, and v5 are accepted so a visual
  fix does not strand saved edits; they render with the current runtime and the
  next successful dirty motion save normalizes the global hash to v5. Preset version,
  not the global hash, owns visual compatibility. Older or unknown hashes fail
  closed.
- Browser and worker parity covers the RGBA motion layer for an identical
  output size, integer frame, scene list, and runtime hash. Browser video
  decoding and the final H.264 encode are outside the byte-identical contract.

The Creator Block catalog contains Wild Type, Signal Stack, Flow Field, Cloud
Break, Offer Flip, Card Stack, Film Strip, Donut Type, and Evolving Type. New
insertions use preset v2. Untouched saved v1 blocks keep their exact evaluator;
content, palette, and timeline changes do not migrate them. Touching a motion
control upgrades only the selected block to v2. A contract or timing change
requires a new immutable preset version. Any renderer change requires a shared
runtime hash change, cross-runtime golden fixtures, and a Docker offline-render
smoke test.

## Creator Block Motion v2

All v2 blocks expose catalog-backed Speed, Intensity, Easing, and Hold controls.
Speed retimes the choreography only, keeps `start_frame` fixed, changes only the
selected block's exclusive end frame, and clamps at the stable base-video
boundary. Each preset publishes a safe minimum speed so every visible value
fits the eight-second/240-frame contract; Evolving Type starts at 0.75×. Pointer
or slider previews do not create history entries, and one completed gesture
creates one undo record. Carousel projection affects preview coordinates only;
stored base-timeline frames never ripple.

Evolving Type defaults to 159 frames (111 choreography + 30 hold + 18 exit),
four deterministic organic icons, 45ms text stagger, 70ms icon stagger, medium
density, 65% morph amplitude, ease-in-out cubic, and a black/white palette. It
supports two to five icons, built-in organic/geometric/botanical styles, compact
or spread layout, forward/reverse/center-out order, icon splitting, typography
scale, backdrop opacity, independent text/icon stagger, intensity, speed, hold,
easing, and palette. Its shapes interpolate trusted fixed-topology path points;
uploaded SVG, arbitrary paths, shaders, scripts, and scene graphs remain invalid.

The v2 audit preserves every preset-v1 formula, including deliberate historical
choreography. V2 replaces Card Stack's discrete active-card snap with continuous
poses and isolates Film Strip image alpha from shared paint state. Offer Flip's
midpoint swap remains an explicit named hard-cut event. All other v2 phase joins
use deterministic named curves with zero-velocity joins where continuity is
required.

## Text Motion v2

Authored `TextElement` records may carry an optional `motion` object with
`version: 2`. Its absence is meaningful: the editor and renderer use the exact
legacy effect timing until a user explicitly changes the animation or a motion
control. Typography, color, position, and copy-only edits never migrate a
legacy element. Motion objects allow unknown keys so a full-list save from an
older client preserves controls introduced by a newer client.

The shared timing contract is:

```text
settle = effectBaseDuration(content, controls) / speed
end    = start + settle + hold + exit
```

Speed keeps `start_s` fixed and resizes only the selected overlay. Text edits
recompute that overlay's end. Manual trims consume or extend hold first, then
raise effective speed up to 4× when the requested window is shorter than the
settle phase. Editor endpoints snap to 0.1 seconds; export samples remain on
the 30fps output grid. Timeline duration, scrub bounds, music, and other lanes
never ripple.

Smooth Type is the first v2-only effect. Defaults are 45ms grapheme stagger
(about 22 clusters/second), a 120ms reveal ramp, ease-out cubic, a subtle
upward entrance, bounded whole-layer blur, no cursor, and a one-second hold.
The browser clips a fully laid-out DOM run. Skia draws shaped full-line
`TextBlob`s and clips the painted layer, so partial frames do not relayout
substrings or split combining marks, emoji ZWJ sequences, ligatures, Arabic,
bidi text, or Turkish characters.

The persisted `motion` contract accepts these optional fields in addition to
`version: 2`:

| Field | Persisted values | Editor control |
| --- | --- | --- |
| `speed` | `0.25–4` | `0.25–4×` |
| `intensity` | `0–1` | `0–100%` |
| `easing` | `linear`, `ease-out-cubic`, `ease-in-out-cubic` | Same enum |
| `stagger_ms` | `0–250` | `0–250ms` |
| `order` | `forward`, `reverse`, `center-out` | Same enum |
| `direction` | `none`, `up`, `down`, `left`, `right` | Same enum |
| `travel_px` | `0–600` | `0–300px` |
| `overshoot` | `0–1` | `0–100%` |
| `blur_px` | `0–12` | `0–12px` |
| `cursor_style` | `none`, `bar`, `block`, `underscore` | Same enum |
| `cursor_blink_ms` | `100–2000` | `100–2000ms` |
| `hold_s` | `0–3600` | `0–10s` |
| `exit_s` | `0–2` | Persisted/rendered; no editor control |
| `reveal_ramp_ms` | `40–400` | `40–400ms` |

Speed and intensity are primary controls for every v2-capable effect. Advanced
controls are capability-gated: Smooth Type exposes easing, stagger, order,
direction, travel, blur, reveal ramp, and hold; slide effects expose easing,
direction, travel, and hold; fade/scale, ink-reveal, and handwriting expose
easing and hold; pop/bounce expose overshoot and hold; typewriter/stream-in
expose cursor style, blink rate, and hold; staggered-slice exposes hold only.
Unsupported controls stay hidden and are not preview-only or render-only UI.

## Runtime paths

- Browser preview:
  `src/apps/web/src/app/plan/items/[id]/_editor/MotionCanvasLayer.tsx`
- Shared editor playback clock:
  `src/apps/web/src/app/plan/items/[id]/_editor/editor-playback-clock.ts`
  and `useVirtualPreview.ts`
- Shared evaluator/drawer: `src/packages/motion-runtime/src/`
- Deno export renderer: `src/packages/motion-runtime/server/`
- Server validation/composition:
  `src/apps/api/app/pipeline/motion_scene.py`

The Deno renderer runs with cached dependencies, no network permission, bounded
read/write roots, a process timeout, and the same schema limits enforced by the
API. It emits transparent PNGs only for contiguous active intervals; separated
intervals retain their exact frame offsets without rendering transparent gap
frames. Overlapping blocks share one interval and are evaluated together.
The 360-frame active-union limit is paired with a 1440 weighted-frame budget,
so overlapping expensive scenes cannot bypass resource validation. Existing
preset-v1 scenes retain legacy weight 1.
FFmpeg composites those segments with `preset=fast` and caches the result below
authored text. The cache key includes the clean-base generation, runtime hash,
normalized scenes, and exact referenced asset generations.

## Rollout

Both flags default off:

```text
MOTION_SCENES_ENABLED=false
NEXT_PUBLIC_MOTION_SCENES_ENABLED=false
```

Editor playback timing has a separate, frontend-only rollout flag:
`NEXT_PUBLIC_FRAME_DRIVEN_PREVIEW_ENABLED=false`. When enabled, the active
video deck publishes decoded frames through `requestVideoFrameCallback` into a
shared output-timeline clock; carousel and other non-video windows fall back to
`requestAnimationFrame`. Motion scenes, authored text, visual blocks, and
playheads consume that clock without rerendering the editor shell at display
refresh rate. Every state sampled at `n / 30` matches the export evaluator;
intermediate browser frames may interpolate continuously. Disabling the flag
restores the legacy `timeupdate` clock without changing saved scenes.

Text Motion v2 has synchronized worker/editor flags:

```text
TEXT_MOTION_V2_ENABLED=false
NEXT_PUBLIC_TEXT_MOTION_V2_ENABLED=false
```

Enable the worker flag before the Vercel flag. When disabled, persisted motion
config remains round-trippable; Smooth Type displays and renders as settled
static text instead of disappearing or being deleted.

Evolving Type exposure has its own synchronized flags:

```text
EVOLVING_TYPE_ENABLED=false
NEXT_PUBLIC_EVOLVING_TYPE_ENABLED=false
```

These flags hide and reject new Evolving insertions and edits only. A persisted
Evolving block remains hydrated, previewed, renderable, saveable alongside
changes to other blocks, and removable while exposure is disabled.

For the first rollout, deploy the API/worker first, enable the Fly flag, then
enable the Vercel flag. For a runtime-hash upgrade, disable both flags, drain
old workers, deploy the backward-compatible runtime or migrate saved scenes,
then re-enable Fly before Vercel. With the worker flag off, a persisted scene
may reuse a fresh cache bound to the same clean-base source for text-only work;
any render that needs to rebuild that cache fails closed instead of publishing
an output with its motion silently removed.

## Verification

```bash
cd src/apps/web
npm test -- --runInBand src/__tests__/lib/motion-runtime.test.ts
npm test -- --runInBand src/__tests__/lib/motion-runtime-generated-contract.test.ts

cd ../api
pytest tests/pipeline/test_motion_scene.py tests/tasks/test_motion_scene_cache.py

cd ../../..
make verify-editor-timeline
make verify-motion-performance
```

The golden test renders entrance, hold, and exit frames for every Creator Block
in portrait and landscape through Node CanvasKit and Deno CanvasKit, then pins
the aggregate PNG SHA-256. Media presets use fixed image fixtures. CI repeats
the Deno render inside the production image with network access disabled.

Pixel hashes are parity/change detectors, not design approval. The same suite
therefore applies independent visual-quality invariants: maximum catalog copy
must stay inside an aspect-relative safe frame in both orientations, Signal
Stack rows must retain visible inter-row space, and Flow Field must render the
full headline rather than only clipped scanlines. Editor tests separately pin
panel ownership: the left Visuals drawer owns discovery and insertion; the
right inspector (or pocket inspector sheet) owns the selected block's content,
motion, timing, palette, asset ordering, and removal.

For a human review sheet, keep the same tested frames and write their PNGs:

```bash
cd src/apps/web
CREATOR_BLOCK_AUDIT_DIR=/tmp/creator-block-audit \
  npm test -- --runInBand src/__tests__/lib/motion-runtime.test.ts \
  -t "pins all nine"
```

Review the middle frame for all nine blocks in both orientations before
updating the aggregate hash. A hash update without the semantic quality tests
and this visual review is not sufficient approval. Docker CI additionally
renders and pins a real text Creator Block with the production font bundle, so
the worker smoke exercises glyph measurement and fitting rather than only the
font-free Route Trace path.

Text Motion parity is pinned by the shared
`tests/fixtures/text-element-parity/motion.json` samples. The TypeScript and
Python suites assert the same Unicode cluster counts, normalized timing, and
per-frame alpha/translation/blur/reveal state. Any Skia or burn-dict change
also requires `make verify-overlays` and visual inspection of its montage.
