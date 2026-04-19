"""Generate a TemplateRecipe-compatible structure from a MusicTrack's beat timestamps.

The recipe is a plain dict (not a SQLAlchemy model) that can be passed directly to
template_matcher.match() and _assemble_clips() — the same code path used by template jobs.

Key concepts:
  - slot_every_n_beats: a clip cut happens every N beats (default 8 ≈ 2 bars at 120 BPM)
  - best_start_s / best_end_s: the window of the track to use (auto-detected or admin-set)
  - _auto_best_section: finds the highest beat-density 45s window (proxy for chorus/drop)
"""

import copy
import math
from bisect import bisect_left, bisect_right

DEFAULT_WINDOW_S = 45.0
DEFAULT_SLOT_EVERY_N_BEATS = 8


def generate_music_recipe(track_data: dict) -> dict:
    """Build a recipe dict from a MusicTrack's stored config and beat timestamps.

    Args:
        track_data: dict with keys matching MusicTrack columns:
            - beat_timestamps_s: list[float]
            - track_config: dict with best_start_s, best_end_s, slot_every_n_beats, etc.
            - duration_s: float | None

    Returns:
        A recipe dict shaped like TemplateRecipe (JSON-serialisable).

    Raises:
        ValueError: if the beat window produces 0 slots (track too short / config mismatch).
    """
    beats: list[float] = track_data.get("beat_timestamps_s") or []
    cfg: dict = track_data.get("track_config") or {}
    duration_s: float = float(track_data.get("duration_s") or 0.0)

    start_s: float = float(cfg.get("best_start_s", 0.0))
    end_s: float = float(
        cfg.get("best_end_s", min(DEFAULT_WINDOW_S, duration_s or DEFAULT_WINDOW_S))
    )
    n: int = int(cfg.get("slot_every_n_beats", DEFAULT_SLOT_EVERY_N_BEATS))

    # Beats inside the configured window (inclusive on both ends)
    window_beats = sorted(b for b in beats if start_s <= b <= end_s)

    # Generate slots: every N beats → one clip cut
    slots = []
    for i in range(0, len(window_beats) - n, n):
        slot_start_s = window_beats[i] - start_s
        slot_end_s = window_beats[i + n] - start_s
        duration = slot_end_s - slot_start_s
        if duration <= 0:
            continue
        slots.append({
            "position": len(slots) + 1,
            "target_duration_s": round(duration, 3),
            "slot_type": "broll",
            "energy": 5.0,      # overridden by _enrich_slots_with_energy in orchestrator
            "priority": 5,
            "text_overlays": [],
            "transition_in": "cut",
            "speed_factor": 1.0,
        })

    if not slots:
        raise ValueError(
            f"Music recipe produced 0 slots: {len(window_beats)} beats in window "
            f"[{start_s:.1f}s–{end_s:.1f}s] with slot_every_n_beats={n}. "
            "Increase the window or decrease slot_every_n_beats."
        )

    # Adjust required_clips from config or derive
    n_slots = len(slots)
    req_min = int(cfg.get("required_clips_min", max(1, math.floor(n_slots / 2))))
    req_max = int(cfg.get("required_clips_max", n_slots))

    recipe = {
        "shot_count": n_slots,
        "total_duration_s": round(end_s - start_s, 3),
        "hook_duration_s": slots[0]["target_duration_s"] if slots else 0.0,
        "slots": slots,
        "beat_timestamps_s": [round(b - start_s, 3) for b in window_beats],
        "sync_style": "cut-on-beat",
        "pacing_style": "fast",
        "color_grade": "none",
        "transition_style": "cut",
        "copy_tone": "energetic",
        "caption_style": "none",
        "creative_direction": "beat-sync music video",
        "interstitials": [],
        "required_clips_min": req_min,
        "required_clips_max": req_max,
    }
    return recipe


