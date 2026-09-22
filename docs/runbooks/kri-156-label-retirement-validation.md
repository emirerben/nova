# KRI-156 label retirement validation

Date: 2026-09-22. Implementation integrates `origin/main` through `6a73bf1a8`, including KRI-133 semantic scheduling, KRI-158 clip identity checks, KRI-151 background preparation, KRI-154 deferred vision queries, KRI-157 cache preservation, and the independent clip-intent inventory.

## Implementation

- `CreativeStrategy` and the web mirror use generic `clip_intents`; the retired sport/context/participant/score fields, request regexes, and sport allowlist renderer are removed.
- Transcript labels declare `label_source=transcript` and a participant, score, or topic kind. They require the pinned guided voiceover contract and stay outside the visual resolver. The existing narration materializer still derives score text from transcript spans and checks participant focus.
- Visual labels use the existing clip evidence grounding. Mixed visual/transcript requests materialize both sources; forged resolved transcript assignments cannot reach the visual renderer.
- Historical execution receipts retain only a null `context_label_intent` key for byte-identical v1–v7 compiler hashes; old non-null inputs are discarded and cannot activate rendering.
- Stored strategy JSON maps at read time without rewriting approved bytes or hashes. Persisted rendered label snapshots still replay. New receipt identities include the semantic transcript request so changes such as sport to city regenerate labels; historical receipt reuse requires canonical legacy identities.
- An old unresolved visual strategy needs resolution through chat; the removed allowlist is not a render fallback.

## PR check repair and merge verification

The failed API shards were caused by a stale retired-field assertion in the edit-proposal test, a redelivery test double missing its narration field, and the Kria tool-schema snapshot still containing the removed fields. The same snapshot drift failed the portable iOS contract lane; the other two red checks were aggregate jobs.

The first complete GitHub rerun cleared the original failures. API shard 1 then hit a 60-second timeout in the unchanged, uncapped carousel focus render test (7,080 other tests passed). That integration test now has the same 180-second budget as its two neighboring uncapped focus tests, preserving the real FFmpeg/Skia path and assertions. The targeted render test passes locally in 6.46 seconds.

The merge preserves background preparation checkpoints and token fences, deferred vision query receipts, cached clip evidence, and multiple grounded labels per clip. The new inventory planner keeps transcript requests in the strategy while sending only visual requests to the visual resolver. Both creator entry points require the guided narration contract before accepting transcript labels. Guided timeline revisions preserve every grounded label and its provenance.

- Affected backend integration: **807 passed**, including the repaired tests, Kria snapshot replay, cache round trips, source routing, background query handling, and narration/visual materialization. Focused merge checks: **105 passed**; guided revision module: **118 passed**. Counts overlap.
- Portable iOS helper suite: **74 passed**. `python -m app.cli.kria_contracts --check` passed for the tools snapshot, mobile contract, and mobile OpenAPI.
- Scoped Ruff lint/format: **36 files passed**. Frontend `tsc --noEmit`, instruction-size guard, and whitespace checks passed.
- Full local API testing was attempted but interrupted by local disk exhaustion; the incomplete run is not counted as passing. GitHub CI is the complete-suite gate.

The newly merged `clip_intent_planner` prompt was bumped from `2026-09-22.2` to `2026-09-22.3` for transcript routing. Replay evals pass **8/8**. Live structural samples pass **6/8** for both versions: the candidate fixes `mixed_sources`, while `selection_preserves` fails both and `cooking` fails the candidate sample. Source and transcript-kind requirements are now checked explicitly. Judge scoring is unavailable because this newly introduced agent has no rubric in the repository; `--with-judge` fails closed instead of assigning scores. These results do not clear the existing draft release gates.

## Deterministic verification

Final pre-PR integration with KRI-151 passed **580 tests**, covering creator routes, background preparation, source-aware resolution, session bindings, transcript receipts, legacy decoding, guided compilation, and Main Creator replay evals. Background checkpoints, preparation token fences, and provider error states are preserved.

