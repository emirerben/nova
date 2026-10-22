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

A permanent batch journey step executes both existing answer-order UI tests against the same built app and simulator. verify-tests requires each exact name to pass, rejecting missing/skipped/zero execution. ChatWorkspaceView is explicitly mapped to this journey so future shared workspace changes cannot omit these scenarios. No duplicate build, changed wait/assertion, physical-device operation, paid call or manual release is introduced. Current combined native proof is required before merge.

## Compatible strict receipt command

The #1736 chip scenario actually passed33.465s, but its new receipt step then failed because require-xctest was not a supported CLI command. Consumed corrected #1736source4168eda84: verify-tests passes the real saved chip result and rejects missing and real+missing names. Both this candidate's chip and batch steps now use that supported strict command; both batch names remain required and their actual execution is still pending. No Swift code, test assertion or timeout changed in this correction.

## Actual published-head named native steps executed

On publishede10af801, native run39743956631/job119719485541 completed the dedicated both-answer-orders step SUCCESS at08:53:35UTC (started08:51:50). Its exact-source workflow executes BOTH named CreationUITests cases and requires each successful result via verify-tests, so this is execution evidence rather than manifest declaration or a generic smoke inference. The suggestion-chip step on that same combined app also executed and succeeded08:50:16–08:51:50. At observation the job was still running Gameday/export; this does not claim whole-run success or final current-main dependency readiness. Download actual named result JSON after artifact publication.

## Terminal native evidence and final published-parent union

Run39743956631 on e10af801 completed full CI SUCCESS. Downloaded actual native artifact12413529691 to /private/tmp/nova-1744-e10-native-evidence and verified both named result rows with the strict CLI: title-first24.958s PASS, clips-first27.781s PASS. The chip scenario passed36.293s and actual Gameday save/reopen/export passed122.132s. No skipped-name inference or generic green substitute.

The final private source consumes the currently published canonical69/16 editorial parent a069537d6, exclusive trim consumerbc4fc03b and CI-only deterministic-overlap repaird5d9c0e. Native application bytes and both batching/chip UI test methods remain exactly identical to successfule10. Backend Python ASTs match the188-pass combined batching/reanswer/consent/contract sourcec56c25a34; only version-history comments differ. All incident gate entries survive, with common entries proven identical. The inherited Gameday test adds its two stronger inspector/panel disappearance prerequisites; no application, batching/chip assertion or timeout changes.

Actual parent merges are still required before this PR lands. Fresh exact-source CI remains required; olde10 artifacts are component/history evidence and are not relabeled as the new head. Full original Don Julio picture/audio, correction/save/export acceptance remains open under KRI-802/KRI-829.

## Shared cross-fix visible-reply correction

Current2923 CI39755712620/API0 failed unchanged KRI793r6: the1200-character global slice omitted the full title request after named-cut/trim explanations grew. Stored receipts were present. Consumed one shared repair through #1728 source291675a83 and canonical editorial parent9a0c88b1d, rather than patching batching or dropping the assertion. The correction preserves request labels/statuses within the original cap when labels fit, uses explicit display ellipses, and never mutates complete receipt reasons. Native application and both required batching/chip UI methods are byte-identical to2923. Final combined batch/persisted reanswer/refusal/consent/editorial/audible-line/caption-reply checks run in /private/tmp/nova-1744-shared-reply-batch-integration.log. Fresh exact-source CI and actual parent merges are still required; original Don Julio output/export acceptance remains KRI829/KRI802.

## Complete final source-group/recovery union

Consumed #1745 source85c3200f7 with actualmain12d after #1732. One cloud import overlap keeps both helpers; common gate entries are identical and every unique gate remains. The workflow conflict keeps the complete dedicated both-answer-orders execution and strict verify-tests requirements. No native application/test bytes differ from73a/2923. Full final batching/label/audible-line/persisted-reanswer/refusal/source-group/caption-reply suite:253 passed. Actual #1745 is now a dependency as well as #1741/#1738; none may be inferred merged from this stacked source. Publishing this real source repair lets exact CI run while parents finish; it does not waive old2923's failed API0 or claim actual full-video fulfillment.

## Stable dependency preparation for parallel CI

The source preserves author `b0ba380d` and normally integrates frozen #1741 source `78ea7b4c`, including frozen #1738 source `0431ea0c`. Neither parent is claimed merged. A local Git-only ordered squash assessment showed the old author head would still conflict after both proposed parents, so this source update is necessary. The prepared replacement now merges cleanly with the hypothetical ordered parent and matches its prepared source tree exactly. Those hypothetical objects are review tools only; they were not published and are not actual-main evidence.

All reviewed behavior remains the `60798f6e` private candidate that passed 201 focused controls. The Main Creator AST is identical and the strict native CI selection is byte-for-byte unchanged. The integration adds parent release metadata and evidence continuations plus one version-history comment. Every common incident-gate object was identical; all 51 IDs and their test references remain. The first prepared registry still produced an overlapping append conflict after a squash. Moving only this PR's unique question-batch gate to a separate stable insertion point made the merge clean; no gate field, assertion or selected test set changed. The gate runner derives sorted test sets rather than first-match priority.

Published source `b0ba380d` previously passed both strict native answer-order scenarios and the Gameday reopen/export journey. That is historical component evidence. The new source includes current-main changes such as the already-landed caption offset correction, so it must pass its own current selected native and aggregate checks; older success is not relabeled.

Fresh CI can run in parallel while merge order remains #1738, then #1741, then this PR, with #1723 last. Before merge, compare actual landed parents and the prospective merge with the tested union. Only explicitly checked release-version metadata may differ; unexpected source changes require reconciliation and applicable fresh checks. Full original Don Julio picture/audio, correction/reopen, physical export and protocol-14 rollout remain pending. No paid model, phone, flag or manual release action occurred.
