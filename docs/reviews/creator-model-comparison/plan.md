# Main Creator and Edit Copilot model comparison implementation plan

Compare Nova's current Gemini configuration with `claude-sonnet-5-5` and `gpt-6.1-sol` on authorized real creator requests. Deliver the provider adapters, accurate accounting, structured-output improvements, reasoning controls, private evaluation workflow, and a reviewed result for each agent. Choose Main Creator and Edit Copilot independently; keeping Gemini is a valid outcome.

Status: implementation and offline checks completed on the isolated branch from `ed90bd9ea859129efb1853b327ca00562ecb54a0`, 19 October 2026. Typed schemas, sealed holdout execution, private source imports and shared-envelope accounting are implemented. The approved $5.66 development pilot is **incomplete and blocked**: three Main Creator responses parsed and compiled, one Gemini submission has unknown billing after timeout, and Sonnet rejected the configured account for insufficient credits. Copilot has one explicitly reconstructed input approved by the user and preflighted offline. There are no live holdout cases or quality verdicts. See [pilot report](pilot-report.md). No deployment or production model selection occurred. Original planning revision: `ac15c4726`.

**1. Scope and existing work**

The experiment changes only Main Creator and Edit Copilot routing. Other agents, media analysis, transcription, renderers, and product capability policy retain their existing ownership. Shared runtime improvements must remain compatible with other agents.

