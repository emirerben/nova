# KRI-524 post-deploy follow-up — 2026-10-08

PR #1481 did not resolve the creator's complete request. Production diagnostics and an authorized, metered replay identified two separate failure stages. This follow-up is not evidence that all prompts now work.

## Causes and changes

1. **Brief extraction rejected correct meaning because of a guessed version.** The saved brief was version 4. Three captured attempts targeted the appropriate existing requirement but returned version 1, a description string, then version 1. The narrow extractor now attaches the immutable pre-inference brief version on the server. Target/content validation and the final persistence compare-and-swap remain authoritative; applying the resulting update to a genuinely newer brief still fails. No new database/API contract.
2. **The editor dropped compatible prior requirements.** The brief retained fade-in/fade-out instructions alongside a subsequent word-sequence request. Flash split the words without fades, then chose a whole-title typewriter effect after the prompt was clarified. Pro with the same clarified prompt produced 13 separate words with sequential fade-in/fade-out, preserving other text. The shared editor defaults to Pro/high, carries compatible accumulated constraints, and receives the negotiated operation version explicitly. This applies to all editor/copilot surfaces, including slides; no phrase-specific routing was added.
3. **A one-word “split” produced misleading completion and identity churn.** A single unchanged segment without a patch now asks for clarification. A genuine style patch on one segment preserves its ID and reports a style update, without inventing sequence ancestry.

The older draft already contained only its first word before the captured edit. Available diagnostics do not establish how that text was lost. This change does not silently restore old wording over current user text.

## Live evidence (six runs, eight provider calls)

All calls used the same enforced $0.50 evaluation envelope; settled reservation total **$0.262114**. Prior KRI-524 evaluations were $2.160176, making the cumulative recorded total **$2.422290 of $5**. No unmetered judge, production edit, save, or regeneration was performed.

| Run | Evidence | Result | Settled USD |
|---|---|---|---:|
| Original extractor v3 | Exact private incident input, Pro/low | 3 attempts; guessed/malformed versions; rejected | 0.047082 |
| Fixed extractor v4 | Same incident input | First attempt accepted; correct semantic change | 0.011032 |
| Editor v74 Flash | Current saved title and accumulated brief | 13 words, missing fades | 0.025403 |
| Editor v75 Flash | Same input, clarified shared prompt | Whole-title typewriter; fails request | 0.020743 |
| Editor v75 Pro/high | Same input and clarified prompt | 13 fading words, no overlap, unrelated labels preserved | 0.089064 |
| Editor v75 Pro/high | Fully authored six-word sequence; “twice as fast” | Six seconds → three; words, fades and unrelated label preserved | 0.068790 |

The private Pro comparison used a 60-second diagnostic timeout and completed in about 23 seconds. The authored faster case used the proposed production settings (40 seconds, two maximum attempts) and completed in about 21 seconds. Production settings are not claimed to have been deployed. Observations are small-sample behavior checks, not model reliability or fleet latency estimates.

Automatic approval rejected an additional private-context faster replay as outside the original replay approval. The faster check therefore used wholly authored synthetic input, with no production conversation, media, or identity.

## Repeatable offline evidence

- New version-binding regressions failed before the change (10 failures), then passed, including stale real-version rejection, removals, and multiple request families.
- New degenerate-sequence regressions failed before the change (two failures), then passed.
- `python -m pytest -q tests/agents tests/evals tests/routes/test_phone_editor_commit.py tests/routes/test_phone_text_sequence_followup.py tests/test_edit_copilot.py tests/services/test_kria_editor_ops.py`: **3,330 passed, 15 skipped**.
- `make verify-kria`: **1,932 passed, 6 skipped**, with isolated local PostgreSQL access.
- Scoped Ruff, diff checks and preship checks pass.
- Portable recorded-response replays use authored/minimized snapshots and independent outcome assertions. Both Flash failures remain negative controls. The synthetic faster capture is separately labelled. Unknown provider outcomes prove one call with no retry/fallback.
- Real phone Save recipe compilation on local fixtures preserves word/phrase timing, fade phases, source/audio tracks and later group style edits. Existing synthetic pixel tests run in the offline suite. These are not a phone export of the creator's actual video.

Initial verification failures were corrected or diagnosed: a stale prompt-visibility assertion was updated; invoking pytest through `python -m` avoids an existing executable-name assumption; the Kria gate needed access to localhost. No failed semantic replay is counted as a success.

## Rollout and remaining limits

- Pro changes cost/latency for every copilot request. Two attempts at 40 seconds bound schema/refusal retries; unknown provider outcomes remain terminal. Existing cost controls reserve up to the global 8,192-token estimate, which can exceed observed spend. No silent Flash fallback.
- `EDIT_COPILOT_MODEL` is independently reversible and loaded at process startup. Check production overrides and the effective model/prompt in API/worker traces after deployment.
- Actual private phone save/export remains unverified: the read-only admin projection omits the private device-render receipt required to reconstruct that save locally. Fixture compilation cannot substitute for that proof.
- The original title-loss mechanism, broad live generalization across the 252-prompt library, and all-format reliability remain unproven. This fixes demonstrated shared-path defects, not every possible request.
- Merge/deployment require fresh approval. After landing, verify deployed API/worker revision and model configuration, then exercise the affected behavior using the existing deployment workflow.
