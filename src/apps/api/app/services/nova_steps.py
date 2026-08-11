"""Owner-safe projection of pipeline internals into a Nova-voiced step feed.

The pipeline already records rich per-step data via ``pipeline_trace``
(``app.services.pipeline_trace``), ``phase_log`` (``app.services.job_phases``)
and ``AgentRun`` rows, but none of it is safe to show a user verbatim --
those sinks intentionally carry admin-only content (overlay text, prompts,
signed URLs, raw LLM I/O). This module is the ONE place that decides which
(stage, event) pairs are safe to surface, how to phrase them in Nova's
voice, and what (if any) numeric/enum detail may ride along.

Two independent defenses keep user/internal content out of the feed:
  1. ``STEP_ALLOWLIST`` -- only a curated set of (stage, event) pairs is
     considered at all. Everything else (including anything carrying
     overlay/caption/hook TEXT, signed URLs, or GCS paths) is dropped.
  2. ``_sanitize_event_data`` -- mirrors the key-substring blocklist in
     ``pipeline_trace._safe_shallow_dict`` (text|prompt|url|path|note) and is
     applied to EVERY allowlisted event's data before any field can reach a
     label or detail line. This is a second line of defense in case a future
     call site reuses an allowlisted (stage, event) pair with a differently
     shaped payload.

``AgentRun`` rows are read for milestones only: ``agent_name``, ``outcome``,
``latency_ms``, ``created_at``, ``id``. ``input_json``, ``output_json``,
``raw_text`` and ``error_message`` are never read by this module -- callers
are encouraged to ``defer()`` those columns at the query level for an extra
guarantee (see ``app.routes.generative_jobs``).

Dark behind ``settings.nova_steps_feed_enabled`` (see PR1 of the "Nova AI
tool-chip activity feed" train) -- this module has no effect until a caller
wires it into a response.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

from app.agents._runtime import SUCCESS_OUTCOMES
from app.models import AgentRun, Job
from app.services.job_status import PLAN_ITEM_JOB_TERMINAL

NovaStepKind = Literal["render", "decision", "agent", "phase"]
NovaStepStatus = Literal["done", "active", "failed"]


class NovaStep(BaseModel):
    """One user-facing row in the Nova activity feed.

    ``id`` is derived from ``stage:event:index`` (or ``agent:<name>:<run
    id>`` for agent milestones) -- NEVER random -- so it stays stable across
    polls of the same, append-only ``pipeline_trace``/``phase_log``/
    ``AgentRun`` sources.
    """

    id: str
    ts: datetime
    kind: NovaStepKind
    label: str
    detail: list[str] | None = None
    status: NovaStepStatus


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------

# Sentinel meaning "every event under this stage is allowed". Used only for
# `render_stage`, whose `event` field is the dynamic FFmpeg-pipeline
# sub-stage name (see `record_render_stage` in pipeline_trace.py) rather
# than a fixed vocabulary -- every payload already passes through
# `_sanitize_render_payload`'s `_SAFE_RENDER_KEYS` allowlist at write time,
# so per-name curation here would be redundant, not safer.
_WILDCARD: frozenset[str] = frozenset({"*"})

# Curated from the real event vocabulary in app/tasks/generative_build.py
# (~77 record_pipeline_event call sites) plus app/services/pipeline_trace.py.
# Start conservative: phase-level milestones a user would recognize as "Nova
# did something", never anything carrying overlay/caption/hook TEXT (e.g.
# overlay.render_window, overlay.agent_text_done are deliberately excluded).
# Adding a stage/event here is a conscious, reviewed decision -- see
# tests/test_nova_steps.py::test_allowlist_pin.
STEP_ALLOWLIST: dict[str, frozenset[str]] = {
    "assembly": frozenset(
        {
            "clip_metadata_done",
            "song_match_done",
            "archetype_selected",
            "archetype_fallback",
            "narrative_order_applied",
        }
    ),
    "reframe": frozenset({"hdr_pretonemap_done"}),
    "overlay": frozenset({"style_set_selected"}),
    "silence_cut": frozenset({"silence_cut_plan"}),
    "smart_captions": frozenset({"plan_compiled"}),
    "media_overlay": frozenset({"cards_applied"}),
    "sound_effects": frozenset({"effects_applied"}),
    "audio_mix": frozenset({"voiceover_mixed"}),
    "render_stage": _WILDCARD,
    "custom_effect": frozenset({"burn_start", "burn_done"}),
    # Only the reapply-failure event -- other "render"-stage events
    # (e.g. fast_reburn_base_probe_failed/fast_reburn_base_canvas_mismatch in
    # generative_build.py) carry base_path/orientation and stay excluded.
    "render": frozenset({"custom_effect_reapply_failed"}),
}


def _event_allowed(stage: str, event: str) -> bool:
    events = STEP_ALLOWLIST.get(stage)
    if events is None:
        return False
    return events is _WILDCARD or event in events


# ---------------------------------------------------------------------------
# Sanitizer (second defense) -- mirrors pipeline_trace._safe_shallow_dict
# ---------------------------------------------------------------------------

_BLOCKED_KEY_SUBSTRINGS: tuple[str, ...] = ("text", "prompt", "url", "path", "note")


def _sanitize_event_data(data: Any) -> dict[str, Any]:
    """Strip any key that might carry user text/URLs/paths, plus any value
    that isn't a JSON scalar. Applied to every allowlisted event's ``data``
    before a single field is allowed to reach a label or detail line.
    """
    if not isinstance(data, dict):
        return {}
    safe: dict[str, Any] = {}
    for key, raw in data.items():
        if not isinstance(key, str):
            continue
        if any(blocked in key.lower() for blocked in _BLOCKED_KEY_SUBSTRINGS):
            continue
        if isinstance(raw, (str, int, float, bool)) or raw is None:
            safe[key] = raw
    return safe


# ---------------------------------------------------------------------------
# Per-event humanizers -- Nova-voiced labels + safe detail lines
# ---------------------------------------------------------------------------

_Humanizer = Callable[[dict[str, Any]], tuple[str, "list[str] | None"]]


def _fmt_seconds(value: Any) -> str | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return f"{float(value):.1f}s"


_ARCHETYPE_LABELS: dict[str, str] = {
    "talking_head": "talking-head",
    "subtitled": "subtitled caption",
    "voiceover": "voiceover",
    "narrated": "narrated",
    "montage": "montage",
}

_FALLBACK_REASON_LABELS: dict[str, str] = {
    "no_speech": "no speech detected",
    "flag_disabled": "feature not enabled yet",
    "spine_extraction_failed": "couldn't find a clear speaker",
    "archetype_bias_no_speech": "no speech detected",
}


def _humanize_clip_metadata_done(data: dict[str, Any]) -> tuple[str, list[str] | None]:
    detail: list[str] = []
    clips = data.get("clips")
    if isinstance(clips, int) and not isinstance(clips, bool):
        detail.append(f"{clips} clip{'s' if clips != 1 else ''} analyzed")
    footage = _fmt_seconds(data.get("available_footage_s"))
    if footage:
        detail.append(f"{footage} of footage")
    return "Nova analyzed your clips", detail or None


def _humanize_song_match_done(data: dict[str, Any]) -> tuple[str, list[str] | None]:
    if data.get("track_id"):
        return "Nova matched a song to your footage", None
    return "Nova looked for a song match", None


def _humanize_archetype_selected(data: dict[str, Any]) -> tuple[str, list[str] | None]:
    archetype = data.get("archetype")
    friendly = _ARCHETYPE_LABELS.get(str(archetype), "custom")
    detail: list[str] | None = None
    coverage = data.get("speech_coverage")
    if isinstance(coverage, (int, float)) and not isinstance(coverage, bool):
        detail = [f"{round(coverage * 100)}% speech coverage"]
    return f"Nova chose the {friendly} edit style", detail


def _humanize_archetype_fallback(data: dict[str, Any]) -> tuple[str, list[str] | None]:
    reason = str(data.get("reason") or "")
    friendly = _FALLBACK_REASON_LABELS.get(reason) or (reason.replace("_", " ") or None)
    return "Nova adjusted the edit style", [friendly] if friendly else None


def _humanize_narrative_order_applied(data: dict[str, Any]) -> tuple[str, list[str] | None]:
    shots = data.get("shot_count")
    detail = (
        [f"{shots} shot{'s' if shots != 1 else ''}"]
        if isinstance(shots, int) and not isinstance(shots, bool)
        else None
    )
    return "Nova ordered your clips to match the shot list", detail


def _humanize_hdr_pretonemap_done(data: dict[str, Any]) -> tuple[str, list[str] | None]:
    n = data.get("clips_converted")
    detail = (
        [f"{n} clip{'s' if n != 1 else ''} balanced"]
        if isinstance(n, int) and not isinstance(n, bool) and n > 0
        else None
    )
    return "Nova balanced HDR footage", detail


def _humanize_style_set_selected(_data: dict[str, Any]) -> tuple[str, list[str] | None]:
    return "Nova picked a visual style", None


def _humanize_silence_cut_plan(data: dict[str, Any]) -> tuple[str, list[str] | None]:
    if not data.get("applied"):
        return "Nova reviewed pauses in your footage", None
    detail: list[str] = []
    removed = data.get("removed_count")
    if isinstance(removed, int) and not isinstance(removed, bool):
        detail.append(f"{removed} cut{'s' if removed != 1 else ''}")
    saved = _fmt_seconds(data.get("time_saved_s"))
    if saved:
        detail.append(f"{saved} saved")
    return "Nova trimmed silences and filler words", detail or None


def _humanize_plan_compiled(data: dict[str, Any]) -> tuple[str, list[str] | None]:
    detail: list[str] = []
    for key, noun in (
        ("styled_captions", "styled captions"),
        ("titles", "titles"),
        ("sfx_intents", "sound cues"),
        ("visuals", "on-screen visuals"),
    ):
        n = data.get(key)
        if isinstance(n, int) and not isinstance(n, bool) and n > 0:
            detail.append(f"{n} {noun}")
    return "Nova built your caption plan", detail or None


def _humanize_cards_applied(data: dict[str, Any]) -> tuple[str, list[str] | None]:
    count = data.get("card_count")
    if isinstance(count, int) and not isinstance(count, bool) and count > 0:
        return "Nova placed on-screen cards", [f"{count} card{'s' if count != 1 else ''}"]
    return "Nova checked for on-screen cards", None


def _humanize_effects_applied(data: dict[str, Any]) -> tuple[str, list[str] | None]:
    count = data.get("effect_count")
    if isinstance(count, int) and not isinstance(count, bool) and count > 0:
        return "Nova added sound effects", [f"{count} effect{'s' if count != 1 else ''}"]
    return "Nova checked for sound effect opportunities", None


def _humanize_voiceover_mixed(_data: dict[str, Any]) -> tuple[str, list[str] | None]:
    return "Nova mixed your voiceover into the audio", None


def _humanize_custom_effect_burn_start(data: dict[str, Any]) -> tuple[str, list[str] | None]:
    count = data.get("filters")
    detail = (
        [f"{count} filter{'s' if count != 1 else ''}"]
        if isinstance(count, int) and not isinstance(count, bool) and count > 0
        else None
    )
    return "Applying your custom look", detail


def _humanize_custom_effect_burn_done(data: dict[str, Any]) -> tuple[str, list[str] | None]:
    count = data.get("filters")
    detail = (
        [f"{count} filter{'s' if count != 1 else ''}"]
        if isinstance(count, int) and not isinstance(count, bool) and count > 0
        else None
    )
    return "Custom look applied", detail


def _humanize_custom_effect_reapply_failed(
    _data: dict[str, Any],
) -> tuple[str, list[str] | None]:
    # Deliberately no detail line -- `reason`/`stage` are machine-readable
    # validator codes, not durations/counts, and adding nothing is safer
    # than risking a confusing internal code leaking to the user.
    return "Couldn't re-apply your custom look — kept the video without it", None


_HUMANIZERS: dict[tuple[str, str], _Humanizer] = {
    ("assembly", "clip_metadata_done"): _humanize_clip_metadata_done,
    ("assembly", "song_match_done"): _humanize_song_match_done,
    ("assembly", "archetype_selected"): _humanize_archetype_selected,
    ("assembly", "archetype_fallback"): _humanize_archetype_fallback,
    ("assembly", "narrative_order_applied"): _humanize_narrative_order_applied,
    ("reframe", "hdr_pretonemap_done"): _humanize_hdr_pretonemap_done,
    ("overlay", "style_set_selected"): _humanize_style_set_selected,
    ("silence_cut", "silence_cut_plan"): _humanize_silence_cut_plan,
    ("smart_captions", "plan_compiled"): _humanize_plan_compiled,
    ("media_overlay", "cards_applied"): _humanize_cards_applied,
    ("sound_effects", "effects_applied"): _humanize_effects_applied,
    ("audio_mix", "voiceover_mixed"): _humanize_voiceover_mixed,
    ("custom_effect", "burn_start"): _humanize_custom_effect_burn_start,
    ("custom_effect", "burn_done"): _humanize_custom_effect_burn_done,
    ("render", "custom_effect_reapply_failed"): _humanize_custom_effect_reapply_failed,
}

# render_stage's `event` is the dynamic sub-stage name passed to
# `record_render_stage`/`render_stage_timer` (see app/tasks/generative_build.py).
# Known names get a specific label; anything new falls back to a generic,
# still-safe humanization of the (already-sanitized, enum-like) name itself.
_RENDER_STAGE_LABELS: dict[str, str] = {
    "asset_persist_durable_sources": "Nova saved your source clips",
    "asset_loading_and_preprocess": "Nova prepared your clips",
    "preprocessing_hdr_tonemap": "Nova balanced HDR footage",
    "ai_text_and_style": "Nova wrote the on-screen text",
    "audio_match": "Nova matched a song to your footage",
    "variant_render": "Nova rendered your video",
    "finalize": "Nova finalized your video",
    "asset_probe": "Nova checked your footage",
    "asset_context_loading": "Nova loaded your edit",
    "silence_cut_analysis": "Nova analyzed pauses in your footage",
    "caption_correction": "Nova corrected your captions",
    "caption_preparation": "Nova prepared your captions",
    "smart_caption_compile": "Nova compiled your captions",
    "transcription": "Nova transcribed your audio",
    "caption_effect_preparation": "Nova prepared caption effects",
    "composition": "Nova composed your captions",
    "caption_burn": "Nova burned in your captions",
    "upload": "Nova uploaded your video",
    "audio_render": "Nova rendered the audio",
}


def _humanize_render_stage(sub_stage: str, data: dict[str, Any]) -> tuple[str, list[str] | None]:
    label = _RENDER_STAGE_LABELS.get(sub_stage) or f"Nova ran {sub_stage.replace('_', ' ')}"
    elapsed_ms = data.get("elapsed_ms")
    detail = None
    if isinstance(elapsed_ms, (int, float)) and not isinstance(elapsed_ms, bool):
        detail = [f"{elapsed_ms / 1000:.1f}s"]
    return label, detail


def _humanize(stage: str, event: str, data: dict[str, Any]) -> tuple[str, list[str] | None]:
    if stage == "render_stage":
        return _humanize_render_stage(event, data)
    fn = _HUMANIZERS.get((stage, event))
    if fn is None:
        # Allowlisted but no humanizer registered — should never happen (every
        # STEP_ALLOWLIST entry has a paired humanizer, pinned by
        # test_allowlist_pin), but fail safe rather than leak the raw
        # stage/event identifiers to the user.
        return "Nova completed a pipeline step", None
    return fn(data)


_AGENT_LABELS: dict[str, str] = {
    "clip_metadata": "Nova analyzed your clips",
    "creative_direction": "Nova planned the creative direction",
    "template_recipe": "Nova built the edit recipe",
    "song_classifier": "Nova classified the song",
    "music_matcher": "Nova matched a song",
    "intro_writer": "Nova wrote your hook text",
    "template_text": "Nova wrote your on-screen text",
    "platform_copy": "Nova wrote your caption copy",
    "audio_template": "Nova planned the audio",
    "transcript": "Nova transcribed your audio",
}


def _humanize_agent_run(agent_name: str, latency_ms: int | None) -> tuple[str, list[str] | None]:
    friendly = _AGENT_LABELS.get(agent_name) or f"Nova ran {agent_name.replace('_', ' ')}"
    detail = None
    if isinstance(latency_ms, int) and not isinstance(latency_ms, bool) and latency_ms > 0:
        detail = [f"{latency_ms / 1000:.1f}s"]
    return friendly, detail


_PHASE_LABELS: dict[str, str] = {
    "queued": "Nova queued your job",
    "download_clips": "Nova downloaded your clips",
    "analyze_clips": "Nova analyzed your clips",
    "match_clips": "Nova matched clips to the plan",
    "assemble": "Nova assembled your edit",
    "mix_audio": "Nova mixed the audio",
    "generate_copy": "Nova wrote your caption copy",
    "upload": "Nova uploaded your video",
    "finalize": "Nova finalized your video",
}


def _humanize_phase(name: str) -> str:
    return _PHASE_LABELS.get(name) or f"Nova completed {name.replace('_', ' ')}"


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------

# Read-side cap: the frontend never needs more than a screenful of history:
# the useful signal is "what's happening now" plus recent context.
_READ_CAP = 40


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def project_nova_steps(job: Job, agent_runs: Sequence[AgentRun] | None = None) -> list[NovaStep]:
    """Merge allowlisted ``pipeline_trace`` + ``phase_log`` + ``AgentRun``
    rows into a Nova-voiced, owner-safe step feed, sorted by time.

    Status rule: every step starts as ``"done"`` unless it carries its own
    failure signal (a ``render_stage`` payload with ``status == "failed"``,
    a ``pipeline_trace`` event whose name ends in ``"_failed"`` (e.g.
    ``custom_effect_reapply_failed`` -- a fail-open event that still
    represents a failure the user should see), or an ``AgentRun.outcome``
    outside ``SUCCESS_OUTCOMES``) in which case it's ``"failed"``. If the
    job itself is non-terminal
    (``job.status not in PLAN_ITEM_JOB_TERMINAL``), the chronologically
    LATEST step is promoted to ``"active"`` -- unless that step already
    failed on its own, in which case it stays ``"failed"``.

    Only reads ``AgentRun.agent_name``/``outcome``/``latency_ms``/
    ``created_at``/``id`` off each row in ``agent_runs`` -- never
    ``input_json``/``output_json``/``raw_text``/``error_message``. Callers
    should additionally ``defer()`` those columns at the query level (see
    ``app.routes.generative_jobs``) so they're never even fetched.
    """
    built: list[tuple[datetime, NovaStep, bool]] = []  # (ts, step, own_failed)

    trace = job.pipeline_trace or []
    for index, raw_event in enumerate(trace):
        if not isinstance(raw_event, dict):
            continue
        stage = raw_event.get("stage")
        event = raw_event.get("event")
        if not isinstance(stage, str) or not isinstance(event, str):
            continue
        if not _event_allowed(stage, event):
            continue
        ts = _parse_ts(raw_event.get("ts"))
        if ts is None:
            continue
        data = _sanitize_event_data(raw_event.get("data"))
        label, detail = _humanize(stage, event, data)
        own_failed = data.get("status") == "failed" or event.endswith("_failed")
        step = NovaStep(
            id=f"{stage}:{event}:{index}",
            ts=ts,
            kind="render" if stage in ("render_stage", "custom_effect", "render") else "decision",
            label=label,
            detail=detail,
            status="failed" if own_failed else "done",
        )
        built.append((ts, step, own_failed))

    phase_log = job.phase_log or []
    for index, raw_phase in enumerate(phase_log):
        if not isinstance(raw_phase, dict):
            continue
        name = raw_phase.get("name")
        if not isinstance(name, str):
            continue
        ts = _parse_ts(raw_phase.get("ts"))
        if ts is None:
            continue
        step = NovaStep(
            id=f"phase:{name}:{index}",
            ts=ts,
            kind="phase",
            label=_humanize_phase(name),
            detail=None,
            status="done",
        )
        built.append((ts, step, False))

    for run in agent_runs or []:
        agent_name = run.agent_name
        ts = run.created_at
        if not isinstance(agent_name, str) or not isinstance(ts, datetime):
            continue
        label, detail = _humanize_agent_run(agent_name, run.latency_ms)
        own_failed = run.outcome not in SUCCESS_OUTCOMES
        step = NovaStep(
            id=f"agent:{agent_name}:{run.id}",
            ts=ts,
            kind="agent",
            label=label,
            detail=detail,
            status="failed" if own_failed else "done",
        )
        built.append((ts, step, own_failed))

    built.sort(key=lambda item: item[0])

    if built and job.status not in PLAN_ITEM_JOB_TERMINAL:
        ts, last_step, own_failed = built[-1]
        if not own_failed:
            built[-1] = (ts, last_step.model_copy(update={"status": "active"}), own_failed)

    steps = [step for _, step, _ in built]
    if len(steps) > _READ_CAP:
        steps = steps[-_READ_CAP:]
    return steps
