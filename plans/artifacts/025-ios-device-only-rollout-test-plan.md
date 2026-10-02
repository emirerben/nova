# iOS device-only Release 1 test plan

**Parent plan:** [`../025-ios-device-only-rollout-qualification.md`](../025-ios-device-only-rollout-qualification.md)
**Baseline:** `origin/main@b93328d88` (2026-10-02)
**Rule:** a required skip, unknown result, or missing evidence is a failure.

**Execution:** see [the 2026-10-02 record](025-ios-device-only-execution.md) for
implemented gaps, discovered regressions, exact results and unresolved gates.

## Test strategy

Use four layers. A lower layer cannot replace a higher one:

| Layer | Purpose | Required result |
|---|---|---|
| Backend contract | Admission, flags, format agreement, dispatch, recipe, completion, and cloud fences | Deterministic green suite under defaults and production-shaped settings |
| Native unit/UI | Protocol handling, update screen, renderer/audio behavior, device state and recovery | Green simulator suite; no unexplained retry-only passes |
| Native render E2E | Real server compiler output through the production AVFoundation exporter | Playable MP4 with asserted video, captions, audio, timing, and capabilities |
| Signed physical-device canary | Production networking, uploads, asset grants, local export, upload/complete, Gallery playback, thermal/memory | Every required journey reaches `published` and passes human audio/visual inspection |

## Existing automated coverage to keep

| Contract | Existing coverage |
|---|---|
| Web retirement, protocol 1/malformed 426, protocol 2 pass-through, cloud-only rejection | `src/apps/api/tests/services/test_ios_device_admission.py` |
| Admission/execution kill switches, task-entry guards, terminalization, preserving completed cloud output | `src/apps/api/tests/services/test_cloud_render_policy.py` |
| Every format × scenario dispatch expectation | `src/apps/api/tests/tasks/test_phone_format_matrix.py` and `_phone_format_expectations.py` |
| Single-clip Subtitled, multi-clip Talking dispatch, recorded narrated dispatch | `src/apps/api/tests/tasks/test_phone_subtitled_narrated_dispatch.py` |
| Subtitled captions/cutaways and required capabilities | `src/apps/api/tests/pipeline/test_phone_subtitled_plan.py` |
| Recorded narrated audio/captions/ducking | `src/apps/api/tests/pipeline/test_phone_narrated_plan.py` |
| Generic recorded-voiceover montage audio mix | `src/apps/api/tests/pipeline/test_phone_voiceover_montage_plan.py` |
| Guided voiceover production-shaped planner/compiler path | `src/apps/api/tests/tasks/test_phone_guided_narration_prod_replay.py` |
| Voiceover grants, device completion, retry/failure, generation fences | `src/apps/api/tests/routes/test_device_render.py` |
| Runtime-v2 device observer and no cloud enqueue after editor approval | `src/apps/api/tests/kria/test_runtime_phone_v2.py` |
| Typed 426 parsing during ordinary request and token refresh | `src/apps/ios/Tests/KriaTests/KriaTests.swift` |
| Ready/play/save/share, unsupported recipe, transient retry UI | `src/apps/ios/Tests/KriaUITests/DeviceRenderUITests.swift` |
| Generic voiceover, sentence/word Subtitled, recorded narrated native export | `src/apps/ios/Tests/KriaTests/DeviceMontageRenderE2ETests.swift` |
| Guided narration with Visuals and cleaned narration native export | `src/apps/ios/Tests/KriaTests/DevicePhotoRenderE2ETests.swift` |

Baseline validation on the plan branch: the admission, cloud-policy,
format-matrix, and phone-rollout suites passed with **176 tests green** on
2026-10-02. This confirms the commands and pre-cutover contracts at the planned
SHA; it does not satisfy the missing A1–A5 or physical-device gates.

## Missing automated tests required before cutover

| ID | Gap | Test to add | Pass condition |
|---|---|---|---|
| A1 | KRI-241 has no regression using a video clip's own voice | `LiveAudioMixTests`: play or decode the original-audio track, apply caption/title-only `updateText`, inspect again | Voice remains present at the same expected level; player identity stays stable for text-only edit |
| A2 | Fast-path coverage does not span all affected mutation classes | Parameterize text, caption style, and gain changes; add trim/full-rebuild control | Voice survives all cases; expected fast/full path is recorded |
| A3 | No native render E2E for multi-clip self-narrated Talking | Generate speaker + cutaway recipe and render it in `DeviceMontageRenderE2ETests` | Continuous speaker voice; cutaway muted; captions remain timed; cutaway visible only in its window |
| A4 | No full UI test for the blocking update screen | Typed-426 UI fixture + XCUITest | Screen replaces all routes, scrolls at accessibility size, and has App Store/support actions |
| A5 | Production flags can make picker/planner/dispatch/worker disagree without one release-profile assertion | Production-profile matrix test | For every scenario, all four layers agree on supported/refused and refusal happens before Job creation |

Do not add an HTTP-to-AVFoundation mega-test. Keep the seams above deterministic;
the physical-device matrix owns real network and production integration.

## Backend commands

