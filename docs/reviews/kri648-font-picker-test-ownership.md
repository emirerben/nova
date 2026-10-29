# Font-picker verification owns its screen bounds

The excluded-font UI replay failed before choosing a font. Its scroll helper queried the parent preferences screen's Save button after opening the font picker. The picker has a Back button and search field, with no Save footer. Reading the disappearing parent button's frame aborted XCTest before the selection, save or reopen assertions ran.

The failure is retained from CI run `40703256734`, screens job `122875269379`, and its `kri-648/native.xcresult` artifact. The unchanged test on current main `db42721ce34e38aea41303ec4775945305fa439a` reproduced the same missing `text-preferences-save` snapshot failure locally. This is a test-helper failure, not evidence that saving preferences failed in the app.

The helper now accepts explicit footer ownership. Only the two calls inside the font picker disable the parent Save bounds. Parent preferences calls retain their existing footer bounds. The change adds no action retries, timeout increases, skips or weaker assertions. Choosing Fraunces, returning, saving, reopening and requiring the saved selection all remain mandatory.

## Verification

The corrected source passed these four complete UI cases on an owned iPhone 17 simulator running iOS 26.5, with zero failures, skips or test retries:

- `testExcludedFontSurvivesPickerReturnSaveAndReopen`.
- `testTextPreferenceDraftSurvivesRoleSwitch`.
- `testTextPreferencesSaveOffEffectsAndReopen`.
- `testTextPreferencesStaleWriteKeepsEditsAndOffersReload`.

The repository's UI result verifier required all four case identifiers to have Passed status and rejected missing, unsuccessful or unexpected UI cases. The exclusion case also reached its end-of-case runtime marker. Inspected screenshots show Fraunces under Never use before saving and selected after saving and reopening.

Retained local evidence is in `.dev/account-picker/owned-footer-four-flows.xcresult`, its log, summary and test JSON, the two exported screenshots, and `owned-footer-proof.json`. The summary SHA-256 is `cfeb5804d8f1f092c619e18acc4d044cd8b63b9e121306512fd8e4f6b6623231`. The proof binds the test source and result files by SHA-256. The unchanged-main red bundle is preserved separately.

Local iOS 26.5 is not the original CI iOS 26.2 runtime. Fresh exact-head screens CI must pass before merging the integrated delivery. This correction changes no product preferences code and does not clear the remaining Cunda launch requirements.
