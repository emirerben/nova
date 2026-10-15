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
Bound history query to five finished projects, cap staged text rows to500, pending suggestions20; index owner/time and unique owner/project. No provider calls or private-text logging. Serialize observation and memory mutation via existing owner/revision locks, deletion fences, and database constraints.

## NOT in scope
- Public activation before KRI-336 cohort/privacy evidence: deferred rollout gate.
- Marketing/web redesign, performance learning, training media retention: separate projects.
- Platform-control clearance: deliberately unverified.

## GSTACK REVIEW REPORT
Architecture/code/test/performance methodology applied in current agent. Original scope retained. Integration source tracing is in progress; no independent agent per user preference. Test gaps above will be filled before implementation completion.
NO UNRESOLVED DECISIONS
