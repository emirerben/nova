"""Read-only clip timeline for phone-rendered Voiceover videos (KRI-281).

A phone `narrated` (recorded voiceover over a script) or `voiceover` (montage
with a recorded voice) variant is compiled on the server and rendered on the
device, so the cloud never produced an `ai_timeline` / `narrated_timings` for
it. The native editor then had no clips to put on its timeline (just the
"Kria outro"), and the preview refused to build for lack of a video track.

The pinned device recipe is the one source of truth for the cut, so both
shapes are PROJECTED from it, the same way
`app.services.phone_subtitled_editor.project_phone_subtitled_editor_sections`
projects lanes: existing videos work with no backfill, and a newly rendered
one writes the identical rows at render time
(`app.tasks.generative_build._run_phone_narrated_job`).

Clip identity is the index into the SOURCE POOL the timeline response already
returns (`all_candidates["clip_paths"]`, matched to a source binding by its
analysis-proxy path), never the matcher's internal clip ids. When the pool does
not cover every bound source the pool falls back to the order of
`PHONE_SOURCES_FIELD`, so a slot always indexes a real entry of `clips`.

Everything here is PURE and read-only: no GCS call, no mutation.
"""

from __future__ import annotations

import re
from typing import Any

from app.kria.recipes_v2 import EditRecipeV2
from app.services.phone_sources import PHONE_SOURCES_FIELD, PhoneSourceBinding
from app.services.phone_subtitled_editor import _pinned_recipe

# Pinned recipe track that carries each archetype's source clips.
_NARRATED_TRACK_ID = "narrated"
_VOICEOVER_TRACK_ID = "montage"

_STEP_ID = re.compile(r"^step-\d+-(?P<step_id>.+)$")


def is_phone_voiceover_family_variant(variant: object) -> bool:
    """A device-rendered `narrated` or montage `voiceover` variant."""
    return (
        isinstance(variant, dict)
        and variant.get("render_destination") == "device"
        and variant.get("resolved_archetype") in {"narrated", "voiceover"}
    )


def _bindings(assembly_plan: dict) -> list[PhoneSourceBinding]:
    rows = assembly_plan.get(PHONE_SOURCES_FIELD)
    bindings: list[PhoneSourceBinding] = []
    for row in rows if isinstance(rows, list) else []:
        try:
            bindings.append(PhoneSourceBinding.model_validate(row))
        except ValueError:
            continue
    return bindings


def source_pool_paths(assembly_plan: dict, all_candidates: dict | None) -> list[str]:
    """The ordered source pool slot `clip_index` values point into.

    `all_candidates["clip_paths"]` when it names every bound source (that is
    the pool the timeline response lists), else the bindings' own order.
    """
    bindings = _bindings(assembly_plan)
    proxies = [binding.proxy_path for binding in bindings]
    declared = (all_candidates or {}).get("clip_paths")
    paths = [p for p in declared if isinstance(p, str)] if isinstance(declared, list) else []
    if paths and set(proxies) <= set(paths):
        return paths
    return proxies


def _clip_rows(
    recipe: EditRecipeV2,
    track_id: str,
    bindings: list[PhoneSourceBinding],
    pool: list[str],
) -> list[tuple[Any, int, PhoneSourceBinding]]:
    track = next((t for t in recipe.tracks if t.id == track_id), None)
    if track is None:
        return []
    by_asset = {binding.media_id: binding for binding in bindings}
    rows: list[tuple[Any, int, PhoneSourceBinding]] = []
    for clip in sorted(track.clips, key=lambda c: c.timeline_start):
        binding = by_asset.get(clip.source_asset_id)
        if binding is None or binding.proxy_path not in pool:
            # A source the pool cannot index would put a slot on the wrong clip.
            return []
        rows.append((clip, pool.index(binding.proxy_path), binding))
    return rows


