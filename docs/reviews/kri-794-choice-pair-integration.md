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

## Actual parent merge and final source reconciliation

After #1729 actually landed as 8568cace1, its squash ancestry overlapped five task hunks. All five retain the already reviewed source-event boundary, scoped answer fallback and shared ask cap from this PR. The task module remains byte-identical to reviewed d44beaf; no app behavior or assertions were changed by this reconciliation. Actual #1724 spoken triggers and #1743 speech validation are included. The combined persisted reanswer/refusal, consent, stale-render, picker, contract and spoken-trigger controls pass (213 cases). The initial preship correctly rejected the old head receipt; it is regenerated for this new source head below.

## Deterministic CI fixture isolation repair

Fresh exact-head CI39746694489/API1 failed the existing KRI-795 refusal acceptance: the observer returned `ignored`, before any card assertion. Another xdist worker ran the global reconcile sweep during the fixture's committed-dispatched-to-observe gap. The workers share scratch Postgres. A new real-sweep control reproduces the cause deterministically: settlement runs first and consumes the fixture (one failure before repair).

Only the fixture handoff changes. It persists the execution as accepted, then marks it dispatched and runs the real observer in one owned real-Postgres transaction; the observer performs its usual failed-card commit. An unrelated sweep cannot consume the fixture first. The original `observed == failed` and every text, option, consent and stale-render assertion remain unchanged. The new control also requires that its sweep actually ran and that exactly one failed event exists.

The focused three-module parallel suite passed45 cases. The full choice/refusal/copy/contract/picker/spoken-trigger plus expiry-sweep suite passed225 cases with four workers on actual main8eef after #1739. No production code, timeout, retry, skip or CI setting changed. Fresh exact-head CI is still required; this is not a waiver of the red shard.

## Direct concurrency proof instead of a scheduling stopwatch

Fresh8c CI39749479102/API1 passed the refusal fixture but failed unchanged template test_probe_and_upload_concurrent_actually_overlaps:1.188s versus its0.35s stopwatch threshold. Its stated purpose is parallelism, not a production latency SLA; both the helper and prior test match actual main. The test now requires its two external-I/O stubs to rendezvous before either returns, and preserves the identity of both returned results. Serialized execution cannot reach the second party. A5s barrier timeout bounds a deadlock; it does not raise a performance target.

All23 existing pipeline-helper checks pass. An independent in-memory serialized implementation fails the unchanged new assertion with BrokenBarrierError, proving the concurrency requirement is retained. No production code, skip, retry or status waiver. Broader four-worker validation passed269 cases;11 reanswer cases hit explicit local Postgres/filesystem disk-full errors. After removing only this task's two completed disposable Next.js caches, all23 unchanged reanswer cases passed. Shared caches, volumes, source and evidence were untouched. The separate real-sweep/refusal225-case proof remains valid. Fresh exact-head CI is still required before merge.
