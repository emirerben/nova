# KRI-1295: a single silent Montage clip kept whole

Parent incident: [KRI-1293](https://linear.app/kria/issue/KRI-1293).
The Q3 run on 2026-11-07 (build 1919) asked for one middle text line for the
whole 11-second clip, no captions, no camera sound or music, and no cuts.
Read-only job evidence records the keep-whole requirement under `style/global`,
not `timing/global`. The saved recovery job superseded that row after the false
hold; the regression restores it to open to replay the original request.

## Acceptance and failure modes (before implementation)

- The original parsed compound brief produces an approval-ready Montage, with no
  advice to switch a silent clip to Talking. Keep-whole remains open, not waived.
- The approved length plan and the real phone montage planner preserve 0–11 s.
  Early windows must not claim verified final fulfillment. The finished-cut
  receipt must reject a shortened or duplicated subset even if its total is 11 s.
- `style`, `timing`, and other global typed keep-whole facts agree. English and
  Turkish labels do not affect the typed fact. Words without the fact, false,
  superseded, clip/title-scoped, and spoken-line triggers must not enable it.
- A compound text-and-keep-whole row still requires its literal in the draft;
  source preservation cannot waive missing words. The text-only subcheck removes
  the keep-whole fact so it cannot route back to the coverage checker.
- An explicit shorter duration or a multi-clip request must not silently become
  approvable. Final-cut preflight misses must reach readiness before approval.
- Rechecking the same saved snapshot must preserve the readiness digest. Missing,
  stale or invalid source evidence must not manufacture a successful receipt.
- Disabling the existing whole-take switch retains the old cut plan and an honest
  hold on its final cuts. No new model calls or feature flags are needed.

## Boundaries and evidence limits

The offline case matrix starts at the recorded parsed brief and crosses the real
length planner, source binding, phone route, montage preflight, readiness and
final source-cut checker. Only the ready-Visuals database lookup is replaced by
an empty result for this no-Visuals case. A second Postgres journey sends the
original request through the real Main Creator parser, runtime, stored brief,
approval recheck and dispatch claim, then the real phone plan from the pinned
brief/snapshot. Its provider responses are authored from the recorded brief
(production provider I/O is redacted), not observed live-model output.
The original request is preserved in the test fixture. No actual device export
or production rollout is claimed. KRI-1293 remains open for native output, text look/placement and the
full post-recovery journey; KRI-1294 separately owns the recovery-label failure.

## Implemented boundaries

1. `keeps_whole_take` reads the same typed global fact as the receipt, regardless
   of the extractor's requirement kind; it excludes absent/malformed facts and
   spoken-line triggers. The existing whole-take length-plan guard remains.
2. Readiness imports source-cut evidence only after validating the pinned length
   envelope. Early windows are unverified, not proof of a finished whole take.
3. Phone preflight checks whole-take asks under any kind and returns final-cut
   misses to readiness. Shorter requested lengths, freezes or a disabled whole-
   take switch cannot silently drop the requirement at approval.

## Local checks

- Red control: the new original Q3 replay failed at `report["ready"]` before
  implementation, reproducing the false hold with the recorded style/global ask.
- `bash scripts/check-api.sh test tests/kria/test_kri1295_whole_montage.py
  tests/kria/test_kri1295_whole_montage_postgres.py -q`: 40 passed. Includes the
  original request, EN/TR, all five kinds, stale/missing/conflicting evidence,
  explicit shorter length, two clips, switch-off, correction recovery and a
  repeated-subset wrong-content control. The review-found compound-row regression
  fails before the correction and now refuses missing literal text while allowing
  present text to defer source coverage. Real Postgres approval preserves r3.
- Focused existing whole-take/whole-line/Leverkusen tests: 101 passed.
- Existing phone preflight, length planning and Postgres readiness tests: 128
  passed. These include the existing approval/recheck path.
- `DATABASE_URL=<worktree scratch database> python3 scripts/reliability-gates.py
  run --changed --base origin/main`: 459 passed, zero environment skips/errors.
- No model calls were paid for. No source video, raw job dump, signed URL or
  credential is included. No merge, deployment or production verification occurred.
