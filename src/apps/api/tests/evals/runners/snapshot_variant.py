"""Rebuild a synthetic variant/job pair from a path-free EditCopilot snapshot.

`app.services.kria_editor_ops.build_editor_snapshot()` projects one variant
into the bounded, path-free `variant_snapshot` the model sees. This module
inverts that projection: given an edit_copilot golden's
`input.variant_snapshot`, it fabricates the minimal `variant` dict + duck-typed
`job` object that `compile_editor_ops(job, variant, ops)` and
`prepare_editor_commit(job, variant_id, payload, ...)` need, so an eval
structural check can replay a golden's parsed ops through the REAL server
compiler and commit validators instead of stopping at the parser.

Field mapping (mirrors `build_editor_snapshot` in
`app/services/kria_editor_ops.py`, inverted):

  snapshot key             -> variant / job field
  ------------------------    ---------------------------------------------
  text_bars                -> variant["text_elements"] (kept verbatim, same
                               key set)
  slots[i]                 -> variant["ai_timeline"]["slots"][i] (+ invented
                               source_gcs_path/clip_index -- the snapshot is
                               path-free by design)
  captions.cues             -> variant["caption_cues"] (kept verbatim)
  captions.meta.*           -> variant["caption_*"] (flattened back onto the
                               variant, e.g. meta.y_frac -> caption_y_frac)
  music.current_track_id    -> variant["music_track_id"]
  mix.music_level           -> variant["mix"] (defaulted to 0.5 when absent
                               so a `set_mix` op has something to validate
                               against -- mirrors
                               `tests/routes/test_editor_commit.py::_job`)
  visual_media[i]           -> variant["visual_blocks"][i] (media-kind
                               blocks only; enough shape for
                               `remove_visual_media`)
  base_generation           -> variant["render_generation_id"]
  guided_revision                -> NOT modeled. A real guided-story revision
    needs `job.assembly_plan["guided_edit"]` + `["guided_story_execution_plan"]`
    (an approved `EditProposalSnapshot`, media digests, etc.) -- reconstructing
    that from a path-free snapshot is not viable, and it is a separate, already
    heavily-tested surface (`tests/routes/test_editor_commit.py`). Goldens that
    can only compile through a live guided revision (visual_blocks lane on a
    guided story, guided timeline saves) are named in the structural check's
    allowlist instead of faked here.

The synthetic job is a `types.SimpleNamespace`, the same fixture style already
used by `tests/services/test_kria_editor_ops.py::_job` and
`tests/routes/test_editor_commit.py::_job` -- no ORM/DB `Job` row is needed
because `compile_editor_ops`/`prepare_editor_commit` only ever read
`job.assembly_plan`, `job.all_candidates`, `job.id`, `job.user_id`, `job.status`,
and `job.mode` off of it.
"""

from __future__ import annotations

import types
import uuid
from datetime import UTC, datetime
from typing import Any

DEFAULT_VARIANT_ID = "song_text"

# Ops whose family needs `resolved_archetype in {"narrated", "subtitled"}`
# (`_is_editable_caption_variant`) before `prepare_editor_commit` will accept a
# `caption_cues`/`caption_meta` section at all.
_CAPTION_OPS = {
    "edit_caption",
    "replace_caption_text",
    "set_caption_timing",
    "set_caption_emphasis",
    "set_caption_meta",
}

_CAPTION_META_KEY_MAP = {
    "enabled": "captions_enabled",
    "style": "caption_style",
    "font": "caption_font",
    "y_frac": "caption_y_frac",
    "size_px": "caption_size_px",
    "color": "caption_color",
    "highlight_color": "caption_highlight_color",
    "stroke_width": "caption_stroke_width",
    "shadow_enabled": "caption_shadow_enabled",
    "appearance": "caption_editor_style",
}


