"""Portable Kria editor operations compiled into Nova's atomic Save contract.

The model sees only :func:`build_editor_snapshot`, a path-free projection of
the current variant.  Parsed operations remain inert until this module turns
them into a complete ``EditorCommitRequest`` section.  The existing editor
commit service still owns final capability, baseline, source, and render
validation when an approval is consumed.
"""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from app.routes.generative_jobs import (
    EditorCommitCaptionMeta,
    EditorCommitMix,
    EditorCommitRequest,
    TimelineSlotEdit,
    _editor_capabilities,
    _guided_v2_revision,
    variant_render_baseline,
    visual_block_variant_duration,
)
from app.schemas.edit_proposal import MAX_PROPOSAL_DURATION_S

_IMAGE_SUFFIXES = {".avif", ".heic", ".heif", ".jpeg", ".jpg", ".png", ".webp"}
_PORTABLE_FAMILIES = {
    "automatic_cut",
    "caption",
    "clip",
    "music",
    "text",
    "title",
    "transition",
    "visual_media",
}
_TEXT_STYLE_FIELDS = {
    "alignment",
    "color",
    "effect",
    "font_family",
    "highlight_color",
    "letter_spacing",
    "line_spacing",
    "max_width_frac",
    "position",
    "size_px",
    "stroke_width",
    "text_case",
    "x_frac",
    "y_frac",
    "shadow_enabled",
    "rotation_deg",
}


class KriaEditorOpError(ValueError):
    """A parsed operation cannot be represented by the portable Save contract."""


@dataclass(frozen=True)
class CompiledEditorDraft:
    payload: EditorCommitRequest | dict[str, Any]
    changes: list[str]


def _variant_slots(variant: dict[str, Any]) -> list[dict[str, Any]]:
    timeline = variant.get("user_timeline") or variant.get("ai_timeline") or {}
    return [copy.deepcopy(row) for row in timeline.get("slots") or [] if isinstance(row, dict)]


def _safe_slot(row: dict[str, Any], index: int) -> dict[str, Any]:
    return {
        "key": str(row.get("slot_id") or f"slot-{index}"),
        "slot_id": row.get("slot_id"),
        "parent_segment_id": row.get("parent_segment_id"),
        "clip_index": row.get("clip_index"),
        "in_s": row.get("in_s", 0.0),
        "duration_s": row.get("duration_s"),
        "duration_beats": row.get("duration_beats"),
        "source_duration_s": row.get("source_duration_s"),
        "output_start_s": row.get("output_start_s"),
        "output_end_s": row.get("output_end_s"),
        "removed": bool(row.get("removed")),
        "transition_after": row.get("transition_after", "cut"),
        "transition_duration_s": row.get("transition_duration_s"),
        "look_preset": row.get("look_preset", "none"),
        "media_kind": row.get("media_kind"),
    }


def _visual_media_rows(job: Any, variant: dict[str, Any]) -> list[dict[str, Any]]:
    """Use story-native desired state when present, including an explicitly empty lane."""
    guided = _guided_v2_revision(job, variant)
    owner = guided if guided is not None else variant
    return copy.deepcopy(owner.get("visual_blocks") or [])


def _removable_visual_media(job: Any, variant: dict[str, Any]) -> list[dict[str, Any]]:
    if (
        _editor_capabilities(job, variant).get("visual_blocks") is not True
        or variant.get("text_mode") == "lyrics"
        or not variant.get("base_video_path")
    ):
        return []
    rows = _visual_media_rows(job, variant)
    guided = _guided_v2_revision(job, variant)
    text = (guided if guided is not None else variant).get("text_elements") or []
    linked = {row.get("visual_block_id") for row in text if isinstance(row, dict)}
    ids = [row.get("id") for row in rows if isinstance(row, dict)]
    return [
        row
        for row in rows
        if isinstance(row, dict)
        and row.get("kind") == "media"
        and row.get("origin", "user") == "user"
        and isinstance(row.get("id"), str)
        and row["id"]
        and ids.count(row["id"]) == 1
        and row["id"] not in linked
    ]


