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
  Existing renderer fonts ship in the app bundle with their license files and
  enter the same verified cache only when the recipe's filename/hash matches.
  Shared guided planning builds original/font manifests; music/SFX and overlay
  planning/catalog integration remain outstanding. V1 projects retain their
  original migration and decoding path.
- The native writer always emits AAC, including bounded silent PCM chunks for
  edits without source audio. A real export test decodes that AAC to check
  silence and duration alongside preview/export frame parity. The V2 publish
  verifier rejects missing audio. Native timelines are capped at 30 minutes
  before conversion to Core Media time values; V2 server validation shares the
  duration/source-start budget.
- V2 `text_layers` carries independent time windows and positioned text runs:
  exact font identity, size, baseline, tracking, fill, outline, and rotation.
  `PortableTextDrawing.swift` draws these through the shared compositor, rejects
  missing glyphs/font substitution, and caps prepared text bitmaps at 64 MiB.
  The lane accepts shaped text with static, fade, scale, slide, pop, and bounce
  transforms using complete normalized motion parameters. Native whole-layer
  sampling matches 32 cases captured from the real cloud drawing dispatcher,
  including exit fades; animated fade runs through preview and H.264 export.
  Ordered colored blur layers provide shadows/glow behind each run; a standard
  shadow probe is visually close to Skia (not a full style-parity gate). Stroke
  width is the full centered width, twice the cloud `stroke_px` value. Reveal
  effects remain outstanding. Legacy unshaped runs now carry exact
  glyph IDs/positions from Skia and validate them against the bound font; the
  phone does not reshape those runs. `portable_text_layout.py` compiles the
  supported base text styles with production wrapping/anchor helpers and exact
  font assets. It rejects specialized treatments and legacy animation timing;
  the full scene planner remains incomplete. Linear gradients use resolved endpoints/stops
  and explicit sRGB colors, clipped to glyphs; a reference probe caught and
  fixed device-RGB conversion adding green to a red/blue gradient. A Latin font probe matched cloud ink bounds and caught/fixed
  stroke/fill ordering; this is not full typography parity. The native suite
  verifies the lane's time window in actual preview and H.264 export. The new
  `positionedText` capability remains outside the default supported set.
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
  job state. The guarded guided-story worker now calls it after shared planning
  and stops before rendering. Publication rechecks ownership, generation,
  approval, and source bindings; redelivery preserves the issued request.
- `app/routes/device_render.py` authenticates job ownership, checks current item
  and ownership epoch under locks, reserves create-only uploads with cleanup
  receipts, and verifies generation, checksum, H.264, geometry, frame rate, pixel
  format, rotation, AAC when present/required, and duration before publication.
  It rechecks the current recipe and phase after downloading/probing without DB
  locks. Outstanding reserved exports may complete after the kill switch turns
  off; new reservations are blocked.
- `Kria/Core/DeviceRendering.swift` implements the native API adapter and isolated
  storage-upload session. `DeviceRenderSessions` owns coordinators across screen
  navigation; chat polls the device destination and starts only schema versions
  and capabilities explicitly advertised as verified by the server. Sign-out
  cancels its sessions. The status card distinguishes preparation, rendering,
  local availability, syncing, and synced output, with local playback/sharing
  and sync retry. Editor saves still need integration. Capabilities remain
  disabled until device verification.
- Project proxy uploads now carry immutable original fingerprint, duration,
  geometry, orientation, and audio provenance through reservation and recovery.
  Migration 0105 stores the binding before a signed PUT is issued. Registration
  verifies the proxy generation, duration, geometry, frame rate, rotation, and
  source-audio presence. Reserved proxy IDs remain distinguishable after the
  reservation is consumed. The job constructor accepts proxy footage only for
  gated content-plan jobs with exact private bindings; dispatch also requires
  an approved guided plan. Every other cloud path still rejects proxies.
  The footage picker chooses analysis proxies only when the server advertises
  the required verified native capabilities. Its consent explains that proxy
  video/audio uploads for analysis and finished exports sync after rendering.
  Existing phone sources lock the destination: rollback pauses attachment,
  never converts it to original upload. Existing cloud projects retain their
  original-upload consent. Mixed/unknown sources and unsupported attachment
  roles are blocked; visual-pool/narration proxy support remains outstanding.
- `services/phone_sources.py` resolves selected server-owned upload receipts
  into immutable original bindings, rejects mixed/missing/conflicting sources,
  and requires each approved moment's media ID, path, and generation to match.
  `_phone_sources_v1` is private at every public assembly nesting level.
  `pipeline/phone_guided_plan.py` projects the shared guided execution plan into
  V2 original assets, exact contiguous video trims, audio level, and supported
  text layers. Unsupported media treatments, transitions, and editor lanes
  reject instead of disappearing. Supported plans enter `awaiting_device`
  instead of a cloud-render state; the chat status card consumes this state.

## Remaining implementation gates

1. Extend local bindings to creator visual-pool and narration
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

### Native editor revision integration (staged)

Editor saves retain the existing timeline behavior. The canonical guided runtime
plan owns ordered moments and total duration; `NativeEditorInteraction` projects
that document for scrub bounds and timeline geometry. This integration adds no
insertion, ripple, resizing, or overlap policy: all lane timing remains in the
existing revision, continuous music stays continuous, and the current trim limits
and frame rounding apply. Save acknowledgement preserves follow-up edits and
rebases their undo snapshots; individual gestures remain one undo step.

The phone compiler consumes that same runtime plan. Supported legacy text-only
saves replace only the editable text lane. Compilation and source binding checks
must succeed before either the desired variant or its next device receipt changes.
`test_phone_editor_commit.py` covers revision supersession and atomic failure;
`phone_text_transforms_{v2,legacy}.json` and `PortableTextTests` cover cloud timing
references and real native preview/export. All unimplemented lanes still fail
closed, and the rollout remains disabled pending the complete parity/device gates.


### Relinking local originals

The device render status panel offers **Find original files** after a stopped or
failed local attempt. Selecting a file copies it into app-managed storage and
checks its complete SHA-256 and byte count against the current immutable recipe.
Only an exact match replaces the media binding; mismatches and selections for a
superseded revision leave existing bindings intact. No upload is performed by
relinking. Once all originals are present, retry uses fresh rollout capabilities.
This also supports Gallery/cross-device projects that lack a local source mapping.
Source manifest writes are serialized; neither proxy paths nor symlink escapes
can become original bindings. Tests: `SourceAssetStoreTests` and
`DeviceRenderSessionTests.testRelinkRequiresExactBytesAndCurrentRequest`.
