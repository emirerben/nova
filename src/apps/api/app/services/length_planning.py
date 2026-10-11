"""Server-authored duration-plan envelopes shared by proposal and montage paths."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from app.pipeline.best_parts import BestPart, BestPartsResult, select_best_parts
from app.schemas.edit_proposal import MixedMediaTimingProfile, mixed_media_hold_bounds
from app.services.length_policy import length_owner, natural_length_owner

LENGTH_PLAN_VERSION = 1
# A still has no measured source duration.  This is deliberately kept here,
# rather than imported from ``unified_montage``, so the shared proposal plan
# does not depend on the renderer (which reads this envelope).
_STILL_CAPACITY_S = 3.0
_STILL_PREFERRED_WINDOW_S = 1.2


def _number(value: object) -> float | None:
    return (
        float(value)
        if not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value >= 0
        else None
    )


def _sources(
    snapshot: Mapping[str, Any], selected_ids: Sequence[str], strategy: Mapping[str, Any]
) -> list[dict[str, Any]]:
    selected = set(selected_ids)
    rows = snapshot.get("clip_assignments")
    if not isinstance(rows, list):
        return []
    sources = []
    raw_profile = strategy.get("mixed_media_timing")
    profile = (
        MixedMediaTimingProfile.model_validate(raw_profile) if raw_profile is not None else None
    )
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("media_id"), str):
            continue
        if selected and row["media_id"] not in selected:
            continue
        kind = row.get("kind")
        kind = kind if kind in {"image", "video"} else "video"
        duration_s = _number(row.get("duration_s"))
        analysis = row.get("analysis")
        analysis = dict(analysis) if isinstance(analysis, Mapping) else {}
        if kind == "image":
            # Photos are rendered as synthetic holds.  Seed the selector with
            # the normal 1.2s montage hold, while retaining 3s of capacity for
            # a requested or minimum-duration allocation.
            bounds = mixed_media_hold_bounds("image", profile) if profile is not None else None
            duration_s = bounds.maximum_s if bounds else _STILL_CAPACITY_S
            preferred_s = bounds.preferred_s if bounds else _STILL_PREFERRED_WINDOW_S
            analysis["best_moments"] = [{"start_s": 0.0, "end_s": preferred_s, "energy": 0.0}]
        sources.append(
            {
                "media_id": row["media_id"],
                # ``generation`` is the proposal identity; storage_generation
                # maps it to the immutable object selected at runtime.
                "generation": row.get("generation"),
                "storage_generation": row.get("storage_generation", row.get("generation")),
                "gcs_path": row.get("gcs_path", row.get("proxy_path")),
                "kind": kind,
                "duration_s": duration_s or 0.0,
                "minimum_duration_s": bounds.minimum_s
                if kind == "image" and profile is not None
                else 0.1,
                "analysis": analysis,
            }
        )
    return sources


def build_length_plan(
    strategy: Mapping[str, Any],
    media_snapshot: Mapping[str, Any],
    *,
    creator_target_s: float | None = None,
    selected_ids: Sequence[str] = (),
    order_ids: Sequence[str] = (),
    licensed_music: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the approved, serializable source-window plan.

    Only ``creator_target_s`` supplied by a trusted boundary becomes a hard
    target. Strategy fields are intentionally never treated as proof here.
    """

    natural_owner = natural_length_owner(strategy)
    owner = length_owner(strategy, creator_duration_proven=creator_target_s is not None)
    sources = _sources(media_snapshot, selected_ids, strategy)
    by_id = {source["media_id"]: source for source in sources}
    if len(by_id) != len(sources):
        raise ValueError("duplicate length plan source")
    ordered_sources = [
        by_id[media_id] for media_id in dict.fromkeys(order_ids) if media_id in by_id
    ]
    ordered_sources.extend(source for source in sources if source not in ordered_sources)
    result: BestPartsResult | None = None
    audio = strategy.get("audio_strategy")
    section = strategy.get("selected_music_section")
    section_duration = None
    if isinstance(section, Mapping):
        start, end = _number(section.get("start_s")), _number(section.get("end_s"))
        if start is not None and end is not None and end > start:
            section_duration = end - start
    voice_duration = _number(strategy.get("voiceover_duration_s"))
    eligible = natural_owner in {"footage", "voiceover"} or (
        natural_owner == "song" and audio == "licensed_music" and section_duration is not None
    )
    natural_duration_s = (
        voice_duration
        if natural_owner == "voiceover"
        else section_duration
        if natural_owner == "song"
        else None
    )
    # A proven creator target changes the owner label, not the source-window
    # allocator: footage still supplies the exact realizable windows.
    if eligible:
        budget = creator_target_s if creator_target_s is not None else natural_duration_s
        coverage_floor = round(sum(source["minimum_duration_s"] for source in ordered_sources), 3)
        if budget is not None and budget < coverage_floor <= budget * 1.1:
            budget = coverage_floor
        result = select_best_parts(
            ordered_sources,
            target_duration_s=budget,
            minimum_total_duration_s=3 if budget is None else None,
            minimum_duration_s={
                source["media_id"]: source["minimum_duration_s"] for source in ordered_sources
            },
        )
    windows = result.receipt() if result is not None else []
    conflicts = (
        [
            {
                "code": conflict.code,
                "requested_duration_s": conflict.requested_duration_s,
                "available_duration_s": conflict.available_duration_s,
                "message": conflict.message,
            }
            for conflict in result.conflicts
        ]
        if result is not None
        else []
    )
    # The selector deliberately omits unusable rows from its allocation.  The
    # plan cannot do that: every selected source is an approved input and a
    # missing/too-short metadata row must remain a visible constraint even when
    # another clip can supply the requested duration.
    if any(source["duration_s"] < 0.1 for source in ordered_sources) and not any(
        conflict["code"] == "source_below_minimum" for conflict in conflicts
    ):
        conflicts.append(
            {
                "code": "source_below_minimum",
                "requested_duration_s": 0.1,
                "available_duration_s": result.capacity_s if result is not None else 0.0,
                "message": "Every selected source needs at least a 0.1 second usable window.",
            }
        )
    missing_ids = set(selected_ids) - set(by_id)
    if eligible and len(sources) > 80:
        conflicts.append(
            {
                "code": "source_count_limit",
                "requested_duration_s": creator_target_s,
                "available_duration_s": 120.0,
                "message": "The portable recipe can include at most 80 sources.",
            }
        )
    if missing_ids:
        conflicts.append(
            {
                "code": "no_usable_sources",
                "requested_duration_s": creator_target_s,
                "available_duration_s": 0.0,
                "message": "Some selected clips no longer have usable source metadata.",
            }
        )
    plan: dict[str, Any] = {
        "version": LENGTH_PLAN_VERSION,
        "owner": owner,
        "natural_owner": natural_owner,
        "target_duration_s": creator_target_s,
        "natural_duration_s": natural_duration_s,
        "capacity_raw_s": min(
            120.0, round(sum(source["duration_s"] for source in ordered_sources), 3)
        ),
        "window_eligible": eligible,
        "capacity_s": result.capacity_s if result is not None else None,
        "sources": [
            {
                key: source[key]
                for key in (
                    "media_id",
                    "generation",
                    "storage_generation",
                    "gcs_path",
                    "kind",
                    "duration_s",
                )
            }
            for source in ordered_sources
        ],
        "windows": windows,
        "conflicts": conflicts,
    }
    if licensed_music is not None:
        plan["licensed_music"] = dict(licensed_music)
    elif isinstance(strategy.get("selected_music_section"), Mapping):
        plan["licensed_music"] = dict(strategy["selected_music_section"])
    elif isinstance(strategy.get("licensed_music_selection"), Mapping):
        plan["licensed_music"] = dict(strategy["licensed_music_selection"])
    return plan


