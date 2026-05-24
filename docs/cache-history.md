# Layer-2 cache namespace history

This file preserves the narrative history of the manually-bumped
`TEXT_OVERLAY_VERSION_V2` constant — the bookkeeping pattern that this PR
(content-hash cache invalidation) replaces. Each entry below documents
one Stage-E/D/G change, the bug it caught, and the prod evidence that
forced the bump. Git blame on the prior `template_cache.py` is the
canonical source; this file is a curated narrative for context when
debugging cache-key shape questions in the future.

The new content-hashed scheme makes manual bumps unnecessary — any change
to the prompts, schemas, agent `prompt_version` fields, or relevant
settings keys produces a fresh hash automatically. See
`src/apps/api/app/pipeline/template_cache.py::compute_text_overlay_version`
for the contract.

## History (pre-content-hash)

### 2026-05-19: `v2` → `v2-2026-05-19`

v0.4.34.0 wired transcript_words into agentic builds, rewrote Stage E to
transcript-authoritative, fixed Stage D dedup + Stage G newline normalize.
Pre-fix recipes had garbage like "if you if you put put in" (prod job
`87b7292b`).

### 2026-05-19: `v2-2026-05-19` → `v2-2026-05-19-atomize`

v0.4.34.2. v0.4.34.1 shipped the atomize_mode prompt branching but the
cache constant wasn't re-bumped, so reanalyze cache-hit on the v0.4.34.0
namespace and served the multi-word-stuffed recipes from that run
unchanged.

### 2026-05-20: `v2-2026-05-19-atomize` → `v2-2026-05-20-xphrase-dedup`

Stage D `dedup_overlapping_atomized_phrases` collapses same-text
atomized phrases whose intervals overlap or sit within 0.5s of each
other. Caught after the v0.4.37.0 cache-fix reanalyze of Not Just Luck
(job `673d26d7-edbf-43a8-ac58-50dd604baae0`) produced 21 overlays with
"and" appearing 5×, "combination" 3×, "to" 3×, "the" 2× — all stacked
center-positioned because atomized mode skipped the within-cluster
dedup loop in `_finalize`.

### 2026-05-21: `v2-2026-05-20-xphrase-dedup` → `v2-2026-05-21-progressive-reveal`

v0.4.39.0 progressive word-by-word reveal — Stage G now groups
contiguous atomized phrases into LineGroups and emits cumulative reveal
overlays via `build_line_groups` + `_emit_cumulative_line_overlays`.
Overlay schema gains `text_anchor` + `pop_animated_suffix` fields which
the recipe persists, so cached recipes from before this bump would miss
the new fields and the renderer would default-position every reveal at
canvas center.

### 2026-05-22: `v2-2026-05-21-progressive-reveal` → `v2-2026-05-22-align-dedup-fallback`

Stage E (text_alignment) was itself CREATING duplicates: when the
transcript word count is smaller than the OCR phrase count, the LLM
maps multiple distinct OCR phrases to the same transcript word,
producing N copies of one word at overlapping timestamps that Stage D
dedup never saw because it ran on a clean input. Two fixes shipped
together: (A) `pipeline.run_full_pipeline` now re-runs
`dedup_overlapping_atomized_phrases` on the OUTPUT of Stage E (BEFORE
`build_line_groups` so the new progressive-reveal path sees a clean
phrase set); and (B) the Stage E prompt was rewritten to keep the OCR
phrase verbatim when no transcript word matches within ±0.5s, AND to
assign each transcript word to at most one OCR phrase per overlapping
time window — with a defense-in-depth post-parse pass that enforces both
even if the LLM ignores the prompt. Evidence: prod template `fdaf3bbc`
reanalyze at 05:23:44Z 2026-05-21 produced 20 overlays from 31 input
phrases ("allow" 3×, "anyone" 4×, "combination" 4×) — text_alignment
output_dict in the agent_run table made the LLM-side duplication plainly
visible.

### 2026-05-22: `v2-2026-05-22-align-dedup-fallback` → `v2-2026-05-22-reveal-cohesion`

Three related Layer-2 fixes ship together — all three change overlay
output for the same input, so the namespace bump must cover all of them
at once. (A) `extract_template_text_overlays` refuses to overwrite
template_recipe overlays when transcribe degrades (terminal_refusal,
low_confidence=True, or raised). Without it, Stage E's music-only
passthrough fires on speech videos with a failed transcript and raw OCR
artifacts reach the render. (B) Stage D drops single-character
non-whitelisted alphanumerics ("W", "M", "8"), pure-punctuation tokens,
and punctuation-dominant tokens BEFORE Stage E sees them.
(C) `build_line_groups` skips unmatched phrases mid-group instead of
closing the running group, so an OCR artifact between two matched
transcript words can't fragment the cumulative reveal — groups close
only on real terminators (sentence punctuation in the transcript,
silence gap, max-words cap). Evidence: prod template `89cde014`
reanalyze at 2026-05-22 09:13 had transcript=terminal_refusal and
rendered "luck\""/"W" to pixels in job `d5083a2c`; the 07:42 job before
it (good transcript) showed partial progressive reveal — "The work to
get" cumulative + "there" fragmented because an unmatched OCR closed
the group. After all three fixes the full source phrases reveal
cumulatively. Bumping orphans every Layer-2 cache entry built under the
broken behavior.

### 2026-05-22: `v2-2026-05-22-reveal-cohesion` → `v2-2026-05-22-uniform-style`

