# Phone Voiceover: spoken text and selected source sound

Baseline: origin/main 1e119d4bf (2026-10-22). Reproduction: saved production request in
`tests/fixtures/kri691_boat_n1_voiceover_turns.json`, boat N1: “yirmi dakika” → “20 dk”,
and the broken-boat clip's sound beneath recorded narration. The fixture redacts the
recording URL. Original recordings and a saved transcription were found locally;
they are not committed. Native export verification uses labelled synthetic media.

## Timeline decisions

1. Duration: the pinned EditRecipeV2 video track and existing `replace_voiceover_cut`
   own duration. Narration transcript times refer to the exact pinned recording (or
   accepted cleaned derivative), never translated/corrected caption timestamps.
2. Ripple: footage changes move source sound with its clip. Narration and generated
   spoken text stay in narration time; footage reordering never shifts them. Shortening
   clamps the render projection, not the canonical text rows, so extending restores them.
3. Scrub: existing recipe/video duration remains canonical for all transport geometry.
   No new scrub or inverse mapping is introduced.
4. Resize: existing clip windows/rates and text editor controls apply. Generated text
   starts on its grounded word; explicit manual text timing wins after editing. Render
   clamps to output duration without destructive canonical rewrites.
5. Undo: no new interaction; existing one-gesture snapshot boundary remains. No backend
   operation changes native undo history.
6. Parity: paired backend compilation/save tests and native preview/export tests exercise
   chosen source sound, muted other clips, narration, and spoken text timing. Legacy
   recipes without the new explicit selection retain their previous behavior.

## Rollout and evidence

Implemented locally; draft review only. No production deployment performed. New source audio
requires an app supporting protocol 14; new features start disabled. Backend/worker must
ship before enabling after that app is available. Existing generated rows and saved audio
choices must remain playable when new creation is disabled.

Read-only deployment check: latest successful Fly Deploy workflow at bbd946b365ce;
API machine version 1774, image deployment-01M5HXGCHKT6ER80DQ550HHTKJ. Worker was stopped
(autostop possible; not treated as a failure). Latest TestFlight workflow was in progress
at 1e119d4bf. Workflow HEAD alone does not establish the source of an uploaded binary.

## Root cause and boundaries

The narrated phone compiler had opening titles and captions but no path from a
bound spoken-text requirement to an editable timed text row. Its capability and
receipt checks correctly reported that limitation. The audio contract also
rejected camera sources alongside a recorded narration; native preview muted all
camera clips whenever narration existed. Adding only a model instruction would
leave these execution boundaries unchanged.

The new worker resolves the bound text ask against the recorded word stream,
requires a unique match or an explicit occurrence, and adds the result to the
existing editable title lane. Missing/repeated words recover before a device
recipe is pinned. The compiler validates selected sound sources and marks each
clip 0/1; the final narration-bed gain is applied once. Source identity survives
splits/reordering, and spoken text retains narration time after footage changes.

Placement avoids measured captions/title rows and a conservative face region.
It does not claim sampled face or object detection over arbitrary B-roll.

## Local verification and remaining gaps

- Worker replay covers bound “20 dk”, narration timing, selected camera sound,
  missing/repeated words, and flag-off legacy behavior. The clip alignment boundary
  is replayed; this is not a live planner or a full compound boat-request replay.
- Route tests cover save/reopen, shortening before the text appears, extending to
  restore it, reordering/splitting camera clips and actual response-schema serialization.
- Native preview/export evidence and exact commands are recorded in
  `voiceover-native-export.md`. Its synthetic tones test audible selection and gain;
  its frames test “20 dk” onset. This is not an export of the original boat footage.
- Mapped reliability gates: 2004 passed, 10 skipped, 2 expected failures. Nine skips
  need local test Postgres; one needs the production SFX catalog dump.
- `make verify-editor-timeline`: 115 tests passed; seven suites could not load because
  shared dependencies lack canvaskit-wasm, class-variance-authority, Radix Popover,
  and lucide-react. An isolated install ran out of disk; its incomplete files were
  removed and the existing shared dependency link restored.
- `make verify-overlays` could not run because Docker is unavailable. This does not
  establish production-container text-render parity. Native output was checked separately.
- No prompts/provider settings changed; no paid model calls were made.

The original compound request also asks for named footage, a photo, and an ending.
This change addresses its spoken-text and selected-sound gaps. Full semantic
fulfillment of that compound request remains open under KRI-691. Production
verification must confirm the deployed API/worker revision, effective flags,
protocol-14 app, original-request replay and inspected output; workflow HEAD alone
is insufficient. Turning authoring flags off must not erase saved choices.

## Original recording timing check

The locally retained N1 transcription contains “20” at 16.086–16.296 seconds and
“dakika” at 16.296–16.596 seconds. Replaying that exact word stream through the new
helper matched the creator's “yirmi dakika” trigger and placed “20 dk” at
16.086–18.086 seconds. This verifies reuse of the saved recording timings, not a
new live transcription or visual inspection of a full real-media export.