Reuse [KRI-727](https://linear.app/kria/issue/KRI-727/gate-reliability-releases-on-a-held-out-request-to-export-benchmark), the request-following review queue, eligibility checks, private capture store, deterministic edit compiler, and actual-export verification. Do not create a competing training-data consent system or benchmark dashboard. No Linear records are created or updated by this plan.

[KRI-734](https://linear.app/kria/issue/KRI-734/verify-prepared-edits-against-actual-device-and-cloud-exports-before) is marked Done, but its current implementation verifies a limited phone adapter. It does not establish automatic coverage for music, speech, animated effects, cloud, or slides. Use existing evidence where applicable and record independent human picture/audio review for other supported outputs. Do not expand the renderer merely to make the experiment report green.

Known development cases include KRI-242's lost pacing requirement, KRI-687's repaired shape failure, KRI-675's large-pool timeout, and the existing captured editor corrections. They are regression/development evidence, never unseen holdouts. KRI-672's comment describes a smarter coding assistant, not a runtime-model comparison.

**2. Finish line**

- Both agents can run through the same runtime with Gemini, Sonnet, or Sol, with model, prompt, schema, reasoning, deadline, and retry versions recorded.
- Every provider call reserves and settles its real cost, including failures and uncertain outcomes, under the existing budget ledger.
- Real cases have verifiable provenance, point-in-time agent inputs, source-backed expectations, current eligibility, and immutable split membership.
- An offline rehearsal proves networking, execution, budget, privacy, and reporting boundaries before any paid call.
- Separately approved live batches produce a reproducible comparison and actual-output evidence for the finalists. Unassessed exports and unavailable cases remain visible.
- A human reviews the model recommendation and subjective outcome judgments. Release approval, deployment, and cohort activation remain distinct steps.

Infrastructure being merged, valid JSON, replay tests, and ready Jobs are milestones, not completion of the experiment.

**3. Implementation sequence**

| Milestone | Deliverable | Exit condition |
|---|---|---|
| A | Isolated worktree, source/credential audit, preregistration and failure matrix | Baseline and permitted data sources identified; offline acceptance checks fixed before code |
| B | Normalized provider contract, Sonnet/Sol adapters, complete cost accounting | Recorded HTTP journeys pass with no external calls and no ledger bypass |
| C | Per-agent model/reasoning controls and explicit schema support | Legacy Gemini stays reproducible; supported native schemas compile and unsupported constraints are reported |
| D | Private real-request capture/import, experiment runner and report | Zero-cost rehearsal completes from private snapshot through parsing, compilation and scratch persistence |
| E | Approved development pilot and configuration freeze | Candidate configurations and cost/latency limits fixed before holdout results |
| F | Repeated held-out comparison and finalist export review | Evidence supports agent-specific keep/switch/inconclusive decisions |
| G | Reviewed improvements and optional selected-model rollout | Normal checks, release approval, explicit cohort and rollback evidence |

Keep these as sequential, reviewable changes on one logical task branch. Keep shared runtime/accounting changes separate from opt-in agent behavior. Do not delegate or change the coding agent's selected model.

**4. Milestone A: setup and preregistration**

After execution approval, fetch `origin/main`, create `/private/tmp/nova-creator-model-comparison` on `codex/creator-model-comparison`, and verify its HEAD equals the fetched base before edits. Preserve the existing checkout. Use the new worktree explicitly for every edit/test/commit. Inspect bootstrap configuration before allowing its migration step; use disposable local PostgreSQL for verification.

Inspect effective API and worker configuration through existing supported diagnostics. Record actual Main Creator/Copilot models, prompt versions, output budgets, retry policies, timeouts, capability cohort, and revision. The inspected defaults are Main Creator `gemini-3.1-pro-preview`, high thinking, 16,384 output tokens, 130-second agent timeout, three attempts and a Flash fallback; Copilot uses `EDIT_COPILOT_MODEL`, high thinking and two attempts, with different durable/stateless deadline bounds. Freeze the effective values, not just these defaults.

Verify availability of `GEMINI_API_KEY`, `OPENAI_API_KEY`, and `ANTHROPIC_API_KEY` without displaying their contents. Anthropic is currently only an optional observability/development dependency; add a tested runtime dependency if Sonnet becomes a supported runtime provider. Audit installed SDK capabilities rather than assuming the current loose dependency floors support the required APIs.

This checkout has no `.env`, API `.venv`, or configured `ADMIN_PROD_API_KEY`; the read-only review-queue metadata request therefore did not reach production. Bootstrap may provide existing environment assets, but availability must be checked. Missing credentials are resolved through the existing environment/secret workflow, never pasted into the plan or chat.

Preregister the corpus sampling rule, task matrix, exclusions, retry policy, scoring, configuration search budget, model selection rule, evidence levels and output-review requirements. Record costs and latency limits after baseline measurement but before candidate holdout results. Changes to these terms create a new experiment version.

**5. Milestones B and C: provider integration and accompanying improvements**

Proposed implementation locations, relative to the new worktree:

| Existing or proposed file | Responsibility |
|---|---|
| `src/apps/api/app/agents/_runtime.py` | Provider-neutral invocation/result contract, reservation/settlement for all providers, canonical completion states, shared deadlines |
| `src/apps/api/app/agents/_model_client.py` | Dispatch and unchanged legacy Gemini behavior |
| New `app/agents/_openai_client.py`, `_anthropic_client.py` | Native Responses/Messages adaptation; explicit timeouts; SDK retries disabled |
| New `app/agents/_provider_capabilities.py`, `_output_contracts.py` | Model capabilities, reasoning translation, schema preparation and coverage |
| `app/services/ai_cost_control.py` and a focused price module if needed | Versioned provider/model prices, estimates and normalized billed usage |
| `app/config.py`, `agents/main_creator.py`, `agents/edit_copilot.py`, `.env.example` | Independent model, reasoning, schema and cohort controls for these two agents |
| `src/apps/api/pyproject.toml` and deployment dependency installation | SDK versions available in the real runtime image |

Normalize provider result status to completed, refused, output-truncated, known rejection, and outcome-unknown. Do not fabricate Gemini-shaped responses for other providers. Preserve provider request ID, requested/resolved model, usage, latency, and schema version. Do not store hidden reasoning.

Use the native OpenAI Responses API for Sol and the native Anthropic Messages API for Sonnet. These agents return inert JSON; do not introduce provider-executed tools, autonomous agent frameworks, or broader permissions. Keep current parsers, compiler, ownership checks, revision fences, and approval/execution contracts authoritative.

Disable SDK automatic retries so the runtime owns attempt count and billing. A known pre-contact failure can be retried within the bounded policy. An accepted request with a lost response or running request past its local deadline remains outcome-unknown, charged/reserved conservatively, and cannot trigger an automatic second provider. KRI-675's suggestion of falling back after an unknown outcome is not adopted.

Extend paid-call reservation and settlement beyond the current `model.startswith('gemini')` branches. Reserve before network contact and settle successful and rejected/refused billed responses. Unknown outcomes retain their reservation. Distinguish an omitted price/model from a zero-cost model: an unrecognized experiment model fails preflight instead of inheriting a Google price.

Normalize usage without double-counting reasoning: some providers include it inside output usage, while Gemini exposes candidate and thought counts separately. Cache reads/writes and long-context tiers must match the selected provider. Attribute every call to experiment, arm, source case, repetition, agent and attempt using private identifiers. Keep benchmark calls within existing development limits; do not tag them as production or create canary/budget overrides.

Expose independent model and reasoning configuration for Main Creator and Copilot. Preserve existing defaults and fallback policy in the legacy baseline. New configurations are opt-in and pinned per run; a process environment change must not mutate an in-flight arm. Do not accept arbitrary model/provider routing from a creator HTTP request.

For Gemini test high versus medium; for Sonnet use supported adaptive-thinking/effort settings; for Sol test low versus medium. Reasoning labels are not equivalent units of compute. Select by observed quality, settled cost and latency. Do not silently map a Gemini token budget to another provider or retain unsupported sampling parameters.

**Schema feasibility is a required first check.** Main Creator's `CreativeStrategy` currently has 47 fields, 36 with union/nullable annotations. Copilot's parsed output has 15 top-level fields but `ops` is a generic list of dictionaries; exporting its Pydantic schema alone does not constrain valid edit operations.

Use explicit model-output wire schemas and existing operation definitions. Parse provider output through the existing authoritative parsers; do not expose server-computed output fields merely because they appear on the parsed output model. Enumerate safe, capability-supported operation shapes and preserve required fields and allowed values. Keep exact source-ID validation, arithmetic, length bounds unsupported by a provider, and cross-field rules in code.

Check schema complexity and required/nullable transformations against each provider's actual constraints before live submission. Narrow schema variants only from server-known capabilities, never from a model's guessed interpretation of the request. Record the provider projection, omitted constraints and schema hash. Never silently drop a product field, stringify JSON into an opaque payload to claim strictness, or fall back to JSON-only without recording a distinct profile.

If the complete compatible schema cannot fit within two focused feasibility attempts, produce the exact incompatibility and a bounded wire-contract proposal before a larger redesign. The experiment can still run an explicitly registered JSON-only comparison, but a full-schema improvement cannot be declared delivered. Do not weaken Nova's semantic validators to obtain provider acceptance.

Keep general observability safe for both agents: only operational metadata, counts, IDs/hashes and redacted errors. Audit existing Copilot projections before introducing new providers. Private evaluated inputs/outputs belong only in the authorized experiment store, never ordinary logs, Langfuse, Git, public CI artifacts, or public report bodies. Preserve safe operational diagnostics without changing creator-visible behavior.

**6. Milestone D: real-request corpus and private capture**

Reuse `services/request_following_collection.py`, `request_following_cases.py`, `request_following_private_store.py`, `training_eligibility.py`, `routes/admin_request_following.py`, and the existing review app. Real-request evaluation does not authorize manufacturing eligibility or exposing a holdout.

The queue captures conversations, turn plans and saved outcomes; it does not guarantee the original Main Creator input, original Copilot editor snapshot, or original rendered prompt. AgentRun observability is also not a complete historical prompt archive. Do not rebuild an allegedly exact historical case from today's mutable editor state.

Classify every source as: exact invocation capture; real request with reconstructed context; recorded response replay; or synthetic control. Report these separately. Existing real-request reconstructions are useful development cases but cannot be mixed into an exact historical replay result.

Add opt-in, default-off capture at the real invocation boundary, after full server context is assembled and before provider submission. Main Creator enters through `kria/planner.py::_call_main_creator`; Copilot enters through `routes/_copilot.py::run_copilot_turn` from durable and stateless callers. Deduplicate captures across those paths.

Each immutable case binds the original request/history, agent input, editor/capability/source snapshot, full rendered prompt or immutable prompt dependencies, source hash/revision, agent/prompt/schema versions, effective configuration, eligibility event/grant key, captured/expiry times, split, and source pointers for expectations. Preserve semantically relevant input; scrub signed URL credentials and secrets with explicit markers. A missing or truncated required input makes a case incomplete.

Capture is bounded, private, and failure-isolated from creator work. Reuse current eligibility, scope, owner checks, expiry and revocation behavior. Private input/output/report bodies expire no later than their governing source capture. A new grant cannot revive old copies. Recheck current eligibility before each provider submission, not just during import. Purge derived local exports on revocation/expiry as well as hiding them. Aggregate reports contain no creator excerpts or reversible identities.

Choose development cases from already inspected, eligible real sessions. Hold out entire creators and media events, including near-duplicates, translations and paraphrases. Existing holdout bodies are intentionally unavailable through ordinary review/export routes: do not weaken those routes. New exact holdout captures require a separate restricted evaluation path that can run frozen configurations and return permitted metrics without exposing cases to prompt tuning. Human authorized evaluators may inspect the source/output privately for scoring.

Initial targets: 20 development cases, roughly 10 per agent; a held-out supported matrix of 30 tasks (12 montage, 8 talking, 6 recorded voiceover, 4 slides), with both agents represented; 10 ambiguous/unsupported and 10 recovery controls, scored separately. Include English/Turkish, compound and negative instructions, exact quoted copy, later corrections, large clip pools, missing facts and no-op edits. Audit available case counts before treating these as achievable. Do not fill shortages with invented real cases.

Humans approve expected obligations from creator messages and legitimate later corrections. Neither assistant promises nor model output is ground truth. Use the existing review interface where it fits; do not impersonate its human reviewer. The runner may prepare proposed checklists, never attest that a person watched or listened.

**7. Experiment arms and fairness**

Use immutable configuration manifests rather than mutating class specs or global environment variables between calls. Every arm/repetition receives a fresh agent instance, the identical frozen input and a reset scratch state. Retry and parse feedback cannot leak between cases. Assign unique ledger attempt identities so arms are neither deduplicated together nor permitted to exceed the parent approved run cap.

| Arm | Purpose |
|---|---|
| G0 | Effective current Gemini configuration, current prompt, JSON behavior, reasoning and retry/fallback policy; preserve actual fallback attribution |
| G1 | Same Gemini model/prompt and high reasoning with the verified structured-output improvement; isolates schema effects on development cases |
| G2 | G1 with a lower supported reasoning setting; isolates reasoning effects on development cases |
| S | `claude-sonnet-5-5` with verified output profile, bounded retries, and a development-selected effort |
| O | `gpt-6.1-sol` with verified output profile, bounded retries, and a development-selected effort |

Select one improved Gemini configuration, G*, using the preregistered development rule. Freeze G0, G*, S and O before holdout evaluation. If G* equals G0, deduplicate it and report why. Prompts share the same task content; provider-specific transport/schema instructions must be recorded. Any semantic prompt tuning gets a version bump and equal bounded tuning opportunities on development only.

Compare operational configurations, not an imaginary equal-compute setting. Report first-attempt performance before repair and eventual performance under the bounded policy. Candidate arms never silently fall back to another provider. G0's existing Flash fallbacks remain visible in its operational baseline and are excluded from a purported Pro-only measurement.

For request-local comparisons, hold earlier conversation turns fixed. For closed-loop comparisons, let each candidate's earlier replies influence later steps through the same deterministic acceptance policy. Do not present teacher-forced historical follow-ups as proof that an entire candidate-led conversation works. Ambiguous or negotiated approvals require human-approved branches and cannot be auto-consented by the model.

Run supported holdout tasks three independent times per arm. Randomize/interleave provider ordering to reduce load/time-of-day confounding, within the shared budget. Report cold and warm-cache measurements separately; never intentionally warm only one provider. Freeze all selectors before unblinding.

**8. Runner, reports and actual execution**

Proposed files: `app/evals/model_comparison/{contracts,corpus,runner,scoring,report}.py` and `app/cli/compare_creator_models.py`. Names are proposed interfaces, not existing commands. Reuse current eval fixtures, parsers, structural checks, compiled draft projection and request-following assertions instead of creating another grading framework.

The command supports validate, dry-run, metered live execution, report, and blinded human review packet export. Default mode is offline; it refuses network access and model-triggering product tools. Dry-run emits case/arm coverage, missing evidence, supported schema constraints, worst-case per-batch reservations, expected call count, required keys without values, and the exact proposed live command.

Direct agent calls must not dispatch a Celery render or write production Jobs. Compilation and save/reload checks use scratch PostgreSQL and isolated owners/IDs. Bind publication-like checks to the local experiment only. Reuse actual export verification and reviewed media in a separate step for finalist end-to-end comparisons. Cloud or device routes need their real supported export paths; a mock, desired receipt or historical ready artifact cannot substitute.

For each attempt retain private raw response, validated output, parser repair/rejection summary, compiled operations, scratch persisted diff, latency phases, usage/cost, and request IDs. For final export review bind expected obligations and verdicts to the actual candidate output checksum, generation, source receipt, compiler/verifier/client versions, command and timestamp. Do not borrow an old ready variant's evidence.

Create private detailed JSON/JSONL and a sanitized Markdown/JSON summary, plus randomized A/B review packets. A model judge is optional, separately metered, calibrated against bilingual humans, and never the sole promotion authority. Do not treat the existing Sonnet 4.6 judge as an impartial certification of a Sonnet candidate.

**9. Acceptance and selection rules**

Primary agent metrics: share of supported requests with all obligations preserved; requested-operation precision/recall; first-attempt parse and semantic success; valid source references; unauthorized/incorrect actions; unrelated-edit preservation; and honest claims. Rank per agent and language/task stratum. Good explanations cannot compensate for missing operations.

Operational metrics: eventual fulfillment, clarification and corrective-turn burden, terminal/unknown outcomes, attempts, p50/p95 end-to-end and provider latency, cache state, settled cost plus outstanding reservations, and total cost per acceptable result. Missing, failed and unknown attempts stay in their relevant denominators; ineligible unavailable data is separately counted with reasons, never an invisible exclusion.

Finalist export metrics: request fulfillment in actual picture/audio, save/reload and exact retry correctness, and preservation of the last accepted artifact after failure. Only finalized actual-output evidence can claim export fulfillment. Report generated plans, persisted edits, measured exports, reviewed exports and deployed observations as distinct evidence levels.

Use KRI-727's proposed floors for promotion: at least 95% supported-run fulfillment and 90% of supported tasks passing all three runs; zero observed false completion, unrelated-edit loss or duplicate execution in the dedicated controls. Both supported and unsupported controls need source-defined correct behavior; indiscriminate refusal cannot pass. Ninety supported runs per configuration are an engineering gate, not a population reliability estimate.

Predeclare operational superiority as either at least five percentage points higher all-obligations success, or equivalent success within a two-point descriptive margin with at least 20% lower p95 latency or cost per acceptable result. These are proposed decision thresholds, not statistical proof. Show paired counts and creator/event-clustered uncertainty; if intervals or strata do not support the conclusion, report inconclusive and use a separately approved larger sample. Adjust interpretation for multiple comparisons. Never infer equivalence solely from a non-significant difference.

Choose Main Creator and Copilot independently. Retain Gemini when a candidate misses hard safety/execution floors, fails Turkish/other key strata, exceeds the preregistered latency/cost envelope, or lacks enough evidence. A shortlist is not permission to roll out a provider.

**10. Failure cases and verification fixed before implementation**

| Failure mode | Required evidence |
|---|---|
| Paid request without budget, invalid provider/model or insufficient reservation | No network call; explicit failed preflight |
| Simultaneous arms or retries exceed an approved batch cap | Real PostgreSQL reservation race stays within cap |
| Accepted call times out or response is lost | Outcome-unknown retained; no automatic fallback/double charge |
| Refusal/truncation/empty output | Correct provider-neutral classification and honest terminal report |
| Reasoning counted twice or omitted; cached input charged as fresh | Recorded provider usage reconciles to documented billing semantics |
| Current Gemini defaults change while adding providers | Captured transport and replay outputs remain unchanged for G0 |
| Unsupported schema, unsupported operation, foreign ID or lost constraint | Explicit rejection/coverage gap; authoritative parse/compiler still rejects |
| A parse repair silently changes request meaning | Independent obligation checker fails even if output is valid |
| Caller passes raw user text into logs or a shared artifact | Redaction sentinel fails across success/error/stream paths |
| Revoked/expired/wrong-owner data or modified snapshot | No provider contact; no review/export; derived copies invalidated |
| Holdout content is exposed or a known incident relabeled unseen | Restricted runner denies access; immutable split/provenance checks fail |
| Agent instance, feedback or mutable editor state leaks between arms | Isolated case reruns produce matching deterministic inputs/hashes |
| Compiler/save/render is mocked but report says fulfilled | Evidence-level validation refuses the claim |
| Restart/resume replays an already accepted uncertain paid call | Resume reconciles persisted attempts and does not resubmit blindly |

Prefer existing request-to-save PostgreSQL journeys and real export controls. Add focused adapter/usage contract checks before implementing those boundaries; avoid tests that simply mirror the new code.

Baseline checks to reuse after bootstrap, with cwd set explicitly to the task worktree:

```sh
bash scripts/dev/test-db.sh
bash scripts/check-api.sh test tests/agents/test_runtime.py tests/agents/test_provider_quota.py tests/agents/test_thinking_budget.py -q
bash scripts/check-api.sh test tests/services/test_ai_cost_control.py tests/integration/test_request_following_queue.py -q
bash scripts/check-api.sh test tests/evals/test_main_creator_evals.py tests/evals/test_edit_copilot_evals.py tests/evals/request_following -q
bash scripts/check-api.sh test tests/integration/test_actual_export_verification.py tests/integration/test_actual_export_completion.py tests/routes/test_actual_export_publication.py -q
bash scripts/preship-check.sh
```

Select additional required reliability/journey checks from the actual diff. Prompt changes bump both relevant prompt versions and pass free replay gates; replay is not live quality proof. Apply the editor-timeline verification skill if implementation changes timeline behavior, and the existing actual renderer gate if burn/layout changes become necessary. Neither is an excuse to enlarge this provider experiment.

**11. Paid stages and rollout**

No paid call starts during implementation or dry-run. First prepare a small, balanced development pilot with existing per-run approvals and a cap no greater than $2. The $0.20 provider-smoke cap applies if using that purpose. Include all providers in the estimate and reserve output/retry maxima conservatively. If the smallest fair batch cannot fit, reduce scope or output policy transparently; do not bypass the ledger.

After that pilot, present actual usage, latency, failure rates, total proposed experiment spend, per-batch manifests, and the next exact command for approval. A full repeated comparison requires multiple separately approved capped runs. Do not auto-split a large experiment into fresh run IDs to manufacture spending authority. Paid judging, product-triggered specialist calls and render/storage costs must be separately included or explicitly excluded from the estimate.

Review and merge the infrastructure and low-risk runtime improvements with routing/capture default-off. Enable capture only for the approved eligible cohort after deployment approval; do not enable all-user collection as an implementation side effect. Run evaluation without changing live creator model routing.

If the experiment supports a provider change, prepare a separate agent-specific internal cohort rollout, exact API/worker revision check, latency/error/fulfilled-request monitoring and rollback to the pinned Gemini configuration. Rollback stops new routing; it does not replay unknown calls or mutate approved drafts. Deploy through normal Fly/Vercel rules only when relevant and approved.

**12. Deliverables and immediate dependencies**

Deliver reviewed code, offline journey results, private corpus/manifest with eligibility and split metadata, dry-run and budget estimates, approved live result files, blinded human reviews, exact export evidence for supported finalists, a sanitized per-agent recommendation, and rollout/rollback instructions. Report unfinished evidence as pending.

Immediate dependencies are authorization to resume implementation setup after automatic review rejected the fetch, a task-local development environment, permitted real-request access, and later approval of concrete paid batches. Credential availability, eligible case counts, full schema compatibility, and broad actual-export coverage remain facts to verify rather than assumptions.

Execution is not complete until the real comparison and required output review are finished. At present only this plan and source-level feasibility checks are complete.

**References**

- Existing request capture and review: `docs/runbooks/request-following-review-app.md`; `docs/runbooks/request-following-learning-loop.md`.
- Agent/runtime/accounting: `app/agents/{main_creator,edit_copilot,_runtime,_model_client}.py`; `app/services/ai_cost_control.py`.
- Existing execution authority: `app/services/kria_editor_ops.py`; `docs/pipelines/creator-render-contract.md`; `docs/reviews/kri-734/implementation.md`.
- [Sonnet models](https://platform.claude.com/docs/en/about-claude/models/overview), [Claude structured outputs and limits](https://platform.claude.com/docs/en/build-with-claude/structured-outputs), [Claude pricing](https://platform.claude.com/docs/en/about-claude/pricing).
- [GPT-6.1 Sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol), [OpenAI reasoning and parameter guidance](https://developers.openai.com/api/docs/guides/latest-model), [OpenAI data controls](https://developers.openai.com/api/docs/guides/your-data).
- [Gemini structured output](https://ai.google.dev/gemini-api/docs/structured-output), [Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing), [Anthropic data retention](https://platform.claude.com/docs/en/build-with-claude/zero-data-retention).
