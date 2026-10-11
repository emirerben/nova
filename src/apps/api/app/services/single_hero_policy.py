"""Shared single-hero composition allocation for planning and rendering."""

import math

# Single-hero is intentionally a small, strict composition: one owned hero
# clip plus at most three supporting cutaways.  The hero must occupy at least
# 60% of the planned timeline; the explicit duration cap is the same product
# ceiling used by the rest of the generative renderer.
_SINGLE_HERO_MIN_CLIPS = 2
_SINGLE_HERO_MAX_SUPPORTING_CLIPS = 3
_SINGLE_HERO_MIN_HERO_RATIO = 0.60
_SINGLE_HERO_MIN_HERO_DURATION_S = 3.0


class SingleHeroPolicyError(RuntimeError):
    """A single-hero contract failure that must never downgrade to montage."""

    def __init__(self, reason: str, message: str):
        self.reason = reason
        super().__init__(message)


def _single_hero_order(
    clip_metas: list,
    clip_id_to_local: dict[str, str],
    clip_durations_s: dict[str, float] | None = None,
) -> list[str]:
    """Return a deterministic hero plus only positively-probed support clips.

    Production callers pass the ingest probe map.  A missing, non-finite, or
    non-positive duration is not usable footage for a strict single-hero
    composition and is filtered before ranking, so the policy fails with the
    stable ``insufficient_media`` reason instead of producing a zero-length
    support slot.
    """

    durations = clip_durations_s
    available = [
        meta
        for meta in clip_metas
        if (
            (clip_id := str(getattr(meta, "clip_id", "") or "")) in clip_id_to_local
            and (
                durations is None
                or (
                    isinstance(durations.get(clip_id), (int, float))
                    and math.isfinite(float(durations[clip_id]))
                    and float(durations[clip_id]) > 0.0
                )
            )
        )
    ]
    ranked = sorted(
        enumerate(available),
        key=lambda pair: (-float(getattr(pair[1], "hook_score", 0.0) or 0.0), pair[0]),
    )
    return [
        str(meta.clip_id)
        for _, meta in ranked[: 1 + _SINGLE_HERO_MAX_SUPPORTING_CLIPS]
        if str(getattr(meta, "clip_id", "") or "")
    ]


def _build_single_hero_recipe(
    order: list[str],
    *,
    available_footage_s: float,
    clip_durations_s: dict[str, float] | None = None,
    max_duration_s: float,
) -> dict:
    """Build the strict one-hero/short-cutaway slot policy.

    The hero is capped by the source duration and the overall duration is
    reduced when necessary to preserve the 60% dominance invariant.  This
    makes the policy explicit and prevents ``allow_slowdown_fill=False`` from
    accidentally turning a short hero into a supporting clip at render time.
    """

    if len(order) < _SINGLE_HERO_MIN_CLIPS:
        raise SingleHeroPolicyError(
            "insufficient_media",
            "single_hero requires one usable hero clip and at least one cutaway.",
        )
    durations = clip_durations_s or {}
    hero_id = order[0]
    hero_source_s = float(durations.get(hero_id, 0.0) or 0.0)
    if hero_source_s < _SINGLE_HERO_MIN_HERO_DURATION_S:
        raise SingleHeroPolicyError(
            "insufficient_media",
            "single_hero hero clip is too short for the minimum dominant duration.",
        )
    support_ids = order[1 : 1 + _SINGLE_HERO_MAX_SUPPORTING_CLIPS]
    support_source_s = sum(max(0.0, float(durations.get(cid, 0.0) or 0.0)) for cid in support_ids)
    if support_source_s <= 0:
        raise SingleHeroPolicyError(
            "insufficient_media",
            "single_hero requires at least one usable supporting cutaway.",
        )
    target_total = min(float(available_footage_s), float(max_duration_s))
    # A hero cannot dominate beyond its source; shrink the overall target to
    # preserve hero dominance rather than stretching or silently downgrading.
    target_total = min(target_total, hero_source_s / _SINGLE_HERO_MIN_HERO_RATIO)
    if target_total <= 0:
        raise SingleHeroPolicyError("insufficient_media", "single_hero has no usable duration.")
    hero_duration = min(hero_source_s, max(_SINGLE_HERO_MIN_HERO_DURATION_S, target_total * 0.70))
    support_total = target_total - hero_duration
    if support_total <= 0 or hero_duration / target_total < _SINGLE_HERO_MIN_HERO_RATIO:
        raise SingleHeroPolicyError(
            "duration_out_of_bounds",
            "single_hero could not preserve hero dominance.",
        )
    per_support = support_total / len(support_ids)
    slots = []
    for index, clip_id in enumerate(order[: 1 + _SINGLE_HERO_MAX_SUPPORTING_CLIPS]):
        duration = hero_duration if index == 0 else per_support
        slots.append(
            {
                "position": index + 1,
                "target_duration_s": round(duration, 3),
                "slot_type": "hero" if index == 0 else "cutaway",
                "energy": 7.0 if index == 0 else 5.0,
                "priority": 10 if index == 0 else 4,
                "text_overlays": [],
                "transition_in": "cut",
                "transition_duration_s": 0.0,
                "speed_factor": 1.0,
            }
        )
    return {
        "shot_count": len(slots),
        "total_duration_s": round(sum(slot["target_duration_s"] for slot in slots), 3),
        "hook_duration_s": round(hero_duration, 3),
        "slots": slots,
        "beat_timestamps_s": [],
        "sync_style": "freeform",
        "pacing_style": "medium",
        "color_grade": "none",
        "copy_tone": "creator-led",
        "caption_style": "none",
        "transition_style": "cut",
        "creative_direction": "single hero with supporting cutaways",
        "interstitials": [],
        "required_clips_min": len(slots),
        "required_clips_max": len(slots),
    }
