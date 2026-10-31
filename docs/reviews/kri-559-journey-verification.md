# KRI-559 journey verification baseline

This is a redacted baseline for request-to-delivery journeys. It records what
the cited changes establish and what they leave open. It does not turn a
passing simulator or compiler check into proof of a live render. New work follows
the [request-to-output standard](../runbooks/request-to-output-standard.md); this
historical baseline is evidence, not a broader completion claim.

## Incident matrix

| Journey / evidence | Confirmed observation | Cause status | Enforcement owner and point |
| --- | --- | --- | --- |
| Emir, KRI-524: [#1481](https://github.com/emirerben/nova/pull/1481), [#1496](https://github.com/emirerben/nova/pull/1496), [#1499](https://github.com/emirerben/nova/pull/1499), [#1511](https://github.com/emirerben/nova/pull/1511) | The simulator passed #1499, but production rejected three captures. #1511 fixed the production gate and was deployed; whether the user's actual post-deploy video succeeds remains unknown. | Confirmed: the earlier simulator harness lacked the required production validation, and the production gate rejected three captures. The user's post-deploy video outcome remains unverified. | PR author supplies a reproducible render; reviewer checks the required gate and evidence level; CI blocks a missing required render; deployment operator records deployed revision and post-deploy result. |
| Yasin text Save: [#1418](https://github.com/emirerben/nova/pull/1418), [#1419](https://github.com/emirerben/nova/pull/1419), [#1421](https://github.com/emirerben/nova/pull/1421), [#1426](https://github.com/emirerben/nova/pull/1426) | Save/persistence changes preserve fonts, entrance animations and placement after saving text. #1419 landed on the wrong branch and reached main only after #1426 re-landed it. | Confirmed: the branch delivery required a follow-up PR. The review has no proof of the creator's private phone export. | PR author verifies destination branch and main ancestry; reviewer checks saved state and a user retest; CI checks the focused save contract; deployment operator confirms the live revision. |
| Yasin KRI-456: [#1423](https://github.com/emirerben/nova/pull/1423), [#1429](https://github.com/emirerben/nova/pull/1429) | #1423 uses resolved clip order for phone narration and exposes planning decisions. #1429 recovers updates placed in the wrong response field. | Confirmed: order and nested brief updates were addressed in code. The feed is not proof of a correct export, and no broad live reliability claim is made. | PR author preserves source-path and brief-version bindings; reviewer checks order and nested-update cases; CI runs focused request and narration checks; deployment operator confirms the effective build. |

Keep these related ownership boundaries explicit: KRI-499 and KRI-555 are
separate work and must retain their own owners and acceptance evidence. KRI-391
and KRI-395 remain canceled; this baseline does not revive them.

## Evidence and invalidation rules

Use the strongest applicable label in every handoff:

| Level | Meaning |
| --- | --- |
| Offline replay | Deterministic fixtures, compiler checks or focused tests; no provider or device claim. |
| Live model | A metered provider call with its prompt/input, outcome and cost recorded; it does not prove production or device output. |
| Native output | A real phone or renderer output inspected for the requested text, order, timing and audio; a feed or plan is insufficient. |
| Production | Deployed revision/process, UTC observation and relevant user or production capture. |

Bind evidence to the tested revision. After relevant code or fixture changes,
rerun the affected check on the final SHA and mark superseded evidence stale. A PR
is blocked when its required render evidence is missing. PR CI replays journeys
offline and runs the iPhone unit leg; the fixture-driven iPhone renders run in
nightly CI, so render evidence for a PR comes from a manual CI dispatch on its
branch at the final SHA. A narrower local fix
may proceed when its own acceptance checks pass, with honest limits and the parent
journey remaining open; narrowing a claim does not waive a required gate. The
live evaluation budget is capped at **$2** for this baseline; paid runs still
require existing approval. Unknown provider, device and production values stay
labeled **unknown**. This baseline does not require paid calls on every PR.

Historical red control (2026-10-09): running the new KRI-524 fixture generator
against the API code at `a6817e9e8` (immediately before #1511) fails at
`check_guided_plan_text` with “missing confirmed on-screen text.” The same
generator passes against the KRI-559 base `52383860b`. This establishes the
production validation regression; neither result proves the user's private
post-deploy capture.

## Baseline for the next three applicable fixes

For each of the next three fixes that affects this journey, record one row with
the same fields: PR and HEAD, owner, enforcement point, evidence level, escaped
regressions, follow-up PRs, user retest result, test time, and cost. Use
`unknown` rather than estimating any value that was not measured.

1. Define acceptance from the original request before implementation. Reproduce
   the failure through the relevant real boundaries and add it to the focused
   gate. Keep plan executability, recipe fidelity and request fulfillment separate.
   For named actions, use independently reviewed source intervals and reject a
   wrong-action control with the same count/duration. Record relevant compound,
   recovery, EN/TR and neighboring cases and any exclusions.
2. Label offline replay, live model, native output and production evidence
   separately. Where an authorized live model run is relevant, use the protected
   `Agent evals` workflow within its **$2** cap. Inspect native output for a
   native-output claim; record unavailable evidence as pending, never as passed.
3. After authorized deployment, retest the complete affected journey. Record the
   final SHA, effective API/worker settings, relevant client build and account/cohort,
   observed output/receipt, UTC time, elapsed test time and settled cost. Record
   escaped regressions and follow-up PRs; keep the parent incident open while
   original acceptance criteria remain unmet.

The owner is accountable at the enforcement point: PR author for the repro and
branch, reviewer for evidence and acceptance, CI for required gates, and the
deployment operator for effective revision and production retest. See the
[KRI-524 reviews](./kri-524-postdeploy-followup.md),
[Kria runtime runbook](../runbooks/kria-agent-runtime.md), and
[agent navigation guide](../runbooks/agent-navigation.md) for the existing
capture, replay and deployment procedures.
