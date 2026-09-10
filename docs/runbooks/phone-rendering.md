# Phone rendering — KRI-29 work in progress

Phone rendering is **disabled and not integrated into creator flows**. This branch
contains renderer, source-provenance, recovery, and publication foundations, not a completed KRI-29
implementation. Do not enable `PHONE_RENDERING_ENABLED` as a rollout: no creator
style has passed the complete parity and physical-device gates.

## Implemented foundations

- `KriaMediaEngine/SourceAssetStore.swift` binds opaque server media IDs to
  fingerprint-verified originals in app-managed project storage. Proxies,
  traversal paths, and escaping symlinks cannot resolve as originals.
- `app/kria/render_assets.py` and `KriaMediaEngine/RenderAssets.swift` define the
  portable asset manifest: local media IDs plus exact fingerprints, or library
  catalog IDs plus generations and fingerprints. The native resolver checks the
  recipe identity against the local original binding. The library cache verifies
  copied bytes before installation, detects corruption, and rejects original
  sources. Neither manifest contains download URLs or storage paths. V2 device
  requests carry this manifest in their digest; the coordinator's source resolver
  uses it before composition. The owner/revision-fenced `device-render/assets`
  route grants short-lived generation-pinned downloads only for published,
  ready music/SFX named in that recipe. It verifies the bytes and rechecks the
  owner, revision, and catalog after verification. The native authorized resolver
  downloads through a separate ephemeral session and verifies cache installation.
  Shared planning still needs to build these manifests; font/overlay catalog
  downloads and chat integration remain outstanding. V1 projects retain their
  original migration and decoding path.
- `TextMotionTiming.swift` mirrors the cloud's normalized v2 phase grid and
  smooth-type reveal math. `phone_text_motion_v2.json` covers all 17 effect timing
  rules plus multilingual, emoji, empty-line, ordering, and speed cases. This
  verifies timing calculations, not the unimplemented typography/effect pixels.
- `Composition.swift`, `RecipeVideoCompositor.swift`, and `RecipeWriter.swift`
  share timeline interpretation between preview and H.264/AAC export. Current
  coverage is basic composition, transforms, variable speed, explicit overlapping
  crossfades, basic timed text, basic audio mixing, and photo-only timelines.
  `StillTimelineClock.swift` creates a one-frame on-device clock for photo-only
  composition and retains it with the player item/asset. Photos are decoded
  through ImageIO before AVFoundation video probing, with EXIF orientation applied.
  Unsupported effects must
  fail closed. This is not cloud-renderer parity.
- `DeviceRenderCoordinator.swift` persists request identity and export/upload
  recovery state. A newer request fences a late export or upload completion.
  A completed local file can retry upload without rendering again.
- `app/kria/device_render.py` defines immutable recipe identity and typed upload
  requests. `app/services/device_render.py` pins one recipe per revision in private
  job state. No generation worker calls that pinning service yet.
- `app/routes/device_render.py` authenticates job ownership, checks current item
  and ownership epoch under locks, reserves create-only uploads with cleanup
  receipts, and verifies generation, checksum, H.264, geometry, frame rate, pixel
  format, rotation, AAC when present/required, and duration before publication.
  It rechecks the current recipe and phase after downloading/probing without DB
  locks. Outstanding reserved exports may complete after the kill switch turns
  off; new reservations are blocked.
- `Kria/Core/DeviceRendering.swift` implements the native API adapter and isolated
  storage-upload session. The coordinator is not yet called from chat or editor.
- Project proxy uploads now carry immutable original fingerprint, duration,
  geometry, orientation, and audio provenance through reservation and recovery.
  Migration 0105 stores the binding before a signed PUT is issued. Registration
  verifies the proxy generation, duration, geometry, frame rate, rotation, and
  source-audio presence. Reserved proxy IDs remain distinguishable after the
  reservation is consumed. The cloud job constructor and worker entry reject
  proxy footage or voiceovers. The picker still uses consented cloud uploads;
  selecting the phone destination remains part of the integration work.

## Remaining implementation gates

1. Connect the completed clip-proxy provenance contract to destination
   negotiation. Extend local bindings to creator visual-pool and narration
   assets, with separately consented cloud recovery and source relinking.
2. Separate shared cloud analysis/planning from media processing for every
   creator style. Phone jobs must persist a portable recipe and stop before any
   cloud effect generation or final encode. Preserve approval/revision fences
   and worker-redelivery idempotency.
3. Replace the limited V1 recipe with a negotiated complete contract. Inventory
   creator-accessible effects at the agreed baseline and build a coverage matrix
   including combinations. Port typography/fonts, captions, lyrics, narration,
   SFX, overlays, camera effects, motion scenes, and carousel treatments. Do not
   silently map those treatments to basic text or cuts.
4. Connect chat creation and editor saves to the durable coordinator, including
   local playback/sharing, distinct syncing state, cancellation, relinking,
   retry, and separately consented cloud fallback.
5. Integrate final publication with poster generation and existing finalization
   side effects. Audit account deletion, retention, expired reservation renewal,
   original-audio presence validation, and cross-device behavior.
6. Produce cloud reference fixtures and compare preview/export typography,
   layout, compositing, timing, and audio. Test interruption and re-edits across
   the full matrix. Run the complete repo gates before opening a release PR.
7. Measure iPhone 13 and a current iPhone: sustained 30-fps preview, seek p95
   <=250 ms, 60-second exports <=120 seconds, peak memory and thermal behavior,
   with no crashes or critical thermal state. Enable only verified groups;
   KRI-29 remains incomplete until every agreed group passes.

## Verification commands

```sh
cd src/apps/api
.venv/bin/pytest tests/test_device_render.py tests/routes/test_device_render.py \
  tests/test_mobile_auth_contract.py tests/services/test_public_assembly_plan.py -q
.venv/bin/python -m app.cli.kria_contracts --check
```

```sh
cd src/apps/ios/Packages/KriaMediaEngine
swift test
```

From the worktree root: `make ios-verify` and `bash scripts/preship-check.sh`.
Simulator and synthetic fixture checks do not satisfy the physical-device or
cloud-reference parity gates.
