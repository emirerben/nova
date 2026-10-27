# Native verification coverage, 27 October 2026

The iPhone map now covers eight fixture journeys. The existing creation, media,
editor, delivery and slides drives are joined by account access/deletion,
camera recovery and retention warnings. Billing and direct external publication
are recorded as missing native flows; their harness commands exit 3 and cannot
count as passing tests.

## Observed fixture proof

All eight drives passed on an owned iPhone 17 simulator: 19 tests, zero failures
and zero skips in the final matrix. Account controls and deletion use a fixture
account. Camera checks cover unavailable capture, script persistence and take
recovery. The new retention test reads the actual warning response shape,
observes the accessible expiry warning, and reuses the upload in two posts
without another upload request. This does not run a storage-deletion sweep.

The native input fingerprint is
`5be4a7fd4b7bb7c2c76c389f49a7b4f44a280db5bc5fbba4395480ffefafcd4e`.
The private `.dev/verify-kria-ios/verified-features.json` binds the individual
result bundles to this fingerprint and their native source patch. Result
bundles, screenshots where the tests capture them, failed attempts and logs are
retained under that directory. Inspected images include the photo picker,
recoverable failed camera intake, saved slide ink and the two reused posts.
Five existing selected tests do not attach screenshots; their assertions and
result bundles provide the interaction evidence.

Two preliminary retention attempts failed in the new test's context-menu
interaction. The final test focuses on the accessible warning and post reuse;
it makes no automated context-menu claim. Both failures remain in the evidence.
The helper's owned simulators were deleted after the final run; ten result
bundles remain, including those failures.

## Boundaries and remaining checks

After adding request-outcome metadata, all eight drives passed again with 19
tests, zero failures and zero skips. The request-context, structured-answer and
suggestion suites also passed 46 tests without skips. This build's fingerprint
is `1af5c1907466daff0059608c148dea1b054ca89b01cffb68346ca109029f33c4`, which
still matches after rebasing onto the corrected Cunda parent. Nine result
bundles remain under private `.dev/outcome-ios/`. Its owned simulator was deleted
after the final drive.

- Real Google identity, session refresh and server erasure are separate from
  fixture consent and deletion controls.
- The physical camera test remains opt-in. Camera/microphone capture,
  interruption movies and audio require the paired iPhone and movie inspection.
- Retention manifests and protected sources remain server checks. KRI-331 and
  KRI-652 are separate from this presentation/reuse proof.
- There is no native StoreKit purchase flow or direct platform-publication
  completion. Account settings and Share controls cannot substitute for them.
- Wi-Fi app metadata identified installed Kria build 1763. The successful
  TestFlight workflow for that build is run 40495234435, source
  `19dd554d7953ee02b224637ce4cdb1362fe92b33`. This does not establish a physical
  export or exposure to the unmerged Cunda fix in PR #1869.

These checks establish fixture UI behavior. They do not establish original
Troya, marathon, Tigre, Don Julio or Cunda fulfillment, a production render,
a real Photos write, or public-launch clearance.

## Cunda candidate compatibility

An isolated read-only server process used the candidate song-reference helper
with the deployed compiler and the saved Cunda approval. It bound all eleven
original video identities and ten approved photos, compiled the 28.804-second
phone recipe, and passed the contract check with three pinned text requirements.
No account state changed. This is recipe compatibility proof, not deployment,
a native MP4 or Photos proof. The sanitized result is in
[cunda-candidate-phone-plan.json](cunda-candidate-phone-plan.json).
