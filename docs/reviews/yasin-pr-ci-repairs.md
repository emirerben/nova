# Yasin PR CI repairs — 28 October 2026

Emir approved repairing and admin-merging PRs 1902, 1903, 1905, 1909–1916 once checks pass. This change carries the shared native repairs on PR 1902; later PRs must include it and pass fresh CI.

## Failure evidence and acceptance

- `AccountPrivacyUITests.testExcludedFontSurvivesPickerReturnSaveAndReopen`: CI queried the parent Save button's frame after entering a child picker, where it does not exist. The first local replay also exposed unreliable scrolling to Fraunces. The helper no longer measures that disappearing control. The test searches the real picker, requires the font's selected state, then retains the return, Save and reopened-selection checks.
- `CreationUITests.testServerSuggestionChipsSendOnTapAndReplaceFixedChips`: the fixture delivers a full set on its second poll, so the pending assertion races normal polling. A held-pending fixture now checks the one-chip UI separately; the existing delivered-set/send/ready journey starts delivered. `SuggestionChipsTests` still checks polling, the same-set replacement and the ceiling. No production chip behavior or delays change.
- `BackgroundUploadRetryTests.testAChosenClipHasAThumbnailAndAFilenameBeforeItHasUploaded`: a five-second polling deadline raced AVFoundation on a loaded simulator; deselection could also leave an independently running thumbnail task to write after cleanup. Early-preview tasks are now owned and cancelled by the coordinator, and cancelled generation never writes. The test waits for the real decode while the upload reservation stays blocked, then checks deletion on deselection. A cancelled-preview regression checks that no thumbnail is written.

## Evidence boundaries

Local verification uses a dedicated iPhone 17 / iOS 26.5 simulator and real AVFoundation decoding. CI's runtime remains authoritative for merge. This is a focused repair, not a physical-device, account rollout or production claim. No tests are skipped or outcome assertions removed to obtain a green check. Earlier failed/interrupted local results are retained with the approving Codex session.

Separate PR 1916 repairs one explicit selector expectation to include its new journey; the remaining apparent selector failures cascaded because the table advances its base only after a successful assertion. All 41 selector checks and 30 backend copy/revision checks passed locally.

## Local result

The final font search/select/return/Save/reopen test passed (34.5 s). The same focused build passed early-thumbnail visibility and cancellation checks, suggestion policy/polling, pending-chip UI, delivered/send/ready-chip UI, and keyboard-open Open editor. The settings failure was retained separately rather than counted as a passing suite. Native orchestration checks passed 117 tests; the final UI-selection policy checks passed 25. CI must still pass on each pushed head.
