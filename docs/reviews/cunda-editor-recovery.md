# Saved editor recovery and bounded phone frame padding

## User outcome and evidence limits

A failed render must not make a valid saved edit unreachable. Reopening, refreshing,
and a chat request that changes nothing must preserve the saved draft. An explicit
retry must use that draft, not creatively regenerate the project. The correction
is shared behavior for eligible iPhone video edits, with no account-specific rule.

Read-only production diagnosis on 2026-11-06 found the earlier saved-render failure
still persisted from before PR #2122 deployed. One stored legacy slot requested
1.933333 seconds (approximately 58/30) at zero, with a stored source-duration hint
of 1.92 seconds and no explicit source endpoint. The immutable original bound and
its matching receipt count have not been verified.
The original export and saved-draft reference remained recorded. The latest text
request was correctly a no-op and created no render. The pinned draft body,
original footage/output, and installed-client recovery have not been inspected.
These tests reproduce the geometry and shared state contracts, not the private
project's full historical output. The broader Save contract is tracked by KRI-750;
this document does not reopen or claim completion of that historical issue.

## Backend contract

The generic and cloud source-window checks retain their behavior. For a legacy
phone-authored video without an explicit source endpoint:

1. Existing strict fits and unlocked reanchoring win first.
2. Only a failed strict fit can use the immutable original's remaining source span.
3. Its output duration at the unchanged authored playback rate must round to the
   exact authored duration on the 30fps grid, using ties-to-even rounding.
4. The positive final-frame hold is bounded by half an output frame plus 1µs.
5. Source identity must resolve to exactly one original receipt. Conflicting or
   duplicate receipts cannot authorize this compatibility behavior.

Compilation preserves the source start, output clock, playback rate, ordering and
other authored lanes. It neither changes stored source endpoints nor slows the
source to fill the slot. The 13.333ms example consumes 1.92 seconds at 1× and holds
its final frame for 13.333ms. A 1.201-second request from 1.2 seconds remains a real
refusal because it is not the rounded output frame. Missing footage, invalid
explicit endpoints, longer shortages and unrelated prior recovery remain errors.

Saved-render preparation rechecks an immutable original rather than trusting a
stale saved/proxy duration if its strict row check refuses the window. No account
allowlist or rollout switch is introduced.

## Saved editor availability

The full public variant emits `editor_draft_available: true` only after attachment
validates the draft's creator, item, job, variant and generation. The existing
published-source-generation alias is validated on the server, not guessed by the
client. Raw draft references and stale persisted availability hints are insufficient.
The ephemeral attachment cache is cleared before each read to prevent reuse after
a reference or generation change. Failed render status remains failed.

Native work uses this hint to separate editor availability from render success,
preserve local edits, and offer an explicit saved-render retry. Native integration,
full simulator tests and physical-device recovery remain separate gates.

## Tests and verification

Tests were added before the backend behavior changed. The initial frame-padding
suite had 13 failures and 15 passing controls. An independent review reproduced an
ambiguous-original acceptance; its negative control failed before the uniqueness
guard and passes after it. Availability tests failed for stale hints/cache before
the projection change. The final focused and neighboring backend suite passed
617 tests. Scoped Ruff checks, format checks and `git diff --check` passed.

Durable regressions live in:

- `src/apps/api/tests/pipeline/test_editor_frame_padding.py`: actual geometry,
  rate/anchor matrix, strict reanchoring, non-grid and long-shortage negatives,
  stale source hints, original identity and duplicate controls, half-even ties,
  real prepared Save with timeline included/omitted, and the guided approval-bound
  catalog with a following clip whose clock remains unchanged.
- `src/apps/api/tests/services/test_editor_empty_drafts.py`: stale and attached
  availability, cleared attachment cache, current/published generation acceptance,
  wrong generation/variant and missing snapshot rejection.
- Existing source-window, authored-timeline/speed, editor-commit, empty projection,
  phone-commit, rendered-clock and source-registry suites remained green.

The expanded tests were run offline with `socket.socket.connect`, `connect_ex`
and `socket.create_connection` patched to raise before I/O. The full 617-test set
completed without network access. Database-backed Save/reopen, hosted CI, actual
native output and rollout verification are not claimed by these local checks.