Run from `src/apps/api`:

```bash
pytest -q \
  tests/services/test_ios_device_admission.py \
  tests/services/test_phone_rollout.py \
  tests/services/test_cloud_render_policy.py \
  tests/tasks/test_phone_format_matrix.py \
  tests/tasks/test_ios_device_release_profile.py

pytest -q \
  tests/tasks/test_phone_subtitled_narrated_dispatch.py \
  tests/pipeline/test_phone_subtitled_plan.py \
  tests/pipeline/test_phone_narrated_plan.py \
  tests/pipeline/test_phone_voiceover_montage_plan.py \
  tests/tasks/test_phone_guided_narration_prod_replay.py

pytest -q \
  tests/routes/test_device_render.py \
  tests/kria/test_runtime_phone_v2.py

ruff check \
  app/config.py \
  app/services/phone_rollout.py \
  tests/services/test_ios_device_admission.py \
  tests/services/test_cloud_render_policy.py \
  tests/tasks/test_phone_format_matrix.py \
  tests/tasks/test_ios_device_release_profile.py
```

If implementation touches `content_plan_build.py` or its tests, also run:

```bash
pytest -q tests/tasks/test_content_plan_build.py \
  -k 'phone_gate or self_narration or voiceover or subtitled_lanes'
```

Finish with the full backend suite if any production Python changed:

```bash
pytest
ruff check .
ruff format --check .
```

## iOS commands

From the repository root:

```bash
cd src/apps/ios/Packages/KriaMediaEngine
swift test
cd ../../../../..
make ios-verify
```

The full verify command includes generated-project checks, build settings,
Kria unit tests, and the registered UI suite. Retry-only UI success must be
reported as flaky and investigated; it is not clean rollout evidence.

### Native render E2E: montage, Subtitled, narrated, Talking Head

Choose an available iPhone simulator name with `xcrun simctl list devices available`.
Then, from the repository root:

```bash
src/apps/api/.venv/bin/python scripts/ios/phone-montage-render-e2e.py \
  /tmp/kria-rollout-montage-e2e

cd src/apps/ios
TEST_RUNNER_KRIA_E2E_DIR=/tmp/kria-rollout-montage-e2e \
xcodebuild -project Kria.xcodeproj -scheme Kria \
  -skipPackagePluginValidation -derivedDataPath .derived-data \
  CODE_SIGNING_ALLOWED=NO \
  -destination 'platform=iOS Simulator,name=<AVAILABLE_IPHONE>' \
  -only-testing:KriaTests/DeviceMontageRenderE2ETests test
```

Required active cases after A3 lands:

- `narration`
- `subtitled_sentence`
- `subtitled_word`
- `narrated`
- `talking_head`

The three retired cases (`cuts_text`, `music`, `crossfade`) remain intentional
skips in this fixture: voiceover-less montage now uses the unified planner.
Replacement coverage is `tests/tasks/test_unified_montage_dispatch.py` and the
release-profile unified row, plus `DevicePhotoRenderE2ETests` for the guided
compiler/exporter path (Visuals, original footage, text and crossfades). They
do not replace the five active speech/voiceover cases listed above.

### Native render E2E: guided voiceover

Use a separate fixture directory because both native suites read `e2e.json`:

```bash
src/apps/api/.venv/bin/python scripts/ios/phone-photo-render-e2e.py \
  /tmp/kria-rollout-photo-e2e
cd src/apps/ios
TEST_RUNNER_KRIA_E2E_DIR=/tmp/kria-rollout-photo-e2e \
xcodebuild -project Kria.xcodeproj -scheme Kria \
  -skipPackagePluginValidation -derivedDataPath .derived-data \
  CODE_SIGNING_ALLOWED=NO \
  -destination 'platform=iOS Simulator,name=<AVAILABLE_IPHONE>' \
  -only-testing:KriaTests/DevicePhotoRenderE2ETests test
```

At minimum, narrated-story and cleaned-narrated-story must be active and green.
Record the generated MP4 path and assertions; a skipped case is not evidence.

## Static release gates

Run from the repository root:

```bash
bash scripts/check_claude_md_size.sh
bash scripts/preship-check.sh
git diff --check
```

If generated API types changed, regenerate them using the repository command and
include the generated diff. A mismatch is a STOP, not a waiver.

## Signed physical-device canary matrix

Use short, disposable media with clear speech and known expected output. Run on
the oldest supported test phone and a current phone. Use the signed build that
will reach production.

