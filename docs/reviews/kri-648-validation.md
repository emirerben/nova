# KRI-648 Release 1 validation

The phone text pass resolves actual font faces, fit and independent effect preferences
before compiling the recipe. It must persist the same text rows the editor reads.

## Failure inventory (before implementation)

- A saved false/zero is mistaken for an unset value when applying a font or preset.
- A current-video font/color/placement loses to an account default.
- A role preference accidentally changes every text lane or changes existing videos.
- A narrow font lacks Turkish glyphs; fallback removes letters or selects an excluded font.
- A wrapped title's anchor clears a pin but its full glyph bounds overlap the pin.
- A title moves under the watermark, platform controls, captions or an animated face.
- The phone recipe contains different styles from the rows used by the editor.
- A retry reads today's preferences instead of its generation's pinned preferences.
- Font-selection failure prevents an otherwise drawable ordinary render.
- Profile changes race and overwrite a newer preference revision.
- A temporary instruction is saved globally, or a compound standing instruction saves one effect only.

## Journeys

Run real compiler/export journeys for short/medium/long titles, EN/TR, limited-coverage
fonts, required/excluded/favorite fonts, title plus pins plus captions, and changing
footage. Compare saved rows, preview and MP4 frames. Include preset/font changes,
restart, account/current-video precedence, stale writes, and preference reset.
Measure animation bounds throughout each text window. Platform-overlay clearance
requires a real posting interface; Emir explicitly left that claim unverified.
Published safe-area guides and the attached rectangles remain provisional.

Duration and scrub bounds remain the existing approved timeline. No lane ripples,
trims, resizes or time remapping are introduced. Existing editor gestures retain their
Undo boundary; preference writes use the existing memory operation Undo.

## Delivery

Release 1 includes typography, explicit preferences and KRI-539 collision handling.
Release 2 follows its merge and uses KRI-336 for approved repeated-edit suggestions.
No automatic merge/deploy. Record each command, result and HEAD in the autoship ledger.

## Evidence notes

Real iPhone 13 Pro exports passed for short/medium/long EN and Turkish titles,
captions, narrated titles/pins, separate label/lyric roles and all 15 palette faces. Sampled caption
preview/export bounds matched exactly; title bounds differed by at most one pixel. Artifacts live outside git in
`/private/tmp/nova-kri-648-evidence/device-frames-release` and
`device-final-integrated.xcresult` in the same evidence directory.
The repeatable kit includes synthetic footage and a panned installed sample portrait.
The actual proxy/OpenCV path detected its face at five samples and reused those
proxies for five background observations. Supplied moving-face rectangles separately
exercise the collision contract. These fixtures do not establish live-camera accuracy.

The all-font device proof caught Pacifico ligature drift between the unshaped
portable recipe and CoreText preview. Only version-1 rows now request shared shaping;
legacy rows retain their existing render path. The integrated 26-case proof passed.
The repeatable kit now also includes a 27th journey with caption blocks of
different widths and heights, fitting every block against the same obstacles.

Current-source production-runtime overlay proofs passed 62 legacy and 30 new styled
overlays. Docker disk exhaustion required task-owned RAM mounts with current source;
no unrelated images or containers were pruned. Reports and reviewed montages are
in `overlay-proof/legacy` and `overlay-proof/kri648` under the evidence directory.
These run the production image's Skia/FFmpeg/font runtime with current source
streamed into task-owned RAM mounts; the complete image was not rebuilt after
the shared Docker disk filled. Render-only reports do not claim OCR verification.

The focused backend and free replay run passed 1,684 checks with 15 optional/live
skips. All 593 selected reliability checks passed with no environment skips.
The final native build ran 1,732 tests: 1,708 passed, 24 opt-in tests skipped,
and none failed. All 190 selected UI journeys passed on an isolated simulator;
the existing gallery read/upload journey passed on its third attempt and is
reported as flaky. A separate final-build run covers the two settings journeys.
Results, logs, measurement reports and exported media remain outside Git.
The final 27-case kit passed native simulator preview/export, including differently
sized caption blocks. CI maps `kri-648-phone-text` to the same export kit and both
settings journeys, retaining their results under the existing journey artifact.
Font registry, web mirror, active-font count and bundled identification data are
synchronized at 45 active faces. The existing 44 embedding vectors remain within
0.000001 cosine distance of their previous values. The production artifact loader,
68 focused font checks, 99 CI regression checks, generated-contract verification
and web typecheck passed. The final native rebuild also passed all 1,708 enabled
tests against these artifacts.

The raw-request reader exception is limited to initial dispatch pinning validated
visual properties. Route, duration, voice, order and required copy retain their
existing authority; workers and retries use stored rules.

The real-memory font-only chat replacement journey failed before its fix:
superseding a title rule dropped its zero outline, disabled shadow, color and
corner, exposing the parent rule's effects. Chat supersede now merges only
specified properties before atomically replacing the row. Settings PATCH remains
a complete rule replacement for individual resets. The journey passes through
revision-checked storage, duplicate replay, actual text compilation and Undo;
all 65 focused memory/extractor/typography checks passed.

New caption geometry requires client protocol 8. The independent settings choices
include Use defaults, Automatic and a required font, avoiding a misleading inherited
font state. Caption appearance includes the resolved corner, width and alignment.

The physical journey reproduced a cache failure for the second font on iOS:
Foundation normalized an existing parent to `/var` and a missing leaf to
`/private/var`. The cache now compares the parent first while retaining the
full escape check for existing leaves. Existing hash/symlink guards passed.

Emir explicitly chose to keep TikTok/Reels/Shorts platform clearance unverified.
Platform margins remain provisional; no production-clearance claim is made.
