# 015 — Intro-writer anti-slop: kill the "the monkey changed my whole marketing perspective" class

**Status:** IN PROGRESS — implemented on PR #780; merge blocked on T5 (live judge shadow A/B, keyed machine)
**Planned at:** `fdfaaab8` (2026-08-05)
**Origin:** /investigate session 2026-08-05 (root cause verified static; learning key `intro-writer-persona-glue-slop`)
**Priority:** P1 — burned-on-screen output quality on the highest-traffic prompt in the repo
**Scope decisions:** full scope, no runtime rejection (eng-review D3); remaining calls auto-decided per user directive (2026-08-05): best technical call, ZERO model-spend increase — treat as prompt fine-tuning. Outside-voice review absorbed (10 findings; see GSTACK REVIEW REPORT at the end); notably its #3 **supersedes eng-review D4** — the runtime warn hook was dropped in favor of the offline scan (W6), which gives strictly more signal with less shipped code.

## Model-spend constraint (binding)

- **Runtime LLM calls per job: unchanged.** Still exactly one `overlay_format_matcher` + one `intro_writer` call per montage render; same model (`gemini-2.5-flash`), same `thinking_budget=512`, same output caps. All new guards are regexes and fixtures — free.
- **Prompt length delta:** W1 adds ~100 input tokens per intro_writer call (~$0.00001/job at flash pricing — noise, noted for honesty).
- **CI cost: zero.** Structural floor + guard test + fixtures run in replay mode, no network.
- **One-time validation cost only:** the repo's prompt-change rule mandates live judge evals before merging any prompt bump. Minimized: iterate in replay/structural first, then ONE live shadow A/B pass (intro suite: 14 fixtures = 9 golden + 1 existing adversarial + 4 new; plus one matcher fixture) — ~$2-4, one-time, keyed machine. No recurring cost.
- **Legacy remediation (optional, W6):** clearing a slopped persisted intro forces ONE intro_writer re-run on the next re-render — ~$0.0001 per affected job, bounded by the scan's list, one-time.
- **Explicitly rejected on spend grounds:** second-pass LLM critique/rewrite agent (~2x per-hook cost), model upgrade to a pro tier, thinking-budget increase.

## Problem

A montage plan-item render with a monkey in the footage produced the intro hook
"the monkey changed my whole marketing perspective". The line is a fabricated
transformation claim gluing the footage subject to a persona pillar. It reads as
AI thought-leadership slop, not something a human editor would write. Users see
this burned onto their video as the opening frame — the single most visible
piece of AI text in the product.

## Root cause (verified, see /investigate 2026-08-05)

Three compounding defects, none catchable by the current test suite:

1. **The pillar instruction outweighs the escape clause.**
   `prompts/write_intro_text.txt:21` instructs "nod to one of their content
   pillars, and tease this video's theme/idea". The drop-the-theme escape
   clause EXISTS in the same paragraph ("If the persona/theme can't be honored
   truthfully for this clip, ignore it") but only bites on invented facts/
   places/events — the model doesn't treat a subjective transformation claim
   as dishonoring truth, so it believes it honored both footage and pillar.
2. **The slop ban is literal strings, not a pattern class.** Line 64 bans
   "changed everything" / "changed me" verbatim. "changed my whole marketing
   perspective" walks past. The class — retrospective transformation / lesson
   framing — is never named.
3. **The exemplar library teaches the banned phrase.**
   `prompts/overlay_examples.json:63` ships `transformation-before-after-karaoke-01`
   with text `"this is what changed everything"` (added PR #338; the ban landed
   later in PR #507 — prompt/exemplar drift with no guard test).

Why tests miss it: CI runs safety-only structural checks (`check_intro_writer`
in `tests/evals/runners/structural.py:1256` — URL/word-cap/highlight only); the
quality judge is opt-in (`--with-judge`); every golden persona fixture has
persona PERFECTLY aligned with footage (zero conflict fixtures); and the rubric
(`tests/evals/rubrics/intro_writer.md:52`) scores persona_coherence **1** for
"ignores the persona/theme entirely" — punishing the correct drop-the-theme
behavior.

## Guard-point map

```
                       plan-item montage render
                                │
        all_candidates["persona"] {tone, pillars, theme, idea}
                                │
                                ▼
   overlay_format_matcher ──selects──▶ exemplars (overlay_examples.json)
                                │            ▲
                                │            │  [W2] exemplar sweep +
                                ▼            │  [W3c] guard test: every exemplar
                          intro_writer       │  passes slop_structural_failures()
                        (write_intro_text)   │
                                │            │
              [W1] prompt: pattern-class ban │
                   + translate-don't-echo    │
                                │            │
                                ▼            │
                          parse() ───────────┘
                                │
                                ▼
                 variants[i]["intro_text"] (persisted;
                 re-renders REUSE it, no LLM call)
                                │
                                ▼
                      burned intro overlay (prod)

   Offline / CI (all free, no prod code):
     [W3b] check_intro_writer + slop_structural_failures()  ← CI replay
     [W4]  4 persona-conflict fixtures + rubric fix          ← judge, opt-in
     [W6]  scripts/dev/scan_intro_slop.py over persisted
           intro_text: pre-deploy baseline + legacy slop
           list, post-deploy delta                           ← read-only DB
```

Single source of truth for the pattern list: `slop_structural_failures()` lives
in `app/agents/intro_writer.py` and is imported by the structural floor, the
exemplar guard test, and the offline scanner — the exact pattern
`quote_structural_failures` (`sequence_quote_writer.py:86`) already established,
so consumers can never drift.

## Workstreams

### W1 — Prompt revision (`prompts/write_intro_text.txt`), staged

1. Extend the DON'T list with the named pattern class:
   > No retrospective transformation or lesson-learned framing: "X changed
   > my/our ...", "what X taught me about ...", "X made me realize/rethink ...",
   > "the day X changed ...", "opened my eyes". These read as AI ad-copy. If
   > your line claims the footage changed or taught the creator something,
   > delete it and describe the moment instead.
2. Translate-don't-echo rule (eng-review D5): "If the theme/idea itself uses
   lesson/transformation framing ('how X changed my life'), do NOT repeat that
   framing — translate it into an in-the-moment hook about what the footage
   shows." Covers the second route the reported line could have arrived by
   (planner-authored slop framing echoed faithfully).
3. Add one self-check bullet: "It claims the footage changed/taught/made the
   creator realize something (transformation framing)."
4. **Deliberately NOT touched (outside-voice #5):** the existing pillar
   instruction + escape clause at line 21. The drop-the-theme permission
   already exists verbatim; rewriting it is wording churn carrying the plan's
   only real regression risk (model over-corrects, drops persona when
   aligned). Escalate to a conditional-pillar rewrite ONLY if the W5 shadow
   A/B shows the pattern ban alone leaves conflict fixtures failing.
5. Bump `IntroTextWriterAgent.spec.prompt_version` → `2026-08-05` with a
   changelog comment (existing convention in `intro_writer.py`).

### W2 — Exemplar sweep (`prompts/overlay_examples.json` + version tripwires)

1. Retext `transformation-before-after-karaoke-01` ("this is what changed
   everything") to an in-the-moment before/after hook that passes the new lint
   (e.g. "day 1 vs day 30" shape; final wording at implementation, must pass
   `slop_structural_failures`). Keep id/effect/colors — only `text` /
   `highlight_word` change.
2. `food-night-cluster-01` ("this bite changed plans") passes the lint
   (concrete, in-the-moment, no my/our/everything object) — KEEP as-is.
3. Bump `library_version()` in `app/agents/overlay_examples.py` → `2026-08-05`.
4. Bump `OverlayFormatMatcherAgent.spec.prompt_version` → `2026-08-05` per the
   bank-edit convention (both consuming agents bump on a bank content edit).
5. Update the committed-constant assertions in
   `tests/agents/test_market_research_banks.py`
   (`test_overlay_bank_version_couples_to_agent_versions`,
   `test_success_factor_bank_version_couples_to_consuming_prompt_versions`)
   with dated changelog comments, matching the existing comment style.
   **Note (outside-voice #4): W1+W2 are ONE atomic task** — the intro
   prompt_version is pinned in both tripwire tests, so a partial landing is
   red by construction.

### W3 — Deterministic slop patterns, one source of truth

1. **(a) Pattern module** in `app/agents/intro_writer.py`. Normalization
   (outside-voice #1, empirically verified): `text.casefold()` then STRIP
   `̇` (combining dot above — Python casefolds Turkish İ to `i` +
   U+0307, which breaks in-word matching; `[ıi]` classes alone do NOT fix
   it). Patterns written lowercase; unit-test with uppercase Turkish input
   ("DEĞİŞTİRDİ" case pinned):
   ```python
   _SLOP_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
       # EN — retrospective transformation / lesson framing
       ("en_changed_my",   re.compile(r"\bchanged\s+(my|our|everything)\b|\bchanged\s+the\s+way\s+(i|we)\b")),
       ("en_shifted_my",   re.compile(r"\b(shifted|transformed|reshaped)\s+(my|our)\b")),
       ("en_taught_me",    re.compile(r"\btaught\s+(me|us)\b")),
       ("en_made_realize", re.compile(r"\bmade\s+me\s+(realize|rethink|see|understand)\b")),
       ("en_opened_eyes",  re.compile(r"\bopened\s+my\s+eyes\b")),
       ("en_perspective",  re.compile(r"\b(whole|new|entire)\s+(\w+\s+){0,2}(perspective|mindset|outlook)\b")),
       # TR — same class (lowercase; input normalized per above)
       ("tr_degistirdi",   re.compile(r"\b(hayat[ıi]m[ıi]|bak[ıi]ş\s+aç[ıi]m[ıi]|her\s+şeyi)\s+değiştirdi\b")),
       ("tr_ogretti",      re.compile(r"\bbana\s+[\wçşğıöü\s]{0,24}öğretti\b")),
   )

   def slop_structural_failures(text: str) -> list[str]:
       """Names of transformation-slop patterns the hook matches (empty = clean)."""
   ```
   Final regex set tuned at implementation against ALL existing golden fixture
   outputs + the full exemplar library (both must pass clean). Pinned
   acceptance cases: the monkey line fails (`en_changed_my` +
   `en_perspective`); "this bite changed plans" passes; boundary negatives
   pass ("changed myth night", "shifted mystery tour" — outside-voice #6:
   trailing `\b` required); "changed my mind about berlin" FAILS by design
   (transformation claim — the prompt should produce "berlin at 6am was not
   the plan" instead); uppercase-TR positive matches after normalization.
2. **(b) Structural floor**: `check_intro_writer` in
   `tests/evals/runners/structural.py` appends
   `slop_structural_failures(text)` results — same import pattern as
   `check_sequence_quote`. Runs in CI replay mode, no network.
3. **(c) Exemplar guard test** (new file
   `tests/agents/test_overlay_examples_slop_guard.py`): every entry from
   `load_overlay_examples()` passes `slop_structural_failures` — kills the
   PR#338/#507 EXEMPLAR-drift class permanently. Plus unit tests for the
   pattern fn itself (cases above).
4. **(d) — REMOVED** (outside-voice #3, supersedes eng-review D4). No runtime
   warn hook: intro outputs are already persisted on variants and in agent-run
   rows, so the offline scan (W6) yields the same signal with a pre-deploy
   baseline, covers the legacy backlog the runtime hook structurally cannot
   see (persisted-intro reuse skips the LLM entirely,
   `_resolve_intro_text` ~`generative_build.py:2098`), and ships zero prod
   code. A blind warn rate would also conflate accepted false positives
   ("changed my mind about berlin") with real slop.

### W4 — Smarter evals

1. **Four adversarial fixtures** under
   `tests/fixtures/agent_evals/intro_writer/adversarial/`:
   - `persona_conflict_monkey_marketing.json` — marketing pillars/theme, zoo/
     monkey footage; recorded output = grounded footage-only hook.
   - `persona_conflict_fitness_cooking.json` — fitness persona, cooking clip.
   - `tr_persona_conflict.json` — TR language, mismatched persona.
   - `persona_aligned_slop_idea.json` (eng-review D5) — gym footage + idea
     "share how the gym changed my life"; recorded output = in-the-moment hook
     (pins the translate-don't-echo rule).
   Recorded outputs are hand-authored to the desired behavior (footage-only /
   in-the-moment, passes structural floor); live mode re-runs them against the
   real model.
2. **Rubric fix** (`tests/evals/rubrics/intro_writer.md`):
   - persona_coherence "Not applicable" rule extended: when persona/theme IS
     given but the hero clip cannot honor it truthfully, a grounded
     footage-only hook scores **5** (dropping the theme is the correct move) —
     the current anchor scoring 1 for "ignores the persona/theme" applies only
     when the footage COULD have honored it.
   - New auto-1 anchor on persona_coherence AND voice_match: a hook that
     bridges footage to a pillar via a claimed lesson/transformation ("the
     monkey changed my whole marketing perspective") scores 1 on both.
   - Add that line verbatim as a calibrated 1-example.

### W6 — Offline slop scan (baseline, legacy backlog, post-deploy delta)

New read-only dev script `scripts/dev/scan_intro_slop.py` (stdlib + app
imports, same style as `scripts/dev/reset-stuck-plans.py`):
- Iterates `Job.assembly_plan["variants"][*]["intro_text"]` (+
  `sequence_quote` values, report-only) and runs `slop_structural_failures`;
  prints job_id, variant, matched patterns, text.
- Run BEFORE deploy → prevalence baseline + the legacy remediation list (the
  actual monkey job is on it — persisted intros survive re-renders forever).
- Run AFTER deploy (new jobs window) → the before/after delta that replaces
  the dropped runtime warn as the rollout signal.
- **Remediation of listed jobs (optional, bounded):** clearing a variant's
  `intro_text` makes the next re-render re-run intro_writer under the new
  prompt (~$0.0001/job, one-time). NOT automated in this plan — the
  `_finalize_job` allowlist / wholesale-variant-replace pitfalls (learnings
  `finalize-allowlist-strips-new-variant-fields`) make bulk writes to
  variants dangerous; listed as a follow-up with the scan output as its
  input.

### W5 — Verification & rollout

Offline (this machine, CI — all free):
- `cd src/apps/api && pytest tests/` — full tree per repo convention.
- Replay evals: `pytest tests/evals/ -v` (structural floor now includes slop
  patterns; all recorded outputs must pass).
- `bash scripts/preship-check.sh`.
- CI "Require eval fixture (T8)" gate: satisfied by the 4 new fixtures
  (learning `nova-eval-fixture-gate-matches-schemas-dir`).
- **Honesty note (outside-voice #9):** replay CI guards ARTIFACTS (recorded
  outputs, exemplars, prompt/version tripwires), not model behavior.
  Post-merge protection against the monkey class RECURRING rests on the
  live-eval rule below being followed on future prompt edits — green CI alone
  is not behavioral coverage.

Live (keyed machine — REQUIRED before merge, prompt-change rule):
- Shadow A/B: `NOVA_EVAL_MODE=live pytest tests/evals/test_intro_writer_evals.py
  -v --eval-mode=live --with-judge --allow-cost --shadow-prompts-dir=prompts.candidate`
  — candidate must not regress any golden fixture and must pass the 4 new
  adversarial fixtures. If conflict fixtures still fail → escalate W1.4
  (conditional-pillar rewrite) and re-run once.
- Run `test_overlay_format_matcher_evals.py` live once (its prompt_version
  bumped via the bank tripwire; behavior should be unchanged — regression
  check).

Rollout: no feature flag — the prompt version IS the rollout; deploy ships it.
Rollback = revert the squash-merge commit. Post-deploy signal: W6 scan delta +
spot-check hooks in `/admin/jobs` debug view.

## NOT in scope

- **`sequence_quote_writer` treatment** — rhythm-mode quotes are deliberately
  aphorism-like; the transformation-frame ban may be WRONG for that surface.
  W6 scans its outputs report-only to size the problem first. (Follow-up TODO.)
- **Runtime hard-rejection in parse()** — rejected at eng-review D3: a false
  positive on the highest-traffic prompt would downgrade good hooks to the
  generic "watch X unfold" fallback with no human in the loop.
- **Runtime warn hook** — dropped per outside-voice #3 (supersedes D4): the
  offline scan dominates it (baseline, legacy coverage, zero prod code).
- **Planner-side cringe wording** (`generate_content_plan.txt`) — the
  `/audit-plan-quality` skill's territory.
- **Plan-footage mismatch detection** (outside-voice #10) — the upstream
  question this bug exposes: the user filmed a monkey against a marketing
  plan item, and a footage-only hook silently stops serving the plan item
  with no signal to planner or user. Detection can piggyback on W6 scan
  output + judge notes from the one-time live run at zero recurring cost.
  (Follow-up TODO — genuinely separate product scope.)
- **Bulk legacy remediation** — W6 produces the list; automated clearing of
  persisted intros is follow-up work gated on the finalize-allowlist pitfalls.
- **Stale TODOS.md entry** ("Clip notes → intro_writer" — already shipped in
  prompt_version 2026-06-18) — status-flagged in TODOS.md, not deleted
  (collaborative repo).

## What already exists (reused, not rebuilt)

- `quote_structural_failures` shared validation pattern
  (`sequence_quote_writer.py:86` ← `structural.py`) — W3 copies this shape.
- `_REFUSAL_PATTERNS` in `intro_writer.py` — precedent for output pattern lists.
- Eval fixture format, judge harness, `--shadow-prompts-dir` live A/B — used
  as-is for W4/W5.
- Committed-constant tripwires in `test_market_research_banks.py` — extended,
  not replaced.
- Persisted variant `intro_text` + agent-run rows + `/admin/jobs` debug view —
  W6 reads them; zero new prod observability code.
- `scripts/dev/reset-stuck-plans.py` — style precedent for the W6 scanner.

## Failure modes (new codepaths)

| Codepath | Realistic failure | Test? | Handled? | User-visible? |
|---|---|---|---|---|
| `slop_structural_failures` regex | False positive on a good hook ("changed myth night") | unit boundary negatives | eval-only ⇒ no prod behavior change | invisible |
| `slop_structural_failures` regex | False negative (new paraphrase: "flipped my outlook") | known-miss class documented in unit tests | judge rubric is the semantic backstop | slop ships until next prompt iteration |
| TR normalization | combining-dot casefold miss on uppercase TR | unit: "DEĞİŞTİRDİ" positive pinned | `̇` strip after casefold | invisible |
| structural floor addition | existing recorded fixture output trips a new pattern | replay eval run in CI | fix pattern or fixture BEFORE merge | n/a (CI) |
| prompt change | model over-corrects: drops persona even when aligned | golden persona fixtures re-judged live (shadow A/B gate) | W1 staged — pillar clause untouched unless evidence demands | hooks lose persona voice |
| bank version bump | forgot a tripwire assertion | `test_market_research_banks.py` fails | mechanical; W1+W2 atomic | n/a (CI) |
| W6 scanner | DB unreachable / malformed variant JSON | manual dev script; tolerant per-row try/except | non-blocking, read-only | n/a (dev tool) |

Critical-gap check: the false-negative row has no deterministic test AND no
runtime handling — by design (judge + prompt are the semantic layer; runtime
rejection explicitly rejected at D3). Accepted residual risk. 0 critical gaps
(no silent, untested, unhandled failure affecting users).

## Test plan

```
CODE PATHS                                              EVALS / USER FLOWS
[+] app/agents/intro_writer.py                          [+] montage upload → intro hook
  ├── slop_structural_failures()                          ├── [→EVAL] persona-conflict EN ×2 (new adversarial fixtures)
  │   ├── [NEW ★★★] monkey line fails (unit)              ├── [→EVAL] persona-conflict TR ×1 (new adversarial fixture)
  │   ├── [NEW ★★★] all 9 golden outputs pass (unit)      ├── [→EVAL] aligned + slop-framed idea ×1 (D5 fixture)
  │   ├── [NEW ★★★] boundary negatives (changed myth…)    ├── [→EVAL] aligned-persona goldens must NOT regress (existing 9)
  │   ├── [NEW ★★★] TR uppercase casefold+U+0307 (unit)   └── [→EVAL] live shadow A/B on keyed machine (gate)
  │   └── [NEW ★★ ] empty/None text → [] (unit)
  └── prompt_version bump                               [+] format matcher (version bump only)
      └── [EXISTING] test_market_research_banks tripwires    └── [→EVAL] existing matcher evals re-run live once
[+] tests/evals/runners/structural.py                   [+] exemplar library
  └── check_intro_writer + slop floor                        └── [NEW ★★★] every exemplar passes slop lint (guard test)
      ├── [NEW ★★★] slop output → structural failure listed
      └── [EXISTING] safety checks unchanged            [+] scripts/dev/scan_intro_slop.py
                                                             ├── [NEW ★★ ] matches slopped variant fixture (unit, sqlite/stub)
                                                             └── [NEW ★★ ] tolerant of malformed variants (unit)
COVERAGE: all new branches tested; behavioral regression risk carried by the live shadow A/B gate (see W5 honesty note)
```

## Worktree parallelization

Sequential implementation, no parallelization opportunity — W1–W4 converge on
`intro_writer.py` and its tripwire tests; W6 depends on W3's pattern fn. Total
CC effort under an hour.

## Implementation tasks

- [ ] **T1 (P1, human: ~3h / CC: ~15min)** — prompts — W1 staged prompt revision + W2 exemplar sweep + ALL version bumps + tripwire assertions (atomic per outside-voice #4)
  - Files: `src/apps/api/prompts/write_intro_text.txt`, `src/apps/api/prompts/overlay_examples.json`, `src/apps/api/app/agents/intro_writer.py`, `src/apps/api/app/agents/overlay_examples.py`, `src/apps/api/app/agents/overlay_format_matcher.py`, `src/apps/api/tests/agents/test_market_research_banks.py`
  - Verify: `pytest tests/agents/`
- [ ] **T2 (P1, human: ~3h / CC: ~15min)** — agents — W3 `slop_structural_failures` (casefold + U+0307 strip + EN/TR patterns) + structural floor + exemplar guard test
  - Files: `src/apps/api/app/agents/intro_writer.py`, `src/apps/api/tests/evals/runners/structural.py`, `src/apps/api/tests/agents/test_overlay_examples_slop_guard.py` (new)
  - Verify: `pytest tests/ && pytest tests/evals/ -v`
- [ ] **T3 (P1, human: ~2h / CC: ~15min)** — evals — W4 four adversarial fixtures + rubric fix
  - Files: `tests/fixtures/agent_evals/intro_writer/adversarial/*.json` (4 new), `tests/evals/rubrics/intro_writer.md`
  - Verify: `pytest tests/evals/test_intro_writer_evals.py -v` (14 fixtures = 9 golden + 1 existing adversarial + 4 new)
- [ ] **T4 (P2, human: ~2h / CC: ~10min)** — tooling — W6 offline scan script + unit tests; run pre-deploy, save baseline output
  - Files: `scripts/dev/scan_intro_slop.py` (new), unit test alongside existing script tests
  - Verify: `python3 scripts/dev/scan_intro_slop.py --help` + unit tests
- [ ] **T5 (P1, human: ~1h / CC: n/a — needs keys)** — verification — W5 live shadow A/B judge run on a keyed machine; matcher evals re-run; record deltas in PR body; escalate W1.4 only if conflict fixtures fail
  - Verify: judge avg ≥ 3.5 on all 14 fixtures incl. 4 new; no golden regression
- [ ] **T6 (P2, human: ~15min / CC: ~2min)** — docs — DECISIONS.md entry (prompt/exemplar drift class, the guard that now pins it, and the D4-superseded-by-scan rationale)
  - Files: `agents/DECISIONS.md`
  - Verify: n/a

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Codex Review | `/codex review` | Independent 2nd opinion | 1 | ISSUES_ABSORBED (claude subagent) | 10 findings: 8 accepted, 1 accepted-as-staging (#5), 1 moot (#8) |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR | 12 issues, 0 critical gaps (D3 scope gate, D4, D5 + code-quality/test/outside-voice absorptions) |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | — |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

- **CROSS-MODEL:** Outside voice (Claude subagent; Codex CLI not installed) contradicted two in-review calls and won both on evidence: (a) runtime warn hook dropped — offline scan W6 dominates it (baseline + legacy persisted-intro coverage + zero prod code); **supersedes eng-review D4/1A**, decided under the user's auto-decide directive; (b) W1 pillar-clause rewrite staged behind live shadow A/B evidence — the escape clause already exists verbatim at write_intro_text.txt:21, and the rewrite carried the plan's only real regression risk. Its empirical TR-casefold catch (İ → i + U+0307 breaks in-word regex matching) fixed a latent bug in the plan's own sketch.
- **VERDICT:** ENG CLEARED — ready to implement. User constraint honored throughout: zero model-spend increase (no new runtime LLM calls; one-time ~$2-4 live-eval validation mandated by the repo prompt-change rule is the only cost). Live shadow A/B on a keyed machine is a merge gate (T5), not optional.

NO UNRESOLVED DECISIONS
