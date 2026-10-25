# Runtime creative skills — acceptance and evidence

Base: 37f09a2b24a6fbb11cd5b2cbc559fa91816c461c. Related quality work: KRI-215;
wording-authority incident: KRI-651. This change does not close either issue.

## Acceptance defined before implementation

- Production agent prompts receive packaged, bounded editorial instructions at
  their actual model-call boundary; filesystem names never come from user input.
- The phone post-caption path passes available transcript and approved strategy
  summary, with intent explicitly separated from observed footage. Missing inputs
  stay missing; a title alone must not license invented locations or outcomes.
- Hooks preserve wording approval, existing language/shape limits and source-data
  trust boundaries. Editing guidance cannot expand renderer capabilities or replace
  pinned order, timing, audio or text.
- Social copy may return zero useful hashtags; no mandatory filler or unsupported
  trending/reach claims. A conservative product budget is at most five hashtags
  each for TikTok and Instagram, not a claim about current platform limits.
- Replay contracts remain valid. Provider transport can be replayed, but successful
  replay does not establish better generated writing or a successful native export.

## Failure modes and checks

Missing packaged files; unrelated skills injected; frontend-only discoverability;
placeholder metadata sent instead of the skill body; user-selected paths; missing
context; intent mistaken for rendered evidence; fabricated speech on silent footage;
context injection; changed approved Unicode wording; blank tags rejected by evals;
fallback restores generic hashtags; private context persisted by instrumentation.

Check the real agent runtime with captured provider prompts and recorded responses,
project job context through the phone caption caller with provider transport replayed,
run existing main-creator and intro-writer fixtures (including adversarial and TR),
and exercise empty-tag and placeholder negative controls. Check missing/unknown skill
names fail visibly. Keep DB-backed feed/persistence checks separate.

## Audit findings

- MainCreatorAgent already has detailed capability, consent, language and request
  preservation rules. Its unified phone montage compiler is deterministic, not an
  LLM: installing a skill there would have no effect.
- IntroTextWriterAgent already uses a substantial prompt, exemplars, creator context,
  preference feedback and success-factor references. A hook skill should add a small
  selection/payoff procedure, not replace these constraints.
- PlatformCopyAgent has minimal inline instructions and mandates 5 TikTok / 10 IG
  hashtags; its rubric rewards generic broad tags and penalizes empty tags.
- The phone plan-block caller supplies only title + language, even though Job stores
  transcript and all_candidates.creator_strategy. Existing fallback also emits
  generic viral/trending tags.
- No runtime SKILL.md loading exists on these paths. Local coding-agent skills are
  not automatically consumed by deployed Python agents. Files inside app/ ship via
  the existing Docker COPY; no host skill directory or new deployment mount needed.

The supplied hook-caption-hashtag research is secondary context, not instructions.
No platform-rule or engagement-lift claims from it are encoded as verified facts.

## Evidence limits

Creative preference on held-out edits, source-video inspection, native export and
production rollout remain unverified until those actual checks are performed.
Offline replay returns recorded responses regardless of prompt; it proves wiring
and compatibility, never comparative generation quality. No paid model calls or
production changes are part of this local verification.

## Implemented boundaries

| Agent/path | Before | Change |
| --- | --- | --- |
| Main creator | Detailed contracts, no reusable runtime skill | Explicit edit-story + grounded-hook bodies before the existing prompt; v81; original base prompt hash still checked |
| Intro writer | Extensive examples and grounding rules | Shared opening-selection/payoff procedure; existing parsing and layout constraints retained |
| Platform copy | Hook + transcript, fixed tag quotas, forced energetic voice | Social-caption skill, JSON source-data boundary, approved-intent field, 0–5 social hashtag budget |
| Phone plan blocks | Title and language only | Bounded Job transcript/strategy projection, DB session released before provider call |
| Offline scoring | Required filler tag counts | Empty relevant sets accepted; placeholder negative control retained; rubric rewards evidence and added context |

## Verification record (24 October 2026)

Working directory for every command: `/private/tmp/nova-creative-agent-skills`.

- `bash scripts/check-api.sh test tests/evals/ -q`: **2173 passed, 15 skipped,
  7 xfailed**. Offline only. The skips/expected failures belong to existing suite
  coverage; this is not a claim that those cases now work.
- `bash scripts/check-api.sh test tests/kria/test_plan_snapshot_postgres.py
  tests/kria/test_plan_review_feed_postgres.py -q`: **10 passed** against the
  task's isolated migrated Postgres. The existing caption-persistence test now
  exercises real Job loading → real agent/runtime → replayed provider → persisted
  caption → reduced feed; it asserts transcript, intent and skill reach the
  provider. Existing retry/timeout/scoped-update behavior is covered here.
