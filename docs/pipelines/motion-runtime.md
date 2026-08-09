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

- The public payload is `motion_scenes`: at most eight versioned preset
  instances. It accepts preset IDs, integer 30 fps frame windows, two palette
  colors, intensity, and preset-specific parameters. Each instance is at most
  eight seconds and the union of all active windows is at most eight seconds;
  blocks may be placed anywhere in the video.
- `creator-blocks.catalog.json` and `motion-scene.schema.json` define the eight
  Creator Blocks, their immutable IDs, defaults, parameter bounds, asset
  requirements, and AI exposure. The existing `route_trace` preset remains a
  separate legacy preset.
- Media blocks persist only `{asset_id, gcs_path}` pairs from the plan item's
  ready image pool. Save and export revalidate ownership, exact path, status,
  image kind, dimensions, and storage prefix; URLs and video assets are never
  accepted as scene parameters.
- Raw SVG, arbitrary paths, shader source, scripts, and user-authored scene
  graphs are not accepted. Trusted SVG assets are compiled into immutable path
  data in `src/packages/motion-runtime/src/presets.ts`.
- `MOTION_RUNTIME_HASH` binds the evaluator, preset version, CanvasKit version,
  and CanvasKit payload hashes. The editor sends it with every dirty motion
  section. The API rejects a mismatched runtime before changing variant state.
  The previous hash is accepted only when loading an unedited, persisted
  `route_trace` v1 scene; the next save upgrades it to the current hash.
- Browser and worker parity covers the RGBA motion layer for an identical
  output size, integer frame, scene list, and runtime hash. Browser video
  decoding and the final H.264 encode are outside the byte-identical contract.

The Creator Block catalog contains Wild Type, Signal Stack, Flow Field, Cloud
Break, Offer Flip, Card Stack, Film Strip, and Donut Type. Adding or changing a
preset requires a new immutable preset version, shared runtime hash change,
cross-runtime golden fixtures, and a Docker offline-render smoke test.

## Runtime paths

- Browser preview:
  `src/apps/web/src/app/plan/items/[id]/_editor/MotionCanvasLayer.tsx`
- Shared evaluator/drawer: `src/packages/motion-runtime/src/`
- Deno export renderer: `src/packages/motion-runtime/server/`
- Server validation/composition:
  `src/apps/api/app/pipeline/motion_scene.py`

The Deno renderer runs with cached dependencies, no network permission, bounded
read/write roots, a process timeout, and the same schema limits enforced by the
API. It emits transparent PNGs only for contiguous active intervals; separated
intervals retain their exact frame offsets without rendering transparent gap
frames. Overlapping blocks share one interval and are evaluated together.
FFmpeg composites those segments with `preset=fast` and caches the result below
authored text. The cache key includes the clean-base generation, runtime hash,
normalized scenes, and exact referenced asset generations.

## Rollout

Both flags default off:

```text
MOTION_SCENES_ENABLED=false
NEXT_PUBLIC_MOTION_SCENES_ENABLED=false
```

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

cd ../api
pytest tests/pipeline/test_motion_scene.py tests/tasks/test_motion_scene_cache.py
```

The golden test renders entrance, hold, and exit frames for every Creator Block
in portrait and landscape through Node CanvasKit and Deno CanvasKit, then pins
the aggregate PNG SHA-256. Media presets use fixed image fixtures. CI repeats
the Deno render inside the production image with network access disabled.
