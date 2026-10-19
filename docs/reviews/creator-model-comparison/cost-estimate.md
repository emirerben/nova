# Paid evaluation cost record — approved $5.66 pilot

User approved **$5.66 total**, retaining the **$2 per-batch ceiling**. Final
actual-input preregistration reserved at most **$5.260004** across five batches.
The partial pilot settled **$0.163816** and holds **$0.433948** for one unknown
Gemini timeout: **$0.597764 settled plus held**. All further paid submissions are
stopped. Sonnet's insufficient-credit rejection released its reservation at $0.
See [pilot report](pilot-report.md) and `pilot-results.json`.

The sizing below is the original historical estimate shown before approval,
retained for audit. Official standard API rates were checked 19 October 2026.
Amounts cover inference only, excluding exports, storage, infrastructure and review.

| Model | Input / million | Output / million | Settings |
| --- | ---: | ---: | --- |
| Gemini 3.1 Pro preview | $2 | $12 | high; current repository retries/fallback |
| Claude Sonnet 5.5 | $2 | $10 | adaptive/high; one attempt |
| GPT-6.1 Sol | $2 | $10 | medium; one attempt; store=false |

The price registry includes cache discounts/writes and long-context tiers:
Gemini >200k input $4/$18; Sol >272k input $4/$15. No cache savings or batch
API discounts are assumed. Reasoning is billed as output without double-counting.
The original sizing allowed a second output-priced reasoning allowance for every
provider. Final actual-input sizing uses the inclusive output cap for Sol/Sonnet;
Gemini retains the conservative separate thinking allowance. Prompt tokens are estimates, not provider token-count measurements.

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

## Continuation boundaries

Reconcile the exact unknown Gemini receipt from authoritative provider usage before
another submission in this envelope. Confirm that the configured Anthropic account is funded. The user approved
labeled reconstructions; the updated Copilot preflight maximum is $0.935036,
inside the same approval envelope. The late Gemini request ended in ReadError
without authoritative usage, so its unknown reservation remains. Never start a new
envelope ID to bypass held money. New hardening changes the runtime/schema hashes,
so superseded frozen profiles intentionally fail drift checks; pin a new profile
while retaining the same approved envelope and historical failures.

Schema and sealed-executor implementation is complete offline. Provider acceptance,
held-out data, human scoring and finalist exports remain evidence gaps. No further
spend approval is implied for a larger corpus.

Sources:
- [OpenAI Sol model](https://developers.openai.com/api/docs/models/gpt-6.1-sol)
- [Anthropic models](https://platform.claude.com/docs/en/about-claude/models/overview)
- [Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing)

Reproducible numbers: `cost-preflight.json`. Offline parser replay:
`offline-replay.json` (nine parsed rows; quality, persistence and export unassessed).
