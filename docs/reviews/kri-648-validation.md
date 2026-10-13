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
captions and narrated text. Sampled title preview/export bounds differed by at
most one pixel. Artifacts live outside git in
`/private/tmp/nova-kri-648-evidence/device-frames` and the physical XCTest bundle.
The render kit uses synthetic footage and supplied moving-face rectangles;
this does not establish live-face detection accuracy.

The physical journey reproduced a cache failure for the second font on iOS:
Foundation normalized an existing parent to `/var` and a missing leaf to
`/private/var`. The cache now compares the parent first while retaining the
full escape check for existing leaves. Existing hash/symlink guards passed.

Emir explicitly chose to keep TikTok/Reels/Shorts platform clearance unverified.
Platform margins remain provisional; no production-clearance claim is made.
