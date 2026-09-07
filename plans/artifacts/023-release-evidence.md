# Kria runtime-v2 release evidence

Recorded 2026-09-07. This report separates implementation proof from external
cohort acceptance. Runtime v2 remains default-off; synthetic evidence does not
authorize creator rollout.

## Implementation evidence

| Gate | Evidence | Result |
|---|---|---|
| Focused contract gate | `make verify-kria` | PASS — 168 passed, 1 environment skip, 3.30s wall time, 0 retries |
| Backend repository suite | `pytest` | PASS — 12,073 passed, 76 skipped, 2 xfailed |
| Frontend repository suite | `npm test -- --runInBand` | PASS — 299 suites, 3,624 tests |
| Web contract | `npx tsc --noEmit` | PASS |
| Format structure | checked generator + fixture | PASS — 8 tokens × 30 cases = 240 unique cases |
| Durable Postgres paths | runtime-v2 integration module | PASS when the isolated test database is available; fail-closed environment skip otherwise |
| Query bound | `test_delta_query_count_is_constant_when_transcript_length_grows` | PASS — two queries independent of transcript length |
| Approval | integration and route suites | PASS — 30-minute expiry, exact fingerprint/revision/generation, idempotent terminal decisions |
| Receipt honesty | runtime task/integration suites | PASS — accepted/dispatched/completed language and exact-generation review links |
| Draft retention | `prune_kria_drafts` integration coverage | PASS — bodies only, superseded non-head drafts older than 30 days, approval snapshots preserved |
| Accessibility behavior | workspace Jest guards | PASS — semantic ordering, coalesced announcement, 44px actions, live-edge behavior, focus restoration |
| Operations | admin API/UI tests | PASS — redacted chain, latency metrics, alerts, read-only recovery actions |

The repository-wide counts above were obtained after merging the latest
`origin/main`. Only version and documentation metadata changed afterward; the
focused Kria gate, scoped Ruff checks, TypeScript, and frontend lint were rerun
on the release tree.

## Frozen structural outcome baseline

The independent checked fixture contains 240 unique structural requests, 30 for
each of `montage`, `talking_head`, `day_vlog`, `single_hero`, `subtitled`,
`narrated`, `narrated_planned`, and `narrated_ready`. Every token includes the 15
universal creation/recovery categories plus its required edit operations. It is
credential-free and contains no media. It proves contract coverage and recovery
classification, not edit quality or creator outcomes.

The cohort scorecard must record, independently of the manifest:

- accepted/exported first cut;
- hands-on edit time and total render wait;
- number and type of manual corrections;
- Nova editor escape and CapCut escape;
- abandonment and recovery outcome;
- first useful response and upload-to-playable latency.

## External gates — not yet passed

These require authority or inputs absent from this worktree and remain hard
rollout blockers:

1. Explicit consent, retention date, and revocable deletion procedure for
   Nermin's lifestyle, matcha-business, and travel sources and derivatives.
2. Three uncoached Nermin projects plus the additional target-user baseline
   sessions in Milestone 0.
3. Timecoded grounding and real-media render/recovery results for every canonical
   format.
4. Live judged planner/observer evaluation and shadow lease/queue measurements.
5. Frozen-workflow runs in relevant incumbent/AI editors, with hands-on time,
   quality, edit breadth, and recovery scored by the same rubric.
6. Manual 375×667, tablet, desktop, 200% zoom, screen-reader, and reduced-motion
   QA on the production-intended build.
7. First playable cut p50/p90 and useful-response p95 from the defined production
   media/device/network class.

No external creator may be admitted while any item above is missing. The dark
deployment is protected by `KRIA_RUNTIME_V2_ENABLED` and
`NEXT_PUBLIC_KRIA_RUNTIME_V2_ENABLED`, both default off, plus explicit
`runtime_version: 2` creation for internal clients only.

## Competitive wedge and pending benchmark

The frozen comparison unit is one continuous workflow: footage intake → useful
editorial decision → reversible draft → explicit render approval → playable cut
→ natural-language revision → recovery from one injected stale or ambiguous
state. Score the same source/request on hands-on minutes, first playable time,
editor escape, operations completed, false completion claims, lost work,
recovery specificity, and final blinded quality.

Kria's testable wedge is continuity and trustworthy execution: one durable
conversation owns creation and revision, reversible work happens before consent,
every render has exact approval, and recovery preserves the last good cut. The
incumbent runs themselves are pending; this document deliberately makes no claim
about their current performance.
