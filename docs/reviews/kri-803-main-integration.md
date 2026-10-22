# KRI-803 audible-line integration

Fresh base: `36031e054c1ffd2d6a3fdf3f5e492a4979154124`.
Original source: `78af74462123026d30b1827e62af343e4b0626bd` (#1741).
Dependency: #1738 `c8769456593718de722b461d2a9555dba6c1d166`, which includes
#1727 → #1733 → #1724 and #1739. Merge those before this PR.
Original code and semantic test assertions are unchanged. The only source merge
conflict was append-only gate JSON; all existing IDs remain and KRI-803 is appended.

The requested quoted source sound routes through named-line grounding rather
than word-triggered pop-ins. Exact short lines are considered before stem matches.
Only one unambiguous transcript can trigger the bounded fallback word pass;
a failed/missing/ambiguous line remains uncertain. All named lines in one clip
must fit the source window, independent of their order in the request. Existing
whole-line and closing-line paths and truthful final-receipt controls remain.

## Executed checks

`bash scripts/check-api.sh test tests/tasks/test_keep_line_audible_montage.py tests/kria/test_whole_line_routing.py tests/tasks/test_whole_line_ask_montage.py tests/tasks/test_named_line_windows_montage.py tests/tasks/test_long_take_moments_montage.py tests/services/test_length_planning.py -q --tb=short`: **140 passed**, isolated migrated
Postgres `nova_pr_1741_integration_test`. These preserve source-interval,
wrong-cut, missing-word, ambiguous-word and partial-receipt controls.

`python3 scripts/ci/test_select_tests.py`: **41 passed**.
`CI=1 E2E_FIXTURES=true make verify-editor-timeline` with the isolated DB:
**14 frontend suites / 209 tests; 28 desktop browser cases; 42 mobile viewport
cases; 583 backend parity tests**, all passed. Deterministic localhost4310 fixtures;
no reused server and no physical phone. The report/trace output is local to this
worktree's Playwright directories. This verifies existing editor boundaries, not
the actual Don Julio phone video.

Mandatory committed preship and exact-head hosted CI remain required before merge.
The inherited finite-frame Gameday helper still requires actual native verification.
Recorded fixture speech timings come from the author's local kit inspection, not
a new audio inspection or live provider call performed here. The original
production request and full rendered output remain KRI-802. No paid call,
physical-phone operation, rollout setting or manual release was performed.

## Timing decisions

# Audible quote timeline contract

1. Duration owner: `build_length_plan` freezes ordered source windows; the approved length plan feeds `plan_unified_montage` and the phone recipe compiler. No UI independently recalculates its duration.
2. Ripple: named speech expands a source window before the length plan is approved, within its duration/reuse constraints. No persisted text, SFX, overlay or music-bed lane is shifted; this introduces no editor insertion behavior.
3. Scrub: existing canonical projected editor duration and native timelineScrubDuration remain authoritative. Pointer, keyboard, playback, ruler and inverse mapping logic are unchanged by this server grounding repair.
4. Resize: generated source windows cover all named lines within each clip; neighboring duration and frame constraints remain in the existing length planner/compiler. No new resize handles, rounding or stretch semantics are introduced.
5. Undo: grounding belongs to the initial generated/approved draft snapshot. It adds no pointer operation, repeated timestamp mutation or undo snapshot.
6. Preview/render parity: original KRI-803 tests carry independently measured quoted-audio spans through grounding, length plan, unified compiler, phone recipe verification and final receipt. Wrong spans/missing/ambiguous words remain negative controls. Run the required verify-editor-timeline gate and preserve its current native dependency limits.

## Post-picker actual parent reconciliation

Merged #1738 source7f8ac50c2 normally, including actualmain1bf6d14a6 and refreshed parser source33115. No source or assertion conflicts/edits occurred. Existing native files are unchanged from old45fe, whose CI39732108721 completedSUCCESS. Current exact-parent backend checks:159passed, covering all original audible-line/source-window/length negatives plus the newly landed unmatched-exclusion picker. The earlier required timeline result remains scoped to identical timeline/renderer input code; no new timing implementation was added by this reconciliation. New exact-head selected CI and normal preship remain required; merge after actual #1738.

## Final combined source and recovery verification

Final normal merges preserve #1738 canonical Main Creator69/Brief Extractor16 source a069537d, current #1730 consumer bc4fc03b and #1732 source-bound recovery plus deterministic CI controls d5d9c0e1. Gate conflicts were ordering/append-only: all common IDs have identical entries and every unique incident gate remains. No audible-line assertions or production iOS bytes changed. Relative to successful prior72241102, the only native difference is the inherited four-line Gameday inspector/panel dismissal prerequisite from #1738. That prerequisite still needs the final source's hosted native result.

Combined source-window/quoted-line/language/length/contract controls:303 passed; the initial sandbox prevented local PostgreSQL access, so both persisted recovery modules and both language persistence cases were then run with permitted task-local PostgreSQL/Redis access:36 passed. Combined339 passed, one explicit pre-existing local-medium kill-switch case inapplicable to that replay variant skipped. Logs: /private/tmp/nova-1741-final-combined.log and /private/tmp/nova-1741-final-postgres.log. No failed source behavior was waived.

The refreshed timeline gate's frontend209 passed. Its seven-worker desktop attempt hit22 JavaScript chunk-load timeouts before assertions (six passed); web input code is byte-identical to main. Unchanged browser assertions then passed serially:28 desktop and42 mobile. Backend parity583 passed. This is qualified component verification, not a claim that the literal parallel make invocation passed. Earlier successful complete timeline run remains historical evidence. No actual Don Julio original-footage export or phone acceptance is claimed: KRI-802 under KRI-829 owns that remaining work.

## Shared reply-overflow correction

Current sourceb7's CI39757137077/API0 independently reproduces the same KRI793r6 visible-title truncation as #1744; its actual assertion log is /private/tmp/nova-1741-b7-api0.log. This is an integration bug, not a native failure or an ignored flaky test. Consumed final canonical editorial parent9a0c88b1d and #1728 source291675a83 with the single shared correction; no duplicate patch or altered incident assertion.169 audible-line/window/length/caption-reply controls pass on this union. Existing source-bound recovery implementation remains unchanged, as do all native application/test bytes relative to b7. No repeated timeline/native local run was started for identical code. Current exact-head hosted checks remain required; no actual full Don Julio export is claimed.
