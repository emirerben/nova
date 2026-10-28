# Keep the Gameday quick-add check reliable

PR #1890's creation job failed while waiting for the fifth Gameday clip; the edit
remained at four clips. The Gameday-specific correction changes only UI-test tap
readiness and failure evidence. The same PR also fixes an inspector Undo boundary,
covered in [the native inspector report](native-editor-gesture-verification.md).

The journey still edits text, adds the distinct fifth source, checks the current
preview, exercises Undo and Redo, saves, reopens, and exports to Photos. All
outcome assertions and timeout values stay unchanged. Tap readiness requires the
same finite, usable frame on two polls, enabled and hittable controls, and an
open add menu that owns the next tap. Failure-only screenshots retain missed
controls and missing fixture state.

## Evidence and limits

The original failed CI job is
[122721909697](https://github.com/emirerben/nova/actions/runs/40659294757/job/122721909697),
with artifact 12804931871, on iPhone 17 Pro/iOS 26.2.
Unchanged main `7e013cf56e` also failed locally, earlier at the first plus tap.
That is not an exact reproduction of the later CI fifth-clip failure. Adding
only timing diagnostics let the full journey pass on unchanged app code.
The exact original CI cause remains unconfirmed.

The final readiness change passed locally without successful-path diagnostics.
The original-runtime creation job then passed in CI 40668700336. Artifact 12807399359
retains its result bundle and screenshots showing the fifth Istanbul source and
Saved to Photos acknowledgement. That workflow still failed its separate pinch
UI check; no failed or pending gate was overridden.

After integrating main `06863782d` and the inspector-close fix, the full Gameday
journey passed again as part of `latest-main-inspector-close.xcresult`: four unique UI
journeys and 244 editor-session tests passed, zero failures and zero skips.
This local simulator is iPhone 17/iOS 26.5. Fresh exact-head CI on the original
runtime is still required.

The actual latest simulator export was written at 14:02:59 UTC. It is H.264/AAC,
1080×1920, 8.6 seconds, 7,457,976 bytes. SHA-256:
`b1baaf6bdbbc089d0273d5431f1862063a8c6a2ae4f94bcfbe1e79b9d77c5dca`.
The inspected 0.8-second frame retains "Gameday edited title"; 5.6 seconds shows
the distinct fifth Istanbul source. The 1.6-second branded outro follows the
seven-second content timeline. Media remain outside git. The simulator's Photos
acknowledgement does not establish independent library playback on an iPhone.

Retained earlier failures include `unchanged-main.xcresult`, timing diagnostics,
an unqualified old-assertion runtime result, and failed physical slider-driver
experiments. The adjacent inspector report explains which result qualifies and
which candidates were discarded. The final original endpoint test is unchanged.

This is a local test correction with an inspected fixture export, not a proved
production import fix. The Cunda native fix needs its own current-main CI and
candidate delivery. Original timing, quiet audio, and remaining original-media
replays stay open under KRI-984. Launch is not cleared.

After the native animation integration, both current export frames remained
byte-identical to the inspected title and fifth-source frames. This establishes
those visual checkpoints only.
