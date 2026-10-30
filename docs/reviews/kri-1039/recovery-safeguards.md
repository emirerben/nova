# Failed editor recovery safeguards

This bounded recovery change builds on merged compose lane E, PR #1936.
It does not replace E's sequence operations, prompts, capability registry or Save lifecycle.

## Acceptance

A creator who corrects a failed or needs-attention variant with a valid current editor head receives a staged correction in the same conversation. The original request, last-good output, title timing, source assets and audio stay intact. No approval or render starts merely because the creator sends the correction. The creator explicitly saves the draft through the existing native editor workflow.

A missing variant or missing, stale, corrupt or foreign head cannot become an invented editable draft. An edit-shaped correction receives an honest no-change alternative. Explicit new-version requests retain the planner route. An unsupported mixed bundle cannot apply only its supported subset. Unknown render outcomes prevent new correction/render work at the checked planner, commit and approval boundaries.

The immutable recovery basis contains the canonical draft ID and snapshot hash. Commit checks revalidate it even if the variant becomes ready during planning. Compose-off admission retains the previous unavailable-target behavior.

## Case matrix

`tests/kria/test_failed_draft_correction_postgres.py` contains the persisted-state acceptance checks.

- Failed and needs-attention corrections stage a new head without approval, job mutation or dispatch.
- Same-event replay and repeated worker delivery do not stage duplicate work.
- Missing variant/head, strategy-only head, stale generation and invalid hash refuse mutation.
- Foreign actor and stale thread revision preserve admission checks.
- Changed head, failed-to-ready head change and snapshot corruption during planning refuse old operations.
- Prior and mid-planning unknown outcomes preserve the saved request and head.
- A separately requested approval refuses a late unknown render outcome. This is a legacy/separate approval-flow check, not automatic approval after correction.
- The native Save compiler receives the staged head only after explicit Save. Exact corrected words, title window, picture/audio tracks, assets and last-good identity are checked.
- Compose-off target loading remains unavailable for failed/needs-attention variants.
- Unsupported mixed bundles stage nothing. Existing supported sequence operations remain owned by E.

## Local verification

The focused database suite passed 20 cases using a disposable UTF8 PostgreSQL database. Model/provider and queue edges were stubbed. Planner, persistence, compiler, approval admission and native recipe preparation ran locally. The combined scope passed404 tests before rebase and407 tests against merged E/current main, including both compose settings. All845 repository-mapped reliability tests passed with zero environment skips/errors. Neighboring planner, editor operation, retry, sequence and capability tests also passed. This is a representative state replay, not the unavailable original voice incident.

The native simulator ran 25 failure-card and approval-request tests with zero failures. The task created its own iOS 26.5 simulator after the previous task simulator had been removed.

The corrected explicit-Save recipe was rebound only to generated local media/font fingerprints and exported with `AVFoundationLocalExporter` on the arm64 Mac host. All 22 selected exporter and strict asset-guard tests passed. `ffprobe` reported 1080 by 1920, 3.000 seconds and an audio stream. Visual inspection and OCR showed exactly `Keep every word` at one second, with no detected text after the original two-second title window. A no-text control was exported separately. No original media was used and no production verifier or threshold changed.

Local export SHA256 is `87d5b55b5b47a9383919d1d427c269d85a34fb9c0340d64ecc5b46349785808c`. The media remains a local task artifact and is not committed. This proves a synthetic host-native output, not physical-phone acceptance, original-media semantics or production export verification.

An independent Astra reviewer found no blocking defect in the safeguards. Its suggestion to reuse phone Save wording was applied and covered by the staged summary assertion. A direct late-unknown approval regression was then added. Comment review found no new correctness or safety suppressions.

## Integration limits

The earlier attempted E/main merge was aborted without editing restricted replay-harness files. E subsequently merged as PR #1936, and the recovery-only commits rebased cleanly onto main. No changes were made to the existing owner's branch. The recorded 65.6-second creation turn, D's persisted-schema compatibility with old API/workers, real providers, original phone incident and rollout remain separate limitations under KRI-1039 and its dependency lanes.

T6 does not require a template ledger for this recovery diff. Its exact local-test path matcher in scripts/ci/check-touched-paths.sh returns no matches. GitHub run40885781024/job123488978032 passed with the marker step skipped. Native evidence is still required by the journey mapping and is documented above. No server render, fabricated run ID, skip marker or policy change was used.

The approval unknown-outcome guard does not add a new check to the separate native Save HTTP endpoint. Late execution-outcome transitions at that endpoint remain outside this bounded change and must not be claimed as covered by the approval regression.

No server-render fallback was introduced. Server planning is allowed; native execution capability failures remain failures. No PR approval review or deployment is part of this change.

## Verification after the E merge

The recovery commits rebased onto main `91838ad62` without conflicts. The final main-base run passed407 backend cases, including explicit checks for both compose settings, and25 simulator contracts. Final preship passed with845 mapped incident cases and no failed metadata, mapping or lint checks. A fresh independent review of the main-base diff and flag-state corrections found no blocking defect. The two flag-state assertions initially assumed E's old true default; they now name both settings and confirm that false retains the legacy unavailable reason.

## CI fixture and no-variant follow-up

