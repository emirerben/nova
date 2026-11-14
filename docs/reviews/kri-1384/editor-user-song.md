# KRI-1394: a chat song change survives the iPhone editor

Chat's `set_user_song {volume, window_start_s, window_end_s, remove}` writes the draft's flat
`user_song` section (`EditorCommitUserSong`, `kria_editor_ops.merge_editor_draft`). That is the same
section the editor's Sounds tab saves. The iPhone editor did not load it. `DraftSnapshot.stagedLaneKeys`
listed every staged chat lane except `user_song`, and `EditorDocument(snapshot:)` never decodes a
song edit. So an editor opened before approval showed the saved song. A Save from there sent only
the creator's own changes and dropped the chat's.

## Fix

- `DraftSnapshot.stagedChatEdit` reads the flat `user_song` key into `StagedChatEdit.userSong` and
  marks `.userSong` changed. An unreadable field drops the section.
- `NativeEditorSession.stagedChatDocument` puts it on the staged document's `userSong`. It applies
  the gates the hand-edit setters use: each field only while its `user_song.*` capability is open,
  and never a lip-sync song's window. Preview and Save then go through the existing song path. That
  path re-clamps a start to `MIN_PLAYABLE_SONG_S` and to the creator's end.
- `DraftSnapshot.editorDraft` treats a head carrying `user_song` as a chat head, not a committed
  snapshot. This is the same rule as the other chat lane keys (KRI-1333).

## Failure modes, written before the tests

1. The draft's song section is ignored. The editor opens clean and Save sends nothing for the song.
2. The song is shown but Save omits it, because `.userSong` is not a changed section.
3. A hand edit after the chat edit does not win its field, or it wipes the chat's other fields.
4. A chat removal is not shown as removed, or Save sends more than `removed`.
5. A lip-sync song's window moves although its takes were filmed against it.
6. A head whose only chat lane is the song is read as a committed snapshot, so its `sections`
   become the clean document.
7. Undo does not return to the saved song.
8. A chat turn that lands while the editor is open does not stage the song.

## Tests

`src/apps/ios/Tests/KriaTests/NativeEditorSessionTests.swift`:

- `testChatSongVolumeAndWindowOpenAsUnsavedEditsThatSaveAndYieldToAHandEdit` (modes 1, 2, 3, 7)
- `testChatSongRemovalOpensRemovedSavesTheRemovalAndUndoes` (4, 7)
- `testChatSongWindowOnALipSyncSongStagesOnlyTheVolume` (5)
- `testChatSongArrivingAfterOpenStages` (8)

`src/apps/ios/Tests/KriaTests/ChatDraftStagingTests.swift`:

- `testAChatHeadWithOnlyASongEditIsNotACommittedSnapshot` (6)

## Results (iPhone 17 Pro simulator, iOS 26.5)

Before the fix, on the commit that adds the four session tests first: 4 tests, 18 failures. The
hand-edit Save sent `EditorCommitUserSong(volume: 0.7, windowStartS: nil, windowEndS: nil)` instead
of keeping the chat's window `1.5`...`10`. Saving the chat edit alone sent no `user_song`.

```
testChatSongArrivingAfterOpenStages: XCTAssertEqual failed: ("nil") is not equal to ("Optional(EditorUserSongState(volume: Optional(0.3), ...))")
testChatSongRemovalOpensRemovedSavesTheRemovalAndUndoes: XCTAssertEqual failed: ("nil") is not equal to ("Optional(EditorCommitUserSong(volume: nil, windowStartS: nil, windowEndS: nil, removed: true))")
testChatSongVolumeAndWindowOpenAsUnsavedEditsThatSaveAndYieldToAHandEdit: XCTAssertEqual failed: ("Optional(EditorCommitUserSong(volume: Optional(0.7), windowStartS: nil, windowEndS: nil, removed: false))") is not equal to ("Optional(EditorCommitUserSong(volume: Optional(0.7), windowStartS: Optional(1.5), windowEndS: Optional(10.0), removed: false))")
testChatSongWindowOnALipSyncSongStagesOnlyTheVolume: XCTAssertEqual failed: ("nil") is not equal to ("Optional(EditorCommitUserSong(volume: Optional(0.5), ...))")
Executed 4 tests, with 18 failures
```

After the fix, all five tests pass. `NativeEditorSessionTests`, `ChatDraftStagingTests`,
`NativeEditorMergeTests` and `EditorDocumentTests` together ran 505 tests with 0 failures.

Control for mode 6. With the `payload[Self.userSongKey] == nil` gate removed,
`testAChatHeadWithOnlyASongEditIsNotACommittedSnapshot` fails. The clean text becomes the head's
`["unsaved"]` instead of the published `["hello"]`. With the gate restored, it passes.

## Chat undo puts the song back (KRI-1384, after #2316)

#2316 drops a staged chat lane from an open editor when a newer chat head carries that lane at the
saved value (`DraftSnapshot.restoredChatSections`, `NativeEditorSession.dropRestoredChatSections`).
The song lane now takes part. Replaying a `set_user_song` restore record writes the section as
`removed: false` with no field set (`chat-undo.md`), so `chatLanes` reads a readable, empty
`user_song` as restored and a non-empty one as changed. An unreadable one is neither. No editor
snapshot carries the song edit, so the drop compares `document.userSong` with the staged song
instead of `snapshot(for:)`, which is nil for this lane on both sides.

Today the server does not seal `set_user_song` turns for undo, so chat refuses to undo them. This
covers the iPhone half only and is not an end-to-end song undo.

- `testANewerChatHeadThatPutsTheSongBackDropsTheStagedSong`: the staged song leaves the editor,
  the editor's Undo brings it back, and the same head is not applied twice.
- `testANewerChatHeadThatPutsTheSongBackLeavesASongTheCreatorEditedSinceAlone`: a volume the
  creator set after the chat staged the song survives.

Controls. Without the restored insert in `chatLanes`, the first test fails 5 assertions (the song
stays at volume 0.4). With the drop comparing `snapshot(for:)` as before, the second test fails:
the creator's 0.7 is erased to nil. Both pass with the change. After the rebase the four suites
above ran 511 tests with 0 failures.

## Not covered

- Native export of a staged chat song is not exercised here. The preview and Save reuse the
  existing hand-edit song path (`effectiveUserSong`, `commitUserSong`), which
  `testUserSongEditsDriveDirtyStateCommitUndoAndPreview` already covers.
- A merge with a newer render counts a staged chat song as the creator's edit
  (`NativeEditorMerge.converge` compares `userSong` outside the per-lane `assistantSections`).
  Inferred from reading `applyConverged`, not tested: this changes only whether the "Some of your
  edits were replaced" notice appears when the latest render replaced the song. The same was true
  before this change for any unsaved song edit.
