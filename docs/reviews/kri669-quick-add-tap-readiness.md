# Keep the Gameday quick-add check reliable

PR #1890's creation job failed while waiting for the fifth Gameday clip.
The edit remained at four clips. This change hardens the UI test's tap
readiness checks. It changes no app, API, fixture transport, or renderer code.

The original journey still edits text, adds the distinct fifth source, checks
the current preview, exercises undo and redo, saves, reopens, and exports to
Photos. All outcome assertions and timeout values remain unchanged. The test
also verifies that the menu owns the next tap and that controls are enabled.

## Evidence and limits

The failed CI job is [122721909697](https://github.com/emirerben/nova/actions/runs/40659294757/job/122721909697).
Its retained native artifact is [12804931871](https://github.com/emirerben/nova/actions/runs/40659294757/artifacts/12804931871).
The CI device was iPhone 17 Pro on iOS 26.2.

A clean worktree of unchanged main `7e013cf56e2213b5c166e26d6a9ca3c4aa6e5779`
also failed this test locally, but earlier: the first plus tap did not open
the menu. That is not an exact reproduction of CI's later fifth-clip failure.
The local simulator runs iOS 26.5; iOS 26.2 is unavailable locally.

With only tap diagnostics and screenshots added, the entire journey passed
on the same unchanged app code. The controls were enabled. The final change
removes those successful-path delays. It requires equal, finite, usable
bounds on two polls before synthesizing a tap, following the inspector
tests' existing stable-geometry pattern. The deadline remains five seconds.

The complete journey then passed with that readiness condition and no
successful-path diagnostic screenshots. One test executed with no failures.
After integrating current main `1882b38392b82f0e1ff53c0b1b1b0bcc478ac1b3`,
including the native permission-recovery change, the full journey passed again
with one test, zero failures and zero skips.

Failure-only screenshots now preserve future missed-control or missing-state
evidence instead of leaving only a label dump.

This supports a timing-sensitive test correction. It does not establish the
precise cause of the original CI failure or prove a production import bug
fixed. Fresh CI on its original runtime remains mandatory. If that fails,
inspect the new evidence before another correction; do not rerun blindly.

```bash
make ios-generate
xcodebuild -project src/apps/ios/Kria.xcodeproj -scheme Kria -configuration Debug -destination 'platform=iOS Simulator,id=EA0784A0-7DBD-4390-84E2-27AEB5BF7265' -derivedDataPath .dev/gameday-add/ios-build -skipPackagePluginValidation -parallel-testing-enabled NO -only-testing:KriaUITests/GamedayAddClipUITests CODE_SIGNING_ALLOWED=NO test
```

Use an available local simulator identifier when repeating the command.
The task retains `unchanged-main.xcresult`, `main-tap-diagnostics.xcresult`,
and `stable-tap.xcresult`, plus their logs and captured target geometry.

The actual simulator export is H.264/AAC, 1080×1920, 8.6 seconds, 7,457,976
bytes. SHA-256:
`f033dff0dab6e5ce8433bc5339286c55d5968229504d1f4560d1f8c86b019ff7`.
The inspected 0.8-second frame retains “Gameday edited title”. The inspected
5.6-second frame shows the expected fifth Istanbul source; it was compared
with the bundled original at 1.6 seconds. The 1.6-second branded outro follows
the seven-second content timeline. An initial 7.2-second inspection landed in
that outro and was corrected using the fixture's content clock.
Media remain outside git. This is simulator fixture output, not a user's
production video or physical Photos-library playback claim.

The Cunda native fix still requires its own fresh CI before merging. The
original timing request, named TestFlight candidate verification, and remaining
original-media replays stay open in KRI-984. Launch is not cleared here.
