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
