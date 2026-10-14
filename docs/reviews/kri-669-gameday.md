# Gameday hierarchy and clip addition (KRI-669)

## Incident and acceptance

The October 14, 2026 job `f4993a14-bcc5-4330-9654-f9cbc35d463c`
compiled “Gameday”, “Aston Villa v Fenerbahçe”, and “6 am” at 52px each,
with equal centre spacing, despite the request for a smaller second title.
The initial strategy retained only wording and corner placement. The saved
editor head subsequently had different sizes and positions and must not be
used as evidence for the initial layout.

The creator also reported adding a fifth video through the iPhone timeline
plus button: duration grew, the clip was not visible, and preview showed the
old finished video. The job's current raw variant has no admission registry;
the exact import failure is not established by server state. Static source
catalog and native placement defects are covered separately from that claim.

Scope is heading hierarchy/group spacing and reliable native clip addition.
KRI-648 owns font selection and personal preference learning. Related incidents
are KRI-179 and KRI-535.

## Failure scenarios recorded before implementation

- Relative size and “under” relationships disappear during request/brief
  extraction; equal pins can incorrectly pass the composition shortcut.
- A wrapped title changes its measured height, causing overlap or changing
  the intended relative sizes when each renderer fits lines independently.
- Unrelated or non-concurrent text in one corner is incorrectly grouped;
  manual positions or legacy pins are unexpectedly restyled.
- A ready admitted source is absent from the returned timeline catalog or
  resolves to a different original; a sanitized projection leaks private data.
- Placement changes the document before its source resolves; cancellation,
  stale generation or duplicate callbacks append an invalid or duplicate slot.
- Slot identity changes on cache refresh, undo/redo, save or reopen; the newly
  appended clip remains offscreen.
- A missing import is silently polled for minutes or an obsolete finished
  render appears as though it represented the creator's current edits.

## Timeline contract

1. Duration and ordered clip geometry are owned by
   `NativeEditorTimelineProjection`; components consume that projection.
2. Addition appends editable content before the branded outro. Existing text,
   visual, motion/camera, SFX and media-overlay timestamps are unchanged;
   continuous music is unchanged. The outro follows the projected end.
3. Ruler, playhead, clip geometry, pointer/keyboard seeking and inverse mapping
   use the projected editor bounds even while preview is rebuilding or failed.
   An obsolete player's playback bounds remain separate.
4. Existing trim handles, minimums, rounding and transition caps are unchanged.
5. One successful source placement is one undo snapshot. Source resolution and
   failed placement create no snapshots. Placement UUID is the clip identity.
6. Admission → timeline GET → preview → save/reopen → exported recipe must
   preserve source identity, order, duration and the existing text edits.

## Verification record

Execution worktree: `/private/tmp/nova-kri-669-gameday`, branch
`codex/kri-669-gameday`, fetched base `24fbb0fcb1e0b5eb79a458e21f3ccc5d442a8001`.
An isolated migrated database is selected by `scripts/check-api.sh`.

The final implementation preserves optional heading/group/size intent through
the brief and compiler, measures groups on the actual portrait/landscape canvas,
retains measured line breaks across renderers, and verifies relationships before
skipping composition. Default sizes are 72/52/40px with 24/8px block gaps; exact
creator sizes remain exact. Legacy pins retain their original serialization/layout.

Device timeline responses now carry every authorized admitted source and its
baseline generation. Native placement resolves the original before mutation,
assigns the placement UUID, reveals the clip and creates one undo snapshot.
Polling distinguishes uploading from missing admitted imports; obsolete finished
renders cannot appear or export as current. No registry-loss repair was made.

### Checks and evidence

| Check | Result |
| --- | --- |
| Text pipeline/brief/composer regressions | 485 passed, including 19 gameday/neighbour cases |
| Admission/timeline/save boundary suites | 243 passed, one existing xfail; route/worker/compiler real, storage/DB doubled |
| Free replay agent evaluations | 1,187 passed, 15 skipped; no live/paid provider calls |
| `make verify-editor-timeline` | 209 web assertions, desktop/mobile browser checks, 582 API checks passed |
| Final `bash scripts/preship-check.sh` | Green; 1,381 mapped reliability assertions passed |
| Mobile OpenAPI/shared contracts | `python -m app.cli.kria_contracts --check` passed |
| iOS selector/verification script checks | 102 passed |
| Physical iPhone 13 Pro session tests | 212 passed |
| Physical gameday UI journey | Passed in 54.1s: edit, fifth clip, reveal, undo/redo, save, reopen, Photos export |
| Production-image overlay CLI | 62 cases passed; montage inspected |
| Native typography exports | Three real server recipes exported with AVFoundation; measured 72/52/40px and 24/8px gaps; wrapped text inspected |
| Phone audio parity | All 14 cases checked; seven required negative controls detected |

The physical fixture uses four bundled montage originals and a distinct Istanbul
original for the fifth clip. Inspection of the resulting 1080×1920 H.264/AAC,
8.6-second export confirmed the edited title near the start and Istanbul near the
tail. This is physical native output against deterministic provider/HTTP fixtures;
it is not an authenticated production re-send of the creator's private footage.

Retained local evidence is under `.dev/gameday-device-ui.xcresult`,
`.dev/gameday-device-verified.xcresult`, `.dev/gameday-device-export.mp4`,
`.dev/gameday-text/`, `.dev/gameday-audio/`, and `.overlay-verify/` in the task worktree.
The native typography comparison is attached to KRI-669. Generated media is never
committed. Repeat the typography prepare/export/compare commands documented in
`scripts/ios/kri-669-gameday-text.py`; the real UI journey is
`GamedayAddClipUITests/testEditedGamedayAddsFifthClipAndReopensCurrentPreview`.

`make verify-overlays` initially failed because the local container disk was full.
The equivalent CLI then passed in existing production image
`sha256:b1864bb96363a229ce19c9e3777517983a043159219aa0192e83a93670a80b13`,
with current app source and fixtures streamed into temporary memory filesystems.
No Docker images/volumes belonging to other work were removed. Native build and
verification directories are now excluded from the API Docker build context.

Merge/deployment and the original account's post-deploy re-send require the normal
approval and remain outstanding. No production app or account was modified.