Stage-G overlays now ship with uniform styling — every overlay forced
to text_size="large" (120 px), text_anchor="left", and a hard 5%
left-edge anchor. Replaces the prior per-overlay size_class + role-based
sizing path (different sizes per text block, centered text clipping on
long phrases). The `_layer2_uniform` sentinel skips these overlays in
`agentic_template_build._classify_overlay` so the body config +
text_designer can't clobber the pinned fields. Evidence: prod template
`89cde014` test render with varying sizes + center-anchor clipping.
Bumping orphans every Layer-2 cache entry under the prior styling so
the next access reanalyzes through the uniform bridge.

### 2026-05-22: `v2-2026-05-22-uniform-style` → `v2-2026-05-22-atomized-single-word`

Two related Stage-E/G changes ship together — both change overlay output
for the same input. (A) Stage E (text_alignment) now reverts any
multi-word LLM output back to the OCR single word when
`atomize_mode=True`. The prompt already says "NEVER concatenate multiple
transcript words into a single output line" but the LLM violates it
(template `89cde014` reanalyze 18:19: single-word OCR `["luck"]`
returned as `["luck just is a"]`). Multi-word outputs killed downstream
`_is_atomized` so the phrases fell out of `build_line_groups`, emitting
as multi-word singleton overlays. Defense walks atomized outputs after
parse and drops corrected lines with whitespace — OCR fallback restores
the single word. (B) Stage G now suppresses ungrouped singleton overlays
that overlap a cumulative LineGroup in y + time. Without it, an
unmatched OCR phrase like "there" rendered on top of the "The work to
get" cumulative reveal (same y, overlapping time). Suppression keeps
the cumulative reveal as the canonical rendering for that band of
screen at that time. Bumping orphans every Layer-2 cache entry under
the prior alignment and orphan-singleton behavior.

### 2026-05-23: `v2-2026-05-22-atomized-single-word` → `v2-2026-05-23-cumulative-stack`

PR #286 follow-up. The `v2-2026-05-22` fix only verified the renderer
baseline anchor (PR #286 part C) locally; the cumulative-emit fixes
(parts A/B) never exercised. Prod template `89cde014` still rendered
split sub-groups stacked on top of each other at the same y, sub-frame
cumulative flashes, and trailing OCR quote artifacts. This bump orphans
the `v2-2026-05-22` cache entries written under the broken emit:
(A) `_emit_cumulative_line_overlays` Pass-1 now measures widths at the
uniform Layer-2 render size (large=120 px) so long lines actually split —
previously it measured at the classifier's `size_class` (often
"small"=36 px) and never overflowed, emitting one massive multi-line
overlay. (B) Pass-2 stacks split sub-groups vertically via the renderer's
intrinsic ascent+descent line step. Each sub-group's anchor_y now offsets
by `sub_group_idx * line_step_norm` so split lines render at distinct y.
(C) The cumulative emit floors stage duration at 0.2 s (vs the lyric
injector's 0.05 s) so a 50-ms middle stage drops out of the reveal
instead of flashing for one frame. (D) Stage E `_sanitize_aligned_line`
strips unmatched trailing quote / escape characters that OCR leaks on
phrase boundaries (the dangling `"` on `luck"` was rendering literally).

### 2026-05-23 (b): Stage E mis-mapped-duplicate defense

The `v2-2026-05-23` cumulative-stack fix shipped, but the next prod
reanalyze of `89cde014` still rendered jumbled sub-groups ("and
combination" at y=0.51, "good don't allow" stacked). Root cause was
UPSTREAM of the cumulative emit: Stage E's Gemini call violated its
uniqueness rule, mapping OCR "timing" → "combination" and OCR "don't" →
"and" (both duplicates of earlier phrases) 1s+ apart, so the existing
time-gap dedup (0.5 s) missed them. The wrong words entered the
cumulative LineGroup and the emit faithfully rendered nonsense. The 3b
uniqueness defense now also reverts a duplicate to OCR when the LLM
output disagrees with the phrase's own OCR word, regardless of time gap.

### 2026-05-23 (c): Cumulative reveal de-clustering

Same prod reanalyze still revealed words in 2-4 word pops, not one-by-one
("It's not" jumped straight to "It's not just luck"). Root cause: OCR
first-seen timestamps cluster (coarse frame sampling stamps every word in
a held frame at the same t — `89cde014` had "just"/"luck"/"is"/"a" all at
t=6.50, "your"/"hard"/"work." all at t=10.00). `build_cumulative_stages`
then dropped the sub-renderable intermediate stages.
`_emit_cumulative_line_overlays` now de-spaces each sub-group's word
reveal times (min 0.30 s apart) before building stages, so every word
lands its own reveal beat. No prod deploy happened between (b) and (c),
so both shipped under one namespace string
(`v2-2026-05-23c-declustered-reveal`) on main — these are the final
manual-bump entries before this PR replaces the constant with a hash.

### 2026-05-23: content-hash (this PR)

Manual bumps end here. `TEXT_OVERLAY_VERSION_V2` is now derived from
content. Any future change to a Layer-2 prompt, schema, agent
`prompt_version`, or relevant settings key produces a fresh `v2-<hash>`
string automatically. The lone known gap: edits to agent `.py` source
that don't bump `prompt_version` won't invalidate. Mitigation lives in
`TODOS.md` under "Cache invariants".
