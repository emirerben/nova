# Creation and media intake

Creators choose a format, attach footage/audio, review the proposed video and
explicitly confirm creation. File-read, upload and rendering failures must remain
understandable and recoverable.

## Sub-features

- `creation-confirm`: Montage → Send clips → Create this video → Open editor.
- `creation-photos`: in-app and Apple Photos pickers return readable media.
- `creation-audio`: Narrated requires a deliberate voiceover/footage-audio choice.
- `creation-render-state`: ready, unsupported-recipe and transient retry cards.

## How to get to it (user POV)

- Open projects → New chat, then choose Montage or Narrated in `format-carousel`.
- Use Choose videos (`choose-videos`), Choose from Photos, or Attach footage.
- Switch pickers with `gallery-toggle-picker`; finish with `photos-picker-done`.
- Review and tap Send clips → Create this video. The ready card offers Open editor.

## Driving it with XCUITest

Preconditions: Launch/Doctor. Tests seed media/capabilities at their existing
transport boundary; Photos tests seed videos in the owned simulator's library.

```sh
"$KRIA_VERIFY_CONTROL" drive --run "$KRIA_VERIFY_RUN" --feature creation \
  CreationUITests/testCreationWithAttachedFootageReachesConfirmationAndReadyForBothRuntimes \
  CreationUITests/testGalleryPickIsReadFromPhotosAndReachesUpload \
  CreationUITests/testLibraryPickerReturnsToChatByDoneAndBySinglePick \
  AttachmentFlowUITests/testNarratedAttachmentsSwipeAndRequireExplicitAudioChoice
```

- **Confirm both flows.** Select `format-montage`, Send clips, then Create this
  video. Both v1/v2 fixture flows eventually offer Open editor.
- **Read Photos.** Select gallery tiles in touch order, deselect some, switch
  between pickers, and finish. Media reaches upload reservation rather than
  `This file couldn't be read`; the seeded reservation response intentionally
  fails and must be reported as that boundary limit.
- **Choose narration.** Select `format-narrated` → Attach footage. Swipe from
  `attachment-step-footage` to voiceover; Record/upload choices are visible and
  continuing requires an explicit choice. Use `voiceover-skip` to reach overlays.
- **Render failures.** In a separate fresh run select
  `DeviceRenderUITests/testUnsupportedRecipeAsksForANewEditWithoutRetry` and
  `DeviceRenderUITests/testTimedOutRenderRetriesToReady`: unsupported recipes
  have no retry, while a transient timeout offers `device-render-retry` and
  reaches the synced state after tapping it.

## Gotchas

- Photos authorization is changed only on the owned disposable simulator.
  Actual selected bytes are read, but the fixture deliberately lacks valid
  upload reservations. Successful upload is unverified, even if this test passes.
- Creation-ready replies are fixture API responses. DeviceRenderUITests also
  substitute AVFoundation export and upload; Play/Save/Share there are state/UI
  evidence, not a rendered media file or backend publication.
- The format chooser does not select unavailable capabilities. A missing
  capability/media precondition is a reported gap, not a reason to inject an
  internal model value or bypass creator confirmation.
