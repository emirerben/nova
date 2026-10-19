# Paid evaluation cost estimate — approval pending

**Spent on provider evaluations: $0.** No live comparison, paid judge or model
availability smoke request has run. Official standard API rates were checked
19 October 2026. This estimate is for inference only, excluding actual exports,
storage, infrastructure and human review.

| Model | Input / million | Output / million | Settings |
| --- | ---: | ---: | --- |
| Gemini 3.1 Pro preview | $2 | $12 | high; current repository retries/fallback |
| Claude Sonnet 5.5 | $2 | $10 | adaptive/high; one attempt |
| GPT-6.1 Sol | $2 | $10 | medium; one attempt; store=false |

The price registry includes cache discounts/writes and long-context tiers:
Gemini >200k input $4/$18; Sol >272k input $4/$15. No cache savings or batch
API discounts are assumed. Reasoning is billed as output without double-counting.
The reservation estimator allows the full configured answer budget plus another
full output-priced reasoning allowance; it is deliberately larger than typical
usage. Prompt tokens are estimates, not provider token-count measurements.

## First three-item priced pilot

Sizing uses two historical Main Creator inputs and one recorded Copilot input.
These are **size references, not an approved live corpus**. One Main Creator case
is reconstructed from production with a hand-authored response; the other two
have live-capture provenance in the existing fixture metadata. All are reported
as historical recorded replays; no case is promoted to an exact eligible capture.

| Reference request | Gemini G0 maximum | Sonnet maximum | Sol maximum | Total |
| --- | ---: | ---: | ---: | ---: |
| Main Creator: recorded voiceover context | $1.718445 | $0.368930 | $0.368930 | $2.456305 |
| Main Creator: SFX correction context | $1.636398 | $0.349040 | $0.349040 | $2.334478 |
| Copilot: recent edit history context | $0.465968 | $0.200216 | $0.200216 | $0.866400 |
| **Pilot** | **$3.820811** | **$0.918186** | **$0.918186** | **$5.657183** |

Nine arm evaluations allow at most 20 provider submissions when every registered
Gemini retry/fallback is exhausted. The runner stops on the first unknown outcome.
First-attempt and eventual results must be reported separately; configured effort
names are not equal units of compute.

Keep the existing **$2 per run** ceiling. A concrete batching arrangement is five
manifests: first Main/G0 $1.718445; first Main/S+O $0.737860; second Main/G0
$1.636398; second Main/S+O $0.698080; Copilot/G0+S+O $0.866400. Their sum is
$5.657183. Approval of a larger envelope never bypasses per-run enforcement.

After three-item pilot review, a balanced 20-case development matrix (10 per
agent, one repetition, these three arms) would reserve approximately **$32.62**
if request sizes resembled these references. This is a planning number, not a
spend request. Price the actual development and held-out corpus after collection;
repetitions, additional schema/reasoning arms and retries multiply the budget.
Do not authorize the full experiment from this small reference set.

## Preconditions before requesting a live manifest approval

- Deploy the eligibility reader and explicitly enable scoped private capture.
- Obtain complete exact invocation inputs and human input-bound expectations.
- Freeze identical inputs/configurations, verify the effective production baseline,
  and refresh estimates from those actual prompts.
- Present each frozen manifest hash and <=$2 cap. Only then request paid approval.

The full native-schema improvement and restricted holdout executor remain open,
with exact blockers in `schema-feasibility.md`. The JSON-only development pilot
is an explicitly separate profile, not evidence that native schemas are delivered.

Sources:
- [OpenAI Sol model](https://developers.openai.com/api/docs/models/gpt-6.1-sol)
- [Anthropic models](https://platform.claude.com/docs/en/about-claude/models/overview)
- [Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing)

Reproducible numbers: `cost-preflight.json`. Offline parser replay:
`offline-replay.json` (nine parsed rows; quality, persistence and export unassessed).