The pre-rebase creator/schema/route/session/render/eval regression suite passed **1,256 tests**. After integration with current main, the combined run passed 1,269 tests and exposed five failures: four newly merged test cases used the retired function signature, and one legacy compiler hash check detected a missing inert null key. Those issues were fixed. The final affected integration suite passed **206 tests**, with **5 live-only semantic scheduler evals skipped**. It covers guided compilation and revisions, legacy v1–v7 hash receipts, clip identity ambiguity, generative labels, semantic scheduling, and replay evals. Counts overlap and are not additive.

- `bash scripts/preship-check.sh`: passed scoped Ruff lint/format (29 Python files), frontend `tsc --noEmit`, no overlap with current main, and release-metadata ownership.
- `bash scripts/check_claude_md_size.sh`: passed.
- `git diff origin/main --check`: passed.
- Focused receipt and narration materializer checks: 26 passed, including changed attributes, disguised legacy IDs, malicious resolved payloads, old receipt replay, and score-span authority.

Local complete logs and live outputs are in `.dev/pr-integration-tests.log`, `.dev/rebased-tests.log`, `.dev/rebased-preship.log`, and `.dev/eval-results/` in the implementation worktree. These are local validation artifacts, not committed source assets.

## Live prompt comparison

Candidate `main_creator` prompt `2026-09-22-v33` was compared with baseline `2026-09-22-v31` from `df1d2a5c7`, using the same 19 current fixtures, current schema/read adapter, and updated source-aware rubric. Each fixture has one sample; judge scores are not a statistical non-regression guarantee. Model: Gemini 3.1 Pro Preview; judge threshold: 3.5/5.

- All 19 outputs in both samples passed the generic structural checks.
- Baseline: 16/19 judge passes; mean score 3.815/5.
- Candidate: 15/19 judge passes; mean score 3.991/5.
- New transcript-label fixture: baseline 4.67/5 (read-time mapping), candidate 5.00/5, correct explicit transcript source and all three kinds.
- Candidate below-threshold cases: `barcelona_shot_labels`, `phone_original_audio_17_clips`, `remove_uploaded_visuals`, and `video_reuse_followup`. The latter two also failed the baseline; the first two passed baseline and require review before merge.
- The existing KRI-129 caption fixture lacked `live_keywords`, causing `zip(strict=True)` to fail independently of correct model outputs in both runs. Added the missing food/park metadata and verified both captured live outputs against the corrected assertions without more provider calls.
- The baseline run reserved its bounded budget before its last two fixtures. Those two were completed under a separate $0.30 cap; no incomplete provider samples are counted above.

| Fixture | Baseline | Candidate |
| --- | ---: | ---: |
| `alternating_matches` | 4.50 | 4.00 |
| `barcelona_shot_labels` | 3.83 | 3.33 |
| `kri127_open_vocabulary_dish_labels` | 4.00 | 4.67 |
| `kri127_speaks_to_camera_selection` | 4.33 | 4.83 |
| `kri129_caption_food_and_weather` | 4.33 | 4.33 |
| `kri156_transcript_labels` | 4.67 | 5.00 |
| `long_montage_target_duration` | 3.50 | 4.17 |
| `madrid_pastel_title` | 3.83 | 4.17 |
| `montage_strategy` | 4.17 | 4.33 |
| `native_mixed_media_timing_repaired` | 3.83 | 4.17 |
| `phone_original_audio_17_clips` | 3.50 | 3.33 |
| `production_prompt_fah` | 4.33 | 4.50 |
| `remove_uploaded_visuals` | 2.50 | 2.50 |
| `semantic_text_english` | 4.17 | 3.67 |
| `semantic_text_spanish` | 4.17 | 4.17 |
| `semantic_text_turkish` | 4.17 | 3.83 |
| `sport_labels_bottom_right` | 3.83 | 4.00 |
| `video_reuse_default` | 2.33 | 4.00 |
| `video_reuse_followup` | 2.50 | 2.83 |

## Release gates still open

This implementation is submitted as a draft PR; it has not been merged or deployed. Production flags are unchanged. The live judge gate is not green; review the failing cases before merge. KRI-156 also requires reviewing a couple of weeks of KRI-127 flag-on production observation. This task does not establish that evidence.

The invoked autoship skill targets changeset/npm publication; the autoship CLI is not installed and this repository owns release metadata through post-merge automation. No npm release, version bump, or release workflow was run.