| ID | Journey | Required setup | Expected result |
|---|---|---|---|
| D0 | Protocol/update | Old protocol build, then current protocol-2 build | Old build shows blocking update UI; current build reaches normal auth and creation |
| D1 | Existing cloud read | Open one ready pre-cutover cloud output | Project loads and video plays; no mutation occurs |
| D2 | Base device lifecycle | One simple supported montage | `awaiting_device` → local render → upload/complete → `published`; Play/Save/Share work |
| D3 | Single-clip Talking | 15–30 s portrait clip with clearly audible EN or TR speech | Original speech audible; correct-language captions visible and timed; output published |
| D4 | Talking word captions | Same shape, word style | No flashing/unreadable words; words track speech across the full clip |
| D5 | Caption edit | Edit a line before Save, play; then Save and play replacement | Voice remains audible before Save and after rerender; edited caption persists |
| D6 | Multi-clip self-narrated Talking | Speaker clip plus at least one cutaway | If enabled: continuous speaker audio, muted cutaway, captions remain timed. If disabled: not advertised/refused before Job creation with actionable copy |
| D7 | Generic recorded-voiceover montage | 2–3 clips plus a 10–20 s recording | Voiceover audible; footage/music bed follows expected gain; no captions are invented by this format |
| D8 | Recorded narrated | At least two clips plus recorded narration | Voiceover audible; caption text appears and tracks narration; output published |
| D9 | Guided voiceover | Guided story with Visuals plus recorded narration | Narration audible, Visuals render, expected narration text/captions appear, output published |
| D10 | Structural refusal | Request one unsupported/cloud-only combination | Capability absent or typed refusal before Job creation; no silent cloud render; actionable UI |
| D11 | Transient recovery | Controlled retryable device failure | `needsAttention` shows retry; retry mints fresh identity and reaches published |
| D12 | Legacy mutation fence | Attempt a render-affecting edit on D1 after device-only admission | Typed non-mutating refusal; original ready output still plays |

### Cross-account requirement

- Run D2 once on each of the four active mobile accounts.
- Run D3–D12 on the designated canary account.
- Repeat D3 and D7 after `CLOUD_RENDER_EXECUTION_ENABLED=false`.

## Per-canary assertions

For every render canary, verify all of the following:

1. `/creation-threads/capabilities` advertised exactly the path used.
2. No cloud source upload occurred for original footage; an analysis proxy may
   upload. Recorded voiceover uses its existing cloud upload contract.
3. The Job/variant records `render_destination=device` and reaches
   `awaiting_device` before export.
4. The installed app accepts every `required_capability`; there is no `.cloud`
   decision hidden behind the UI.
5. Audio is present and subjectively correct: speech/narration intelligible,
   cutaway audio muted where promised, and no unexpected silence.
6. Captions exist only where the format promises them, use the correct language,
   remain inside the canvas, and stay synchronized through the final spoken word.
7. Upload reservation, verification, completion, and generation identity match.
8. Final status is `published`; playback works after app relaunch and on another
   signed-in device where applicable.
9. No cloud render task begins for the canary.

## Performance and device health

For D2–D9 record:

- source duration and export duration;
- wall-clock time from approval to `awaiting_device`, export start to local
  ready, and completion to `published`;
- peak memory if available;
- thermal state at start/end;
- app foreground/background transitions;
- device model, OS, app version/build.

Use the existing general phone qualification target as the release budget:
sustained 30-fps preview, seek p95 at most 250 ms, and a 60-second export within
120 seconds, with no crash or critical thermal state. A shorter canary should not
be extrapolated to claim the 60-second budget; run one 60-second qualification
case on the oldest supported phone.

## Admission and cloud-disable probes

Run the exact HTTP, broker, and database probes in
`docs/runbooks/ios-device-only-runtime.md`. Expected boundary results remain:

| Probe | Expected |
|---|---|
| Web creation mutation | 410 `web_creation_retired` |
| Native mutation without protocol 2 | 426 `native_update_required` |
| Protocol-2 native mutation with fake auth | 401 after admission passes |
| Cloud-only native creation path | 422 `device_render_unsupported` |
| Post-kill-switch cloud task | Terminal `processing_failed` with `cloud_render_disabled`, before renderer/upload work |
| Device recipe after kill switch | Continues to `awaiting_device` and can publish |

Unknown/timeout is not an acceptable expected result.

## Evidence record template

Keep this record private:

```text
Rollout stage:
UTC start/end:
Git SHA:
Fly deployed revision:
iOS version/build:
Device model / OS:
Active boolean flag names:
Verified capability names:
Canary ID:
Job ID:
Variant ID:
Attempt ID:
Status transitions:
Audio result:
Caption result:
Playback/relaunch result:
Export duration / wall time:
Peak memory / thermal state:
Cloud-task check:
PASS / FAIL:
Notes without PII or media/transcript content:
```

Never record access tokens, signed URLs, Redis/database URLs, storage paths,
transcript text, email addresses, or user IDs.

## Go/no-go checklist

- [ ] A1–A5 implemented and green.
- [ ] Focused backend suites green.
- [ ] Full backend/lint gates green for changed Python.
- [ ] KriaMediaEngine and `make ios-verify` green.
- [ ] Required native render cases active and green.
- [ ] Signed protocol-2 TestFlight build installed and verified.
- [ ] KRI-241 closed with device evidence, or editor lane explicitly disabled
      and the accepted limitation recorded.
- [ ] D0–D12 pass according to enabled scope.
- [ ] D2 passes on all four active mobile accounts.
- [ ] Broker and database drain agree on zero live cloud work.
- [ ] D3 and D7 pass after cloud execution is disabled.
- [ ] Twenty-four-hour monitoring completes with no STOP condition.