- Ruff check and format check: clean on all thirteen changed Python files.
- Skill Creator `quick_validate.py`: all three runtime skills valid (run with
  the API venv Python, which supplies PyYAML).
- `git diff --check`: clean.

Recorded provider output is unchanged by new instructions in replay. These results
establish packaging/wiring, context delivery, output-contract compatibility and
persistence, not factual grounding of newly generated copy or editorial preference.
The placeholder control is structural; independent wrong-footage rejection and
human-blind creative preference remain pending. No comparative quality score,
engagement lift, inspected native export or live production verification is claimed.

Live A/B needs the existing development usage-ledger reservation with an explicitly
approved budget (maximum $2, no paid judge by default). Review the same held-out
source edits and inputs under baseline/candidate code and record prompt versions,
raw output references, costs and blind human decisions. Pass requires no new
unsupported factual claims, no approved-wording changes, and a strict preference
improvement; ties do not establish a quality gain. No such spend has been authorized
or executed in this change.

- `python3 scripts/reliability-gates.py run --changed --log
  .dev/creative-skills-reliability.log`: **505 passed**, no environment skips/errors.
  Covers the four selected incident gates (per-shot text, voiceover alignment,
  Berlin montage and thinking budget). The initial run exposed four expected
  prompt-byte changes plus the version assertion; the repaired guard still checks
  the original base-prompt hashes beneath the explicit skill layer.
- `bash scripts/check-api.sh test tests/agents/test_main_creator_agent.py
  tests/agents/test_market_research_banks.py tests/agents/test_intro_writer.py -q`:
  **120 passed**. Exact-version guards updated for the intentional prompt bumps;
  optional reaction-beat omission still checks the same base template under the
  skill prefix. Research-bank versions themselves remain unchanged.

## Revision 2 acceptance — 25 October 2026 (before editing)

User blind preferences for v1: skills 8 / pre-skill 2 / indifferent 1 / neither 1.
These were option-level votes, not separate hook/caption quality measurements.
The creator explicitly rejected the captions on cases 8 and 9, rejected both
midnight-pasta options, and was indifferent on the monkey/marketing example.

Root causes visible in the submitted prompts/outputs:
- The hook template explicitly says to nod to a content pillar and tease the
  theme; this competes with its own footage-first rule and the packaged skill.
- Persona style and domain are conflated: a trainer's direct delivery becomes
  a compulsory discipline message, even over cooking footage.
- Caption guidance asks for an added layer/question, which can reward invented
  personal stakes, a motivational conclusion or an unnecessary engagement ask.
- Copy is given a generated hook; that is a draft, not independent factual evidence.

Bounded edit set: two focused body revisions (grounded-hook and social-caption),
remove the contradictory persona-steering sentence in the hook template, put the
hook skill's final check after the template, and bump all affected prompt versions.
Keep routing, JSON contracts, creator authorization, renderer limits, language
handling and generation settings unchanged. Do not add lexical output filters.

Observable acceptance: source topic stays intact despite an unrelated account
niche; natural creator voice without invented first-time/confession/recipe/result
claims; a brief situational caption is acceptable when no added information is
available. Explicit creator-requested analogies and confirmed personal claims
remain allowed within existing approval rules.

Verification: existing hook/creator/copy replay and wiring checks plus a new blind
live comparison of current skill v1 against revised v2. Include six reviewed failure
or neighboring cases and six newly authored synthetic transfer cases. Freeze the
revised guidance before running them; keep models/settings/input context equal.
One shared development-ledger run, maximum $2 including retries, no paid judge,
no stylistic rerolls. Preserve all outputs and randomize labels before provider calls.
Human preference is the creative gate; prompt/keyword assertions cannot prove
these semantic properties. Do not claim v2 improves writing until that review.

Revision 2 local checks before live review: 222 focused replay/wiring/agent checks
passed; 505 selected reliability checks passed without environment skips/errors;
Ruff and both revised skill validators passed. Transfer inputs are retained in
`tests/fixtures/creative_copy_review/v2_cases.json` as input-only hand-authored
synthetic data, with no expected generated wording. No live-quality acceptance is
claimed before the new blind review.

Revision 2 verification record:
- Tested code revision: `dc1a0a204`; skill-v1 comparator: `04cc8ced2`.
- Full free replay suite: **2173 passed, 15 skipped, 7 xfailed**; focused checks
  **222 passed**; selected reliability gates **505 passed**. No new environment
  skips/errors in the reliability run. Both revised skills validate; Ruff is clean.
