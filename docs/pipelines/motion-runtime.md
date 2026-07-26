# Shared motion runtime

Nova's motion lane uses one TypeScript frame evaluator and one CanvasKit draw
implementation in both the browser editor and the export worker. It is separate
from authored text and visual-block rendering:

```text
clean video → visual blocks → motion presets → authored text → captions → media overlays
```

This preserves Nova's existing media-overlay ordering, keeps motion below
authored text, and lets a text-only edit reuse the cached motion base. Caption
archetypes are deliberately excluded from motion v1.

## Contract

- The public payload is `motion_scenes`: at most four versioned preset
  instances. It accepts preset IDs, integer 30 fps frame windows, palette
  colors, and intensity.
- Raw SVG, arbitrary paths, shader source, scripts, and user-authored scene
  graphs are not accepted. Trusted SVG assets are compiled into immutable path
  data in `src/packages/motion-runtime/src/presets.ts`.
- `MOTION_RUNTIME_HASH` binds the evaluator, preset version, CanvasKit version,
  and CanvasKit payload hashes. The editor sends it with every dirty motion
  section. The API rejects a mismatched runtime before changing variant state.
- Browser and worker parity covers the RGBA motion layer for an identical
  output size, integer frame, scene list, and runtime hash. Browser video
  decoding and the final H.264 encode are outside the byte-identical contract.

The first preset is `route_trace`, a draw-on route animation.
Adding a new preset requires a new immutable preset version, shared runtime hash
change, cross-runtime golden fixture, and Docker offline-render smoke test.

## Runtime paths

- Browser preview:
  `src/apps/web/src/app/plan/items/[id]/_editor/MotionCanvasLayer.tsx`
- Shared evaluator/drawer: `src/packages/motion-runtime/src/`
- Deno export renderer: `src/packages/motion-runtime/server/`
- Server validation/composition:
  `src/apps/api/app/pipeline/motion_scene.py`

The Deno renderer runs with cached dependencies, no network permission, bounded
read/write roots, a process timeout, and the same schema limits enforced by the
API. It emits only transparent PNG frames for the active scene span. FFmpeg
composites those frames with `preset=fast` and caches the result below authored
text.

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

The golden test renders the same 1080×1920 midpoint frame through Node
CanvasKit and Deno CanvasKit and pins the PNG SHA-256. CI repeats the Deno render
inside the production image with network access disabled.
