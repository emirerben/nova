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