def merge_audio_recipe(beat_recipe: dict, gemini_recipe: dict) -> dict:
    """Merge beat-based timing with Gemini visual properties.

    Beat detection (FFmpeg) gives exact cut points. Gemini gives visual style
    (transitions, color, overlays). This function combines both using
    proportional mapping — the same algorithm as merge_template_with_track.

    Args:
        beat_recipe: Recipe from generate_music_recipe() with exact beat timing.
        gemini_recipe: Recipe from analyze_audio_template() with visual properties.

    Returns:
        A merged recipe with beat timing + Gemini visuals.
    """
    beat_slots = beat_recipe["slots"]
    gemini_slots = gemini_recipe.get("slots", [])

    if not gemini_slots:
        # No visual data from Gemini — return beat recipe as-is
        return beat_recipe

    n_beat = len(beat_slots)
    n_gemini = len(gemini_slots)

    for i, b_slot in enumerate(beat_slots):
        # Proportional index into Gemini slots
        g_idx = min(math.floor(i * n_gemini / n_beat), n_gemini - 1)
        g_slot = gemini_slots[g_idx]

        # Copy visual properties from Gemini slot
        for key in ("transition_in", "color_hint", "speed_factor", "slot_type"):
            if key in g_slot:
                b_slot[key] = g_slot[key]

        # Copy text overlays (scale timing proportionally)
        g_overlays = g_slot.get("text_overlays", [])
        if g_overlays:
            g_duration = g_slot.get("target_duration_s", 1.0)
            b_duration = b_slot["target_duration_s"]
            scaled = []
            for ov in g_overlays:
                s = copy.deepcopy(ov)
                if g_duration > 0:
                    start_frac = ov.get("start_s", 0.0) / g_duration
                    end_frac = ov.get("end_s", g_duration) / g_duration
                    s["start_s"] = round(start_frac * b_duration, 3)
                    s["end_s"] = round(min(end_frac * b_duration, b_duration), 3)
                    if s["end_s"] <= s["start_s"]:
                        s["end_s"] = round(min(s["start_s"] + 0.1, b_duration), 3)
                scaled.append(s)
            b_slot["text_overlays"] = scaled

    # Copy top-level visual fields from Gemini
    for key in (
        "copy_tone", "caption_style", "creative_direction",
        "color_grade", "transition_style", "pacing_style",
        "subject_niche",
    ):
        if key in gemini_recipe:
            beat_recipe[key] = gemini_recipe[key]

    # Remap interstitials proportionally
    gemini_interstitials = gemini_recipe.get("interstitials", [])
    if gemini_interstitials and n_gemini > 0:
        mapped = []
        for inter in gemini_interstitials:
            old_after = inter.get("after_slot", 1)
            new_after = max(1, min(round(old_after * n_beat / n_gemini), n_beat))
            m = dict(inter)
            m["after_slot"] = new_after
            mapped.append(m)
        beat_recipe["interstitials"] = mapped

    beat_recipe["slots"] = beat_slots
    return beat_recipe


