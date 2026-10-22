# KRI-794 and KRI-795 combined recovery

Original author sourcef21a1374cd7b7ddf9d6f3ad8d3565744e5187ac1 is preserved by normal merge from freshmain7eaf217cb15bc8ebd8d2fea552bbcfe3ee7b0fea, then #1729 sourcef5e9e3ccd. Merge #1729 first. No source conflict occurred.

## Actual combined failures before repair

The untouched combined tests produced six failures/135passes. #1729 now re-asks on the failure card itself; #1732's older tests assumed no such card and a later next-turn question. Reconciled the existing tests to the immediate card BEFORE product changes, retaining exact option/digest, draft, current-answer, contract and stale-old-render checks. Four failures/19passes remained: wrong recommendation when a historical job lacks its binding; an extra ask on repeated refusal; delegation to the same refused answer; and an old render reopening the choice after a newer answer was already queued.

## Source-bound observer repair

The failure observer supplies the refused turn's source-event boundary to the shared card helper. The helper folds server-authenticated answers at that boundary and now, never a model-authored answer. It uses the approved answer as a fallback only when the job lacks its pinned answer. If a newer answer exists, the old render does not reopen the choice. The card uses the same MAX_ASKS_PER_QUESTION cap as the planner. Existing unknown-source fixtures keep their former fallback. No request field, schema, policy flag, timeout or consent validator changed.

Tests now target the NEW open failure-card question for typed/tapped/delegated answers, rather than the closed original ID. They require only two questions across repeated refusals; the original in-flight stale-render test is retained unchanged and still requires no new question. Closed client selections and consent-only questions remain refused.

## Verification and limits

141 combined real Postgres/observer/submit/gate/render-contract and refusal-copy/choice/cloud controls passed after the repair; Ruff/format/whitespace passed. Only recorded Main Creator transport is substituted. The first repair pass exposed 46 legacy SimpleNamespace observer fixtures without source_event_id; compatibility now uses absent-source fallback, with no original copy assertions relaxed. The real database-backed stale-answer path remains protected by the actual model's source_event_id.

No paid model call, phone access or manual deployment. Full original T1/native/export acceptance remains open under KRI-787. Exact-head hosted CI and normal preship are still required before merge.

Current-main658fa0eab (landed #1742 picker) merged normally without conflicts. The complete choice-pair suite plus the real unmatched-exclusion picker path passed160 controls on that union. No additional semantic changes or assertion updates were needed.
