# KRI-152: both speech choices failed before video creation

Investigated on 22 September 2026 against `origin/main` at `730e7e81`.
Issue: https://linear.app/kria/issue/KRI-152/investigate-ios-video-creation-failure-with-both-speech-cleanup

## Production evidence

Read-only inspection found two projects matching the reported title and screenshot
times. Each retained its footage and 48.618667-second voiceover, had no video Job,
and had used one of two allowed creation attempts.

| Project | Accepted speech choice | Planning attempt | Stored failure |
| --- | --- | --- | --- |
| `cc719c5f-4239-42f8-abe8-889c310f8571` | `clean`, 07:46:08 UTC | `cfb8d343-f6b0-45e4-aad3-a29e744c1046` | `guided_edit_infeasible`: duration 48.618667 exceeds feasible 29.40 |
| `9461df8e-7913-4bdf-b444-24121c44f45d` | `keep_original`, 08:40:55 UTC | `aa31e29b-7837-4e5d-88cc-a7a9f3f0e004` | `guided_edit_infeasible`: duration 48.618667 exceeds feasible 32.20 |

Both plans use `guided_story`, relaxed pacing, once-only video usage, and ordinary
photo timing. The earlier project has five approximately 5.04-second videos and
three photos; the later project has five videos and five photos. The app build,
iOS version, and HTTP request IDs were not provided. The stored action receipts
establish which choice reached planning; rejected HTTP requests are not action
events, so the later 409 cannot be tied to a specific request from these rows.
No production state was changed or render submitted during investigation.

## Root causes

`feasible_guided_duration_s` gives each photo 1.4 seconds of estimated story time.
The task incorrectly used that estimate as a hard maximum after the planner
returned a narration-length story. This accounts exactly for both stored limits:
roughly 25.2 seconds of video plus 4.2 or 7 seconds of photos. The strict renderer
already supports longer photo holds and allocates the exact narration frame
budget. Both choices therefore failed in the same planning guard, before cleanup
application or Job creation.

After a failed attempt, both native buttons still submit `generate` with their
respective speech choice and analysis ID. Only `retry` previously reopened a
failed session. The real confirmation controller rejects `generate` on a failed
session with `Creator plan changed`, which iOS displays as “This project changed.
Review the latest options and try again.” This code path explains the retry loop;
the specific rejected request was not retained in the action ledger.

Tracing the next step found a second planning-to-render blocker: the native
confirmation path forwards the selected cleanup identity and choice, but guided
confirmation previously discarded them before its deferred worker dispatch.
In enforced preflight mode that dispatch returns `speech_cleanup_analysis_conflict`
before creating a Job. This was not the stored incident failure, because the
duration guard stopped those attempts earlier, but fixing it is necessary for
either choice to complete the guided path.

## Fix and regression boundary

Ordinary narrated guided stories now use the existing renderer capacity helper
for the early feasibility floor and final duration guard. Photo holds can cover
the voiceover. Video-only plans remain bounded and every candidate must still
pass strict compilation against the sources actually selected. Quick-photo
timing, non-narrated target selection, and fast montages keep their existing rules.

An explicit `clean` or `keep_original` confirmation may reopen only its exact,
retryable failed planning attempt with no Job in the thread, session, or item.
It then uses the existing confirmation controller with the selected choice.
Revision, plan hash, ownership, manifest, cleanup identity, attempt budget, and
duplicate-submission checks remain in force.

The confirmation now commits a speech-consent envelope with the new guided
attempt ID. Deferred dispatch recovers it only from the matching owner, item,
ownership epoch, and attempt, validates its analysis UUID and choice, and forwards
the exact values to the existing row-locked dispatcher. New attempts overwrite
or clear prior consent; receipt replay preserves it. Invalid envelopes fail
closed, and legacy attempts without the envelope keep their prior behavior.
The dispatcher still rejects analyses whose source or policy changed while
planning; it never silently substitutes newer consent.
Resuming the same confirmed draft or approved proposal also uses this dispatcher,
so recovery before Job creation preserves the same intent and consent.

Regression coverage uses synthetic media matching both production shapes,
48.618667-second narration, and the real strict compiler. Route tests exercise
both action aliases and both speech choices, plus ineligible failures and stale
analysis. Controller mocks in those route tests establish handoff/state behavior,
not a live queue or completed video render.
Real-controller tests cover consent persistence before enqueue, clearing prior
consent, and idempotent replay. Worker tests exercise the real context resolver
and exact dispatch arguments, including no-findings and malformed/stale consent.

## Verification after deployment

The backend fix uses the existing mobile API and needs no new iOS build. After
the approved Fly Deploy succeeds, verify the deployed commit and `/health`.
For device acceptance, reopen a matching narrated project, refresh its options,
choose each speech behavior from a valid state, and confirm planning reaches a
Job and accurate progress. Retry a failed attempt with fresh state and ensure
stale options refresh without duplicate Jobs. Record the build, iOS version,
thread, attempt, and Job IDs. A completed device render remains separate evidence
from the deterministic backend regressions.
