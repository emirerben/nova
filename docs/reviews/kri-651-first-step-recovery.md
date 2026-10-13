# KRI-651: fresh-edit planning recovery

The post-deploy fresh edit failed before hook approval or rendering. Its single
user turn recorded two identical Main Creator inputs, each ending with a
provider-outcome-unknown error after about 130 seconds. Automatic chat naming
changed the thread revision during the first claim. Exact revision checks at
worker completion discarded that result and republished the same turn.

Before implementation, verify these failure shapes: title-only changes during
a successful question, draft, or read; title changes during an unknown provider
outcome; real user/media changes; missing/incorrect title revision markers;
English/Turkish recovery; shared model deadlines; no duplicate provider contact,
approval, execution receipt, or retry publish. Provider failures must remain
failures, with saved input and private diagnostics. A transport deadline must
not silently trigger another potentially billable call.

Tests use real PostgreSQL sessions, runtime claiming/completion, planner parsers,
title persistence, and public delta reads. Substitute provider/broker transport.
No paid evaluations or changes to model/thinking settings are required.

Production reference: thread `4c4fb8d8-71eb-4252-ac90-5180d72becfa`, turn
`74ea2e91-3167-4c70-8240-182b5b618c69`, 2026-10-13 13:21–13:25 UTC,
revision `695da7b4`. User text and media references are excluded from this note.

Provider availability remains an external dependency. This repair must not
claim that every provider call succeeds, or close the release acceptance gate
without a finished production artifact and checked receipt.

Initial verification: the unchanged `97d44d932` base fails seven new cases:
provider contact repeats for both EN/TR questions and unknown outcomes, and all
three completion paths discard a title-only result. Six real-change controls
already pass. The repaired tree passes 308 focused runtime, brief, title, and
provider-budget checks, including all 13 new cases. No paid provider was contacted.

Final local checks: preship and 592 mapped reliability cases pass; 1,187 free
replay evals pass with 15 optional skips; all 54 CI selection tests pass. The
registered gate's red control at `97d44d932` fails the duplicate-call assertion
after 59 passing checks, with real PostgreSQL available (no environment skip).

No model/thinking settings, approval rules, database schema, renderer, or
HTTP endpoint changed. Main Creator and brief extraction now share the turn's
remaining provider budget, preserving 20 seconds for finishing the turn.

## Saved title placement through clip inventory

The same fresh thread later completed a device render on the deployed hook fix,
before this follow-up PR merged. Between wording approval and plan approval, the
clip-instruction inventory asked whether the already approved opening hook was a
title or a chapter caption. Main Creator had the current-media opening-title
approval and the one-clip manifest; the downstream inventory received neither.
Its free-form question therefore reopened a decision owned by the saved approval.

Failure shapes specified before implementation: approved opening and closing
titles; English and Turkish; one versus several clips; title-only versus explicit
clip captions, labels, grouping and ordering; a fresh change to copy or placement;
stale media, revised or pending wording; and cancelled copy. The raw request
inventory remains authoritative even when the earlier planner omitted an operation
from its brief or candidates. Both runtime and legacy callers pass typed current
title approval and the real owned-clip count. No title-only or single-clip shortcut
is introduced. Later explicit caption or placement changes remain instructions
even when they reuse the approved words.

The regression crosses real PostgreSQL submission, saved choice selection,
planner parsers, worker completion and public delta reads. The transport returns
the redundant placement question if inventory lacks the saved placement. Native
rendering remains unchanged; this is a pre-render approval/context repair.

Validation of the added boundary: 11 real-Postgres approval/inventory journeys and
4 narrow follow-up extraction/recovery cases pass. The registered red control
at prior PR revision `be1073184` fails the redundant-question assertion after 76
passing cases, with no database skips or collection errors. Updated preship passes
1,361 selected reliability checks; 1,187 free replay evaluations pass with 15
optional skips. Contracts and the approved-hook phone recipe compile unchanged.
Both independent agent reviews found no outstanding correctness issues after
rejecting an inventory shortcut that could hide requests omitted from the brief.

The inventory prompt is now `2026-10-13.1`. On its largest existing replay fixture,
prompt length grows from 15,025 to 16,182 characters, or 16,812 with both maximum
280-character confirmed titles. Model, 384-token thinking budget, 20-second
timeout and at most two attempts are unchanged; approval journeys still make one
inventory call. Actual live tokens/cost are unknown: no paid model was called.
This proves context delivery through real parsers and consent boundaries with
substituted provider transport, not live-provider compliance. The required native
export stays selected in CI, and merge/deploy approval remains pending.