def merge_template_with_track(parent_recipe: dict, track_data: dict) -> dict:
    """Merge a parent template's visual recipe with a music track's beat-based timing.

    Algorithm:
      1. Generate beat-based slots from track_data via generate_music_recipe().
      2. For each music slot at position P, find the parent slot at
         floor(P * len(parent_slots) / len(music_slots)) — proportional mapping.
      3. Copy visual properties (text_overlays, transition_in, color_hint, etc.)
         from the mapped parent slot, scaling overlay timing proportionally.
      4. Carry over top-level recipe fields from parent.
      5. Override beat/sync/pacing from the music recipe.

    Args:
        parent_recipe: The parent template's recipe_cached dict.
        track_data: dict with beat_timestamps_s, track_config, duration_s
                    (same shape as generate_music_recipe expects).

    Returns:
        A merged recipe dict ready for recipe_cached.

    Raises:
        ValueError: If the music recipe produces 0 slots.
    """
    music_recipe = generate_music_recipe(track_data)
    music_slots = music_recipe["slots"]
    parent_slots = parent_recipe.get("slots", [])

    if not parent_slots:
        # No visual data to merge — return music recipe as-is
        return music_recipe

    n_parent = len(parent_slots)
    n_music = len(music_slots)

    for i, m_slot in enumerate(music_slots):
        # Proportional index into parent slots
        p_idx = min(math.floor(i * n_parent / n_music), n_parent - 1)
        p_slot = parent_slots[p_idx]

        # Copy visual properties from parent slot
        for key in ("transition_in", "color_hint", "slot_type", "speed_factor"):
            if key in p_slot:
                m_slot[key] = p_slot[key]

        # Copy and scale text overlays
        p_overlays = p_slot.get("text_overlays", [])
        if p_overlays:
            p_duration = p_slot.get("target_duration_s", 1.0)
            m_duration = m_slot["target_duration_s"]
            scaled_overlays = []
            for ov in p_overlays:
                scaled = copy.deepcopy(ov)
                if p_duration > 0:
                    start_frac = ov.get("start_s", 0.0) / p_duration
                    end_frac = ov.get("end_s", p_duration) / p_duration
                    scaled["start_s"] = round(start_frac * m_duration, 3)
                    scaled["end_s"] = round(min(end_frac * m_duration, m_duration), 3)
                    # Ensure end > start
                    if scaled["end_s"] <= scaled["start_s"]:
                        scaled["end_s"] = round(
                            min(scaled["start_s"] + 0.1, m_duration), 3
                        )
                scaled_overlays.append(scaled)
            m_slot["text_overlays"] = scaled_overlays

    # Remap interstitials proportionally
    parent_interstitials = parent_recipe.get("interstitials", [])
    if parent_interstitials and n_parent > 0:
        mapped_interstitials = []
        for inter in parent_interstitials:
            old_after = inter.get("after_slot", 1)
            # Proportional mapping: parent slot index → music slot index
            new_after = max(
                1, min(round(old_after * n_music / n_parent), n_music)
            )
            mapped = dict(inter)
            mapped["after_slot"] = new_after
            mapped_interstitials.append(mapped)
        music_recipe["interstitials"] = mapped_interstitials

    # Carry over top-level visual fields from parent
    for key in (
        "copy_tone", "caption_style", "creative_direction",
        "color_grade", "transition_style",
    ):
        if key in parent_recipe:
            music_recipe[key] = parent_recipe[key]

    # Music recipe overrides stay (beat_timestamps_s, sync_style, pacing_style)
    music_recipe["slots"] = music_slots

    return music_recipe


def auto_best_section(
    beat_timestamps_s: list[float],
    window_s: float = DEFAULT_WINDOW_S,
    track_duration_s: float = 0.0,
) -> tuple[float, float]:
    """Find the window of *window_s* seconds with the highest beat density.

    Beat density is used as a proxy for the chorus/drop — the most energetic
    part of the song. Uses a sliding-window sweep over detected beat positions,
    so it only considers windows that start *at* a beat (not every 1s).

    This avoids librosa as a dependency.

    Args:
        beat_timestamps_s: sorted or unsorted list of beat timestamps in seconds.
        window_s: desired window length in seconds.
        track_duration_s: full track duration (used as fallback end cap).

    Returns:
        (best_start_s, best_end_s) — the best window boundaries.
    """
    if not beat_timestamps_s:
        end = min(window_s, track_duration_s) if track_duration_s > 0 else window_s
        return 0.0, end

    candidates = sorted(set(beat_timestamps_s))
    best_start: float = candidates[0]
    best_count: int = 0

    for start in candidates:
        end = start + window_s
        count = bisect_right(candidates, end) - bisect_left(candidates, start)
        if count > best_count:
            best_count = count
            best_start = start

    best_end = best_start + window_s

    # Cap to track duration if known
    if track_duration_s > 0 and best_end > track_duration_s:
        best_end = track_duration_s
        # If capping makes the window shorter than desired, backtrack start
        best_start = max(0.0, best_end - window_s)

    return best_start, best_end