def read_length_plan(
    envelope: Mapping[str, Any] | None, clips: Sequence[Any]
) -> tuple[BestPart, ...] | None:
    """Validate an approved envelope against current media identities.

    ``None`` signals that legacy callers may select their historic windows;
    malformed approved plans are rejected rather than silently reselected.
    """

    if envelope is None:
        return None
    if type(envelope.get("version")) is not int or envelope.get("version") != LENGTH_PLAN_VERSION:
        raise ValueError("unsupported length plan version")
    sources = envelope.get("sources")
    windows = envelope.get("windows")
    if not isinstance(sources, list) or not isinstance(windows, list):
        raise ValueError("invalid length plan envelope")
    if envelope.get("conflicts"):
        raise ValueError("approved length plan still needs a constraint answer")
    actual = {str(getattr(clip, "media_id", "")): clip for clip in clips}
    expected: dict[str, Mapping[str, Any]] = {}
    for source in sources:
        if not isinstance(source, Mapping) or not isinstance(source.get("media_id"), str):
            raise ValueError("invalid length plan source")
        if source["media_id"] in expected:
            raise ValueError("duplicate length plan source")
        clip = actual.get(source["media_id"])
        source_generation = source.get("storage_generation", source.get("generation"))
        clip_generation = getattr(clip, "storage_generation", getattr(clip, "generation", None))
        source_path = source.get("gcs_path")
        clip_path = getattr(clip, "proxy_path", None)
        if (
            clip is None
            or str(source_generation) != str(clip_generation)
            or (source_path is not None and source_path != clip_path)
        ):
            raise ValueError("length plan source identity changed")
        kind = source.get("kind", "video")
        if kind not in {"image", "video"} or kind != getattr(clip, "kind", "video"):
            raise ValueError("length plan source kind changed")
        expected_duration = _number(source.get("duration_s"))
        actual_duration = _number(getattr(clip, "duration_s", None))
        # A still's recorded capacity is an allocation rule, not media
        # metadata.  Its upload duration is commonly None or zero and must not
        # invalidate the approved synthetic hold.
        if expected_duration is None or (
            kind != "image"
            and (actual_duration is None or abs(expected_duration - actual_duration) > 0.001)
        ):
            raise ValueError("length plan source duration changed")
        expected[source["media_id"]] = source
    parts: list[BestPart] = []
    included: set[str] = set()
    for window in windows:
        if not isinstance(window, Mapping) or window.get("media_id") not in expected:
            raise ValueError("invalid length plan window")
        start = _number(window.get("source_start_s"))
        end = _number(window.get("source_end_s"))
        score = _number(window.get("score", 0))
        clip = actual[window["media_id"]]
        if window["media_id"] in included:
            raise ValueError("duplicate length plan window")
        source_duration = expected[window["media_id"]]["duration_s"]
        if (
            start is None
            or end is None
            or score is None
            or end - start < 0.1 - 1e-9
            or end > float(source_duration) + 0.001
        ):
            raise ValueError("length plan window exceeds source")
        included.add(window["media_id"])
        parts.append(
            BestPart(
                window["media_id"],
                start,
                end,
                score,
                str(window.get("reason") or "approved_length_plan"),
            )
        )
    if envelope.get("window_eligible") is True and included != set(expected):
        raise ValueError("length plan window coverage changed")
    return tuple(parts)