def _allowed_families(job: Any, variant: dict[str, Any]) -> list[str]:
    caps = _editor_capabilities(job, variant)
    families: list[str] = []
    if caps.get("text_elements") is True:
        families.extend(["text", "title"])
    if caps.get("timeline") is True:
        families.append("clip")
        clips = caps.get("clips") or {}
        transition = clips.get("transitions") if isinstance(clips, dict) else None
        if transition is True or (
            isinstance(transition, dict) and transition.get("editable") is True
        ):
            families.append("transition")
    if variant.get("caption_cues") or variant.get("resolved_archetype") in {
        "subtitled",
        "talking_head",
    }:
        families.append("caption")
    if caps.get("automatic_cut") is True and variant.get("speech_cut_candidates"):
        families.append("automatic_cut")
    music_ops = caps.get("music_operations") or {}
    if (
        caps.get("mix") is True
        or caps.get("swap_song") is True
        or any(
            value is True or (isinstance(value, dict) and value.get("editable") is True)
            for value in music_ops.values()
        )
    ):
        families.append("music")
    if _removable_visual_media(job, variant):
        families.append("visual_media")
    return sorted(set(families) & _PORTABLE_FAMILIES)


_TEXT_APPEARANCE_FIELDS = ("stroke_width", "shadow_enabled")


def _text_appearance_enabled() -> bool:
    from app.config import settings  # noqa: PLC0415

    return bool(getattr(settings, "text_appearance_enabled", False))


def _text_appearance_inventory(text_bars: list[dict[str, Any]], *, cues_present: bool) -> dict:
    """Mirror the web drawer's `buildTextAppearanceInventory` for text bars.

    Targets are the editable text bars; `identity` fingerprints the fields
    the parser compares so a stale inventory cannot address a changed bar.
    Caption and motion targets are not modelled on this path yet.
    """
    targets = []
    for row in text_bars:
        bar_id = row.get("id")
        if not isinstance(bar_id, str) or not bar_id.strip():
            continue
        editable = row.get("role") != "lyric_line"
        identity_source = json.dumps(
            {
                key: row.get(key)
                for key in ("id", "text", "start_s", "end_s", *_TEXT_APPEARANCE_FIELDS)
            },
            sort_keys=True,
            default=str,
        )
        targets.append(
            {
                "id": bar_id,
                "kind": "text",
                "supported_fields": list(_TEXT_APPEARANCE_FIELDS) if editable else [],
                "values": {
                    "stroke_width": row.get("stroke_width"),
                    "shadow_enabled": row.get("shadow_enabled", True),
                },
                "identity": hashlib.sha256(identity_source.encode("utf-8")).hexdigest()[:16],
            }
        )
    return {"version": 1, "caption_cues_editable": cues_present, "targets": targets}


