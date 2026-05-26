"""Shared helpers for promoting song_sections rank-1 picks to canonical bounds.

The ``song_sections`` agent (Phase 2, library-side ranking) returns 1-3 ranked
windows per track in ``MusicTrack.best_sections``. These supersede the legacy
``auto_best_section()`` 45s peak-density window stored in
``MusicTrack.track_config.best_start_s/best_end_s``.

This module centralizes "rank-1 is canonical" so both
``analyze_music_track_task`` (write side, on every reanalyze) and the
auto-music / generative orchestrators (read side, defense-in-depth) use one
code path. Backfill scripts call the same helpers.

Schema constants live in ``app/agents/_schemas/song_sections.py``:
- ``CURRENT_SECTION_VERSION``: gate stale rows out of the read path.
- ``MAX_SECTION_DURATION_S``: clamp the end_s of any section to avoid drift.
"""

from __future__ import annotations

import math
from typing import Any

from app.agents._schemas.song_sections import (
    CURRENT_SECTION_VERSION,
    MAX_SECTION_DURATION_S,
)


def rank_one_bounds_from_sections(
    sections: list[Any] | None,
    section_version: str | None,
) -> tuple[float, float] | None:
    """Extract validated rank-1 (start_s, end_s) from raw sections + version.

    Plain-data variant used by ``analyze_music_track_task`` before the
    SQLAlchemy row has been reloaded post-write. Same gating + clamp as
    ``current_best_section_for_track`` below.

    Returns None when the version mismatches, the list is empty, or the
    rank-1 entry is malformed. Clamps end_s to ``start_s + MAX_SECTION_DURATION_S``
    so a drifted agent row can't extend a section past the schema bound.
    """
    if section_version != CURRENT_SECTION_VERSION:
        return None
    if not isinstance(sections, list) or not sections:
        return None
    first = sections[0]
    if isinstance(first, dict):
        start_raw = first.get("start_s")
        end_raw = first.get("end_s")
    else:
        start_raw = getattr(first, "start_s", None)
        end_raw = getattr(first, "end_s", None)
    try:
        start_s = float(start_raw)
        end_s = float(end_raw)
    except (TypeError, ValueError):
        return None
    if end_s <= start_s:
        return None
    end_s = min(end_s, start_s + MAX_SECTION_DURATION_S)
    return start_s, end_s


def current_best_section_for_track(track: Any) -> tuple[float, float] | None:
    """Row-based variant of ``rank_one_bounds_from_sections``."""
    return rank_one_bounds_from_sections(
        getattr(track, "best_sections", None),
        getattr(track, "section_version", None),
    )


def track_config_with_rank_one(track: Any) -> dict:
    """Return a copy of ``track.track_config`` with rank-1 bounds overlaid.

    Defense-in-depth for auto-music + generative paths. After
    ``analyze_music_track_task`` reconciles at write time this is a no-op
    (cfg already has section-1 bounds); kept so old rows that predate the
    backfill still render against rank-1.
    """
    cfg = dict(getattr(track, "track_config", None) or {})
    bounds = current_best_section_for_track(track)
    if bounds is None:
        return cfg
    start_s, end_s = bounds
    cfg["best_start_s"] = round(start_s, 3)
    cfg["best_end_s"] = round(end_s, 3)
    return cfg


def reconcile_track_config_to_rank_one(
    *,
    track_config: dict,
    beats: list[float],
    sections: list[Any] | None,
    section_version: str | None,
) -> tuple[dict, str]:
    """Promote rank-1 bounds into a copy of ``track_config``.

    Returns ``(new_track_config, best_section_source)``. ``best_section_source``
    is ``"song_sections"`` when promotion happened, ``"auto_best_section"`` when
    the existing bounds were kept (no valid rank-1, or the section window
    produces zero slots at the current ``slot_every_n_beats``).

    Also recomputes ``required_clips_min`` / ``required_clips_max`` for the
    new (shorter) window so ``POST /music-jobs`` validates against the
    correct count.

    Pure: does not mutate inputs, does not touch the database, does not
    regenerate ``recipe_cached`` (caller composes that via
    ``refresh_recipe_cached_for_bounds`` if needed).
    """
    bounds = rank_one_bounds_from_sections(sections, section_version)
    if bounds is None:
        return dict(track_config), "auto_best_section"

    sec_start, sec_end = bounds
    # `or 8` (not just default arg) handles legacy DB rows where the JSONB
    # field exists but holds None. `int(None)` raises; backfill rows can
    # pre-date a clean `analyze_music_track_task` run.
    n = int(track_config.get("slot_every_n_beats") or 8)
    window_beats = [b for b in beats if sec_start <= b <= sec_end]
    n_slots = len(range(0, max(0, len(window_beats) - n), n))
    if n_slots == 0:
        # Section window too narrow for current slot_every_n_beats. Keeping
        # the legacy 45s window beats overwriting to a recipe that would
        # raise ValueError("produced 0 slots") at every job. The caller
        # sees source="auto_best_section" and logs accordingly.
        return dict(track_config), "auto_best_section"

    new_config = dict(track_config)
    new_config["best_start_s"] = round(sec_start, 3)
    new_config["best_end_s"] = round(sec_end, 3)
    new_config["required_clips_min"] = max(1, math.floor(n_slots / 2))
    new_config["required_clips_max"] = max(1, n_slots)
    return new_config, "song_sections"


def refresh_recipe_cached_for_bounds(
    *,
    recipe_cached: dict,
    beats: list[float],
    track_config: dict,
    duration_s: float,
) -> dict:
    """Regenerate the beat recipe against new bounds and re-merge visuals.

    ``recipe_cached`` already holds a merged shape (beat + Gemini visual).
    Feeding it back as the ``gemini_recipe`` arg to ``merge_audio_recipe``
    pulls the visual layer (transitions, color_hint, text_overlays) onto
    the new beat timing — equivalent to redoing the original merge with
    the corrected window.

    Raises ``ValueError`` (from ``generate_music_recipe``) if the new
    window produces 0 slots. Other exceptions from the merge bubble up.
    Caller decides whether to swallow.
    """
    # Local import to keep this module light on cold-import cost.
    from app.pipeline.music_recipe import (  # noqa: PLC0415
        generate_music_recipe,
        merge_audio_recipe,
    )

    refreshed_beat = generate_music_recipe(
        {
            "beat_timestamps_s": beats,
            "track_config": track_config,
            "duration_s": duration_s,
        }
    )
    return merge_audio_recipe(refreshed_beat, recipe_cached)
