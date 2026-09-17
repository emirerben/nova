"""`GenerativeVariantDecision` — the pure-decision output of the montage-family
generative renderer (KRI-114 P1-1).

`app.tasks.generative_build._render_generative_variant` used to do everything
in one function: pick shots (matcher/consolidate_slots), decide music/text/
transition/look parameters, THEN assemble/mix/burn/upload the actual video.
That function is now split into `_decide_generative_variant` (this module's
consumer — pure, no media processing) and `_process_generative_variant`
(assembly through upload). `GenerativeVariantDecision` is the boundary value:
everything `_process_generative_variant` needs to turn a decision into pixels,
without re-running clip analysis or the matcher.

Field mapping to the legacy persisted variant dict (`Job.assembly_plan
["variants"][i]`, built by `_render_generative_variant`'s `base` dict):

  - `variant_id`      <- `base["variant_id"]`
  - `rank`            <- `base["rank"]`
  - `text_mode`       <- `base["text_mode"]`
  - `resolved_archetype` <- `base["resolved_archetype"]`
  - `edit_format`     <- best-effort `spec.get("archetype")`; the montage-family
    renderer itself has no `edit_format` concept (day_vlog/single_hero are
    captured by `resolved_archetype` + `strict_day_vlog`/`strict_single_hero`
    in `extras`) — present for forward compatibility with callers that do
    (e.g. the phone-pilot dispatcher).
  - `music_track_id`  <- `base["music_track_id"]`
  - `music_start_s`   <- `base["music_start_s"]`
  - `music_window_video_duration_s` <- `base.get("music_window_video_duration_s")`
  - `mix`             <- `base["mix"]` (voice-prominence slider; voiceover only)
  - `orientation`     <- `base["orientation"]` (post masonry/landscape fallback)
  - `duration_s`      <- sum of `assembly_steps[*].target_duration_s` (the
    PLANNED total; the actually-rendered duration can differ slightly after
    `_assemble_clips` snaps to real clip boundaries — see `resolved_plans` in
    `_process_generative_variant`)
  - `assembly_steps`  <- the matcher's `AssemblyStep` list (`steps`), before the
    carousel-moment splice (which renders media and so is process-only)
  - `text_elements`   <- best-effort projection of the resolved agent-text intro
    params (`_at_params`/`agent_text`), NOT the authoritative render input
    (that's `extras["intro_overlay_params"]` + `extras["agent_text"]`, consumed
    unchanged by `_process_generative_variant`) and NOT validated against the
    stricter `app.agents._schemas.text_element.TextElement` (font/registry
    checks there could reject a value the legacy renderer already accepted).
    None when the variant has no agent-text intro to project (footage-only,
    lyrics, context-label-only).

`extras` carries everything else `_process_generative_variant` needs that
doesn't have a dedicated typed field yet — implementation details of the
montage-family renderer, not stable public contract. Documented keys:

  - `base`: dict — the full legacy `base` dict as it stood at the end of the
    decide phase (before any assembly/mix/burn/upload mutations). Read/write
    to it. This is what a failure return re-emits as ``{**base, "ok": False,
    ...}`` — it's already JSONB-persisted in production, so it's guaranteed
    JSON-safe.
  - `variant_t0`: float — `time.monotonic()` snapshot taken once, at the very
    start of decide-phase work, threaded through so `_process_generative_variant`
    (and the composed `_render_generative_variant`'s failure handler) can time
    the WHOLE variant (`_record_render_subphase(..., "variant_total", ...)`),
    matching the pre-split single-function timer.
  - `recipe`: dict — a minimal `{"beat_timestamps_s", "color_grade"}`
    projection of the `TemplateRecipe` (NOT `dataclasses.asdict`, so test
    doubles built from `SimpleNamespace` survive); rehydrated with
    `SimpleNamespace(**extras["recipe"])`. Those two attributes are the only
    ones `_process_generative_variant` reads during assembly.
  - `beats`: list[float] — section-relative beat grid (`beat_grid` for
    `_build_ai_timeline` + sequence/rhythm overlay timing). Empty for no-music
    variants.
  - `effective_music_window`: dict | None — `_effective_music_window()`'s
    result (`start_s`, `end_s`, `duration_s`, `validated`, `track_config`).
  - `voiceover_gcs_path`: str | None, `voiceover_local`: str | None,
    `voiceover_target_s`: float — voiceover mix inputs (`voiceover_local` is a
    path already downloaded into `variant_dir` by the decide phase — reading a
    proxy/original path is fine per the proxy-safety contract; only ENCODING
    from it is media processing).
  - `mix`: float — voice-prominence slider (mirrors `base["mix"]`, but always
    populated here even for non-voiceover variants, matching the original
    local variable's lifetime).
  - `track_config`: dict | None — resolved per-track config (music variants).
  - `masonry_requested`: bool, `resolved_montage_preset`: str,
    `effective_available_footage_s`: float, `assembly_landscape_fit`: str,
    `canvas`: dict {"width", "height"} — reconstruct with
    `Canvas(**extras["canvas"])`.
  - `lyrics_rendered`: bool — whether lyric overlays were injected into the
    recipe (gates the base-video upload branch for lyrics variants).
  - `intro_overlay_params`: dict | None — the resolved `_at_params` dict for
    the agent-text intro (spread into `build_persistent_intro_overlays` and
    read for `layout`/`text_color`); None when no agent-text intro applies.
  - `agent_text_intro_px`: int | None — resolved intro font size in px.
  - `single_hero_order`: list[str] | None — hero-then-supporting clip id order
    (single_hero archetype only; informational, not consumed by
    `_process_generative_variant`).

None of `extras`'s values require media processing to produce — they are
either passthroughs of caller-supplied data, or the deterministic output of
recipe/consolidation/matching math.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class GenerativeAssemblyStepDecision(BaseModel):
    """One planned cut — mirrors `app.pipeline.agents.gemini_analyzer.AssemblyStep`
    (`slot` + `clip_id` + `moment`) in a flatter, phone-compiler-friendly shape.

    `source_path` is a decision input snapshot (`clip_id_to_local[clip_id]` at
    decide time), not something `_process_generative_variant` re-derives from
    it — the actual assembly still uses the live `clip_id_to_local` mapping
    passed to `_process_generative_variant` directly, so a stale snapshot here
    can never cause a wrong render; it exists so an out-of-process compiler
    (the phone-pilot lane) can resolve clips without needing that mapping.
    """

    model_config = ConfigDict(extra="forbid")

    clip_id: str
    source_path: str | None = None
    in_s: float | None = None
    out_s: float | None = None
    target_duration_s: float | None = None
    rate: float | None = None
    transition_in: str | None = None
    transition_duration_s: float | None = None
    exact_window: bool = False
    slot_type: str | None = None
    # Passthrough for any `slot`/`moment` dict keys not modeled above (e.g.
    # `position`, `priority`, `energy`, `description`) — keeps the projection
    # lossless without hand-modeling every recipe-slot field.
    slot_extra: dict[str, Any] = Field(default_factory=dict)
    moment_extra: dict[str, Any] = Field(default_factory=dict)


class GenerativeVariantDecision(BaseModel):
    """Pure decision output of `_decide_generative_variant`. See module
    docstring for the full field mapping to the legacy persisted variant dict.
    """

    model_config = ConfigDict(extra="forbid")

    variant_id: str
    rank: int
    text_mode: str
    resolved_archetype: str | None = None
    edit_format: str | None = None
    music_track_id: str | None = None
    music_start_s: float | None = None
    music_window_video_duration_s: float | None = None
    mix: float | None = None
    orientation: str
    duration_s: float | None = None
    assembly_steps: list[GenerativeAssemblyStepDecision] = Field(default_factory=list)
    text_elements: list[dict[str, Any]] | None = None
    extras: dict[str, Any] = Field(default_factory=dict)

    @property
    def base(self) -> dict[str, Any]:
        """The legacy `base` dict accumulated during decide — see `extras["base"]`."""
        return self.extras["base"]

    @property
    def variant_t0(self) -> float:
        """The shared `time.monotonic()` origin for `variant_total` timing."""
        return self.extras["variant_t0"]
