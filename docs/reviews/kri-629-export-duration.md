# T1 strict exported duration (KRI-629)

## Acceptance and failure modes, before implementation

The original request was “40 saniyenin altında olsun” with a small closing
“Loy Tree” badge. The independent 2026-10-12 artifact is already correct:
FFprobe measures 39.576667 s including the 1.6 s branded ending; the last
three seconds of footage show the badge. The incorrect question and receipt
were the incident. This change closes the remaining boundary failures:

- A strict maximum must not use the approximate target's ten-percent tolerance.
- A shorter valid result must not fail for being too short.
- The branded ending must count toward a whole-video maximum.
- Cleanup and a natural source estimate must not silently waive a maximum.
- Missing final measurements must not become a verified success.
- Upload, cached retry, and an edited revision must retain the same bound.
- Old contracts and requests without a maximum must remain readable.
- Badge timing uses edited footage time; it must not extend the video.

Evidence source: KRI-633 and the local run
`kria-cabin-test/T1_deniz_subscribe_cringe/runs/2026-10-12_0DD3FF4E/`.
Media, private transcripts, signed URLs and job payloads are not committed.
The unrelated opening-line and spelling issues remain outside this change.

### Corrected duration clauses

A global timing requirement can retain a rejected earlier duration before its
current limit: "Do not keep it under 20 seconds; keep the video under 40 seconds"
or "20 saniyenin altında olmasın; 40 saniyenin altında olsun". The recorded
`duration_s: 40` must still yield a strict 40-second whole-export maximum. Ignore
only the negated clause; preserve the later matching positive clause. The
approved-brief digest and final-file equality refusal must remain enforced.

## Timeline decisions

1. Duration source: the compiled recipe owns edit duration; the measured exported
   asset owns finished duration. The creator contract owns the exclusive limit.
2. Ripple: no new cuts or inserts; existing cleanup keep segments and text
   projection stay authoritative. Continuous music is unchanged.
3. Scrubbing: existing editor/preview recipe duration and branded preview remain
   authoritative. This change adds a limit check, not another scrub duration.
4. Resize: existing handles, limits and frame rounding are unchanged. No forced
   trimming, rate changes or branding removal to meet a limit.
5. Undo: existing editor snapshot boundaries stay unchanged; checks add no undo
   entries. Revised exports revalidate the bound.
6. Parity: replay the real T1 keep segments through the Talking compiler, assert
   closing placement, and test precise native and server export boundaries.

## Verification record

The implementation reads the exclusive maximum from the immutable approved brief,
validated against the existing contract digest. Stored contract and device-request
schemas are unchanged. The response-only field lets new iOS builds check both the
estimated complete length and the measured export. Publication independently probes
the uploaded file, then rechecks the revision before saving its measured duration.

New phone requests reserve the standard 1.6 s ending. Historical requests are checked
against their actual declared ending at upload, so a valid old unbranded file is not
rejected by assuming that it has an ending. Approximate requests retain their existing
tolerance. An explicit choice to accept a longer result changes the approved brief,
including when the choice equals the old cap numerically. Manual saves inherit that
exact brief; a malformed or mismatched binding refuses rather than drops authority.

No new speech truncation, speed changes, badge extension, or automatic montage/music
budgeting is introduced. Over-budget compiled phone plans refuse with a shorter-part
or longer-video choice. Cloud rendering is outside this iOS fix. Old workers can read
the stored objects; enforcement requires the new API/worker code. Older clients may
ignore the optional limit, but the new server still rejects an invalid uploaded file.

### Executed evidence

- Regression tests were written before implementation: the initial run was red
  (23 failures, 2 passes). Final cases exercise EN/TR exclusive wording, unrelated
  and trailing negation, quoted/scoped numbers, exact-40 boundaries, naturally shorter
  results, approximate targets, explicit waivers, changed authority, later manual
  edits, stale completion, and old requests with no ending.
- Real native exports used the original T1 H.264 source and the local HEVC Main10
  59.94 fps variant, through the production Talking compiler, original 13 cleanup
  keep intervals, `AVFoundationLocalExporter`, and `DeviceRenderCoordinator`.
  Both measured **39.576667 s**, including the ending. Server FFprobe independently
  accepted both files. This was macOS native rendering with local source resolution
  and an in-memory publication acknowledgement, not a live chat or iPhone run.
- The compiled edit is 37.977 s; “Loy Tree” occupies 34.977–37.977 s. Inspected PNGs
  at 36.5 s show the exact words, and the later frame shows the Kria ending.
  The no-text control keeps timing/footage unchanged: badge-color pixels in the upper
  band drop from 20,034 to 468. The HEVC case has 19,864 badge-color pixels. Pixel
  counts establish the negative control's signal; visual inspection establishes text.
- `KRIA_IOS_TEST_MODE=unit make ios-verify`: 1,896 passed, 25 skipped, no failures.
  This is the iOS simulator app build and suite. `swift test --filter
  DeviceRenderCoordinatorTests`: 16 passed. No physical-phone app was installed.
- No prompt/model changes or live provider calls. Model evaluation cost: $0.

Native reproduction: generate snake-case device requests with the production compiler
and source fingerprints, with the keep intervals recorded in
`tests/services/test_strict_export_duration.py`. Supply `cases.json` entries with
`name`, `source_file`, `request_file`, `expected_edit_s`, `maximum_s`, and
`badge_sample_s` under `KRIA_T1_DURATION_DIR`. Run `swift test --filter
StrictDurationT1Tests` from the media-engine package, then the API's
`tests/services/test_strict_export_duration.py` with the same environment variable.
The opt-in test skips without private local media; it never downloads or uploads it.

Retained local artifacts are under `.dev/t1-duration/{h264,hevc10}/`: output MP4,
badge/outro PNGs and report JSON; `.dev/t1-duration/server-report.json` records the
independent file probe. The sanitized measurements above are the durable review record.

### Remaining verification

KRI-629 and parent KRI-633 stay open for the deployed API/worker revision, new iOS
build, an original-request replay on the physical iPhone, and the resulting chat
receipt. The original request also includes opening removal, karaoke captions,
emphasis and English spelling; this bounded replay does not certify those unrelated
requirements. No production deployment or full request-fulfillment claim is made.
