# KRI-648 Release 2 / KRI-336

Implement the user-approved Release 2: separate default-off finished-edit learning; repeated changes suggest inactive rules; approval alone changes future direction. Sequential current-agent execution.

## What already exists
Reuse CreatorDirectionService revision/idempotency/Undo, memory routes and inactive suggestions, phone editor atomic validation and immutable recipes, verified device-export publication, account export/deletion controls, iPhone text preferences and native journey harness.

## Data flow
Validated editor Save -> normalized property deltas (no text/media) -> pinned recipe identity -> verified export -> one distinct project vote -> latest five projects -> three agreeing votes + latest relevant agreement -> inactive memory suggestion -> owner Accept/Dismiss.

Account + cohort + privacy gates precede all observation writes. Disabling learning or reset deletes history/pending proposals, preserves approved preferences, and clears stale provenance. No backfill. Global rollout stays off.

## Regression journeys / failure inventory (defined before implementation)
- Opt-in alone cannot bypass cohort/privacy/global gates; ordinary memory remains independent.
- Two finished projects no suggestion; third suggests; fourth duplicate export still three; distinct projects only; window older than five expires evidence.
- Failed or reserved uploads produce no vote; publication must match staged job/variant/recipe identity and accepted Save.
- Reversed changes and deleted text carry no vote; layout/compiler-only changes are ignored; explicitly temporary instructions suppress affected evidence.
- Conflicting values within a role/property give no vote; latest relevant contradiction prevents proposal; pending evidence refreshes/retracts.
- Repeated target fonts become favorites; exclusions require repeated rejection across differing replacements; one font replacement never bans a font.
- Accept merges only proposed properties with existing role preferences, preserving zero/false/color/font properties; generation snapshot changes only for new output; stale writes409; duplicate Accept idempotent; Undo exact.
- Dismiss keeps preferences unchanged and suppresses same normalized proposal until reset; account deletion cascades; project deletion removes evidence links; owner export includes version/provenance without raw text.
- Settings/chat show rule and supporting count; Accept/Dismiss; save/reopen consent; disable clears pending/history but retains approved values; reset and stale-revision recovery.
- iPhone real preview/export after acceptance: EN/TR words unchanged, saved/export style aligned, effects remain zero/false; preserve existing default/legacy render journeys.

## Verification
Write PostgreSQL integration journeys before source code. Extend XCUITest account/chat journey and existing render fixtures; retain MP4s, sampled frames, measurement reports and xcresults. Free replay evals, scoped backend gates, selected reliability gates, make ios-verify and pre-PR. Overlay render proof only if render/layout paths change. Platform clearance remains unverified by user decision. Separate PR with explicit Land now approval; Fly/iOS/TestFlight pipeline after approval; device resend after deploy.

## Performance
Bound history query to five finished projects, cap staged text rows to500, unfinished candidate projects20; index owner/time and unique owner/project. No provider calls or private-text logging. Serialize observation and memory mutation via existing owner/revision locks, deletion fences, and database constraints.

## NOT in scope
- Public activation before KRI-336 cohort/privacy evidence: deferred rollout gate.
- Marketing/web redesign, performance learning, training media retention: separate projects.
- Platform-control clearance: deliberately unverified.

## GSTACK REVIEW REPORT
Architecture, correctness, migration, security, API compatibility, UI and performance passes completed in the current agent; no external agents or paid reviews, per user preference. Original scope retained. Every phone Save entry point routes through the existing validated compiler; verified publication alone finalizes evidence. The owner lock refreshes consent/revision before observing or mutating memory. Proposal approval uses the existing revision/idempotency/Undo ledger and merges only the proposed property.

Review fixes include fresh consent under the owner lock, evidence deletion and Undo invalidation on revocation, explicit wording when an exclusion replaces a required font, distinct-project/window idempotency, requiring active phone preference enforcement before collection, retaining temporary-scope fences across later Saves, and keeping chat decision buttons above the composer as proposals load. No renderer, layout, wording, timing or generation defaults change in Release 2.

### Verified on merged main (`6b2f5f5de`)
- 346 focused backend checks, including nine PostgreSQL learning journeys and actual Save -> verified MP4 publication -> inactive proposal -> HTTP approval -> owner export.
- 1,220 free structural replay/direction checks passed; 15 optional/live cases skipped. No prompt changes or paid calls.
- 592 selected reliability checks passed without environment skips; pre-PR gate green.
- `make ios-verify`: 1,761 executed, 24 opt-in skipped, zero failures (1,737 passed).
- 65 CI orchestration and 102 iOS policy checks passed; generated contracts, scoped Ruff and web type checking passed.
- Final simulator learning settings, chat Accept/Dismiss, existing preference regressions and 27-case preview/export journeys passed. The chat proof asserts both buttons have 44-point targets and sit above the composer; screenshots were inspected. The final on-return refresh also passed its focused chat recheck.
- Migration0120 applied from an empty PostgreSQL database through the complete migration chain.

Artifacts are retained outside git under `/private/tmp/nova-kri-648-learning-evidence`: native result bundles, exported MP4s, sampled preview/export frames, layout measurement reports, publication/approval provenance, logs and restore points. The learned-font fixture consumes the rule returned by the real approval journey; the CI fallback is explicitly labeled as a contract fixture.

### Remaining delivery gates
The paired iPhone is unavailable to CoreDevice on 2026-10-15. A signed development build is prepared separately; real-device preview/export proof remains required before merge approval. Production deployment and a revision-checked device re-send occur only after the separate PR and explicit **Land now** approval.

Public collection remains off. KRI-336 cohort outcomes and privacy approval are external activation gates; synthetic tests do not satisfy them. Platform-control clearance is deliberately unverified, as requested.
NO UNRESOLVED DECISIONS
