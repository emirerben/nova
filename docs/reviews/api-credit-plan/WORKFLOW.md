# API credit implementation workflow

This branch integrates metered generated images, owned image edits, generated narration, and the existing online evaluation judge for native Kria. Gemini remains the production default. Final video rendering remains entirely on the iPhone. Optional research, script and localization paths retain their existing text-provider behavior. There is no automatic model promotion.

The first increment supplied offline contracts and allocation preflight. The integrated implementation adds migration 0127, durable operations, provider transport, atomic provider/capability budgets, authenticated native routes, immutable storage admission, and SwiftUI controls. See the [operational runbook](../../runbooks/api-credit-capabilities.md) and [verification record](VERIFY.md).

## Acceptance and failure boundaries

A credit grant or proposed 40/40/20 allocation never authorizes spending. All new feature switches default false and all new budgets default zero. Billing organization, recent price verification, explicit live approval, attribution and applicable phone capabilities must pass before submission. The $2 image reservation is a conservative operator-verified estimate, not an independently proved hard provider bound.

Duplicate events cannot buy another generation. Changed input under the same event is rejected. Ambiguous provider outcomes retain liability and must not be resubmitted automatically. Storage failures retain completed bytes for storage-only retry. Images enter the owned asset pool; narration acceptance rechecks approved script identity. Editable titles remain separate from generated pixels. No generated-asset route dispatches a final render.

## Evidence and remaining gates

Offline contracts, transport fixtures, route/admission checks, dedicated PostgreSQL checks and native simulator tests have run. These establish bounded implementation behavior, not live quality or inspected phone exports. Independent final review found no blocking code finding at `8d13cc470`; the generated contract correction was separately reviewed. [Verification](verification.json) records focused API, reliability and native results; the final corrected targeted selection passed 1,660 tests and 24 subtests with zero skips after integrating the landed deadline runtime. The broad attempt exposed three corrected integration failures; the corrected-source full offline API run subsequently passed 34,753 tests with zero failures/errors (37 skips, 15 expected failures, 24 subtests). Current GitHub checks remain a separate gate.

The metadata-only production inventory at 13:20 UTC on 29 October 2026 found zero approved eligible holdouts and zero approved development cases. Historical inventory contained one creator and 173 threads, with owner_map_complete=true and media_map_complete=false. New real holdout enrollment is BLOCKED. Sealed targets were not read. No live quality winner is established.

No live provider calls, billing verification, or physical-phone export inspection occurred. Main pushes trigger Fly deployment. On 29 October 2026 at 15:15 UTC the owner explicitly authorized conditional admin merge and automatic backend/TestFlight consequences once exact-head checks pass. No spending or feature activation was authorized. [Draft PR #1940](https://github.com/emirerben/nova/pull/1940) contains this implementation. No new issue was created.

## Tracking and publication gates

Earlier automatic approval review denied dedicated ticket creation and release labels. New explicit owner approval at 15:15 UTC superseded those denials; one bounded retry created [KRI-1051](https://linear.app/kria/issue/KRI-1051/implement-metered-api-credit-capabilities-for-native-kria) and applied `release:minor` plus `release:added`. [KRI-797](https://linear.app/kria/issue/KRI-797/compare-gemini-sonnet-55-and-gpt-61-sol-for-main-creator-and-edit) remains related provider-comparison work and stays open.

Admin merge and automatic distribution are authorized only after current-head checks pass. Actual backend and any TestFlight outcome must be verified. Live provider tests, spending, credentials, billing qualification and feature activation remain separately unauthorized. No workflow was disabled, no blanket CI skip was introduced, and no approval review was submitted: the authenticated GitHub actor is the PR author and cannot self-approve.
