# Layer-2 text-overlay pipeline — internals

Reference doc for the multi-stage OCR pipeline. CLAUDE.md carries the cache-bump rule
and CI guard; this file carries the stage detail and divergence notes.

See also `docs/layer2-fdaf3bbc-diagnosis.md` for the original incident diagnosis.

## Overview

Multi-stage OCR pipeline replacing the single Gemini call when
`settings.text_overlay_v2_enabled=True` (default `False`) or admin
`reanalyze-agentic?use_layer2=true`.

**Stage flow:** A (frames) → B (OCR) → C (temporal group) → D (phrase reconstruction)
→ E (transcript alignment) → F (classification) → G (output)

Entry: `run_full_pipeline()` in `app/pipeline/text_overlay_v2/pipeline.py`; returns
`TemplateTextOutput` (Layer-1 schema). Routing in `TemplateTextAgent.run()`
(`_run_layer2()` when flag on).

## Stage E detail

`nova.compose.text_alignment` (`app/agents/text_alignment.py`) rewrites OCR phrases
verbatim from the Whisper transcript (drops hallucinated phrases). `_sanitize_aligned_line`
strips ASS tags, Unicode control/format codepoints, `\n`/`\N` literals, and debug
markers, but does NOT dedup adjacent tokens (refrains must survive). Stage D `_finalize`
and Stage G `sample_text` normalization also strip duplicate events + debug markers.

## Cache namespace

`template_cache.TEXT_OVERLAY_VERSION_V2` (dated string). Bumping it is the ONLY thing
that invalidates Layer-2 cache (force_layer2 builds only; `recipe-only` manual templates
use a separate namespace).

> **Trap:** Stage E/F agent `prompt_version`s are NOT in the cache key. Editing those
> prompts without bumping is invisible in prod. CI guard
> `.github/workflows/layer2-cache-guard.yml` enforces the bump. Escape hatch:
> `[skip-layer2-cache-bump]` in a commit message.

## OCR backend divergence (local ≠ prod)

Stage B picks its backend at runtime via `default_backend()` in
`app/services/text_overlay_ocr.py`:
- **Prod**: Cloud Vision (when `GOOGLE_SERVICE_ACCOUNT_JSON` /
  `GOOGLE_APPLICATION_CREDENTIALS` is set)
- **Local macOS**: Apple Vision (fallback)

They return different boxes/confidence/line reconstructions, so a result verified locally
on Apple Vision will NOT match the prod (Cloud Vision) render. Generate Layer-2 fixtures
against Cloud Vision, or pin via the `backend=` kwarg on `run_full_pipeline()`.

## Eval fixtures

Eval fixtures are a follow-up PR — until then run evals against Layer-1 (flag off).
See `tests/evals/README.md` for the full prompt-iteration loop.

## template_text agent

`nova.compose.template_text` — focused text-overlay extraction pass after
`template_recipe` in agentic templates only (NOT music jobs, NOT manual templates).
Replaces `recipe.slots[*].text_overlays` with overlays carrying a required normalized
bbox + font color.

Eval consumes optional OCR ground truth at
`tests/fixtures/agent_evals/template_text/ground_truth/<slug>.json` (build via
`scripts/build_text_ground_truth.py`); without it the judge inspects qualitatively per
rubric. Live-eval wrapper: `bash src/apps/api/scripts/run_template_text_eval.sh`.
