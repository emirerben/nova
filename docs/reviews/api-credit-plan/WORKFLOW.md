# API credit implementation workflow

This branch integrates metered generated images, owned image edits, generated narration, and the existing online evaluation judge for native Kria. Gemini remains the production default. Final video rendering remains entirely on the iPhone. Optional research, script and localization paths retain their existing text-provider behavior. There is no automatic model promotion.

The first increment supplied offline contracts and allocation preflight. The integrated implementation adds migration 0127, durable operations, provider transport, atomic provider/capability budgets, authenticated native routes, immutable storage admission, and SwiftUI controls. See the [operational runbook](../../runbooks/api-credit-capabilities.md) and [verification record](VERIFY.md).

## Acceptance and failure boundaries

A credit grant or proposed 40/40/20 allocation never authorizes spending. All new feature switches default false and all new budgets default zero. Billing organization, recent price verification, explicit live approval, attribution and applicable phone capabilities must pass before submission. The $2 image reservation is a conservative operator-verified estimate, not an independently proved hard provider bound.

Duplicate events cannot buy another generation. Changed input under the same event is rejected. Ambiguous provider outcomes retain liability and must not be resubmitted automatically. Storage failures retain completed bytes for storage-only retry. Images enter the owned asset pool; narration acceptance rechecks approved script identity. Editable titles remain separate from generated pixels. No generated-asset route dispatches a final render.

## Evidence and remaining gates

Offline contracts, transport fixtures, route/admission checks, dedicated PostgreSQL checks and native simulator tests have run. These establish bounded implementation behavior, not live quality or inspected phone exports. Final consolidated verification and review binding remain pending with the integrating parent.

The metadata-only production inventory at 13:20 UTC on 29 October 2026 found zero approved eligible holdouts and zero approved development cases. Historical inventory contained one creator and 173 threads, with owner_map_complete=true and media_map_complete=false. New real holdout enrollment is BLOCKED. Sealed targets were not read. No live quality winner is established.

No live provider calls, billing verification, or physical-phone export inspection occurred. Publication authorization does not authorize deployment. Main pushes trigger Fly deployment; merge/deployment handling remains a separate parent gate. No PR or new issue reference is asserted here.

## Tracking and publication gates

The dedicated Linear issue creation is paused after automatic approval review rejected that specific organizational write. An explicit approval question is pending. No new issue was created. [KRI-797](https://linear.app/kria/issue/KRI-797/compare-gemini-sonnet-55-and-gpt-61-sol-for-main-creator-and-edit) remains related provider-comparison work and is not closed by this implementation.

Draft publication and CI are authorized. Admin merge is authorized only for a ready head. The existing Fly workflow deploys automatically on main pushes, while deployment remains separately gated in this task. Merge must preserve that restriction. No workflow was disabled and no blanket CI skip was introduced.