def _text_elements_from_snapshot(snapshot: dict) -> list[dict]:
    elements = [dict(row) for row in snapshot.get("text_bars") or [] if isinstance(row, dict)]
    known_ids = {row.get("id") for row in elements}
    # `patch_text_appearance`'s scope-contract goldens model the appearance
    # inventory (`text_appearance.targets`) without necessarily repeating every
    # target back out as a full `text_bars` row (the inventory IS the model's
    # only view of those fields). `compile_editor_ops` resolves
    # `patch_text_appearance` target_ids against `variant.text_elements` by id,
    # so any inventory target of kind "text" missing from `text_bars` needs a
    # stub element here -- built from the target's own `values` so the "before"
    # state a replayed patch diffs against still matches what the model saw.
    appearance = snapshot.get("text_appearance")
    targets = appearance.get("targets") if isinstance(appearance, dict) else None
    for index, target in enumerate(targets or []):
        if not isinstance(target, dict) or target.get("kind") != "text":
            continue
        target_id = target.get("id")
        if not isinstance(target_id, str) or not target_id or target_id in known_ids:
            continue
        values = target.get("values") if isinstance(target.get("values"), dict) else {}
        elements.append(
            {
                "id": target_id,
                "text": "",
                "start_s": float(index),
                "end_s": float(index) + 1.0,
                "role": "generative_intro",
                "position": "middle",
                **values,
            }
        )
        known_ids.add(target_id)
    return elements


def _slots_from_snapshot(snapshot: dict) -> list[dict]:
    slots: list[dict] = []
    for index, row in enumerate(snapshot.get("slots") or []):
        if not isinstance(row, dict):
            continue
        slots.append(
            {
                "slot_id": row.get("slot_id") or row.get("key") or f"slot-{index}",
                "parent_segment_id": row.get("parent_segment_id"),
                "clip_index": (row["clip_index"] if row.get("clip_index") is not None else index),
                # The snapshot is deliberately path-free (`build_editor_snapshot`
                # strips real GCS paths before this ever reaches the model).
                # Invent a stable placeholder so `job.all_candidates["clip_paths"]`
                # and slot `source_gcs_path` have *something* to key off.
                "source_gcs_path": f"users/eval/{index}.mp4",
                "source_duration_s": row.get("source_duration_s"),
                "in_s": row.get("in_s", 0.0),
                "duration_beats": row.get("duration_beats"),
                "duration_s": row.get("duration_s"),
                # `split_clip`/`trim_output_start` in `compile_editor_ops` key off
                # of `output_start_s` (cumulative output-timeline position), not
                # just the slot's own `duration_s` -- carry it through or a split
                # at a real output timestamp looks "outside the clip".
                "output_start_s": row.get("output_start_s"),
                "output_end_s": row.get("output_end_s"),
                "removed": bool(row.get("removed", False)),
                "transition_after": row.get("transition_after") or "cut",
                "transition_duration_s": row.get("transition_duration_s"),
                "look_preset": row.get("look_preset") or "none",
                "media_kind": row.get("media_kind"),
            }
        )
    return slots


def _caption_fields_from_snapshot(snapshot: dict) -> dict[str, Any]:
    captions = snapshot.get("captions")
    if not isinstance(captions, dict):
        return {}
    cues = [dict(row) for row in captions.get("cues") or [] if isinstance(row, dict)]
    meta = captions.get("meta") if isinstance(captions.get("meta"), dict) else {}
    fields: dict[str, Any] = {"caption_cues": cues}
    for source_key, variant_key in _CAPTION_META_KEY_MAP.items():
        if meta.get(source_key) is not None:
            fields[variant_key] = meta[source_key]
    return fields


def _visual_blocks_from_snapshot(snapshot: dict) -> list[dict]:
    rows = snapshot.get("visual_media")
    if not isinstance(rows, list):
        return []
    blocks: list[dict] = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        blocks.append(
            {
                "id": row["id"],
                "kind": "media",
                "origin": row.get("origin", "user"),
                "media_kind": row.get("media_kind"),
                "start_s": row.get("start_s", 0.0),
                "end_s": row.get("end_s", 1.0),
                "asset_id": row["id"],
                "src_gcs_path": f"users/eval/visual-{row['id']}.mp4",
            }
        )
    return blocks


