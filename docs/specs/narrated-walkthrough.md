# Narrated Walkthrough — Slice 1: Backend Core

**Architect:** Claude Opus 4.8  
**Date:** 2026-06-19  
**Branch:** feat/narrated-walkthrough-2026-06-19  
**Status:** FROZEN — do not edit after Codex Pass 1 starts

---

## What to build

A new `"narrated"` archetype in the generative pipeline that:

1. **Accepts a per-step script** from `PlanItem.filming_guide` (each shot's `what` field = the spoken line for that step).  
2. **Force-aligns the script to a recorded voiceover** using the existing `lyrics_alignment` module → emits `[{step_id, start_s, end_s, confidence}]` per step.  
3. **Trims one clip per step** to its aligned duration and concatenates them in step order using the existing `single_pass` machinery.  
4. **Lays the voiceover over the whole sequence**, footage fully muted, using the existing `_mix_user_voiceover`.

### Files to create or extend

| File | Action | What |
|------|--------|------|
| `app/pipeline/narrated_alignment.py` | **CREATE** | Pure function `align_script_to_voiceover(script_steps, voiceover_path) → list[StepTiming]`. Downloads VO, calls `_transcribe_openai` (word timestamps), then `align()` from `lyrics_alignment`. Falls back to even-split when confidence is low. No network mock in unit tests — the function accepts pre-computed `whisper_words` for tests. |
| `app/pipeline/narrated_assembler.py` | **CREATE** | `assemble_narrated(step_timings, clip_assignments, voiceover_path, output_path)`. Trims each step's clip to `[start_s, end_s]` via `single_pass._compute_output_durations` / `_build_xfade_chain`, concatenates, then mixes in the voiceover via `_mix_user_voiceover(mix=1.0)`. |
| `agents/_schemas/edit_format.py` | **EXTEND** | Add `"narrated"` to `EditFormat` literal. |
| `tasks/generative_build.py` | **EXTEND** | In `_resolve_archetype`: detect `narrated` when `item.edit_format == "narrated"` AND `item.voiceover_gcs_path` is set AND `item.filming_guide` has ≥2 shots. In `_specs_for_archetype`: return a single-variant spec for `narrated`. Add `_render_narrated_variant` that orchestrates `narrated_alignment.align_script_to_voiceover` + `narrated_assembler.assemble_narrated`. |
| `app/core/config.py` (or env) | **EXTEND** | Add `NARRATED_ARCHETYPE_ENABLED` bool, default `False`. Gate the entire `narrated` dispatch path on this flag (mirror `TALKING_HEAD_ENABLED` pattern). |

### What NOT to touch

- Do NOT modify `app/pipeline/lyrics_alignment.py` — reuse `align()` as-is.
- Do NOT modify `app/pipeline/transcribe.py` — reuse `_transcribe_openai` as-is.
- Do NOT modify `app/tasks/template_orchestrate.py` — `_mix_user_voiceover` is imported, not changed.
- Do NOT modify any Alembic migration files.
- Do NOT modify the public API contract (`routes/plan_items.py` endpoint signatures).
- Do NOT touch any frontend file (`src/apps/web/`).
- Do NOT read `~/.claude/`, `.claude/skills/`, or `agents/`.
- Do NOT modify `agents/openai.yaml`.

---

## Hard acceptance criteria (frozen — verify before marking done)

1. `EditFormat` in `agents/_schemas/edit_format.py` includes `"narrated"` as a literal value.
2. `_resolve_archetype(item)` in `tasks/generative_build.py` returns `"narrated"` when `item.edit_format == "narrated"` and `item.voiceover_gcs_path` is non-null and `len(item.filming_guide) >= 2`.
3. `_resolve_archetype(item)` returns any non-narrated archetype when `NARRATED_ARCHETYPE_ENABLED=false` (kill switch gate is present).
4. `align_script_to_voiceover(script_steps, whisper_words)` is a **pure function** (accepts pre-computed `whisper_words: list[Word]`) that returns `list[StepTiming]` where each `StepTiming` has `step_id`, `start_s`, `end_s`, `confidence`. No network calls in the function; it is unit-testable offline.
5. When all steps align with high confidence: `sum(t.end_s - t.start_s for t in timings) ≈ total_voiceover_duration` (within 0.1s tolerance in tests).
6. When a step's confidence is below threshold (simulate with a deliberately mismatched word list): that step's timing falls back to an even split and `confidence` is below 0.5.
7. `pytest src/apps/api/tests/pipeline/test_narrated_alignment.py` and `pytest src/apps/api/tests/pipeline/test_narrated_assembler.py` both pass with **no skips**, covering criteria 4–6 plus an integration smoke-test of `assemble_narrated` using a real FFmpeg subprocess (can use a tiny generated sine-wave clip + silence as the VO fixture).
8. `pytest src/apps/api/tests/tasks/test_task_time_limits.py` still passes (adding `_render_narrated_variant` must not violate the time-limit invariant).
9. `cd src/apps/api && ruff check . && ruff format --check .` exits 0.

---

## Reality checks (verify these against the actual codebase before writing a line)

1. **`lyrics_alignment.align()` signature** — confirm exact parameters and return type in `app/pipeline/lyrics_alignment.py`. The spec assumes `align(canonical_lines: list[str], whisper_words: list[Word]) → list[LineAlignment]`; verify.
2. **`_transcribe_openai` is importable** from outside `pipeline/transcribe.py` — or find the public wrapper used by callers.
3. **`_mix_user_voiceover` location and signature** — the reuse map says `tasks/template_orchestrate.py:5648`; confirm the exact import path and params.
4. **`TALKING_HEAD_ENABLED` config pattern** — find how it's declared in `app/core/config.py` (or wherever) and mirror that exact pattern for `NARRATED_ARCHETYPE_ENABLED`.
5. **`_resolve_archetype` / `_specs_for_archetype` hook points** — confirm lines 2220 and 2401 in `tasks/generative_build.py` are still the right places (the line numbers are from exploration; the branch may have diverged since main).
6. **Celery task time-limit invariant** — read `tests/tasks/test_task_time_limits.py` before adding the render function so the new task doesn't violate the invariant.
7. **`filming_guide` shot structure** — confirm `PlanItem.filming_guide` shape from `models.py:640`: it is `list[{what, how, duration_s, clip_count, shot_id}]`. `what` is the spoken line; `shot_id` is the join key to `clip_assignments`.
