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
