# KRI-1351: chat goes blank after Send (many uploads, keyboard up)

## Report

2026-11-10, Emir (TestFlight 1948, commit 0f36ed61d, client protocol 46, thread 147eb928): fifteen videos added
through the composer's +, a long prompt typed, Send tapped. The middle of the chat went white; the top bar and
the composer stayed; swiping changed nothing; the prompt was gone. After closing and reopening the app everything
was back. Yasin saw the same on the dev app after uploads plus Send ("only the middle was white").

Server side (admin events for the thread, read-only): fifteen `media_added` events in 35 s, then one
`user_message` 95 s later, all in order. Nothing wrong before the client.

## Acceptance

- After Send with the keyboard up and many clips attached, the sent prompt and the transcript stay on screen.
- The media strip looks as before (files 8 pt apart, 20 pt to other rows).
- Wrong result: a blank transcript between the header and the composer, at Send or when the reply lands.

## Root cause

`ChatTimelineGroup` drew every run of adjacent media receipts as one row of the transcript's `LazyVStack`
(since #1141). Fifteen clips make that single row about 1,560 pt tall. With the software keyboard up and a
multi-line prompt, about 380 pt of the chat is visible. On Send the stack is asked to scroll to its end while
rows are added under that one very tall row; it loses its place and realises no rows at all (accessibility
tree: scroll content 1,761-1,834 pt tall, zero children) until the reader drags it.

It needs several clips and the keyboard up at Send, so it looked random. Simulator runs without the software
keyboard never showed it.

## Fix

`ChatTimelineRow`: each file of a media receipt is its own lazy row. A row directly under another file's row is
pulled up to the strip distance (`ChatConversationSpacing`), so the strip looks the same.

## Evidence (iPhone Air simulator, iOS 26.5, Debug, offline chat fixture)

`CreationUITests/testSendingALongPromptWithTheKeyboardUpAfterFifteenUploadsKeepsTheChatOnScreen`: 15 seeded
videos picked through +, uploads land (`KRIA_CHAT_UPLOADS_LAND=1`: the fixture reserves, accepts the PUT and
attaches one `media_added` per file), the reported prompt typed with the software keyboard up, Send.

| Code | Result |
| --- | --- |
| Before the fix (4 runs, incl. the control with only the fix files reverted) | blank: "the sent prompt is drawn" fails every run |
| Variant: scroll-follow budget of #2229 lifted (1,000,000) | still blank |
| Variant: no spring animation on the sent bubble | still blank |
| Variant: keyboard dismissed before Send | fine |
| Variant: one row per media file | fine |
| Variant: eager `VStack` instead of `LazyVStack` | fine |
| With the fix | passes (2 runs) |

Neighbouring cases, with the fix: `testTypedPromptAppearsBetweenMediaReceiptAndAssistantResponse`,
`testCreationWithAttachedFootageReachesConfirmationAndReadyForBothRuntimes`, `testClipsOnlySubmissionUsesSuggestAnEdit`,
`testIncomingResponseDoesNotPullReaderFromScrolledHistory`, `testPendingSubmissionStatusStaysVisibleWithKeyboardOpenAndClosed`,
`testCleanUpSpeechApprovalShowsTheRenderStageAndKeepsTheConversationInView`,
`testComposerTrayUploadFromPhotosOpensTheGalleryAndAddsThePick`, `testFailedUploadWithRecordDoesNotBlockSendAndOffersRetry`
all pass. `KriaTests`: 3,156 passed, 1 failed (`NativeEditorSessionTests/testEditorResumeWithPersistedAndServerStopTakesTwoTapsAndSurvivesRestoration`,
the known resume flake; passes alone).

## Gaps

- Not checked on a physical iPhone. On Emir's phone swiping did not bring the transcript back; in the simulator a
  drag did. Same blank state, but the device check after the next TestFlight build is owed.
- Only media receipts are split. Another single row taller than the visible chat (a long direction card with the
  keyboard up) is still one lazy row; no report of it blanking, not tested here.
- The generation failure that followed on the same thread (`phone_plan_failed`) is a separate issue.
