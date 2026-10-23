# KRI-853: collection review and reliable resume

Original request: Find story ideas fails after returning the next day with “This edit changed elsewhere”; too many clicks and text.
Direction A was selected by Emir: review footage, review ideas, then choose one. Design: https://app.paper.design/file/01M5KPZWPM6NBTGS5X4YXS0JQ1/p-1-0

## Acceptance and plan review (before implementation)

Architecture: keep native Photos/library bindings and the existing collection → index → stories → separate selection/approval path. No renderer, prompt, cohort or production flag changes. Backend serializes writes by owner. An identical canonical selection is a no-op, so its revision and story cache remain usable after resume; actual edits still use optimistic revision checks.

Code quality: reuse the shared collection write projection for both HTTP encoding and local saved-idea validity. Scope error mapping to collection/story views; do not change global editor conflict behavior. Persist optional saved-idea context account-scoped; legacy snapshots remain readable. Share the current query/research context with the saved result. Revalidate against the backend before reopening ideas.

Tests: before fixes, extend the real HTTP/PostgreSQL journey to reproduce unchanged-save revision churn and cache loss, retain changed-membership/concurrent-write controls. Native UI must cover relaunch → saved ideas, actual unavailable-analysis copy, compact controls and existing story corrections. Record real device/account evidence separately; fixtures cannot prove production.

Performance: no model calls for saved-idea reopen, no media reupload, no new dependency. Keep the 50-item/32-source bounds. Save results only when their selection still matches; do not run metadata refresh concurrently with generation.

## Failure modes preregistered

1. Unchanged PUT increments the revision, changing the story cache key and producing a cache-only 409.
2. Every 409 is surfaced as an unrelated editor conflict.
3. Process termination loses suggestion/query context; a fresh visit asks for analysis again.
4. Rename, member removal/reorder, corrected group or source change incorrectly reuses old ideas.
5. A genuine competing edit is silently overwritten, or a future revision is accepted.
6. Interrupted response repeats an accepted save; duplicate writes must stay idempotent.
7. Background/metadata refresh replaces the active task or races its collection mutation.
8. Limited/revoked Photos permission or unavailable sources are hidden by the resume path.
9. Simplification hides required original/clip review or bypasses selection/render approval.
10. Legacy snapshots, disabled cohort and cache-only policy are accidentally broken.

## Intended behavior

Collection grid and one primary action. Optional direction/research/group/analysis controls live in options. A matching saved result offers View saved ideas and an explicit Find more ideas action. Ideas show the title, short angle, preview strip and proposed wording; clip review/edit controls expand on demand, and Use this idea remains explicit. No automatic selection or render.

Identical canonical saves return the current revision without mutation. Actual changes with stale revisions still return a collection-specific conflict. Cache misses, pending work and unavailable sources have separate messages. On a genuine conflict, latest-collection review preserves local changes before an explicit adoption.

## Evidence boundaries

No affected request/collection identifier or installed client build was supplied. The current failure chain is code-grounded and will be reproduced through HTTP; production attribution remains pending under KRI-853. Physical-device export is unchanged and cannot be claimed from API/UI fixtures. No paid live runs are authorized by this change.

## Delivery and rollout

Implementation stays on iOS and the shared collection backend. Read the selected Paper A SVG export through Paper MCP; spacing/type/primary-action choices use existing native Kria tokens. No web-editor changes, prompts, dependencies or migration.

Deploy the API fix through Fly Deploy before distributing the matching native build through the existing main iOS/TestFlight workflow. Merge/deploy still require autoship's explicit Land now choice after a verified PR. Existing cohorts and cache-only limits stay in force. VERSION/CHANGELOG remain owned by post-merge automation.

The camera-roll gate is still marked open_pr in the pre-existing manifest and is excluded by automatic `select`; run `kri-729-camera-roll-approval` explicitly. The CI camera-roll journey remains the native/API contract route. Scope includes KRI-853 regression coverage, without relabelling historical pilot evidence as a production verification.

## Review disposition

Architecture: existing owned routes and source bindings retained; no-op writes cannot overwrite content, and genuine competing writes remain serialized and rejected. Code quality: one native HTTP projection also guards the local shortcut; error wording stays scoped. Tests: real HTTP negative control plus native relaunch/unavailable/conflict journeys, existing correction/rejection and smoke coverage. Performance: cache-only reopen, at most 50 original-bound members, no new upload or provider work. Review is performed in the current agent per the user's no-delegation direction; no independent-model review is claimed.

Native UI caught a real accessibility-identifier propagation issue: assigning an
identifier to the outer DisclosureGroup replaced the individual clip control IDs.
Keep the disclosure's native label and preserve each child control's identifier;
retain the replacement/order/audio/rejection assertions rather than weakening them.

## Main integration review (before conflict resolution)

Main `ab510e0eb` contains landed #1747 / KRI-731. Preserve its 500-item
catalogue capacity, explicit partial-analysis choice, distinct-idea handling and
all named native/API checks. The 50-item statements above describe the original
base and are superseded by that landed capacity; selected-story render bounds
remain separate.

New integration failure modes: saved partial ideas lose their cache identity if
the partial-analysis flag is omitted; reopening can silently widen partial consent
if a different set of videos becomes unavailable; compact details can hide the
existing independent-idea rejection controls; fixture resets can destroy the
process-relaunch case. Persist the explicit partial choice with matching saved
selection/revision, reuse it only for the same excluded-video set, and require a
fresh ready-video choice when coverage changes. Preserve all prior assertions and
add a relaunch/changed-coverage journey before implementing that new branch.

Re-run the real HTTP/PostgreSQL camera-roll gate, native build/unit suite, every
collection/story UI journey (including landed independent-idea and partial-choice
tests), smoke, preship, and new exact-head remote CI. Original account/physical
acceptance remains unverified. No web-editor changes, paid calls, manual release,
phone changes or cleanup. The earlier head-bound review/CI receipt will be stale
after integration; record new code trees and results before handoff.
