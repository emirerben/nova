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
Measure animation bounds throughout each text window. Real-device platform overlays
are a separate required evidence item; published safe-area guides are provisional.

Duration and scrub bounds remain the existing approved timeline. No lane ripples,
trims, resizes or time remapping are introduced. Existing editor gestures retain their
Undo boundary; preference writes use the existing memory operation Undo.

## Delivery

Release 1 includes typography, explicit preferences and KRI-539 collision handling.
Release 2 follows its merge and uses KRI-336 for approved repeated-edit suggestions.
No automatic merge/deploy. Record each command, result and HEAD in the autoship ledger.

## Evidence notes

Real iPhone 13 Pro exports passed for short/medium/long EN and Turkish titles,
captions, narrated titles/pins and all 15 palette faces. Sampled caption
preview/export bounds matched exactly; title bounds differed by at most one pixel. Artifacts live outside git in
`/private/tmp/nova-kri-648-evidence/device-frames-final` and the physical XCTest bundle.
The repeatable kit includes synthetic footage and a panned installed sample portrait.
The actual proxy/OpenCV path detected its face at five samples and reused those
proxies for five background observations. Supplied moving-face rectangles separately
exercise the collision contract. These fixtures do not establish live-camera accuracy.

The all-font device proof caught Pacifico ligature drift between the unshaped
portable recipe and CoreText preview. Only version-1 rows now request shared shaping;
legacy rows retain their existing render path. The corrected 24-case proof passed.

Current-source production-runtime overlay proofs passed 62 legacy and 27 new styled
overlays. Docker disk exhaustion required task-owned RAM mounts with current source;
no unrelated images or containers were pruned. Reports and reviewed montages are
in `overlay-proof/legacy` and `overlay-proof/kri648` under the evidence directory.

New caption geometry requires client protocol 8. The independent settings choices
include Use defaults, Automatic and a required font, avoiding a misleading inherited
font state. Caption appearance includes the resolved corner, width and alignment.

The physical journey reproduced a cache failure for the second font on iOS:
Foundation normalized an existing parent to `/var` and a missing leaf to
`/private/var`. The cache now compares the parent first while retaining the
full escape check for existing leaves. Existing hash/symlink guards passed.

Emir explicitly chose to keep TikTok/Reels/Shorts platform clearance unverified.
Platform margins remain provisional; no production-clearance claim is made.
