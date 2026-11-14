# KRI-1423 failure modes: a Save of a photo the creator cut or trimmed

The founder could not save their last edit on the phone while testing the split-screen editor
(split sections, multi-clip panels, photos, trims, cuts). Prod logs only keep about 100 lines, so
the rejected request itself was gone by the time we looked. The shape below is the one the phone
writes and the server refuses, reproduced from the Swift code that writes it.

## The failure

The phone cuts a clip with `NativeEditorSession.piece(of:from:to:length:rate:)` and trims its left
edge with `updateTrim`. Both move `in_s` forward by the cut or trim length. A photo has no source
timeline, but the phone does not special-case it, so the right half of a cut photo (or a photo
trimmed from the left) is sent with `in_s > 0` and `source_start_locked: true`.

`resolve_authored_phone_slots` accepts it. The recipe compile then raises
`authored image cannot have source trim or speed` (`phone_authored_timeline.py`, the
`still and (start != 0 or rate != 1)` check), which the Save route answers with 422
`unsupported_phone_edit`. The phone shows "This edit couldn't be prepared for this iPhone. Your
edits are still here. Undo the last change and try saving again."

## What the validator must keep refusing

A still has no clock, so a speed other than 1 or a `source_end_s` on it is a real contradiction
and stays refused. Only `in_s` is void on a still, because no frame is ever read from it.

## Rows

| Row | Shape | Outcome |
| --- | --- | --- |
| `photo-cut-in-panel` | two pieces of one photo in a panel, second at `in_s` 1.5 | saves; both stored at `in_s` 0 |
| `photo-trim-start-in-panel` | photo in a panel with `in_s` 1.0 | saves; stored at `in_s` 0 |
| `photo-cut-full-screen` | two full screen pieces of one photo, second at `in_s` 2.0 | saves; both stored at `in_s` 0 |
| `photo-speed-refused` | a photo with `playback_rate` 2 | still `phone_edit_unsupported` |
| `photo-source-end-refused` | a photo with `source_end_s` | still `phone_edit_unsupported` |