The first full main-target CI run exposed test targets built as `SimpleNamespace` without the new optional recovery basis. The affected helper and direct fixtures now use the real immutable `_EditorTarget`; changing a snapshot constructs a fresh target. No application fallback was added for an invalid internal type. CI also exposed an unnecessary saved-head scan for a failed job with no variant. Recovery now queries a head only when a target row exists, and scans failed saved heads only when a failed/needs-attention variant exists. A two-setting regression confirms no editor-head query for a missing variant. The existing identity check remains mandatory.

The expanded local matrix passed469 tests, including system-chain replay, real-session fallthrough, editor-state turns and all recovery cases. The reviewer found no production blocker. The deliberately incomplete new fixture initially missed its job ID; that fixture was completed and the final run passed. Full CI must pass on the repaired head before merge.

The repaired full CI run reduced shard1 to one missing-plan fixture failure while shard0 passed. The latest normalization captures absent/null assembly plans as an empty filtered variant list once, then reuses that value for failed-state detection and head scanning. This keeps no-variant replan behavior unchanged and removes inconsistent direct field access. A full-router test covers a status-only failed job with compose on and off. The local matrix passed471 tests; independent read-only review found no new authorization bypass or flag semantic change. Full CI must verify the published normalization head.

## Landed deadline-lane integration

Main advanced with deadline recovery PR #1934 (`a6e79aafb`) while validation was running. Its planner context hint and turn-failure handling merged cleanly, with no manual resolution or recovery-specific change to its code. The recovery diff does not modify the inherited replay harness. The current-main matrix passed516 tests, including21 persisted recovery/deadline-context cases. A focused65-case run also passed. The new test proves that a prior provider deadline and a degraded-start hint keep an explicit supported correction on the staged editor path; they are distinct from an unresolved render execution outcome, which remains blocked. Fresh independent review found no integration blocker in ownership, locked basis validation, Save, flag-off behavior or lease claim handling. C's deployment and performance evidence remain its owner's claims; this recovery validation does not establish original-phone fulfillment or rollout.

## Named native journey evidence correction

CI at `b3988aaa8` passed both full API shards (34,695 passed in total), including all21 new persisted recovery cases. Its native unit job123549940159 built and ran2,109 tests with zero failures and26 skips; the two recovery/approval suites passed. The later named-journey evidence step nevertheless failed because this PR's manifest named the two suites rather than individual test methods. That is a mapping failure, not a passing native CI result.

The manifest now lists all25 concrete existing methods across both suites. The unchanged journey verifier accepts those names against the real task-local25-case simulator xcresult. Independent review confirmed no omitted, extra or duplicate methods, and confirmed that a missing-case control still fails. The selector's41 tests passed. No verifier, native requirement, test assertion or API mapping was weakened. Main's landed send-state and export-verifier changes merged cleanly before publication of this correction; their behavior is not claimed as new recovery work. The corrected exact head still requires fresh remote CI before merge.

## Overnight current-main refresh

The published head94b11820 completed all substantive CI checks, including all
three iPhone legs, but conflicted with main6e37de65f. The only conflict was two
branches appending different journeys. The refresh preserves every main journey
unchanged and retains this recovery journey. Planner changes merged automatically;
independent review confirmed owned-current-head admission, immutable recovery
basis, unknown-outcome guards and explicit Save remain intact.

The former disposable database was unavailable. After creating the supported
worktree-specific nova_recovery_integration_test database, the permitted recovery
matrix passed131 tests with zero skips. Full scripts/ci unit tests passed124.
The matrix covers persisted failed-draft correction, render retries, phone text
sequence follow-up, editor-target misses, failed-item choice handling and compose
behavior. A prior combined command mistakenly included two inherited scorecard
wrappers; its result is excluded from accepted verification and that path was not
repeated. No original-project operations are part of the accepted matrix.

This refresh still needs exact-head remote CI. The previously disclosed native
Save HTTP late-unknown gap, original-phone acceptance and production rollout are
not closed by this integration check.

## Native CI follow-up, October 30

Head `7e617ac7c` completed hosted unit and screens successfully. Creation failed
on an exact text-entry deadline and the Closing test's 4x setup assertion;
no recovery product failure was reported. The CI recording eventually shows
Live text in the field and preview. The text test now waits for the keyboard
and keeps its exact value assertion with a 15-second synchronization deadline.

The Closing repair is reused byte-for-byte from owner commit
733cc0fbcee83c81e3906cbe4f424d92d36a73e5, with its validation record in
`docs/reviews/native-speed-setup-validation.md`. Its speed-test source SHA256
is `9c6d03e98a1bb0b77d2839290c63726ae3d84326f25831d4dd4d87e8fd18c7c8`.
No experimental endpoint driver or product edit is included.

A fresh local build on the task-only iOS 26.5 simulator ran each of the three
selected tests three times: text creation 3/3 passed, Closing 3/3 passed,
unchanged strict endpoint 2/3 passed. The endpoint failure reported 3.70x
and 00:01.6 instead of 4.00x and 00:01.5. The combined batch therefore failed;
it is not readiness proof and is not waived. The shared endpoint owner retains
that repair. Private evidence is `/tmp/task4-recovery-ui-repair-repeated.xcresult`
and `.audit/recovery-ui-repair-repeated.log`. The simulator was shut down.

Main e486e5271f merged without conflicts. A disposable combined checkout passed
160 permitted offline recovery/named-moment tests with zero skips. Independent
review found no blocking interaction; the new strategy-only named-moment gate
does not intercept failed-draft editor operations. Original-phone acceptance
and the previously disclosed Save HTTP late-unknown gap remain unverified.
