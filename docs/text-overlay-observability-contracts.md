# Text Overlay Observability Contracts

Frozen for Slice 1 on 2026-06-12. These contracts define the trace events and
`overlay_verify` report fields used to make text-overlay degradation observable.

## Pipeline Trace Events

All events use `record_pipeline_event("overlay", event_name, payload)`. Emission
must be best-effort and must not raise when no `pipeline_trace_for(job_id)`
context is active.

### `intro_layout_selected`

Emitted once per persistent generative intro build.

Payload:

```json
{
  "requested_layout": "cluster",
  "selected_layout": "cluster",
  "reason": "agent_pick",
  "text": "hook text",
  "word_count": 4,
  "has_word_roles": true,
  "fallback": false
}
```

Fields:
- `requested_layout`: layout requested by the caller before renderer fallback.
- `selected_layout`: effective rendered layout, `cluster` or `linear`.
- `reason`: one of `agent_pick`, `explicit_linear`, `disabled`, `position_pinned`,
  `empty_text`, `cluster_declined`, or `cluster_error_fallback`.
- `text`: intro text after caller selection, before rendering.
- `word_count`: count from `text.split()`.
- `has_word_roles`: whether caller supplied word-role annotations.
- `fallback`: `true` when a requested cluster rendered as linear.

### `cluster_roles_derived`

Emitted from the cluster layout engine after the effective per-word roles are
known and before block grouping.

Payload:

```json
{
  "words": ["what's", "your", "favorite", "place?"],
  "input_roles": ["connector", "hero", "hero", "closer"],
  "effective_roles": ["connector", "hero", "hero", "closer"],
  "role_source": "agent",
  "guarantees": {
    "invalid_roles_rederived": false,
    "closer_final_enforced": false,
    "hero_present_enforced": false,
    "signal_free_contrast_enforced": false,
    "all_hero_demoted_to_closer": false
  }
}
```

Fields:
- `words`: token list from `text.split()`.
- `input_roles`: caller-supplied roles, or `null`.
- `effective_roles`: roles used by the cluster engine.
- `role_source`: `agent` when valid caller roles were used, otherwise
  `heuristic`.
- `guarantees`: booleans for the contrast/validity guarantees that fired.

### `cluster_shrink_applied`

Emitted only when cluster-atomic shrink reduces scale below `1.0`.

Payload:

```json
{
  "text": "wandering around old istanbul",
  "base_size_px": 80,
  "scale": 0.8464,
  "min_scale": 0.55,
  "widest_block_frac": 0.82,
  "usable_width_frac": 0.88,
  "block_count": 3
}
```

Fields:
- `text`: cluster text.
- `base_size_px`: input base size before role scaling.
- `scale`: final atomic scale applied to every role.
- `min_scale`: readability floor below which the cluster declines.
- `widest_block_frac`: widest measured block after shrink.
- `usable_width_frac`: frame width available after edge margins.
- `block_count`: number of emitted cluster blocks.

### `font_resolved`

Emitted once per rendered Skia overlay sequence. Use `level: "warning"` when
`fallback` is `true`; otherwise use `level: "info"`.

Payload:

```json
{
  "overlay_index": 0,
  "text": "hook text",
  "effect": "fade-in",
  "requested_font_family": "Missing Font",
  "requested_font_style": "display",
  "resolved_typeface": {
    "name": "Playfair Display",
    "file": "PlayfairDisplay-Bold.ttf",
    "source": "style_default"
  },
  "fallback": true,
  "level": "warning"
}
```

Fields:
- `overlay_index`: index in the Skia burn overlay list.
- `text`: rendered overlay text.
- `effect`: overlay effect string.
- `requested_font_family`: overlay `font_family`, or `null`.
- `requested_font_style`: overlay `font_style` after defaulting, or `null`.
- `resolved_typeface`: the actual registry typeface selected.
- `fallback`: `true` when a requested `font_family` did not resolve to itself.
- `level`: `warning` for fallback, `info` otherwise.

## `overlay_verify` Report Field

Every `report.json.overlays[]` entry must include `resolved_typeface`:

```json
{
  "requested_font_family": "Missing Font",
  "requested_font_style": "display",
  "name": "Playfair Display",
  "file": "PlayfairDisplay-Bold.ttf",
  "source": "style_default",
  "fallback": true
}
```

`overlay_verify` must fail an overlay when `requested_font_family` is non-empty
and `fallback` is `true`.
