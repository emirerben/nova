# KRI-802 combined-question integration

Fresh main: `36031e054c1ffd2d6a3fdf3f5e492a4979154124`.
Original source: `7d69b8ea9ae7bb988697885497c43f40029b55b4` (#1744).
Dependency: #1741 `45fe46a660630b58e0c155374d8ac35f84cc33f7`, including
#1738/#1739 and the #1727 → #1733 → #1724 chain. Merge dependencies first.
Only append-only journey/gate conflicts required reconciliation. Native/backend
code and semantic assertions are the original author's unchanged source.

## Acceptance and review

The app must hold the first answer locally, show its selected state, and submit
both answers in exactly one turn after the second arrives, in either order.
The server may batch only a plain clip picker with a held title-wording question.
It must not draft unapproved wording or combine another choice, reply chips,
continuation, title-related picker or overlong reply. Older apps/web remain on
the old serial behavior; server submit stamps capability only at protocol14+.

The local value `QuestionBatch` belongs to the workspace's per-message @State,
with explicit values passed to existing card components. No global state or
second networking path is introduced. Existing send failure handling reopens
cards when the pending message is removed. UI review used the narrow SwiftUI
state/composition guide selected through ui-skills-root. No UI code was rewritten.

## Executed verification

`bash scripts/check-api.sh test tests/kria/test_kri802_question_batch.py tests/kria/test_kri802_question_batch_postgres.py tests/kria/test_kri805_brief_change_answer.py tests/agents/test_main_creator_editorial_plan.py tests/tasks/test_keep_line_audible_montage.py -q --tb=short`: **68 passed**, isolated migrated Postgres
`nova_pr_1744_integration_test`. The real submit/claim/planner persistence
boundaries, protocol fallback and neighboring parser fixes are exercised.
`python3 scripts/ci/test_select_tests.py`: **41 passed**.

The original source includes named simulator tests for choice-first and
clips-first submission and existing card neighbors. Current integrated-head
native build/unit/UI results are REQUIRED before merge; Python results and Swift
syntax do not replace them. No new physical-phone or simulator operation was
performed by this refresh. Original source simulator evidence is described in
the PR body, not relabeled as a new integrated-head run.

Committed preship and exact-head hosted CI remain required. Rollout needs the
protocol14 app and compatible API/worker; old clients keep serial questions.
The broader Don Julio request/export remains open. No prompt/model change,
paid call, manual deployment, release or physical-phone action occurred.

## Shared native state and actual UI coverage correction

Oldhead2e181 native job in run39732798893 completed SUCCESS, but its actual log contains only generic Creation smoke and the learning chat test. Neither declared batch scenario executed: selectorjob119671461555 chose ios_ui_groups=full and the PR workflow mapped that to smoke. The manifest and UI inventory already named both scenarios; native-units intentionally ignores UI IDs. Thus generic green was not sufficient batch acceptance.

Merged actual reconciled #1741 sourcee1f4bb8 (including #1738newmain source7f8) and #1736baaebb native-state/coverage changes normally. ChatWorkspaceView, CanonicalChatComponents and CreationUITests merged automatically, with no Swift source or semantic assertion edits. The candidate now contains both batching @State and ready-chip scroll updates.

A permanent batch journey step executes both existing answer-order UI tests against the same built app and simulator. require-xctest requires each exact name to pass, rejecting missing/skipped/zero execution. ChatWorkspaceView is explicitly mapped to this journey so future shared workspace changes cannot omit these scenarios. No duplicate build, changed wait/assertion, physical-device operation, paid call or manual release is introduced. Current combined native proof is required before merge.