- Live refinement run `creative-skills-refinement-20261025-v2`: **44 completed calls**
  to the unchanged `gemini-2.5-flash` model configuration, **$0.156015** settled,
  no open/unknown reservations, one atomic **$2** development cap including retries.
- Six reviewed regressions plus five new synthetic transfer inputs were run. The
  overlapping kite scenario was deferred before any provider call: 12 cases had
  a conservative single-attempt bound of $2.001786; 11 cases bound to $1.835073.
  No model/output budget settings were changed to fit the cap.
- V1 skill/template loading was independently checked against all six prior v1
  hook prompt hashes. Inputs and caption context are equal between versions;
  captions receive their own generated hook. No stylistic rerolls or wording edits.
- Blind review artifacts and a separate answer key are retained locally under
  the task's `creative-copy-comparison-v2` artifact directory. The key remains
  hidden pending user votes. No comparative creative-quality pass, no full
  resolution of the theme-forcing problem, and no deployment are claimed yet.

## Requested production delivery — acceptance before implementation

User explicitly authorizes fixing, testing, merging and deploying the runtime
skills. Latest review does not establish a creative-quality pass: 1/2/4/7 preferred
the refinement, 3/5 supplied creator hook alternatives, 6/8/10 rejected both,
9/11 rejected the premise. No further paid generation is required for deployment.

Required boundary changes:
- Public-facing Turkish copy addresses its audience with plural siz/size forms;
  verbatim source speech and first-person creator statements remain unchanged.
- Account pillars, generated themes/ideas, TikTok performance summaries and proposed
  edit summaries must not be sent to hook/caption writers as topic evidence.
- A free-text tone cannot smuggle an account niche into writing. Project only a
  bounded vocabulary of delivery qualities; unknown tone defaults to conversational.
- Explicit creator direction and real source descriptions/transcripts still reach
  their owning writer. This fix does not authorize inventing facts or rewriting
  approved text. The sequence writer shares the same projection and must keep parity.
- Deploy the merged revision through Fly Deploy, verify both API and worker assets
  and prompt versions without triggering paid calls, and label native-output / new
  creative-quality verification pending. No claim of solving every writing weakness.

Failure modes: residual raw tone/topic in the provider prompt; dropped valid voice;
account metadata reintroduced through a shared sequence helper; dropped source or
explicit creator instruction; incorrect formal/robotic Turkish; modified quotations;
missing skill files in the Docker image; only API updated while workers stay old;
CI journey mapping or prompt-version checks omitted. Provider-capture integration
checks are the primary regression gate, with current prompt replay/DB feed tests,
required mapped reliability/journey CI, exact-image deployment and in-container
prompt-load probes. Output semantics and a fresh native export remain separate.

Final delivery input projection (2026-10-25):
- Intro and sequence copy retain legacy input fields for recorded fixtures but
  omit account pillars/theme/idea and TikTok summary from provider prompts.
- Free-text tone is projected through a fixed delivery vocabulary. Unknown or
  niche-only tone becomes natural/conversational. Creator direction is separate.
- The phone main creator receives delivery-only account context and visual knobs;
  auto idea/theme, persona summary/audience/pillars and style rationale are omitted.
  Project notes, filming instructions and capability/current-format state remain.
- The post-caption caller passes the actual available transcript, not a proposed
  strategy summary. Its raw tone uses the same delivery projection.
- Public Turkish audience copy uses plural siz/size in intro, sequence, caption
  and main-creator authored hook/title instructions. Chat address is not changed.
- Regression probes failed before the projection and pass after it. The free
  replay/agent/context batch passed 2329 checks (15 existing skips, 7 xfails);
  168 additional creator checks and 10 real Postgres feed checks passed. Selected
  reliability gates passed 505 checks without environment skips/errors.
- CI gains an explicit copy-skill journey mapping; existing native and API gates
  remain selected. No VERSION/CHANGELOG edits: repository post-merge automation
  owns release metadata. Native render output and new creative preference remain
  unverified; claim stays local_fix with KRI-215 owning that quality gap.

Pre-landing review: current-agent two-pass review completed over source projection,
privacy, trust boundaries, schema compatibility, job-session lifetime, packaging and
changed tests. No SQL mutation, new endpoint or provider/model change. Topic-removal
assertions intentionally replace former topic-presence assertions under the user's
explicit request; plural-address assertions replace the former singular rule.
Hashtag-count floors were replaced by the explicitly chosen 0–5 social tag budget;
placeholder and duplicate-output controls remain. Existing prompt-byte guards still
check the base template beneath the skill prefix. The new PLC0415 suppression is the
existing lazy-import convention in plan_blocks, not an exception hiding test failure.
No independent subagent review was run under the user's no-delegation instruction.
