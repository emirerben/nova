# Recorded-voiceover spoken rule text

Implementation for the timed-text portion of Tigre night-boat N2 (KRI-695,
KRI-699, KRI-692). Brightness changes remain outside this patch.

## Acceptance and timing

The three exact requested lines are preserved as editable text rows, grounded
against the recorded voiceover's played word clock, and compiled into the same
native text layers used by editor Save. Captions keep their separate lane.
Each line holds for up to four seconds, capped at the next rule and recipe end.
Coincident rules, missing cues, invalid timestamps and oversized text produce
unmet receipts rather than invented timing or truncated copy.

The pinned narrated recipe owns duration. No video, audio, photo, caption, SFX
or music timestamps ripple. Existing editor resize, scrub and undo behavior is
unchanged. Saved text changes invalidate stale placement receipts.

Numbered announcements take precedence over an introductory count: “three
rules” must not trigger rule three. Exact phrase matching includes unambiguous
English contraction expansion. Unknown wording is not fuzzily inferred.
The feature uses `PHONE_NARRATED_SPOKEN_TEXT_ENABLED` and existing verified
native text capabilities. Cloud voiceover retains its existing limitations.
Drafts describe conditional placement; actual cue/placement misses are reported
after compilation. Pre-approval transcript grounding is not added by this patch.

## Evidence and remaining gaps

- Offline replay of the original incident transcript placed the three rules at
  4.38, 9.37 and 15.74 seconds, the numbered announcements, not the intro count.
  The third ask was restored from the original request for this replay because
  the failed session had subsequently superseded it. Private transcript stays
  outside git.
- Worker tests exercise a real BriefBinding, cue matching, recipe compilation,
  persisted rows and title recompilation with synthetic timings and exact N2
  copy. Contract tests cover native/cloud distinction, rollout-off, English and
  Turkish replies, missing/stale text and saved-row reconciliation.
- Shared Talking-path regressions passed. Mapped preship incident gates passed
  1,562 checks before the new tests were added to the manifest; DB-dependent
  checks skipped in the local environment. Manifest consistency was rechecked.
- The native fixture generator is `scripts/ios/phone-spoken-rules-e2e.py`.
  It deliberately uses synthetic media and clock data. The matching Swift test
  is `DeviceMontageRenderE2ETests/testSpokenVoiceoverRules`.
- Native export is **pending**: Xcode failed with “No space left on device”.
  This task's temporary build directory was removed. No native-output success
  is claimed. The available local sample recording is 23.94 seconds; the failed
  production recording was 26.33 seconds, so they were not treated as identical.
- The full web timeline gate is blocked by missing local packages
  (`canvaskit-wasm`, `class-variance-authority`, `@radix-ui/react-popover`,
  `lucide-react`); its runnable tests passed. No web runtime files changed.
- No live provider calls, deployment, or production verification occurred.
  KRI-695 remains open until the original compound request is verified on the
  affected iPhone build, including separately owned brightness work.

The implementation is a local fix, not a claim of complete request fulfillment.