def build_editor_snapshot(job: Any, variant: dict[str, Any]) -> dict[str, Any]:
    """Return the bounded, URL/path-free snapshot accepted by EditCopilot."""

    slots = [_safe_slot(row, index) for index, row in enumerate(_variant_slots(variant))]
    duration = visual_block_variant_duration(variant)
    text_bars = [
        {
            key: value
            for key, value in row.items()
            if key
            in {
                "alignment",
                "color",
                "effect",
                "end_s",
                "font_family",
                "highlight_color",
                "id",
                "position",
                "role",
                "size_px",
                "start_s",
                "text",
                "stroke_width",
                "shadow_enabled",
                # Centre fractions let the model see where a bar sits before
                # it moves one ("place the titles top left").
                "x_frac",
                "y_frac",
                "rotation_deg",
            }
        }
        for row in variant.get("text_elements") or []
        if isinstance(row, dict)
    ]
    cues = [
        {
            key: value
            for key, value in row.items()
            if key in {"end_s", "id", "smart_emphasis", "smart_role", "start_s", "text"}
        }
        for row in variant.get("caption_cues") or []
        if isinstance(row, dict)
    ]
    families = _allowed_families(job, variant)
    snapshot: dict[str, Any] = {
        "allowed_op_families": families,
        "base_generation": variant_render_baseline(variant),
        "has_narrated_captions": bool(cues)
        or variant.get("resolved_archetype") in {"subtitled", "talking_head"},
        "max_duration_s": MAX_PROPOSAL_DURATION_S,
        "remaining_duration_s": max(0.0, float(MAX_PROPOSAL_DURATION_S) - duration),
        "slots": slots,
        "text_bars": text_bars,
        "total_duration_s": duration,
    }
    if "text" in families and _text_appearance_enabled():
        # The web drawer builds this inventory client-side; the server chat
        # path never did, so the model's correct "remove shadow and outline"
        # op (patch_text_appearance) was rejected as invalid and, being
        # atomic, took every sibling op down with it (2026-09-19, job
        # d9a965b0: font, size and placement changes all dropped).
        snapshot["text_appearance_version"] = 1
        snapshot["text_appearance"] = _text_appearance_inventory(text_bars, cues_present=bool(cues))
    if cues or snapshot["has_narrated_captions"]:
        snapshot["captions"] = {
            "cues": cues,
            "cues_editable": bool(cues),
            "meta": {
                "enabled": variant.get("captions_enabled", True),
                "style": variant.get("caption_style", "sentence"),
                "font": variant.get("caption_font"),
                "y_frac": variant.get("caption_y_frac"),
                "size_px": variant.get("caption_size_px"),
                "color": variant.get("caption_color"),
                "highlight_color": variant.get("caption_highlight_color"),
                "stroke_width": variant.get("caption_stroke_width"),
                "shadow_enabled": variant.get("caption_shadow_enabled"),
                "appearance": variant.get("caption_editor_style"),
            },
            "total_cues": len(cues),
            "truncated": False,
        }
    if "visual_media" in snapshot["allowed_op_families"]:
        snapshot["visual_media"] = [
            {
                key: row[key]
                for key in ("id", "media_kind", "start_s", "end_s", "origin")
                if key in row
            }
            for row in _removable_visual_media(job, variant)
        ]
    current_track_id = variant.get("music_track_id")
    if "music" in snapshot["allowed_op_families"]:
        snapshot["music"] = {
            "swappable": False,
            "removable": bool(current_track_id),
            "current_track_id": current_track_id,
            "current_track_title": None,
            "candidates": [],
        }
        snapshot["mix"] = {"music_level": variant.get("mix")}
    guided = _guided_v2_revision(job, variant)
    if guided is not None:
        snapshot["guided_revision"] = {
            "revision_number": guided.get("revision_number"),
            "base_generation": variant_render_baseline(variant),
            "state_hash": guided.get("state_hash"),
        }
    if "automatic_cut" in snapshot["allowed_op_families"]:
        snapshot["automatic_cut"] = True
        snapshot["speech_cut_candidates"] = [
            {
                "candidate_id": row.get("candidate_id"),
                "source": row.get("source"),
                "status": row.get("status"),
            }
            for row in variant.get("speech_cut_candidates") or []
            if isinstance(row, dict) and row.get("status") == "pending"
        ]
    return snapshot


def _slot_duration(row: dict[str, Any]) -> float:
    try:
        value = float(row.get("duration_s") or 0.0)
    except (TypeError, ValueError):
        value = 0.0
    return max(0.0, value)


def _timeline_models(rows: list[dict[str, Any]]) -> list[TimelineSlotEdit]:
    return [
        TimelineSlotEdit(
            slot_id=row.get("slot_id"),
            parent_segment_id=row.get("parent_segment_id"),
            clip_index=int(row.get("clip_index")),
            in_s=float(row.get("in_s") or 0.0),
            duration_beats=row.get("duration_beats"),
            duration_s=row.get("duration_s"),
            removed=bool(row.get("removed")),
            transition_after=row.get("transition_after") or "cut",
            transition_duration_s=row.get("transition_duration_s"),
            look_preset=row.get("look_preset") or "none",
            look_adjustments=row.get("look_adjustments"),
        )
        for row in rows
    ]