def build_synthetic_variant(
    snapshot: dict,
    ops: list[dict] | None = None,
    *,
    variant_id: str = DEFAULT_VARIANT_ID,
) -> dict[str, Any]:
    """Invert a `build_editor_snapshot()` projection into a plain, editable,
    non-guided-story variant that a golden's compiled ops can run against.

    `ops` (when given) only steers a couple of archetype/track defaults so the
    right validators are exercised -- it never changes which fields are
    copied from `snapshot`.
    """
    op_names = {op.get("op") for op in ops or [] if isinstance(op, dict)}
    variant: dict[str, Any] = {
        "variant_id": variant_id,
        "render_status": "ready",
        "render_generation_id": str(snapshot.get("base_generation") or "gen-eval-1"),
        "render_finished_at": "2026-09-19T00:00:00Z",
        "text_mode": "agent_text",
        "lyrics_baked": True,
        # `_is_editable_caption_variant` gates caption sections on this being
        # "narrated"/"subtitled"; everything else (text/music/visual_media) is
        # deliberately left on the plain, non-guided-story path so the heavy
        # guided-story-v2 machinery in `prepare_editor_commit` never engages
        # (see the module docstring's `guided_revision` note).
        "resolved_archetype": "subtitled" if op_names & _CAPTION_OPS else None,
        "text_elements": _text_elements_from_snapshot(snapshot),
        "ai_timeline": {"beat_grid": [], "slots": _slots_from_snapshot(snapshot)},
        "base_video_path": f"generative-jobs/eval/{variant_id}/base_1.mp4",
        "video_path": f"generative-jobs/eval/{variant_id}/variant.mp4",
        "output_url": "https://signed/variant.mp4",
    }
    variant.update(_caption_fields_from_snapshot(snapshot))

    music = snapshot.get("music") if isinstance(snapshot.get("music"), dict) else {}
    current_track_id = music.get("current_track_id")
    if current_track_id is None and op_names & {"remove_music", "set_mix", "swap_music"}:
        # A `remove_music`/`set_mix` op is only meaningful (and only
        # `prepare_editor_commit`-valid) on a variant that already carries a
        # track; invent a stable placeholder id when the snapshot didn't
        # advertise one so those ops aren't spuriously rejected as "no song".
        current_track_id = "00000000-0000-0000-0000-0000000eva1"
    variant["music_track_id"] = current_track_id

    mix = snapshot.get("mix") if isinstance(snapshot.get("mix"), dict) else {}
    music_level = mix.get("music_level")
    # `set_mix` (and the commit's mix section) require a non-None
    # `variant["mix"]` (the voiceover-style voice/bed balance) -- default one
    # so a `set_mix` op can validate all the way through `prepare_editor_commit`
    # even when the snapshot's own `mix` section omitted a level.
    variant["mix"] = music_level if music_level is not None else 0.5

    if "remove_visual_media" in op_names or "visual_media" in snapshot:
        variant["visual_blocks"] = _visual_blocks_from_snapshot(snapshot)

    return variant


def build_synthetic_job(variant: dict[str, Any]) -> Any:
    """A minimal duck-typed Job -- see the module docstring for the exact
    field list `compile_editor_ops`/`prepare_editor_commit` read off of it."""
    clip_paths = [
        slot["source_gcs_path"]
        for slot in (variant.get("ai_timeline") or {}).get("slots") or []
        if slot.get("source_gcs_path")
    ] or [f"users/eval/{i}.mp4" for i in range(3)]
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        user_id="eval-user",
        assembly_plan={"variants": [variant]},
        all_candidates={"clip_paths": clip_paths},
        status="variants_ready",
        mode="content_plan",
    )


def build_fake_music_track(track_id: str) -> Any:
    """A duck-typed `MusicTrack` row good enough for the `swap_music` section
    of `prepare_editor_commit` (analysis-ready, published, never archived)."""
    return types.SimpleNamespace(
        id=track_id,
        analysis_status="ready",
        audio_gcs_path="music/eval-track.mp3",
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
        archived_at=None,
        duration_s=180.0,
        beats=[],
    )


def build_synthetic_variant_and_job(
    snapshot: dict, ops: list[dict] | None = None, *, variant_id: str = DEFAULT_VARIANT_ID
) -> tuple[Any, dict[str, Any]]:
    """Convenience: build the variant, then the job that embeds it."""
    variant = build_synthetic_variant(snapshot, ops, variant_id=variant_id)
    job = build_synthetic_job(variant)
    return job, variant