def narrated_timings_and_assignments(
    recipe: EditRecipeV2, bindings: list[PhoneSourceBinding], pool: list[str]
) -> tuple[list[dict], list[dict], list[dict]] | None:
    """`(narrated_timings, narrated_clip_assignments, slots)` from a pinned narrated recipe.

    Same row shapes the cloud narrated render persists and the iOS editor
    already reads (`narrated_timings` + `narrated_clip_assignments`, joined by
    `step_id`); ``clip_id`` is ``clip_<pool index>``.
    """
    rows = _clip_rows(recipe, _NARRATED_TRACK_ID, bindings, pool)
    if not rows:
        return None
    timings: list[dict] = []
    assignments: list[dict] = []
    slots: list[dict] = []
    for clip, index, binding in rows:
        match = _STEP_ID.match(clip.id)
        if match is None:
            return None
        step_id = match.group("step_id")
        start = round(float(clip.timeline_start), 3)
        end = round(float(clip.timeline_start + clip.source_duration / clip.rate), 3)
        if end <= start:
            continue
        clip_id = f"clip_{index}"
        source_start = round(max(0.0, float(clip.source_start)), 3)
        slots.append(
            {
                "slot_id": step_id,
                "clip_index": index,
                "source_duration_s": round(float(binding.original.duration_s), 3),
                "in_s": source_start,
                "duration_s": round(end - start, 3),
                "duration_beats": None,
                "order": len(slots),
                "removed": False,
            }
        )
        timings.append({"step_id": step_id, "start_s": start, "end_s": end, "confidence": 1.0})
        assignments.append(
            {
                "step_id": step_id,
                "clip_id": clip_id,
                "participant_key": f"clip:{clip_id}",
                "source_start_s": source_start,
            }
        )
    return (timings, assignments, slots) if timings else None


def voiceover_ai_timeline(
    recipe: EditRecipeV2, bindings: list[PhoneSourceBinding], pool: list[str]
) -> dict | None:
    """A read-only `ai_timeline` (`beat_grid` + `slots`) from a pinned montage recipe.

    Each slot carries `clip_index`, `in_s` and `duration_s` (output seconds, so
    a retimed clip fills the window it really plays for).
    """
    rows = _clip_rows(recipe, _VOICEOVER_TRACK_ID, bindings, pool)
    slots: list[dict] = []
    for position, (clip, index, binding) in enumerate(rows):
        duration = round(float(clip.source_duration / clip.rate), 3)
        if duration <= 0:
            continue
        # The montage compiler overlaps a clip with the previous one by its
        # transition; carry that as `transition_after` on the LEFT slot (the
        # guided-story projection's convention) so durations don't double-count.
        overlap_s = 0.0
        if position + 1 < len(rows):
            following = rows[position + 1][0]
            overlap_s = max(0.0, clip.timeline_start + duration - following.timeline_start)
        crossfade = overlap_s >= 0.1
        slots.append(
            {
                "slot_id": clip.id,
                "clip_index": index,
                "source_duration_s": round(float(binding.original.duration_s), 3),
                "in_s": round(max(0.0, float(clip.source_start)), 3),
                "duration_s": duration,
                "duration_beats": None,
                "order": len(slots),
                "removed": False,
                "transition_after": "crossfade" if crossfade else "cut",
                "transition_duration_s": round(min(1.0, overlap_s), 3) if crossfade else None,
            }
        )
    return {"beat_grid": [], "slots": slots} if slots else None


def project_phone_voiceover_timeline(
    assembly_plan: dict, all_candidates: dict | None, variant: dict
) -> dict | None:
    """The variant's clip timeline derived from its pinned device recipe.

    Returns ``{"pool": [...proxy paths], "slots": [...], "narrated_timings": [...],
    "narrated_clip_assignments": [...]}`` for `narrated` or
    ``{"pool": [...], "slots": [...], "ai_timeline": {...}}`` for `voiceover`; ``None`` when
    the variant is not one of those, has no pinned recipe, or its clips cannot
    be mapped onto the source pool.
    """
    if not is_phone_voiceover_family_variant(variant) or not isinstance(assembly_plan, dict):
        return None
    variant_id = variant.get("variant_id")
    if not isinstance(variant_id, str) or not variant_id:
        return None
    recipe = _pinned_recipe(assembly_plan, variant_id)
    if recipe is None:
        return None
    bindings = _bindings(assembly_plan)
    pool = source_pool_paths(assembly_plan, all_candidates)
    if variant.get("resolved_archetype") == "narrated":
        projected = narrated_timings_and_assignments(recipe, bindings, pool)
        if projected is None:
            return None
        return {
            "pool": pool,
            "slots": projected[2],
            "total_duration_s": round(float(recipe.duration), 3),
            "narrated_timings": projected[0],
            "narrated_clip_assignments": projected[1],
        }
    timeline = voiceover_ai_timeline(recipe, bindings, pool)
    if not timeline:
        return None
    return {
        "pool": pool,
        "slots": timeline["slots"],
        "ai_timeline": timeline,
        "total_duration_s": round(float(recipe.duration), 3),
    }