def _require_index(rows: list[Any], index: object, label: str) -> int:
    if isinstance(index, bool) or not isinstance(index, int) or index < 0 or index >= len(rows):
        raise KriaEditorOpError(f"{label} changed before this edit could be drafted")
    return index


def _path_kind(path: object) -> str:
    suffix = PurePosixPath(str(path or "")).suffix.casefold()
    return "image" if suffix in _IMAGE_SUFFIXES else "video"


def _summary(op: dict[str, Any]) -> str:
    name = str(op.get("op") or "edit").replace("_", " ")
    return name[:1].upper() + name[1:]


def project_editor_draft(variant: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Overlay a same-generation draft without mutating the rendered Job."""
    projected = copy.deepcopy(variant)
    for key in ("text_elements", "caption_cues", "music_track_id", "visual_blocks"):
        if payload.get(key) is not None:
            projected[key] = copy.deepcopy(payload[key])
    if payload.get("visual_blocks") is not None and isinstance(
        projected.get("guided_edit_revision"), dict
    ):
        projected["guided_edit_revision"]["visual_blocks"] = copy.deepcopy(payload["visual_blocks"])
        projected["guided_edit_revision"]["state_hash"] = ""
    if payload.get("timeline_slots") is not None:
        originals = {
            row.get("slot_id"): row for row in _variant_slots(variant) if row.get("slot_id")
        }
        projected["user_timeline"] = {
            "slots": [
                {**originals.get(row.get("slot_id"), {}), **copy.deepcopy(row)}
                for row in payload["timeline_slots"]
            ]
        }
    if payload.get("mix") is not None:
        projected["mix"] = payload["mix"].get("music_level")
    if payload.get("remove_music"):
        projected["music_track_id"] = None
    for key, value in (payload.get("caption_meta") or {}).items():
        if key != "font_set":
            projected["captions_enabled" if key == "enabled" else f"caption_{key}"] = value
    return projected


def merge_editor_draft(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    merged = {**previous, **current}
    for key in ("caption_meta", "mix"):
        if previous.get(key) and current.get(key):
            merged[key] = {**previous[key], **current[key]}
    if current.get("remove_music"):
        merged.pop("music_track_id", None)
    elif current.get("music_track_id"):
        merged["remove_music"] = False
    elif previous.get("remove_music"):
        merged["remove_music"] = True
    return merged


def compile_editor_ops(job: Any, variant: dict[str, Any], ops: list[dict]) -> CompiledEditorDraft:
    """Compile one all-or-nothing portable operation bundle.

    The input is expected to have passed EditCopilot's schema parser.  This
    function nevertheless rejects unknown operations and rechecks indexes,
    bounds, and bundle effects against the authoritative in-memory variant.
    """

    if not ops:
        raise KriaEditorOpError("No safe draft change was produced")
    if len(ops) > 8:
        raise KriaEditorOpError("A draft may contain at most eight editor operations")
    if any(op.get("op") == "apply_speech_cut_candidate" for op in ops):
        if len(ops) != 1:
            raise KriaEditorOpError("A reviewed speech cut must be rendered on its own")
        candidate_id = str(ops[0].get("candidate_id") or "")
        candidate = next(
            (
                row
                for row in variant.get("speech_cut_candidates") or []
                if isinstance(row, dict)
                and row.get("candidate_id") == candidate_id
                and row.get("status") == "pending"
            ),
            None,
        )
        if candidate is None:
            raise KriaEditorOpError("The reviewed speech cut is no longer available")
        from app.pipeline.speech_cut_state import cut_revision  # noqa: PLC0415

        return CompiledEditorDraft(
            payload={
                "operation": "speech_cut",
                "candidate_id": candidate_id,
                "expected_revision": cut_revision(variant),
            },
            changes=["Apply reviewed speech cut"],
        )

    text = copy.deepcopy(
        [row for row in variant.get("text_elements") or [] if isinstance(row, dict)]
    )
    # `bar_index` always addresses the TEXT BARS list the model was shown
    # (`build_editor_snapshot`), never the list as it shrinks mid-bundle. The
    # web drawer resolves the same way (`textSnapAt` + DELETE_BAR by id);
    # popping in place made "remove bars 1-4" delete bars 1, 3 and 5 and then
    # reject bar 4 as out of range (2026-09-19 phone chat-edit incident).
    text_bars = list(text)
    removed_text_bars: set[int] = set()

    def _text_bar(index: object) -> dict[str, Any]:
        row = text_bars[_require_index(text_bars, index, "Text")]
        if id(row) in removed_text_bars:
            raise KriaEditorOpError("Text changed before this edit could be drafted")
        return row

    captions = copy.deepcopy(
        [row for row in variant.get("caption_cues") or [] if isinstance(row, dict)]
    )
    slots = _variant_slots(variant)
    base_generation = variant_render_baseline(variant)
    changed: set[str] = set()
    changes: list[str] = []
    caption_patch: dict[str, Any] = {}
    mix_level: float | None = None
    remove_music = False
    music_track_id: str | None = None
    title: str | None = None
    visual_blocks: list[dict[str, Any]] | None = None

    for op in ops:
        name = str(op.get("op") or "")
        if name == "remove_visual_media":
            targets = op.get("target_ids")
            if (
                not isinstance(targets, list)
                or not targets
                or len(targets) > 100
                or any(not isinstance(value, str) or not value for value in targets)
                or len(set(targets)) != len(targets)
            ):
                raise KriaEditorOpError("Visual media removal requires unique existing target IDs")
            allowed = {row["id"] for row in _removable_visual_media(job, variant)}
            current = (
                visual_blocks if visual_blocks is not None else _visual_media_rows(job, variant)
            )
            present = {row.get("id") for row in current if isinstance(row, dict)}
            if not set(targets).issubset(allowed & present):
                raise KriaEditorOpError("The selected visual media is no longer removable")
            visual_blocks = [row for row in current if row.get("id") not in set(targets)]
            changed.add("visual_media")
        elif name == "edit_text":
            _text_bar(op.get("bar_index"))["text"] = str(op["text"])
            changed.add("text")
        elif name == "patch_text_style":
            row = _text_bar(op.get("bar_index"))
            patch = {
                key: value
                for key, value in dict(op.get("patch") or {}).items()
                if key in _TEXT_STYLE_FIELDS
            }
            if not patch:
                raise KriaEditorOpError("No portable text style fields were supplied")
            row.update(patch)
            changed.add("text")
        elif name == "set_text_timing":
            _text_bar(op.get("bar_index")).update(
                {key: op[key] for key in ("start_s", "end_s") if key in op}
            )
            changed.add("text")
        elif name == "add_text":
            text.append(
                {
                    "id": f"kria-{uuid.uuid4().hex}",
                    "text": str(op["text"]),
                    "start_s": float(op["start_s"]),
                    "end_s": float(op["end_s"]),
                    "role": "generative_intro",
                    "font_family": "Playfair Display",
                    "size_px": 72,
                    "color": "#FFFFFF",
                    "effect": "static",
                    "alignment": "center",
                    "position": "middle",
                }
            )
            changed.add("text")
        elif name == "remove_text":
            removed_text_bars.add(id(_text_bar(op.get("bar_index"))))
            text = [row for row in text if id(row) not in removed_text_bars]
            changed.add("text")
        elif name == "patch_text_appearance":
            patch = {
                key: value
                for key, value in dict(op.get("patch") or {}).items()
                if key in _TEXT_APPEARANCE_FIELDS
            }
            targets = list(op.get("target_ids") or [])
            if not patch or not targets:
                raise KriaEditorOpError("No text appearance change was supplied")
            live = {row.get("id"): row for row in text if isinstance(row.get("id"), str)}
            for target_id in targets:
                row = live.get(target_id)
                if row is None or id(row) in removed_text_bars:
                    raise KriaEditorOpError("Text changed before this edit could be drafted")
                row.update(patch)
            changed.add("text")
        elif name in {"set_clip_duration", "set_clip_in", "trim_clip_start", "set_look_preset"}:
            index = _require_index(slots, op.get("slot_index"), "Timeline")
            row = slots[index]
            if name == "set_clip_duration":
                row["duration_beats"] = None
                row["duration_s"] = float(op["duration_s"])
            elif name == "set_clip_in":
                row["in_s"] = float(op["in_s"])
            elif name == "trim_clip_start":
                amount = min(float(op["start_s"]), max(0.0, _slot_duration(row) - 0.1))
                row["in_s"] = float(row.get("in_s") or 0.0) + amount
                row["duration_beats"] = None
                row["duration_s"] = _slot_duration(row) - amount
            else:
                row["look_preset"] = op["look_preset"]
                row["look_adjustments"] = None
            changed.add("timeline")
        elif name == "trim_output_start":
            remaining = float(op["start_s"])
            if remaining <= 0:
                raise KriaEditorOpError("The requested output trim has no effect")
            for row in slots:
                if row.get("removed"):
                    continue
                duration = _slot_duration(row)
                if remaining >= duration - 0.1:
                    row["removed"] = True
                    remaining -= duration
                    continue
                if remaining > 0:
                    row["in_s"] = float(row.get("in_s") or 0.0) + remaining
                    row["duration_beats"] = None
                    row["duration_s"] = duration - remaining
                    remaining = 0
                break
            if not any(not row.get("removed") for row in slots):
                raise KriaEditorOpError("The trim would remove the whole video")
            changed.add("timeline")
        elif name == "reorder_clip":
            source = _require_index(slots, op.get("from_index"), "Timeline")
            target = _require_index(slots, op.get("to_index"), "Timeline")
            row = slots.pop(source)
            slots.insert(target, row)
            changed.add("timeline")
        elif name == "remove_clip":
            index = _require_index(slots, op.get("slot_index"), "Timeline")
            if sum(not row.get("removed") for row in slots) <= 1:
                raise KriaEditorOpError("The final clip cannot be removed")
            slots[index]["removed"] = True
            changed.add("timeline")
        elif name == "split_clip":
            index = _require_index(slots, op.get("slot_index"), "Timeline")
            row = slots[index]
            duration = _slot_duration(row)
            split_at = float(op["at_s"])
            output_start = float(row.get("output_start_s") or 0.0)
            local = split_at - output_start if split_at > duration else split_at
            if local < 0.1 or local > duration - 0.1:
                raise KriaEditorOpError("The split point is outside the clip")
            left = {**row, "duration_beats": None, "duration_s": local}
            right = {
                **row,
                "slot_id": None,
                "parent_segment_id": row.get("slot_id"),
                "in_s": float(row.get("in_s") or 0.0) + local,
                "duration_beats": None,
                "duration_s": duration - local,
            }
            slots[index : index + 1] = [left, right]
            changed.add("timeline")
        elif name == "set_transition":
            active = [index for index, row in enumerate(slots) if not row.get("removed")]
            boundary = _require_index(active[:-1], op.get("boundary_index"), "Transition")
            row = slots[active[boundary]]
            row["transition_after"] = op["transition"]
            row["transition_duration_s"] = (
                None if op["transition"] == "cut" else float(op.get("duration_s") or 0.3)
            )
            changed.add("timeline")
        elif name == "add_unused_sources":
            selector = op.get("selector") or {}
            wanted = str(selector.get("media_kind") or "all")
            paths = list((job.all_candidates or {}).get("clip_paths") or [])
            used = {int(row.get("clip_index")) for row in slots if not row.get("removed")}
            durations = {
                int(row["clip_index"]): float(row["source_duration_s"])
                for row in _variant_slots(variant)
                if row.get("clip_index") is not None and row.get("source_duration_s") is not None
            }
            for index, path in enumerate(paths):
                kind = _path_kind(path)
                if index in used or (wanted != "all" and wanted != kind):
                    continue
                slots.append(
                    {
                        "slot_id": None,
                        "clip_index": index,
                        "in_s": 0.0,
                        "duration_beats": None,
                        "duration_s": 3.0
                        if kind == "image"
                        else min(3.0, durations.get(index, 3.0)),
                        "removed": False,
                        "transition_after": "cut",
                        "look_preset": "none",
                    }
                )
            changed.add("timeline")
        elif name in {"set_media_duration", "stack_images"}:
            selector = op.get("selector") or {}
            wanted = str(selector.get("media_kind") or "image")
            paths = list((job.all_candidates or {}).get("clip_paths") or [])
            selected = [
                index
                for index, row in enumerate(slots)
                if not row.get("removed")
                and int(row.get("clip_index")) < len(paths)
                and (wanted == "all" or _path_kind(paths[int(row.get("clip_index"))]) == wanted)
            ]
            if name == "set_media_duration":
                for index in selected:
                    slots[index]["duration_beats"] = None
                    slots[index]["duration_s"] = float(op["duration_s"])
            elif selected:
                first = selected[0]
                selected_rows = [slots[index] for index in selected]
                slots = [row for index, row in enumerate(slots) if index not in set(selected)]
                slots[first:first] = selected_rows
            changed.add("timeline")
        elif name == "edit_caption":
            index = _require_index(captions, op.get("cue_index"), "Caption")
            captions[index]["text"] = str(op["text"])
            changed.add("captions")
        elif name == "replace_caption_text":
            find = str(op["find"])
            replace = str(op["replace"])
            replaced = 0
            for row in captions:
                current = str(row.get("text") or "")
                updated = current.replace(find, replace)
                if updated != current:
                    row["text"] = updated
                    replaced += 1
            if not replaced:
                raise KriaEditorOpError(f'No captions contain "{find}"')
            changed.add("captions")
        elif name in {"set_caption_timing", "set_caption_emphasis"}:
            index = _require_index(captions, op.get("cue_index"), "Caption")
            if name == "set_caption_timing":
                captions[index].update({key: op[key] for key in ("start_s", "end_s") if key in op})
            else:
                captions[index]["smart_emphasis"] = bool(op["emphasis"])
                if not op["emphasis"]:
                    captions[index]["smart_style"] = None
            changed.add("captions")
        elif name == "set_caption_meta":
            caption_patch.update(dict(op.get("patch") or {}))
            changed.add("caption_meta")
        elif name == "set_mix":
            mix_level = float(op["music_level"])
            changed.add("mix")
        elif name == "remove_music":
            remove_music = True
            changed.add("music")
        elif name == "swap_music":
            music_track_id = str(op["track_id"])
            changed.add("music")
        elif name == "set_title":
            title = str(op["title"])
            changed.add("title")
        else:
            raise KriaEditorOpError(f"{name or 'Unknown operation'} is not portable to Kria yet")
        changes.append(_summary(op))

    guided = _guided_v2_revision(job, variant)
    request = EditorCommitRequest(
        guided_revision_number=int(guided["revision_number"]) if guided is not None else None,
        visual_blocks=visual_blocks,
        base_generation=base_generation,
        text_elements=text if "text" in changed else None,
        caption_cues=captions if "captions" in changed else None,
        caption_meta=(
            EditorCommitCaptionMeta(**caption_patch) if "caption_meta" in changed else None
        ),
        timeline_slots=_timeline_models(slots) if "timeline" in changed else None,
        mix=EditorCommitMix(music_level=mix_level) if "mix" in changed else None,
        music_track_id=music_track_id,
        remove_music=remove_music,
        title=title,
    )
    return CompiledEditorDraft(payload=request, changes=list(dict.fromkeys(changes))[:3])


__all__ = [
    "CompiledEditorDraft",
    "KriaEditorOpError",
    "build_editor_snapshot",
    "compile_editor_ops",
]
