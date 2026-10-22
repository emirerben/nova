# N2 verification record

This is a bounded local-fix record. The PR request-evidence JSON binds these checks
to the final committed source SHA. Raw media and private transcript remain local.

## Automated checks

- `DATABASE_URL=<isolated scratch DB> python3 scripts/reliability-gates.py run --changed --base origin/main`:
  2,278 passed, 1 skipped (production SFX library dump unavailable), 2 expected
  failures, zero DB/environment skips. The scratch DB was migrated separately from
  development data. The pre-ship check reruns the final mapped set.
- `src/apps/api/.venv/bin/python scripts/ci/n2-fullscreen-red-control.py`:
  the original narrated compiler at `2dc84bc68` fails the full-screen track assertion;
  the changed compiler passes it. This is a regression control, not semantic proof.
- `tests/pipeline/test_phone_narrated_fullscreen.py`: 17 passed. Covers source restart,
  unchanged other lanes/bounds, full-frame lane placement, wrong same-duration photo/
  video, lost lane, short hold, insufficient visible interval, overlap, editor receipt
  invalidation, asset cleanup and retained video capabilities.
- `tests/routes/test_phone_narrated_editor_commit.py` +
  `tests/evals/test_main_creator_evals.py`: 84 passed after correcting the authored
  N2 request fixture to the exact title and original request. The caption-save route
  keeps the compiled source restart and success receipt. The eval cassette is
  explicitly hand-authored provider transport, not a live-model result.
- All-scope repair and related creator tests: 316 passed. Empty all-scope selection
  includes the owned follow source; explicit subset and Visuals-video negatives
  still reject the sequence.
- `tests/tasks/test_n2_fullscreen_dispatch.py`: 2 passed through the real task,
  grounding, lane compiler and sequence compiler. This caught a creator `asset-`
  prefix versus raw pool-ID mismatch; the fix now accepts canonical identities
  while retaining the wrong-source control. Lost photo coverage invokes request
  recovery and never pins a device request.
- Changed API Python files: Ruff lint and format clean; `git diff --check` clean.

## CI selection check

The first GitHub run failed before selected suites ran: its selector test still
expected clip-scope changes to select only the existing exclusion journey. N2
adds the voiceover-planner journey too. Updated that exact expectation and added
an explicit mapping case for the new sequence compiler; the CI gate remains
fail-closed. The full `python3 -m unittest discover -s scripts/ci -p
'test_*.py' -q` suite is rerun before republishing.

## Editor timeline gate

Executed `make verify-editor-timeline` with the worktree's locked web dependencies,
local Playwright runtimes and isolated migrated database. Results by component:

- Jest: 14 suites, 209 tests passed.
- Desktop browser: 28 passed.
- Mobile browser: 40 passed; two filmstrip tests timed out under six concurrent
  workers while the native export was also running. These were the iPhone 13 trim
  stable-filmstrip assertion and iPhone 14 exact source-window sample assertion.
- Reran exactly those two scenarios on both iPhone projects with one worker:
  4 passed. No web source changes were needed. Do not describe the initial full
  command as an uninterrupted green run.
- API half of the timeline gate: 583 passed.

## Source-identity and recovery controls

`test_same_timing_wrong_media_is_rejected` substitutes the wrong image or original
video while preserving timing. It fails with `wrong_photo` or
`following_clip_already_started`; this proves source identity/timing checks, not a
vision model's recognition of a river. Expectations are independently specified in
`acceptance.md` and the test's fixture bindings.

Unplaceable sequences return the existing request-recovery path instead of rendering
PiP or recording success. Full-screen-disabled standalone beats are left unplaced
with an explicit notice; legacy default PiP payloads retain their old serialization.
A later editor removal cannot retain `sequence_realized: true`.

## Prompt resources

Rendered all 63 Main Creator fixture inputs without contacting a provider. Largest
fixture: `kri598_tigre_use_all_clips.json`. Prompt length changed from 65,049 to
66,055 characters (+1,006). A rough characters/4 estimate is 16,262 to 16,514 input
tokens; these are not measured provider tokens. Model/thinking settings unchanged;
timeout remains 130s and output budget 16,384 tokens. Paid calls: 0; cost: $0.
Prompt version: `2026-10-22-v69`.

## Evidence limits and delivery

Native renderer evidence is recorded separately in `native-evidence.md`. Its
schedule is authored and estimated; it does not establish actual spoken-word
alignment or model request interpretation. Full exact-request fulfillment and the
affected account's TestFlight replay remain pending under related parent KRI-598
and the PR handoff. No paid/model request was sent.

Before merge, a read-only production API check confirmed phone rendering,
voiceover reaction beats/editor lanes and full-screen overlays enabled, required
visual/narration capabilities present, minimum editor protocol 3, and an empty
cohort allowlist (all authenticated accounts eligible under the existing policy).
This is pre-deploy configuration evidence only; the merge/deploy handoff must check
the final API and worker revision, effective settings, and client eligibility again.
