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
import math
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Literal, NamedTuple

import structlog

from app.agents._schemas.text_element import (
    _ALLOWED_FONTS,
    CAPTION_CUE_SOURCE,
    narrated_storyboard_row_updates,
    resolve_narrated_storyboard_rows,
)
from app.config import settings
from app.kria.clip_tilt import ClipTilt
from app.pipeline.camera_effects import easing_bounds, resolve_easing
from app.routes.generative_jobs import (
    EditorCommitBackgroundMusic,
    EditorCommitCaptionMeta,
    EditorCommitMix,
    EditorCommitRequest,
    EditorCommitSpeakerBackground,
    TimelineSlotEdit,
    _editor_capabilities,
    _guided_source_context,
    _guided_v2_layouts,
    _guided_v2_revision,
    _guided_v2_slot_rows,
    _speaker_background_shape_ok,
    is_phone_subtitled_editor_variant,
    is_phone_voiceover_lane_variant,
    variant_render_baseline,
    visual_block_variant_duration,
)
from app.schemas.edit_proposal import MAX_PROPOSAL_DURATION_S
from app.schemas.editor_window import EDITOR_MIN_CLIP_S, below_editor_minimum
from app.services.clip_facts import assignment_facts, facts_for_prompt
from app.services.editor_limits import MAX_EDITOR_OPS
from app.services.kria_editor_ops_diff import (
    EditorDiff,
    diff_compiled_state,
    snapshot_before,
    speech_cut_diff,
)
from app.services.phone_voiceover_timeline import is_phone_voiceover_family_variant

_IMAGE_SUFFIXES = {".avif", ".heic", ".heif", ".jpeg", ".jpg", ".png", ".webp"}
_PORTABLE_FAMILIES = {
    "automatic_cut",
    "background",
    "background_windows",
    "caption",
    "clip",
    "effect",
    "music",
    "overlay",
    "sfx",
    "text",
    "title",
    "transition",
    "visual_media",
}


def timeline_patch_capabilities(job: Any, variant: dict[str, Any]) -> dict[str, bool]:
    """Project field-level timeline support from the same predicates as compile.

    This is advisory model context only; ``_op_patch_slots`` remains the final
    enforcement point. Missing fields stay compatible with older snapshots.
    """
    caps = _editor_capabilities(job, variant)
    clips = caps.get("clips") if isinstance(caps, dict) else {}
    clips = clips if isinstance(clips, dict) else {}
    source_window_timeline = _is_guided_native(job, variant) or _is_authored_phone_native(
        job, variant
    )
    device = variant.get("render_destination") == "device"

    def enabled(key: str, default: bool = True) -> bool:
        value = clips.get(key)
        return (
            default
            if value is None
            else value is True or (isinstance(value, dict) and value.get("editable") is True)
        )

    fields = {
        "duration_s": enabled("duration"),
        "in_s": enabled("in_s"),
        "removed": enabled("remove"),
        "transition_after": enabled("transitions"),
        "transition_duration_s": enabled("transitions"),
        "look_preset": enabled("looks") and not device,
        "playback_rate": enabled("playback_rate", default=not device) and source_window_timeline,
        "source_crop": enabled("source_crop", default=False),
    }
    if enabled("tilt", default=False):
        # KRI-1137: present only while open, so every other snapshot and prompt is unchanged.
        fields["tilt"] = True
        if enabled("tilt_backdrop_media", default=False):
            # KRI-1139: a pool photo or video behind a tilted clip; same rule, open-only.
            fields["tilt_backdrop_media"] = True
    return fields


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
    "bend",
}

# Editor capabilities the copilot snapshot carries (`editor_capabilities`), so a field
# only some app builds keep (`edit_copilot._CAPTION_META_FIELD_GATES`) is offered
# exactly where the editor would offer it.
COPILOT_EDITOR_CAPABILITIES = ("caption_animation",)


def caption_default_phases(variant: dict[str, Any]) -> dict[str, Any] | None:
    """The all-captions animation the editor shows (KRI-1007), else the older Style one."""
    style = variant.get("caption_editor_style")
    for value in (
        variant.get("caption_animation_phases"),
        style.get("animation_phases") if isinstance(style, dict) else None,
    ):
        if isinstance(value, dict):
            return value
    return None


log = structlog.get_logger(__name__)


class KriaEditorOpError(ValueError):
    """A parsed operation cannot be represented by the portable Save contract."""


class TextTimingConflict(KriaEditorOpError):
    """Requested timing needs a creator decision before a draft can be staged."""

    def __init__(
        self,
        requested_duration_s: float,
        required_duration_s: float,
        text_ids: tuple[str, ...],
        *,
        constraint: Literal["preserve", "readability", "duration"] = "preserve",
    ):
        self.constraint = constraint
        self.requested_duration_s = requested_duration_s
        self.required_duration_s = required_duration_s
        self.minimum_duration_s = math.ceil(required_duration_s * 10) / 10
        self.conflicting_text_ids = text_ids
        super().__init__(
            f"Text ends at {required_duration_s:g}s, "
            f"beyond the proposed {requested_duration_s:g}s video"
        )


@dataclass(frozen=True)
class CompiledEditorDraft:
    payload: EditorCommitRequest | dict[str, Any]
    changes: list[str]
    # KRI-218: [{id, clip_id|None, role, before|None, after|None}] for every text
    # whose wording changed / was added / was removed (receipts read this).
    text_diff: list[dict[str, Any]] = field(default_factory=list)
    # KRI-558: what the bundle changed, lane by lane (receipts name these changes).
    diff: EditorDiff = field(default_factory=EditorDiff)
    # KRI-442: the pre-op value of every lane this bundle changed, in COMMIT shape (the
    # same shape `payload` carries), so a live-plan section Undo can put it back. Empty
    # for a speech cut. Never read by the draft/render path itself.
    before: dict[str, Any] = field(default_factory=dict)
    canonical_duration_s: float | None = None


def is_caption_text_bar(row: dict[str, Any]) -> bool:
    """A subtitle/narration caption living in ``text_elements`` (not chat-editable text)."""
    params = row.get("source_params")
    return bool(
        (isinstance(params, dict) and params.get("source") == CAPTION_CUE_SOURCE)
        or str(row.get("id") or "").startswith("narration-caption-")
    )


def _variant_slots(variant: dict[str, Any], job: Any = None) -> list[dict[str, Any]]:
    """The variant's timeline slots; guided v2 variants fall back to the revision.

    A guided story has no legacy ``ai_timeline``/``user_timeline``, so the copilot
    used to see ``slots: []`` and could not anchor "label each landmark" or "the
    bridge shot" to a clip (KRI-191). With ``job`` the fallback derives the same
    unsigned, path-free segment rows the editor's timeline projection uses.
    """
    draft = variant.get("guided_draft_slots")
    if isinstance(draft, list) and draft:
        # KRI-219: an in-flight guided timeline draft (see project_editor_draft).
        return [copy.deepcopy(row) for row in draft if isinstance(row, dict)]
    timeline = variant.get("user_timeline") or variant.get("ai_timeline") or {}
    rows = [copy.deepcopy(row) for row in timeline.get("slots") or [] if isinstance(row, dict)]
    if not rows and job is not None and is_phone_voiceover_family_variant(variant):
        # KRI-290: a phone Voiceover cut is editable but lives in the pinned
        # recipe until its first timeline Save; read it the way the editor does.
        from app.services.phone_voiceover_cut_editor import (  # noqa: PLC0415
            current_voiceover_slots,
        )

        rows = current_voiceover_slots(job, variant) or []
    if rows and job is not None:
        # Legacy slot rows carry no media identity: derive photo/video from the
        # job's clip paths so kind selectors and still-image rules see photos.
        paths = list((getattr(job, "all_candidates", None) or {}).get("clip_paths") or [])
        for row in rows:
            index = row.get("clip_index")
            if (
                row.get("media_kind") is None
                and isinstance(index, int)
                and not isinstance(index, bool)
                and 0 <= index < len(paths)
            ):
                row["media_kind"] = _path_kind(paths[index])
    if rows or job is None:
        return rows
    guided = _guided_v2_revision(job, variant)
    if guided is None:
        return []
    segment_layout, _layouts = _guided_v2_layouts(job, variant)
    rows = _guided_v2_slot_rows(guided, segment_layout, include_source_path=False)
    kind_by_media = {
        str(source.get("media_id")): source.get("kind")
        for source in guided.get("sources") or []
        if isinstance(source, dict)
    }
    for row, segment in zip(rows, guided.get("segments") or [], strict=False):
        row["media_id"] = str(segment.get("media_id"))
        if kind_by_media.get(row["media_id"]) in {"video", "image"}:
            row["media_kind"] = kind_by_media[row["media_id"]]
    return rows


def _safe_slot(
    row: dict[str, Any],
    index: int,
    *,
    moment: str | None = None,
    facts: list[dict[str, Any]] | None = None,
    seen: str | None = None,
    visible_text: str | None = None,
) -> dict[str, Any]:
    slot: dict[str, Any] = {
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
    media_id = row.get("media_id")
    if isinstance(media_id, str) and media_id:
        slot["media_id"] = media_id
    if row.get("playback_rate") is not None:
        slot["playback_rate"] = row["playback_rate"]
    if row.get("source_end_s") is not None:
        slot["source_end_s"] = row["source_end_s"]
    if moment:
        slot["moment"] = moment
    if facts:
        slot["facts"] = facts
    if seen:
        slot["seen"] = {"text": seen, "provenance": "vision"}
    if visible_text:
        slot["visible_text"] = visible_text[:1000]
    if row.get("read_only") is True:
        slot["read_only"] = True
    return slot


# ── Clip context (KRI-191): what the copilot may know about each clip ───────────

_CLIP_LABEL_MEDIA_PREFIX = "clip-label-media-"
_MAX_SLOT_FACTS = 4
_MAX_FACT_VALUE_CHARS = 80
_MAX_BRIEF_CHARS = 12_000


def clip_facts_by_media_id(
    job: Any, variant: dict[str, Any], assignments: list[dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    """Path-free P3 facts (capture time, place, landmark + provenance) per clip.

    ``assignments`` are the plan item's ``clip_assignments`` rows (read by the
    async caller); they are joined to guided sources by storage path, which never
    leaves this function.
    """
    guided = _guided_v2_revision(job, variant)
    if guided is None:
        return {}
    media_by_path = {
        str(source.get("gcs_path")): str(source.get("media_id"))
        for source in guided.get("sources") or []
        if isinstance(source, dict) and source.get("gcs_path") and source.get("media_id")
    }
    out: dict[str, list[dict[str, Any]]] = {}
    for row in assignments:
        if not isinstance(row, dict):
            continue
        media_id = media_by_path.get(str(row.get("gcs_path")))
        if media_id is None:
            continue
        facts = [
            {
                "kind": str(fact.get("kind")),
                "value": str(fact.get("value"))[:_MAX_FACT_VALUE_CHARS],
                "provenance": str(fact.get("provenance")),
            }
            for fact in facts_for_prompt(assignment_facts(row))
            if fact.get("kind") and fact.get("value")
        ]
        if facts:
            out[media_id] = facts[:_MAX_SLOT_FACTS]
    return out


_MAX_SEEN_CHARS = 220


def _seen_text(analysis: object, *, kind: str = "video") -> str:
    """One compact, sanitized sentence about what a clip shows (vision analysis), or "".

    Read through the shared ``clip_record`` projection (its validators already strip
    control characters and cap lengths), then trimmed again for the copilot snapshot.
    """
    from app.services.clip_understanding import clip_record  # noqa: PLC0415

    record = clip_record(analysis if isinstance(analysis, dict) else None, kind=kind)
    if record.is_empty():
        return ""
    parts: list[str] = []
    for value in (
        record.summary or record.subject,
        record.setting,
        record.activity,
        *(m.description for m in record.notable_moments[:2]),
    ):
        text = " ".join(str(value or "").split())
        if text and text.casefold() not in {p.casefold() for p in parts}:
            parts.append(text)
    return "; ".join(parts)[:_MAX_SEEN_CHARS]


def _clip_description_by_media_id(
    job: Any,
    variant: dict[str, Any],
    assignments: list[dict[str, Any]],
    *,
    visible_text: bool = False,
) -> dict[str, str]:
    """Path-free vision descriptions per guided clip (``analysis["understanding"]``).

    Joined to guided sources by storage path exactly like ``clip_facts_by_media_id``.
    A clip with no stored understanding is simply absent: the copilot must then say
    it cannot tell what that clip shows, never guess.
    """
    guided = _guided_v2_revision(job, variant)
    phone_sources = _phone_source_media(job, variant) if guided is None else {}
    if guided is None and not phone_sources:
        return {}
    media_by_path = {
        str(source.get("gcs_path")): str(source.get("media_id"))
        for source in (guided or {}).get("sources") or []
        if isinstance(source, dict) and source.get("gcs_path") and source.get("media_id")
    }
    out: dict[str, str] = {}
    for row in assignments:
        if not isinstance(row, dict):
            continue
        if guided is None:
            # KRI-816: phone sources join on the immutable binding triple, so a
            # re-uploaded (new generation) clip is never described as this one.
            media_id = phone_sources.get(
                (
                    str(row.get("media_id")),
                    str(row.get("gcs_path")),
                    str(row.get("storage_generation")),
                )
            )
        else:
            media_id = media_by_path.get(str(row.get("gcs_path")))
        if media_id is None:
            continue
        if visible_text:
            from app.services.clip_understanding import clip_record  # noqa: PLC0415

            analysis = row.get("analysis")
            text = clip_record(
                analysis if isinstance(analysis, dict) else None,
                kind=str(row.get("kind") or "video"),
            ).on_screen_text[:1000]
        else:
            text = _seen_text(row.get("analysis"), kind=str(row.get("kind") or "video"))
        if text:
            out[media_id] = text
    return out


def _phone_source_media(job: Any, variant: dict[str, Any]) -> dict[tuple[str, str, str], str]:
    """(media_id, proxy path, generation) -> media_id for a phone Talking edit's sources."""
    from app.services.phone_subtitled_editor import (  # noqa: PLC0415
        is_phone_subtitled_editor_variant,
    )
    from app.services.phone_voiceover_timeline import _bindings  # noqa: PLC0415

    assembly = getattr(job, "assembly_plan", None)
    if not is_phone_subtitled_editor_variant(variant) or not isinstance(assembly, dict):
        return {}
    return {
        (binding.media_id, binding.proxy_path, binding.generation): binding.media_id
        for binding in _bindings(assembly)
    }


def clip_seen_by_media_id(
    job: Any, variant: dict[str, Any], assignments: list[dict[str, Any]]
) -> dict[str, str]:
    return _clip_description_by_media_id(job, variant, assignments)


def clip_visible_text_by_media_id(
    job: Any, variant: dict[str, Any], assignments: list[dict[str, Any]]
) -> dict[str, str]:
    """Actual stored OCR, never legacy speech or previous editorial captions."""
    return _clip_description_by_media_id(job, variant, assignments, visible_text=True)


def _clip_label_links(job: Any, variant: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Text-bar id -> {clip_id, inferred} for the per-clip label lane.

    The unified montage writes ``clip-label-{cut_id}`` bars; a copilot-authored
    label uses ``clip-label-media-{media_id}``. Approved label provenance
    (``inferred`` = a model landmark guess the creator may correct) is kept only
    while the bar still shows the approved text, so a creator's own edit is
    never re-flagged as a guess.
    """
    assembly = getattr(job, "assembly_plan", None)
    assembly = assembly if isinstance(assembly, dict) else {}
    guided = assembly.get("guided_edit")
    proposal = guided.get("approved_proposal") if isinstance(guided, dict) else None
    proposal = proposal if isinstance(proposal, dict) else {}
    media_by_cut = {
        str(cut.get("cut_id")): str(cut.get("media_id"))
        for cut in proposal.get("fast_cuts") or []
        if isinstance(cut, dict) and cut.get("cut_id") and cut.get("media_id")
    }
    approved = {
        str(label.get("media_id")): label
        for label in proposal.get("clip_labels") or []
        if isinstance(label, dict) and label.get("media_id")
    }
    links: dict[str, dict[str, Any]] = {}
    for bar in variant.get("text_elements") or []:
        bar_id = bar.get("id") if isinstance(bar, dict) else None
        if not isinstance(bar_id, str) or not bar_id.startswith(_CLIP_LABEL_BAR_PREFIX):
            continue
        if bar_id.startswith(_CLIP_LABEL_MEDIA_PREFIX):
            media_id = bar_id[len(_CLIP_LABEL_MEDIA_PREFIX) :]
        else:
            media_id = media_by_cut.get(bar_id[len(_CLIP_LABEL_BAR_PREFIX) :])
        if not media_id:
            continue
        label = approved.get(media_id)
        same_text = label is not None and str(label.get("text")) == str(bar.get("text"))
        links[bar_id] = {
            "clip_id": media_id,
            "inferred": bool(label.get("inferred")) if same_text else False,
            # Not the approved AI/fact label any more (the creator changed it, or
            # it was authored in chat): `label_each_clip` must leave it alone.
            "edited": not same_text,
        }
    return links


def _is_guided_native(job: Any, variant: dict[str, Any]) -> bool:
    """A story-native variant: no legacy timeline, timing lives in a guided revision."""
    if not variant.get("guided_draft_slots") and (
        variant.get("user_timeline") or variant.get("ai_timeline")
    ):
        return False
    return _guided_v2_revision(job, variant) is not None


def _is_authored_phone_native(job: Any, variant: dict[str, Any]) -> bool:
    """An explicit phone timeline has the same source-window speed contract."""
    return (
        variant.get("render_destination") == "device"
        and variant.get("editor_timeline_mode") == "authored"
        and isinstance(variant.get("user_timeline"), dict)
    )


def _has_label_lane(job: Any, variant: dict[str, Any]) -> bool:
    return bool(_clip_label_links(job, variant))


def _bar_clip_link(row: dict[str, Any], links: dict[str, dict[str, Any]]) -> dict[str, Any]:
    link = links.get(str(row.get("id")))
    if link is None:
        return {}
    out = {"clip_id": link["clip_id"], "inferred": link["inferred"]}
    if link["edited"]:
        out["edited"] = True
    return out


def _slot_moments(job: Any, variant: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, str]:
    """media_id -> a short, creator-visible description of the clip (guided v2)."""
    if not any(row.get("media_id") for row in rows):
        return {}
    guided = _guided_v2_revision(job, variant)
    if guided is None:
        return {}
    assembly = getattr(job, "assembly_plan", None)
    out: dict[str, str] = {}
    for source in guided.get("sources") or []:
        if not isinstance(source, dict) or not source.get("media_id"):
            continue
        context = _guided_source_context(assembly if isinstance(assembly, dict) else {}, source)
        text = context.get("description") or context.get("subject") or context.get("label")
        if text:
            out[str(source["media_id"])] = " ".join(text.split())[:100]
    return out


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


def _music_operation_editable(caps: dict[str, Any], key: str) -> bool:
    music_ops = caps.get("music_operations")
    value = music_ops.get(key) if isinstance(music_ops, dict) else None
    if isinstance(value, dict):
        return value.get("editable") is True
    return value is True


def _background_bed(
    caps: dict[str, Any], variant: dict[str, Any], *, guided_native: bool
) -> dict[str, Any] | None:
    """The adjustable smart background bed, or None when not gain-editable.

    Mirrors the commit route: `background_music` is a full-replacement section
    that needs the `background_music` capability and an existing treatment
    (guided-story v2 rejects the section outright).
    """
    treatment = variant.get("smart_music_treatment")
    if guided_native or caps.get("background_music") is not True or not isinstance(treatment, dict):
        return None
    track_id = treatment.get("track_id")
    if not isinstance(track_id, str) or not track_id:
        return None
    try:
        gain_db = float(treatment.get("gain_db", -18.0))
    except (TypeError, ValueError):
        gain_db = -18.0
    return {"track_id": track_id, "gain_db": round(gain_db, 2)}


def _music_removable(caps: dict[str, Any], current_track_id: str | None) -> bool:
    if not current_track_id:
        return False
    music_ops = caps.get("music_operations")
    # Only guided_story-v2 variants expose a per-operation `music_operations`
    # map (e.g. reference_only music blocks "remove" independently of a track
    # simply being present). Legacy/montage capabilities never set this key,
    # so preserve the prior "any current track is removable" behavior there.
    if isinstance(music_ops, dict) and "remove" in music_ops:
        return _music_operation_editable(caps, "remove")
    return True


def _source_pool_rows(job: Any, variant: dict[str, Any]) -> list[dict[str, Any]]:
    """Path-free bulk-selector catalog for add_unused_sources/set_media_duration/
    stack_images.

    Mirrors just enough of the web drawer's `source_pool` rows for
    `_bulk_source_catalog_present`/`_bulk_target_rows` in app/agents/edit_copilot.py
    to resolve real target counts and a stable selection digest. Without this,
    every bulk selector on the phone/server chat path failed closed with a
    "stale_target"/clarification response because no source catalog (`source_pool`,
    `sources`, or `clips`) was ever present in the snapshot (2026-09-19 audit).
    """
    paths = list((job.all_candidates or {}).get("clip_paths") or [])
    slots = _variant_slots(variant, job)
    used_clip_indexes = {
        row.get("clip_index")
        for row in slots
        if not row.get("removed") and row.get("clip_index") is not None
    }
    durations = {
        int(row["clip_index"]): float(row["source_duration_s"])
        for row in slots
        if row.get("clip_index") is not None and row.get("source_duration_s") is not None
    }
    revision = _guided_v2_revision(job, variant)
    sources_by_path = {
        row["gcs_path"]: row
        for row in (revision or {}).get("sources", [])
        if isinstance(row, dict) and isinstance(row.get("gcs_path"), str)
    }
    context = _approved_research_context(job, variant)
    for index, path in enumerate(paths):
        source = sources_by_path.get(path, {})
        duration = source.get("duration_s")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration > 0:
            durations.setdefault(index, float(duration))
    return [
        {
            "clip_index": index,
            "media_id": str(index),
            "kind": _path_kind(path),
            # `add_unused_sources` requires a truthy generation on every
            # target row (edit_copilot.py: "every added source requires an
            # exact ready generation") as a staleness guard. There is no
            # separate per-clip generation concept on `job.all_candidates`,
            # so derive a stable, path-free proxy: it changes if and only if
            # the underlying clip path changes, which is exactly the
            # invalidation this field exists to provide.
            "generation": hashlib.sha256(path.encode("utf-8")).hexdigest()[:12],
            "duration_s": durations.get(index),
            **(
                {"seen": {"text": seen, "provenance": "vision"}}
                if (seen := context["seen"].get(str(sources_by_path.get(path, {}).get("media_id"))))
                else {}
            ),
            "used": index in used_clip_indexes,
            "ready": True,
        }
        for index, path in enumerate(paths)
    ]


def _allowed_families(job: Any, variant: dict[str, Any]) -> list[str]:
    caps = _editor_capabilities(job, variant)
    families: list[str] = []
    if caps.get("text_elements") is True:
        families.append("text")
    # Title editing is gated independently of text_elements — mirrors the web
    # drawer's canEditIntroControls (`capabilities.intro_controls !== false`).
    # A guided_story-v2 variant can have text_elements editable (its title is
    # an ordinary "guided-title" text bar) while intro_controls stays False;
    # the atomic Save route rejects `payload.title` outright for those variants
    # (`guided_story_editor_v2_section_unsupported`). Advertising "title" there
    # let the model call set_title, which always failed downstream (2026-09-19
    # snapshot-parity audit).
    if caps.get("intro_controls") is not False or _has_guided_title_bar(variant):
        # Guided stories keep their title as the "guided-title" text bar; the
        # parser rewrites set_title into edit_text on that bar (see
        # `_guided_title_index` in app/agents/edit_copilot.py), which the
        # family gate must let through even though intro_controls is False.
        families.append("title")
    # Guided (story-native) variants keep text-only editing unless
    # `kria_guided_timeline_ops` is on: their per-clip label bars are timed on
    # absolute output windows, so the clip/transition families are only opened
    # when the compiler rebases the labels onto their segments (below). The
    # committed path is covered by tests/services/test_kria_editor_guided_timeline.py,
    # tests/routes/test_editor_commit.py (cloud) and
    # tests/routes/test_phone_guided_timeline_commit.py (device).
    guided_native = _is_guided_native(job, variant)
    # KRI-219: with `kria_guided_timeline_ops` the compiler rebases per-clip label
    # bars onto their segments (services/kria_editor_timeline.py), so the guided
    # clip/transition families open up. visual_media stays withheld below.
    guided_timeline_ops = bool(getattr(settings, "kria_guided_timeline_ops", False))
    if caps.get("timeline") is True and (not guided_native or guided_timeline_ops):
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
    original_audio = caps.get("original_audio")
    if (
        caps.get("mix") is True
        # KRI-821: a phone Voiceover edit's footage level (`set_mix original_level`).
        or (
            variant.get("resolved_archetype") in {"narrated", "voiceover"}
            and isinstance(original_audio, dict)
            and original_audio.get("editable") is True
        )
        or caps.get("swap_song") is True
        or any(
            value is True or (isinstance(value, dict) and value.get("editable") is True)
            for value in music_ops.values()
        )
    ):
        families.append("music")
    # Removing a visual block on a guided variant that carries per-clip labels
    # could strand a label bar whose clip is gone; withheld until it drops the bar.
    if _removable_visual_media(job, variant) and not (
        guided_native and _has_label_lane(job, variant)
    ):
        families.append("visual_media")
    # A device-rendered variant must not advertise a family that can only end in a
    # 422 unsupported_phone_edit. `caps` is already the clamped capability map
    # (`generative_jobs._clamp_phone_editor_capabilities`), which keeps `sfx` open only
    # where Save compiles it into the phone recipe (Talking, Voiceover, and guided /
    # montage with PHONE_GUIDED_SFX_ENABLED), so it is the single gate here too (KRI-608).
    # The phone has no camera-effect lane yet (KRI-114 Phase 4).
    on_device = variant.get("render_destination") == "device"
    if caps.get("sfx") is True and not _sfx_base_missing(variant):
        families.append("sfx")
    # KRI-661: a phone Talking edit's picture cards (on top of the speaker or full
    # screen). Only once the draft base carries the card list (`project_editor_draft`
    # derives it from the pinned recipe), so an index never addresses a row Save
    # does not know about; Save compiles `media_overlays` into the phone recipe.
    if (
        on_device
        and caps.get("overlays") is True
        and is_phone_subtitled_editor_variant(variant)
        and isinstance(variant.get("media_overlays"), list)
    ):
        families.append("overlay")
    if caps.get("camera_effects") is True and not on_device:
        families.append("effect")
    if background_state(job, variant)[0]:
        families.append("background")
        if background_windows_open(job, variant):
            families.append("background_windows")
    return sorted(set(families) & _PORTABLE_FAMILIES)


def _sfx_base_missing(variant: dict[str, Any]) -> bool:
    """True for a Talking / Voiceover device variant whose current effects are not on the row.

    Those variants carry their effects only in the pinned recipe until an editor Save
    persists ``sound_effects`` (the HTTP read paths backfill it; the chat draft base does
    not). Offering ``add_sfx`` from that empty base would Save only the new effect and
    silently drop the ones already playing, and ``patch_sfx`` / ``remove_sfx`` indexes
    would not line up. Chat opens ``sfx`` there once a Save has written the key.
    """
    if "sound_effects" in variant:
        return False
    return is_phone_subtitled_editor_variant(variant) or is_phone_voiceover_lane_variant(variant)


_OVERLAY_SNAPSHOT_FIELDS = ("start_s", "end_s", "x_frac", "y_frac", "scale", "z")


def _overlay_snapshot(variant: dict[str, Any], *, tilt: bool = False) -> dict[str, Any]:
    """The picture cards the copilot may address (KRI-661): path-free, indexed.

    ``counts`` is the whole picture even when the prompt can only list the first few
    rows, so "put all my photos on top" is answerable with ONE bulk op instead of
    thirty patches. ``asset_pool`` stays empty on purpose: a Talking Save refuses a
    photo Kria did not pin for this edit, so ``add_overlay`` fails closed in the parser.

    ``tilt`` (KRI-1141, ``editor_capabilities.media_overlay_tilt``) opens ``patch_overlay``'s
    ``rotation_deg`` / ``rotation_delta_deg`` and lists a tilted card's angle. It is present only
    when ON, so a closed gate leaves the snapshot, and so the prompt, byte-identical.
    """
    from app.agents.editor_ops_v2.overlays import card_rotation  # noqa: PLC0415

    rows = [row for row in variant.get("media_overlays") or [] if isinstance(row, dict)]
    cards = []
    for index, row in enumerate(rows):
        card: dict[str, Any] = {
            "index": index,
            "id": row.get("id"),
            "kind": "video" if row.get("kind") == "video" else "image",
            "position": "custom",
            "display_mode": "fullscreen" if row.get("display_mode") == "fullscreen" else "pip",
        }
        card.update({key: row.get(key) for key in _OVERLAY_SNAPSHOT_FIELDS})
        if tilt:
            angle = card_rotation(row)
            if angle:
                card["rotation_deg"] = angle
        label = row.get("label")
        if isinstance(label, str) and label:
            card["label"] = label[:60]
        cards.append(card)
    return {
        "cards": cards,
        "counts": {
            "total": len(cards),
            "images": sum(1 for c in cards if c["kind"] == "image"),
            "videos": sum(1 for c in cards if c["kind"] == "video"),
            "fullscreen": sum(1 for c in cards if c["display_mode"] == "fullscreen"),
        },
        "asset_pool": [],
        "pending_suggestions": [],
        **({"tilt": True} if tilt else {}),
    }


# Why a Talking edit's background cannot be set from chat, in words the model can repeat to the
# creator (S18). Keyed by the reason `background_state` returns.
_BACKGROUND_UNAVAILABLE = {
    "cloud": "this edit is rendered in the cloud, so a photo or video can't go behind you here",
    "shape": "this edit's timeline was customised, so a photo or video can't go behind you here",
    "disabled": "backgrounds behind you aren't switched on yet",
    "token_not_verified": "backgrounds behind you aren't ready on this iPhone app yet",
    "client_protocol_below_min": "this version of the app can't show a background behind you yet",
}
_BACKGROUND_ARCHETYPES = {"subtitled", "talking_head"}
MAX_BACKGROUND_POOL_ROWS = 12


def background_state(job: Any, variant: dict[str, Any]) -> tuple[bool, str | None]:
    """(chat may set a background on this variant, why not).

    Both ``(False, None)``: the kill switch is off or this is not a Talking edit, so the
    snapshot says nothing about backgrounds and stays byte-identical to before the lane.
    Otherwise it is the chat twin of ``generative_jobs._speaker_background_capability``
    (device render, an editable shape, the phone gate), except that a worker-side planner has
    no app build to ask, so the client protocol is checked only inside an HTTP request
    (`speaker_background_unavailable_reason`'s default).
    """
    if not settings.speaker_background_enabled:
        return False, None
    if variant.get("resolved_archetype") not in _BACKGROUND_ARCHETYPES:
        return False, None
    if variant.get("render_destination") != "device":
        return False, "cloud"
    if not _speaker_background_shape_ok(job, variant):
        return False, "shape"
    from app.services.phone_rollout import speaker_background_unavailable_reason  # noqa: PLC0415

    reason = speaker_background_unavailable_reason()
    return reason is None, reason


def background_windows_open(job: Any, variant: dict[str, Any]) -> bool:
    """May chat also time several backgrounds and place the speaker in a corner (KRI-1162)?

    The base lane is open (`background_state`) AND the windows gate holds
    (`speaker_background_windows_unavailable_reason`, same worker-side client rule). False
    adds nothing to the snapshot, so a closed windows gate is byte-identical to before.
    """
    if not background_state(job, variant)[0]:
        return False
    from app.services.phone_rollout import (  # noqa: PLC0415
        speaker_background_windows_unavailable_reason,
    )

    return speaker_background_windows_unavailable_reason() is None


def background_duration_s(variant: dict[str, Any]) -> float:
    """The edit's length in seconds, 0.0 when the variant does not say (KRI-1162)."""
    try:
        return float(visual_block_variant_duration(variant))
    except (TypeError, ValueError):
        return 0.0


def background_frame_preset(frame: object) -> str | None:
    """The named corner a stored ``speaker_frame`` is, ``"custom"`` for any other geometry."""
    if not isinstance(frame, dict) or not frame:
        return None
    from app.kria.portable_speaker_background import SPEAKER_FRAME_PRESETS  # noqa: PLC0415

    for name, preset in SPEAKER_FRAME_PRESETS.items():
        if all(abs(float(frame.get(key, -1)) - value) < 1e-6 for key, value in preset.items()):
            return name
    return "custom"


def background_current(job: Any, variant: dict[str, Any]) -> dict[str, Any] | None:
    """The background behind the speaker now: the variant's own row (an explicit ``None`` is a
    cleared draft), else what the pinned recipe says for a Talking edit never Saved."""
    if "speaker_background" in variant:
        current = variant["speaker_background"]
        return current if isinstance(current, dict) and current.get("media_id") else None
    return derived_phone_background(job, variant)


def _background_snapshot(
    job: Any, variant: dict[str, Any], clip_context: dict[str, Any]
) -> dict[str, Any] | None:
    """The ``background`` section the copilot may address (KRI-1117), path-free.

    ``pool`` is the item's ready photos and videos the server listed (``clip_context
    ["background_pool"]``, built by the planner, which owns the DB read): an empty pool makes
    ``set_background`` fail closed in the parser, like ``add_overlay`` with no asset pool. It is
    a section of its own, never ``overlays.asset_pool``, which would also open ``add_overlay``
    on a Talking edit. None while the lane says nothing (see `background_state`).
    """
    open_, reason = background_state(job, variant)
    if not open_:
        if reason is None:
            return None
        return {"available": False, "reason": _BACKGROUND_UNAVAILABLE[reason]}
    current = background_current(job, variant)
    pool = [
        row
        for row in clip_context.get("background_pool") or []
        if isinstance(row, dict) and row.get("kind") in {"image", "video"}
    ][:MAX_BACKGROUND_POOL_ROWS]
    section: dict[str, Any] = {
        "available": True,
        "current": (
            {
                "media_id": current.get("media_id"),
                "kind": current.get("kind"),
                "source_start_s": current.get("source_start_s", 0.0),
            }
            if isinstance(current, dict) and current.get("media_id")
            else None
        ),
        "pool": pool,
    }
    if background_windows_open(job, variant):
        # KRI-1162: only while the windows gate is open, so a closed gate's section is unchanged.
        windows = current.get("windows") if isinstance(current, dict) else None
        section["windows_available"] = True
        section["duration_s"] = round(background_duration_s(variant), 3)
        section["windows"] = [
            {
                "media_id": window.get("media_id"),
                "start_s": window.get("start_s"),
                "end_s": window.get("end_s"),
            }
            for window in windows or []
            if isinstance(window, dict)
        ]
        section["speaker_frame"] = background_frame_preset(
            current.get("speaker_frame") if isinstance(current, dict) else None
        )
    return section


def derived_phone_overlays(job: Any, variant: dict[str, Any]) -> list[dict[str, Any]] | None:
    """A Talking edit's cards as the editor shows them before its first Save.

    The pinned device recipe is the only record until a Save persists
    ``media_overlays`` (the HTTP read paths backfill the same way,
    `_augment_variant_with_phone_subtitled_editor_sections`); the chat base must
    index the same rows. None when the variant is not a Talking edit or its lanes
    are closed.
    """
    if not is_phone_subtitled_editor_variant(variant):
        return None
    from app.config import settings as app_settings  # noqa: PLC0415
    from app.services.phone_rollout import phone_subtitled_editor_lanes_supported  # noqa: PLC0415
    from app.services.phone_subtitled_editor import (  # noqa: PLC0415
        project_phone_subtitled_editor_sections,
    )

    if not (app_settings.media_overlays_enabled and phone_subtitled_editor_lanes_supported()):
        return None
    projected = project_phone_subtitled_editor_sections(
        getattr(job, "assembly_plan", None) or {}, variant
    )
    rows = (projected or {}).get("media_overlays")
    return [dict(row) for row in rows if isinstance(row, dict)] if rows is not None else None


def derived_phone_background(job: Any, variant: dict[str, Any]) -> dict[str, Any] | None:
    """A Talking edit's background as the editor shows it before its first Save.

    The pinned device recipe is the only record until an editor Save persists the
    ``speaker_background`` section (the HTTP read paths project it the same way,
    `project_phone_subtitled_editor_sections`); a chat turn must see what Save sees. None when
    the variant is not a Talking edit, its lanes are closed, or it has no background.
    """
    if not is_phone_subtitled_editor_variant(variant):
        return None
    from app.services.phone_rollout import phone_subtitled_editor_lanes_supported  # noqa: PLC0415
    from app.services.phone_subtitled_editor import (  # noqa: PLC0415
        project_phone_subtitled_editor_sections,
    )

    if not phone_subtitled_editor_lanes_supported():
        return None
    projected = project_phone_subtitled_editor_sections(
        getattr(job, "assembly_plan", None) or {}, variant
    )
    document = (projected or {}).get("speaker_background")
    return dict(document) if isinstance(document, dict) else None


def _audio_notes(job: Any, variant: dict[str, Any]) -> list[str]:
    """Plain-language audio limits so the copilot answers honestly instead of
    redirecting to a control that does not exist for this edit."""
    notes: list[str] = []
    from app.services.user_song_projection import user_song_for_variant  # noqa: PLC0415

    if user_song_for_variant(job, variant, song_filename=None) is not None:
        # The creator's own song (KRI-374) is part of the render. The editor's Sounds tab
        # changes its volume, start point (not lip-sync) or removes it (KRI-428); this
        # copilot has no operation for it, so point there instead of denying it exists.
        notes.append(
            "This edit plays the creator's own song. They can change its volume, start "
            "point (not for lip-sync) or remove it in the editor's Sounds tab; you cannot "
            "change it from chat."
        )
    elif variant.get("music_playback_mode") == "reference_only":
        notes.append(
            "The matched song is added when the creator posts. It is not part of this "
            "render, so there is no music to make quieter, swap or remove here; the "
            "footage keeps its own sound."
        )
    if variant.get("render_destination") == "device" and _is_guided_native(job, variant):
        notes.append("This edit has no control for the footage's own sound level.")
    return notes


def _has_guided_title_bar(variant: dict[str, Any]) -> bool:
    return variant.get("resolved_archetype") == "guided_story" and any(
        isinstance(row, dict) and row.get("id") == "guided-title" and not row.get("removed")
        for row in variant.get("text_elements") or []
    )


# font_family rides the appearance lane so "change all fonts" is ONE atomic op (KRI-203).
_TEXT_APPEARANCE_FIELDS = ("stroke_width", "shadow_enabled", "font_family")
_CLIP_LABEL_BAR_PREFIX = "clip-label-"


def _text_appearance_enabled() -> bool:
    from app.config import settings  # noqa: PLC0415

    return bool(getattr(settings, "text_appearance_enabled", False))


def _appearance_group(bar_id: str) -> str | None:
    """Selector vocabulary for "all titles" / "all labels" (edit_copilot.py)."""
    if bar_id == "guided-title":
        return "title"
    if bar_id.startswith(_CLIP_LABEL_BAR_PREFIX):
        return "label"
    return None


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
        target = {
            "id": bar_id,
            "kind": "text",
            "supported_fields": list(_TEXT_APPEARANCE_FIELDS) if editable else [],
            "values": {
                "stroke_width": row.get("stroke_width"),
                "shadow_enabled": row.get("shadow_enabled", True),
                "font_family": row.get("font_family"),
            },
            "identity": hashlib.sha256(identity_source.encode("utf-8")).hexdigest()[:16],
        }
        group = _appearance_group(bar_id)
        if group is not None:
            target["group"] = group
        targets.append(target)
    return {"version": 1, "caption_cues_editable": cues_present, "targets": targets}


def _approved_research_context(job: Any, variant: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Older chat paths use the immutable approved media, joined by source generation."""
    from app.services.clip_understanding import clip_record

    revision = _guided_v2_revision(job, variant)
    assembly = getattr(job, "assembly_plan", None) or {}
    proposal = (assembly.get("guided_edit") or {}).get("approved_proposal") or {}
    rows = proposal.get("media") or []
    context: dict[str, dict[str, str]] = {"seen": {}, "visible_text": {}}
    if revision is None:
        return context
    for source in revision.get("sources") or []:
        for row in rows:
            if not isinstance(row, dict) or any(
                row.get(key) != source.get(key)
                for key in ("media_id", "generation", "gcs_path", "lane")
            ):
                continue
            media_id = str(source["media_id"])
            kind = str(row.get("kind") or "video")
            analysis = row.get("analysis")
            seen = _seen_text(analysis, kind=kind)
            visible = clip_record(
                analysis if isinstance(analysis, dict) else None, kind=kind
            ).on_screen_text[:1000]
            if seen:
                context["seen"][media_id] = seen
            if visible:
                context["visible_text"][media_id] = visible
            break
    return context


def build_editor_snapshot(
    job: Any,
    variant: dict[str, Any],
    *,
    clip_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the bounded, URL/path-free snapshot accepted by EditCopilot.

    ``clip_context`` (built by the async planner, which owns the DB read) carries
    ``facts`` (``clip_facts_by_media_id``) and ``brief`` (the rendered Creative
    Brief). Both are optional: with neither, the snapshot is what it always was
    plus the guided-slot fallback and the label-lane links. Pinned approved-media
    descriptions/OCR also support older callers without plan-item context.
    """

    clip_context = clip_context or {}
    facts_by_media = clip_context.get("facts") or {}
    approved_context = _approved_research_context(job, variant)
    seen_by_media = {**approved_context["seen"], **(clip_context.get("seen") or {})}
    visible_by_media = {
        **approved_context["visible_text"],
        **(clip_context.get("visible_text") or {}),
    }
    slot_rows = _variant_slots(variant, job)
    if not slot_rows:
        # KRI-816: a phone Talking cut lives only in its pinned recipe. Project it
        # read-only for the snapshot (research binds to what is pictured); it is
        # never added to `_variant_slots`, which also seeds the applier.
        from app.services.phone_subtitled_editor import (  # noqa: PLC0415
            phone_subtitled_research_slots,
        )

        slot_rows = phone_subtitled_research_slots(job, variant)
    guided_source_labels = _slot_moments(job, variant, slot_rows)
    label_links = _clip_label_links(job, variant)
    label_text_by_clip = {
        link["clip_id"]: str(row.get("text") or "")
        for row in variant.get("text_elements") or []
        if isinstance(row, dict) and (link := label_links.get(str(row.get("id"))))
    }
    slots = []
    for index, row in enumerate(slot_rows):
        media_id = row.get("media_id")
        slots.append(
            _safe_slot(
                row,
                index,
                moment=(
                    label_text_by_clip.get(media_id) or guided_source_labels.get(media_id)
                    if isinstance(media_id, str)
                    else None
                ),
                facts=facts_by_media.get(media_id) if isinstance(media_id, str) else None,
                seen=seen_by_media.get(media_id) if isinstance(media_id, str) else None,
                visible_text=visible_by_media.get(media_id) if isinstance(media_id, str) else None,
            )
        )
    duration = visual_block_variant_duration(variant)
    text_bars = [
        {
            **{
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
                    "bend",
                    # The visible "effect" a creator means is usually the entrance
                    # animation (typewriter/fade/pop), which lives here, not in `effect`.
                    "animation_phases",
                }
            },
            **_bar_clip_link(row, label_links),
            # Sequence identity travels in existing round-trip-safe metadata;
            # expose only its selector descriptor to the model.
            **(
                {"sequence_source_id": row["source_params"]["sequence_source_id"]}
                if isinstance(row.get("source_params"), dict)
                and isinstance(row["source_params"].get("sequence_source_id"), str)
                else {}
            ),
            # Caption/narration bars: selector groups must not sweep them up.
            **({"caption_cue": True} if is_caption_text_bar(row) else {}),
            # Tombstoned generated text: selector ops must not match it.
            **({"removed": True} if row.get("removed") else {}),
        }
        # Storyboard bars carry their burned look, as the editor shows them.
        for row in resolve_narrated_storyboard_rows(variant.get("text_elements"))
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
    caps = _editor_capabilities(job, variant)
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
    if variant.get("resolved_archetype") in {"subtitled", "talking_head"}:
        # KRI-816: the recorded speech is the edit's primary track, so research
        # places sourced text only in its pauses (narrated/voiceover cues do not).
        snapshot["spoken_edit"] = True
    brief = clip_context.get("brief")
    if isinstance(brief, str) and brief.strip():
        from app.kria.brief import BriefCoverageError  # noqa: PLC0415

        if len(brief) > _MAX_BRIEF_CHARS:
            raise BriefCoverageError("editor request exceeds 12,000-character limit")
        snapshot["brief"] = brief
    if "text" in families and any(slot.get("facts") for slot in slots):
        # Server-only capability: `label_each_clip` needs grounded per-clip facts
        # that the web drawer's snapshot never carries, so the parser refuses the
        # op unless this marker is present.
        snapshot["label_facts"] = True
    timeline_max_slots = caps.get("timeline_max_slots")
    if isinstance(timeline_max_slots, int) and timeline_max_slots > 0:
        snapshot["editor_limits"] = {"max_timeline_slots": timeline_max_slots}
    if "clip" in families:
        snapshot["timeline_patch_capabilities"] = timeline_patch_capabilities(job, variant)
        if snapshot["timeline_patch_capabilities"].get("tilt") is True:
            for slot, row in zip(slots, slot_rows, strict=True):
                if tilt := ClipTilt.from_row(row.get("tilt")).row():
                    slot["tilt"] = tilt
        if snapshot["timeline_patch_capabilities"].get("tilt_backdrop_media") is True:
            # KRI-1139: the item's ready photos and videos a backdrop may name, the same rows
            # the Talking background lists (the planner owns the DB read). Empty => the parser
            # refuses every media backdrop, like add_overlay with no asset pool.
            snapshot["tilt_backdrop_pool"] = [
                row
                for row in clip_context.get("background_pool") or []
                if isinstance(row, dict) and row.get("kind") in {"image", "video"}
            ][:MAX_BACKGROUND_POOL_ROWS]
        # Unlocks add_unused_sources/set_media_duration/stack_images bulk
        # selectors — see `_source_pool_rows`.
        snapshot["source_pool"] = _source_pool_rows(job, variant)
    if "text" in families and _text_appearance_enabled():
        # The web drawer builds this inventory client-side; the server chat
        # path never did, so the model's correct "remove shadow and outline"
        # op (patch_text_appearance) was rejected as invalid and, being
        # atomic, took every sibling op down with it (2026-09-19, job
        # d9a965b0: font, size and placement changes all dropped).
        snapshot["text_appearance_version"] = 1
        snapshot["text_appearance"] = _text_appearance_inventory(text_bars, cues_present=bool(cues))
    if cues or snapshot["has_narrated_captions"]:
        from app.pipeline.captions import margin_v_to_y_frac  # noqa: PLC0415

        caption_y = variant.get("caption_y_frac")
        if variant.get("caption_margin_v") is not None:
            caption_y = margin_v_to_y_frac(variant["caption_margin_v"])
        snapshot["captions"] = {
            "cues": cues,
            "cues_editable": bool(cues),
            "meta": {
                "enabled": variant.get("captions_enabled", True),
                "style": variant.get("voiceover_caption_style")
                or variant.get("caption_style", "sentence"),
                "font": variant.get("voiceover_caption_font", variant.get("caption_font")),
                "y_frac": caption_y,
                "size_px": variant.get("caption_size_px"),
                "color": variant.get("caption_text_color", variant.get("caption_color")),
                "highlight_color": variant.get("caption_highlight_color"),
                "stroke_width": variant.get("caption_stroke_width"),
                "shadow_enabled": variant.get("caption_shadow_enabled"),
                "appearance": variant.get("caption_editor_style"),
            },
            "total_cues": len(cues),
            "truncated": False,
        }
        advertised = {key: True for key in COPILOT_EDITOR_CAPABILITIES if caps.get(key) is True}
        if advertised:
            # Present only when ON, so every other snapshot stays byte-identical.
            snapshot["editor_capabilities"] = advertised
            snapshot["captions"]["meta"]["animation_phases"] = caption_default_phases(variant)
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
            # A real swap candidate list needs a DB-backed music-library
            # lookup this pure (job, variant) function cannot make today —
            # see docs/reviews/copilot-snapshot-parity-2026-09-19.md. Keep
            # swappable False rather than advertise a family the parser can
            # only ever reject (swap_music requires `track_id` to resolve
            # against `music.candidates`).
            "swappable": False,
            "removable": _music_removable(caps, current_track_id),
            "current_track_id": current_track_id,
            "current_track_title": None,
            "candidates": [],
        }
        snapshot["mix"] = {"music_level": variant.get("mix")}
        # KRI-219 Lane C: audio controls the parser gates on. Device-only /
        # bed-only keys are omitted elsewhere so other snapshots stay identical.
        if variant.get("render_destination") == "device":
            snapshot["render_destination"] = "device"
            # Guided-native saves project only `mix.music_level` into the
            # revision (routes/generative_jobs `_project_guided_revision_lanes`);
            # `original_level` would be silently dropped, so it is not offered.
            if not _is_guided_native(job, variant):
                snapshot["mix"]["original_level"] = variant.get("original_audio_level")
        if caps.get("mix") is not True and variant.get("resolved_archetype") in {
            "narrated",
            "voiceover",
        }:
            # KRI-821: the family is open only for the footage level (`original_audio`);
            # the voice/music balance has no control here (Save refuses music_level).
            snapshot["mix"].pop("music_level", None)
            snapshot["mix"]["music_level_locked"] = True
        bed = _background_bed(caps, variant, guided_native=_is_guided_native(job, variant))
        if bed is not None:
            snapshot["mix"]["background_music"] = bed
    audio_notes = _audio_notes(job, variant)
    if audio_notes:
        snapshot["audio_notes"] = audio_notes
    if "sfx" in snapshot["allowed_op_families"]:
        placements = [row for row in variant.get("sound_effects") or [] if isinstance(row, dict)]
        snapshot["sfx"] = {
            "placements": [
                {
                    "index": index,
                    "id": row.get("id"),
                    "label": row.get("label"),
                    "at_s": row.get("at_s"),
                    "gain": row.get("gain", 1.0),
                    "duration_s": row.get("duration_s"),
                    "effect_group_id": row.get("effect_group_id"),
                }
                for index, row in enumerate(placements)
            ],
            # The public sound-effects catalog needs a DB lookup this pure
            # function cannot make (same limitation as music candidates
            # above). An empty catalog is safe, not silently broken: the
            # parser's add_sfx branch requires `effect_id` to resolve via
            # `_id_in_section(..., "sfx", "catalog", "id")`, so add_sfx
            # always fails closed as a clarification — it never reaches
            # compile_editor_ops. patch_sfx/remove_sfx on existing
            # placements work fully without a catalog.
            # Built by the async planner (it owns the DB read) and passed in
            # via clip_context; empty => add_sfx fails closed in the parser.
            "catalog": list(clip_context.get("sfx_catalog") or []),
        }
    if "overlay" in snapshot["allowed_op_families"]:
        snapshot["overlays"] = _overlay_snapshot(
            variant, tilt=caps.get("media_overlay_tilt") is True
        )
    background = _background_snapshot(job, variant, clip_context)
    if background is not None:
        # Present only while the lane has something to say, so every other snapshot is
        # byte-identical to before the Talking background existed (KRI-1117).
        snapshot["background"] = background
    if "effect" in snapshot["allowed_op_families"]:
        snapshot["camera_effects"] = [
            {
                "index": index,
                "id": row.get("id"),
                "start_s": row.get("start_s"),
                "end_s": row.get("end_s"),
                "intensity": row.get("intensity"),
                "effect_group_id": row.get("effect_group_id"),
            }
            for index, row in enumerate(variant.get("camera_effects") or [])
            if isinstance(row, dict)
        ]
    drawings = [row for row in variant.get("drawings") or [] if isinstance(row, dict)]
    if drawings:
        # Read-only context (KRI-589): no op family edits freehand drawings, so the model is
        # only told they exist and when, never given their strokes.
        snapshot["drawings"] = {
            "count": len(drawings),
            "windows": [
                {"start_s": row.get("start_s"), "end_s": row.get("end_s")} for row in drawings
            ],
        }
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
    # KRI-219: server-built snapshots ONLY (the web drawer builds its own in
    # snapshot.ts and never carries this). Unlocks v2 ops + their prompt
    # fragments in EditCopilot; see app/agents/editor_ops_v2.
    snapshot["editor_ops_version"] = 2
    return snapshot


def _slot_duration(row: dict[str, Any]) -> float:
    try:
        value = float(row.get("duration_s") or 0.0)
    except (TypeError, ValueError):
        value = 0.0
    return max(0.0, value)


def _slot_rate(row: dict[str, Any]) -> float:
    """Source seconds consumed per output second (1.0 unless a guided retime)."""
    try:
        value = float(row.get("playback_rate") or 1.0)
    except (TypeError, ValueError):
        value = 1.0
    return value if value > 0 else 1.0


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
            # Only guided rows carry these; omission on the wire means "inherit".
            **{
                key: row[key]
                for key in (
                    "playback_rate",
                    "source_crop",
                    "tilt",
                    "source_start_locked",
                    "source_end_s",
                )
                if key in row
            },
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


def fresh_editor_head_payload(
    head: Any, variant: dict[str, Any], job: Any = None
) -> dict[str, Any]:
    """The head draft's editor payload iff it was built on the variant's CURRENT render.

    The session pointer and ``CreatorEditDraft.base_generation_id`` go stale after an
    editor Save bumps ``render_generation_id``, so they cannot prove freshness. The
    payload's own ``base_generation`` (stamped from ``variant_render_baseline``) can:
    a mismatch means the draft predates a saved render and must not be overlaid on it.
    Returns ``{}`` (build on the saved variant) when absent, non-editor, or stale.
    With ``job``, a phone-published export of the payload's generation is current too.
    """
    snapshot = getattr(head, "snapshot_json", None) if head is not None else None
    if not isinstance(snapshot, dict) or snapshot.get("kind") != "editor":
        return {}
    payload = snapshot.get("editor_payload")
    if not isinstance(payload, dict) or not payload:
        return {}
    base = str(payload.get("base_generation") or "")
    if base != variant_render_baseline(variant):
        from app.services.device_render import published_source_generation  # noqa: PLC0415

        if job is None or not base or base != published_source_generation(job, variant):
            return {}
    return payload


class EditorDraftBasis(NamedTuple):
    draft_id: str
    snapshot_hash: str


def failed_editor_draft_basis(
    head: Any,
    job: Any,
    variant: dict[str, Any],
    *,
    thread_id: Any,
    creator_id: Any,
    item_id: Any,
) -> EditorDraftBasis | None:
    """Bind recovery to an owned canonical editor head at the current generation."""
    from app.kria.drafts import KriaDraftDocument, canonical_snapshot  # noqa: PLC0415
    from app.kria.runtime import RuntimeFailure  # noqa: PLC0415

    if head is None or not (
        head.is_head
        and job.user_id == creator_id
        and job.content_plan_item_id == item_id
        and head.creator_id == creator_id
        and head.thread_id == thread_id
        and head.item_id == item_id
        and head.base_job_id == job.id
        and head.variant_key == str(variant.get("variant_id") or "")
    ):
        return None
    try:
        document = KriaDraftDocument.model_validate(head.snapshot_json)
        if document.kind != "editor" or canonical_snapshot(document)[1] != head.snapshot_hash:
            return None
    except (ValueError, RuntimeFailure):
        return None
    if not variant_render_baseline(variant) or not fresh_editor_head_payload(head, variant, job):
        return None
    return EditorDraftBasis(str(head.id), head.snapshot_hash)


def unknown_editor_render_query(session: Any, job: Any, variant: dict[str, Any]) -> Any:
    from sqlalchemy import or_, select  # noqa: PLC0415

    from app.models import CreatorAgentExecution  # noqa: PLC0415

    return (
        select(CreatorAgentExecution.id)
        .where(
            CreatorAgentExecution.session_id == session.id,
            CreatorAgentExecution.target_job_id == job.id,
            CreatorAgentExecution.status == "outcome_unknown",
            or_(
                CreatorAgentExecution.target_variant_id.is_(None),
                CreatorAgentExecution.target_variant_id == variant.get("variant_id"),
            ),
            or_(
                CreatorAgentExecution.target_generation_id.is_(None),
                CreatorAgentExecution.target_generation_id == variant_render_baseline(variant),
            ),
        )
        .limit(1)
    )


def project_editor_draft(
    variant: dict[str, Any], payload: dict[str, Any], job: Any = None
) -> dict[str, Any]:
    """Overlay a same-generation draft without mutating the rendered Job.

    ``job`` is optional; with it, a guided-native variant keeps its draft slots in
    ``guided_draft_slots`` (rows enriched with media identity and re-windowed on
    the output clock) instead of ``user_timeline``, so the variant is still
    recognised as guided on turn 2+ (KRI-219).
    """
    projected = copy.deepcopy(variant)
    for key in ("text_elements", "caption_cues", "music_track_id", "visual_blocks"):
        if payload.get(key) is not None:
            projected[key] = copy.deepcopy(payload[key])
    if payload.get("visual_blocks") is not None and isinstance(
        projected.get("guided_edit_revision"), dict
    ):
        projected["guided_edit_revision"]["visual_blocks"] = copy.deepcopy(payload["visual_blocks"])
        projected["guided_edit_revision"]["state_hash"] = ""
    guided_draft = (
        payload.get("timeline_slots") is not None
        and job is not None
        and _is_guided_native(job, variant)
    )
    if guided_draft:
        from app.services.kria_editor_timeline import (  # noqa: PLC0415
            project_guided_draft_slots,
        )

        rows, total = project_guided_draft_slots(job, variant, payload["timeline_slots"])
        projected["guided_draft_slots"] = rows
        if total > 0:
            projected["duration_s"] = total
    elif payload.get("timeline_slots") is not None:
        originals = {
            row.get("slot_id"): row for row in _variant_slots(variant) if row.get("slot_id")
        }
        projected["user_timeline"] = {
            "slots": [
                {**originals.get(row.get("slot_id"), {}), **copy.deepcopy(row)}
                for row in payload["timeline_slots"]
            ]
        }
    # KRI-219: full-replacement lanes (same wire shape the snapshot/compile read)
    # so a turn-2 add/patch/remove indexes the turn-1 draft, not the pre-draft list.
    # KRI-589: `drawings` rides along read-only: no op edits it, but a turn-2 snapshot (and the
    # draft a chat edit hands back) must see the creator's unsaved drawings, not drop them.
    for key in ("sound_effects", "camera_effects", "drawings"):
        if payload.get(key) is not None:
            projected[key] = copy.deepcopy(payload[key])
    # KRI-661: a Talking edit's picture cards. The client's unsaved lane wins; otherwise a
    # never-Saved edit derives them from its pinned recipe so chat indexes what Save sees.
    if isinstance(payload.get("media_overlays"), list):
        projected["media_overlays"] = copy.deepcopy(payload["media_overlays"])
    elif job is not None and "media_overlays" not in projected:
        derived = derived_phone_overlays(job, projected)
        if derived is not None:
            projected["media_overlays"] = derived
    # KRI-1117: a Talking edit's background. An explicit key (even `None`: cleared) is the
    # draft's own; otherwise a never-Saved edit derives it from its pinned recipe.
    if "speaker_background" in payload:
        projected["speaker_background"] = copy.deepcopy(payload["speaker_background"])
    elif job is not None and "speaker_background" not in projected:
        derived_background = derived_phone_background(job, projected)
        if derived_background is not None:
            projected["speaker_background"] = derived_background
    mix = payload.get("mix")
    if isinstance(mix, dict):
        if mix.get("music_level") is not None:
            projected["mix"] = mix["music_level"]
        if mix.get("original_level") is not None:
            projected["original_audio_level"] = mix["original_level"]
    bed = payload.get("background_music")
    if isinstance(bed, dict) and isinstance(projected.get("smart_music_treatment"), dict):
        if bed.get("gain_db") is not None:
            projected["smart_music_treatment"]["gain_db"] = bed["gain_db"]
    if payload.get("remove_music"):
        projected["music_track_id"] = None
    for key, value in (payload.get("caption_meta") or {}).items():
        if key != "font_set":
            projected["captions_enabled" if key == "enabled" else f"caption_{key}"] = value
        if key == "y_frac" and isinstance(value, (int, float)) and not isinstance(value, bool):
            from app.pipeline.phone_captions import caption_y_fields  # noqa: PLC0415

            # KRI-1008: the next turn's snapshot reads caption_margin_v (the phone compiler,
            # the style y) before caption_y_frac, so the staged spot must reach both.
            projected.update(caption_y_fields(projected, float(value)))
    return projected


EDITOR_STATE_STALE_REPLY = "Your video changed \u2014 reopen the editor and try again."
SPEECH_CUT_NEEDS_SAVE_REPLY = "Save your edits first, then I can cut the silences."


class EditorStateReplyError(KriaEditorOpError):
    """A state-related refusal with an honest, user-facing reply (not a crash)."""

    reply = ""


class EditorStateStaleError(EditorStateReplyError):
    """The client's unsaved state was built on a render the server has since replaced."""

    reply = EDITOR_STATE_STALE_REPLY


class EditorStateSpeechCutError(EditorStateReplyError):
    """Speech processing renders persisted state; unsaved edits would be lost."""

    reply = SPEECH_CUT_NEEDS_SAVE_REPLY


# Save-only fields a turn's editor state never carries (and the draft never stores).
_STATE_DROP_KEYS = frozenset(
    {
        "copilot_receipt_ids",
        "accepted_suggestion_ids",
        "retry_guided_revision",
        "guided_revision",
        "guided_revision_number",
    }
)


class EditorBase(NamedTuple):
    """What a copilot turn is built on. ``source`` is client_state | head | variant."""

    projected: dict[str, Any]
    prior_payload: dict[str, Any]
    source: str
    fallback_reason: str | None = None


def _state_dict(client_state: Any) -> dict[str, Any]:
    # exclude_unset: only lanes the client actually sent are "present"; Save defaults
    # (remove_music=False, copilot_receipt_ids=[]) are not lanes.
    if hasattr(client_state, "model_dump"):
        return client_state.model_dump(mode="json", exclude_unset=True, exclude_none=True)
    return dict(client_state)


def parse_editor_state(raw: Any) -> Any:
    """Re-validate a persisted turn ``editor_state``; None when absent or unusable."""
    if not raw:
        return None
    from app.kria.api_schemas import EditorStateIn  # noqa: PLC0415 - import cycle

    try:
        return EditorStateIn.model_validate(raw)
    except Exception:  # noqa: BLE001 - stored client input; never crash the turn
        return None


def editor_state_has_lanes(client_state: Any) -> bool:
    """True when the state carries at least one lane (i.e. possibly unsaved edits)."""
    lanes = _state_dict(client_state).get("lanes") or {}
    return any(key not in _STATE_DROP_KEYS and key != "base_generation" for key in lanes)


def client_state_payload(client_state: Any) -> dict[str, Any]:
    """Flat commit-shaped lanes of an ``EditorStateIn`` (present = full value)."""
    state = _state_dict(client_state)
    lanes = {
        key: value
        for key, value in (state.get("lanes") or {}).items()
        if key not in _STATE_DROP_KEYS and value is not None
    }
    # Missing slot IDs mean new placements. Keep that meaning through chat so
    # the Save boundary can assign permanent IDs; fabricated IDs look stale.
    lanes["base_generation"] = str(state.get("base_generation") or "")
    return lanes


def resolve_editor_base(
    job: Any, variant: dict[str, Any], head: Any, client_state: Any = None
) -> EditorBase:
    """THE base a copilot turn edits; the planner snapshot and the compile both call this.

    Precedence:
      1. ``client_state`` whose ``base_generation`` equals the variant's current render
         baseline -> authoritative, the head is IGNORED (even for empty lanes).
         A different baseline raises ``EditorStateStaleError`` (never silently rebased).
         Untrusted: a projection failure falls back to 2/3 with ``fallback_reason``.
      2. the head draft when fresh (``fresh_editor_head_payload``).
      3. the bare saved variant.
    """
    reason: str | None = None
    if client_state is not None:
        base = (
            client_state.base_generation
            if hasattr(client_state, "base_generation")
            else dict(client_state).get("base_generation")
        )
        if str(base or "") != variant_render_baseline(variant):
            raise EditorStateStaleError("editor state is based on an older render")
        try:
            prior = client_state_payload(client_state)
            return EditorBase(project_editor_draft(variant, prior, job), prior, "client_state")
        except Exception as exc:  # noqa: BLE001 - client state is untrusted input
            reason = f"client_state_unusable:{type(exc).__name__}"
    fresh = fresh_editor_head_payload(head, variant, job)
    if fresh:
        return EditorBase(project_editor_draft(variant, fresh, job), fresh, "head", reason)
    return EditorBase(project_editor_draft(variant, {}, job), {}, "variant", reason)


def editor_payload_dump(payload: EditorCommitRequest) -> dict[str, Any]:
    """``payload`` as the flat commit-shaped draft dict (None lanes dropped).

    A cleared Talking background is the one lane whose ``None`` MEANS something: Save reads
    an explicit ``speaker_background: null`` as "clear it", so it survives the None-drop (and
    the draft's merge), where every other absent lane means "leave it".
    """
    data = payload.model_dump(mode="json", exclude_none=True)
    if "speaker_background" in payload.model_fields_set and payload.speaker_background is None:
        data["speaker_background"] = None
    return data


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
    _overlay_sections(merged, previous, current)
    return merged


# Commit-request keys that are metadata, not editable sections.
_NON_SECTION_KEYS = frozenset(
    {
        "base_generation",
        "copilot_receipt_ids",
        "retry_guided_revision",
        "guided_revision",
        "guided_revision_number",
        "remove_music",
    }
)


def _overlay_sections(
    merged: dict[str, Any], previous: dict[str, Any], current: dict[str, Any]
) -> None:
    """Keep the nested `sections` copy in step with the flat commit-shaped keys.

    Opening the editor bootstraps a draft whose editable state lives under
    `sections` (`drafts._editor_snapshot`), and the iOS decoder reads `sections`
    first, ignoring flat keys. Chat ops emit flat keys, so without this overlay
    the phone kept showing the pre-edit state after a chat edit said "Done".
    """
    sections = previous.get("sections")
    if not isinstance(sections, dict):
        return
    updated = dict(sections)
    for key, value in current.items():
        if key in _NON_SECTION_KEYS:
            continue
        updated[key] = merged.get(key, value)
    if current.get("remove_music"):
        updated["music_track_id"] = None
    elif current.get("music_track_id"):
        updated["music_track_id"] = current["music_track_id"]
    merged["sections"] = updated


_MAX_CLIP_LABELS = 80  # fast_cuts allows 80 cuts
_LABEL_DEFAULTS = {
    "role": "generative_intro",
    "position": "custom",
    "x_frac": 0.5,
    "y_frac": 0.78,
    "size_px": 58,
    "color": "#FFF8F0",
    "highlight_color": "#D9FF70",
    "effect": "static",
    "alignment": "center",
    "max_width_frac": 0.82,
}
_LABEL_STYLE_KEYS = (
    "font_family",
    "size_px",
    "color",
    "highlight_color",
    "stroke_width",
    "shadow_enabled",
    "shadow_style",
    "x_frac",
    "y_frac",
    "position",
    "alignment",
    "effect",
    "max_width_frac",
)


def _apply_clip_labels(
    op: dict[str, Any],
    text: list[dict[str, Any]],
    slots: list[dict[str, Any]],
    removed_text_bars: set[int],
) -> None:
    """Apply the parser-resolved ``label_each_clip`` plan to the text lane.

    ``op["labels"]`` is built by EditCopilot's parser from the snapshot's clip
    facts (creator text > landmark > place, consecutive repeats dropped) and is
    NEVER model-authored text. An existing label bar keeps its style and timing
    and only has its text replaced; a clip with no label bar gets a new one on
    that clip's output window, styled like a sibling label (or the guided
    defaults). Nothing is ever removed.
    """
    labels = op.get("labels")
    if not isinstance(labels, list) or not labels or len(labels) > _MAX_CLIP_LABELS:
        raise KriaEditorOpError("No grounded clip labels were supplied")
    by_id = {row.get("id"): row for row in text if isinstance(row.get("id"), str)}
    sibling = next(
        (
            row
            for row in text
            if str(row.get("id") or "").startswith(_CLIP_LABEL_BAR_PREFIX)
            and id(row) not in removed_text_bars
        ),
        None,
    )
    style = {
        **_LABEL_DEFAULTS,
        **{key: sibling[key] for key in _LABEL_STYLE_KEYS if sibling and key in sibling},
    }
    if sibling is None:
        title = by_id.get("guided-title")
        if title and title.get("font_family"):
            style["font_family"] = title["font_family"]
        else:
            style["font_family"] = "DM Sans"
    slot_by_media = {
        str(row.get("media_id")): row
        for row in slots
        if row.get("media_id") and not row.get("removed")
    }
    seen_media: set[str] = set()
    seen_bars: set[str] = set()
    for entry in labels:
        if not isinstance(entry, dict):
            raise KriaEditorOpError("A clip label changed before this edit could be drafted")
        value = " ".join(str(entry.get("text") or "").split())
        media_id = str(entry.get("media_id") or "")
        if not value or len(value) > 120 or not media_id:
            raise KriaEditorOpError("A clip label changed before this edit could be drafted")
        # One label per clip: the same media in two segments must never produce
        # two bars (or two writes to one bar).
        if media_id in seen_media:
            raise KriaEditorOpError("A clip can only be labelled once")
        seen_media.add(media_id)
        bar_id = entry.get("bar_id")
        if bar_id:
            if bar_id in seen_bars:
                raise KriaEditorOpError("A clip label bar can only be updated once")
            seen_bars.add(bar_id)
            row = by_id.get(bar_id)
            if row is None or id(row) in removed_text_bars:
                raise KriaEditorOpError("Text changed before this edit could be drafted")
            row["text"] = value
            continue
        slot = slot_by_media.get(media_id)
        start = entry.get("start_s")
        end = entry.get("end_s")
        if slot is not None and (start is None or end is None):
            start, end = slot.get("output_start_s"), slot.get("output_end_s")
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or end <= start:
            raise KriaEditorOpError("That clip is no longer on the timeline")
        new_id = f"{_CLIP_LABEL_MEDIA_PREFIX}{media_id}"
        if new_id in by_id:
            raise KriaEditorOpError("That clip already has a label bar")
        by_id[new_id] = {}
        text.append(
            {
                **style,
                "id": new_id,
                "text": value,
                "start_s": round(float(start), 3),
                "end_s": round(float(end), 3),
            }
        )


def coalesce_text_style_ops(ops: list[dict]) -> list[dict]:
    """Merge per-bar ``patch_text_style`` ops that carry the identical patch.

    "Change all fonts" arrives as one op per bar (13 on the 2026-09-25 East Run
    thread) and would blow the eight-op bundle cap, which counts operations,
    not bars. A merged op addresses every bar through ``bar_indexes``.

    Only a run of adjacent ``patch_text_style`` ops is merged, and a bar that
    already appeared in the run is never folded into an earlier group, so the
    result is byte-identical to applying the ops one by one (a later patch on
    the same bar still wins).
    """

    def _mergeable(op: dict) -> bool:
        index = op.get("bar_index")
        return (
            op.get("op") == "patch_text_style"
            and isinstance(op.get("patch"), dict)
            and isinstance(index, int)
            and not isinstance(index, bool)
            and "bar_indexes" not in op
        )

    merged: list[dict] = []
    by_patch: dict[str, dict] = {}
    seen: set[int] = set()
    for op in ops:
        if not _mergeable(op):
            merged.append(op)
            by_patch, seen = {}, set()
            continue
        index = op["bar_index"]
        key = json.dumps(op["patch"], sort_keys=True, default=str)
        existing = by_patch.get(key)
        if existing is not None and index not in seen:
            existing["bar_indexes"].append(index)
        else:
            grouped = {**op, "bar_indexes": [index]}
            by_patch.setdefault(key, grouped)
            merged.append(grouped)
        seen.add(index)
    return merged


@dataclass
class _DraftState:
    """Mutable working state one compile pass threads through op handlers.

    Handlers mutate this in place (and may REASSIGN text / slots);
    compile_editor_ops builds the commit request from it at the end.
    changed names the lanes that reach the payload ("text", "timeline",
    "captions", "caption_meta", "mix", "music", "title", "camera_effects",
    "sound_effects", "visual_media", "media_overlays", "speaker_background").
    """

    job: Any
    variant: dict[str, Any]
    text: list[dict[str, Any]]
    # `bar_index` always addresses the TEXT BARS list the model was shown
    # (`build_editor_snapshot`), never the list as it shrinks mid-bundle. The
    # web drawer resolves the same way (`textSnapAt` + DELETE_BAR by id);
    # popping in place made "remove bars 1-4" delete bars 1, 3 and 5 and then
    # reject bar 4 as out of range (2026-09-19 phone chat-edit incident).
    text_bars: list[dict[str, Any]]
    captions: list[dict[str, Any]]
    camera_effects: list[dict[str, Any]]
    sound_effects: list[dict[str, Any]]
    slots: list[dict[str, Any]]
    base_generation: Any
    # Slots as they were before this bundle's ops (guided label rebase reads it).
    initial_slots: list[dict[str, Any]] = field(default_factory=list)
    # Snapshot indices keep targeting these objects after a reorder, like the web drawer.
    slot_targets: list[dict[str, Any]] = field(default_factory=list)
    removed_text_bars: set[int] = field(default_factory=set)
    changed: set[str] = field(default_factory=set)
    changes: list[str] = field(default_factory=list)
    caption_patch: dict[str, Any] = field(default_factory=dict)
    mix_level: float | None = None
    original_level: float | None = None
    music_gain_db: float | None = None
    background_track_id: str | None = None
    remove_music: bool = False
    music_track_id: str | None = None
    title: str | None = None
    visual_blocks: list[dict[str, Any]] | None = None
    # A handler may set this to replace the generic "Op name" change summary
    # (e.g. "Rewrite 5 texts"); compile_editor_ops consumes and clears it.
    summary: str | None = None
    # Stable text IDs whose output windows were authored in this bundle.
    explicit_text_ids: set[str] = field(default_factory=set)
    # Slide-post surface only (KRI-301): non-timeline outputs of the slides lane
    # (`cover_slide_id`, `caption`). Empty for every video compile.
    post: dict[str, Any] = field(default_factory=dict)
    # Phone Talking picture cards (KRI-661); reaches the payload as `media_overlays`.
    media_overlays: list[dict[str, Any]] = field(default_factory=list)
    # Phone Talking background (KRI-1117): the document `set_background` chose, None once
    # `remove_background` cleared it. Reaches the payload only when "speaker_background" is in
    # `changed`, so an untouched lane is absent (leave unchanged), never an explicit clear.
    speaker_background: dict[str, Any] | None = None

    def text_bar(self, index: object) -> dict[str, Any]:
        row = self.text_bars[_require_index(self.text_bars, index, "Text")]
        if id(row) in self.removed_text_bars:
            raise KriaEditorOpError("Text changed before this edit could be drafted")
        return row


OpHandler = Callable[[_DraftState, dict[str, Any]], None]


def _op_remove_visual_media(state: _DraftState, op: dict[str, Any]) -> None:
    targets = op.get("target_ids")
    if (
        not isinstance(targets, list)
        or not targets
        or len(targets) > 100
        or any(not isinstance(value, str) or not value for value in targets)
        or len(set(targets)) != len(targets)
    ):
        raise KriaEditorOpError("Visual media removal requires unique existing target IDs")
    allowed = {row["id"] for row in _removable_visual_media(state.job, state.variant)}
    current = (
        state.visual_blocks
        if state.visual_blocks is not None
        else _visual_media_rows(state.job, state.variant)
    )
    present = {row.get("id") for row in current if isinstance(row, dict)}
    if not set(targets).issubset(allowed & present):
        raise KriaEditorOpError("The selected visual media is no longer removable")
    state.visual_blocks = [row for row in current if row.get("id") not in set(targets)]
    state.changed.add("visual_media")


def _op_edit_text(state: _DraftState, op: dict[str, Any]) -> None:
    from app.services.kria_editor_ops_text import keep_authored_breaks  # noqa: PLC0415

    bar = state.text_bar(op.get("bar_index"))
    bar["text"] = str(op["text"])
    keep_authored_breaks(bar)
    state.changed.add("text")


def _op_patch_text_style(state: _DraftState, op: dict[str, Any]) -> None:
    indexes = op.get("bar_indexes")
    if not isinstance(indexes, list) or not indexes:
        indexes = [op.get("bar_index")]
    patch = {
        key: value
        for key, value in dict(op.get("patch") or {}).items()
        if key in _TEXT_STYLE_FIELDS
    }
    if not patch:
        raise KriaEditorOpError("No portable text style fields were supplied")
    for index in indexes:
        bar = state.text_bar(index)
        bar.update(patch)
        if patch.get("position") not in (None, "custom"):
            # A named position ignores x/y fractions on the burn, but the editors
            # draw y_frac first: drop stale ones so the preview matches.
            bar.pop("x_frac", None)
            bar.pop("y_frac", None)
    state.changed.add("text")


def _op_set_text_timing(state: _DraftState, op: dict[str, Any]) -> None:
    bar = state.text_bar(op.get("bar_index"))
    updates = {}
    for key in ("start_s", "end_s"):
        if key in op:
            value = float(op[key])
            if not math.isfinite(value):
                raise KriaEditorOpError("Text timing must be finite")
            updates[key] = value
    bar.update(updates)
    if isinstance(bar.get("id"), str):
        state.explicit_text_ids.add(bar["id"])
    state.changed.add("text")


def _explicit_text_timing_ids(
    initial_text: list[dict[str, Any]], current_text: list[dict[str, Any]]
) -> set[str]:
    """Find bars whose output window was authored by this bundle.

    Stable IDs let text timing and timeline edits compose without making the
    compiler infer intent from operation ordering. Newly added bars are
    explicit by definition; existing bars are explicit only when their timing
    differs from the pre-bundle snapshot (including a newly supplied bound).
    """
    before = {
        str(row.get("id")): row for row in initial_text if isinstance(row, dict) and row.get("id")
    }
    explicit: set[str] = set()
    for row in current_text:
        bar_id = row.get("id") if isinstance(row, dict) else None
        if not isinstance(bar_id, str) or not bar_id:
            continue
        previous = before.get(bar_id)
        if previous is None or any(
            row.get(key) != previous.get(key) for key in ("start_s", "end_s")
        ):
            explicit.add(bar_id)
    return explicit


def _text_ids(rows: list[dict[str, Any]]) -> set[str]:
    return {
        row["id"]
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"]
    }


def _validate_timing_numbers(op: dict[str, Any]) -> None:
    if op.get("op") not in {
        "set_text_timing",
        "set_texts_timing",
        "retime_text_sequence",
        "add_text",
    }:
        return
    for key in ("start_s", "end_s", "shift_s"):
        if key in op and not math.isfinite(float(op[key])):
            raise KriaEditorOpError("Text timing must be finite")


def _op_add_text(state: _DraftState, op: dict[str, Any]) -> None:
    if any(key in op for key in ("style_from", "patch", "clip_id", "research")):
        # KRI-219 v2 extension (style copy / patch / clip-linked label).
        from app.services.kria_editor_ops_text import add_text_v2  # noqa: PLC0415

        add_text_v2(state, op)
        return
    from app.services.kria_editor_ops_text import keep_authored_breaks  # noqa: PLC0415

    row = {
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
    keep_authored_breaks(row)
    state.text.append(row)
    state.explicit_text_ids.add(str(state.text[-1]["id"]))
    state.changed.add("text")


def _op_remove_text(state: _DraftState, op: dict[str, Any]) -> None:
    state.removed_text_bars.add(id(state.text_bar(op.get("bar_index"))))
    state.text = [row for row in state.text if id(row) not in state.removed_text_bars]
    state.changed.add("text")


def _op_label_each_clip(state: _DraftState, op: dict[str, Any]) -> None:
    _apply_clip_labels(op, state.text, state.slots, state.removed_text_bars)
    if op.get("label_from") == "capture_time":
        count = len(op.get("labels") or [])
        state.summary = f"{'Add filming time to' if op.get('mode') == 'append' else 'Label'} " + (
            f"{count} clip{'s' if count != 1 else ''}"
        )
    state.changed.add("text")


def _op_patch_text_appearance(state: _DraftState, op: dict[str, Any]) -> None:
    patch = {
        key: value
        for key, value in dict(op.get("patch") or {}).items()
        if key in _TEXT_APPEARANCE_FIELDS
    }
    targets = list(op.get("target_ids") or [])
    if not patch or not targets:
        raise KriaEditorOpError("No text appearance change was supplied")
    if "font_family" in patch and patch["font_family"] not in _ALLOWED_FONTS:
        raise KriaEditorOpError("That font is not available")
    live = {row.get("id"): row for row in state.text if isinstance(row.get("id"), str)}
    for target_id in targets:
        row = live.get(target_id)
        if row is None or id(row) in state.removed_text_bars:
            raise KriaEditorOpError("Text changed before this edit could be drafted")
        row.update(patch)
    state.changed.add("text")


def _snapshot_slot_index(state: _DraftState, index: object) -> int:
    targets = state.slot_targets or state.slots
    target = targets[_require_index(targets, index, "Timeline")]
    for current, row in enumerate(state.slots):
        if row is target:
            return current
    # Splitting replaces the left-hand object but retains its durable slot ID.
    key = target.get("slot_id")
    matches = [i for i, row in enumerate(state.slots) if key and row.get("slot_id") == key]
    if len(matches) == 1:
        return matches[0]
    raise KriaEditorOpError("Timeline target changed before this edit could be drafted")


def _fit_slots_to_footage(state: _DraftState, before: list[dict[str, Any]]) -> None:
    """Make every window chat changed one the Save accepts (KRI-772).

    ``set_clip_duration`` / ``set_clip_in`` write what was asked. Asked for 3 s on a 3.36 s clip
    that starts at 0.9 s, they staged a window that needs 3.9 s of footage, and the creator's
    Save was refused. Chat owns the invariant: ``in_s + duration * rate <= source_duration_s``
    (the Save's own bound, with its 50 ms slack left to the Save). Shorten first; slide the start
    back only when less than the editor minimum would remain. A window that already fit, a clip
    whose footage length is unknown, and a clip chat did not change are never touched. The
    summary says what was done, so the creator is not surprised by a shorter clip.
    """
    from app.services.authored_editor import SOURCE_BOUNDS_TOLERANCE_S  # noqa: PLC0415

    previous = {
        (row.get("slot_id") or id(row)): (row.get("in_s"), row.get("duration_s")) for row in before
    }
    visible = 0
    for row in state.slots:
        if row.get("removed"):
            continue
        visible += 1
        source = row.get("source_duration_s")
        if source is None or row.get("media_kind") == "image" or row.get("kind") == "image":
            continue
        try:
            source = float(source)
        except (TypeError, ValueError):
            continue
        rate = _slot_rate(row)
        in_s = max(0.0, float(row.get("in_s") or 0.0))
        duration = _slot_duration(row)
        if duration <= 0 or in_s + duration * rate <= source + SOURCE_BOUNDS_TOLERANCE_S:
            continue
        if previous.get(row.get("slot_id") or id(row)) == (row.get("in_s"), row.get("duration_s")):
            continue  # chat did not touch this window; the Save's own refusal stays the verdict
        available = (source - in_s) / rate
        if not below_editor_minimum(available, EDITOR_MIN_CLIP_S):
            fitted_in, fitted = in_s, min(duration, available)
        else:
            fitted = min(duration, source / rate)
            fitted_in = max(0.0, source - fitted * rate)
        fitted = math.floor(fitted * 1e6) / 1e6
        row["in_s"] = round(fitted_in, 6)
        row["duration_s"] = fitted
        row["duration_beats"] = None
        if "source_end_s" in row:
            row["source_end_s"] = round(fitted_in + fitted * rate, 6)
        state.changes.append(
            f"Clip {visible} only has {fitted:.2f}s of footage, so it is that long"
        )


def _op_set_clip_duration(state: _DraftState, op: dict[str, Any]) -> None:
    name = str(op.get("op") or "")
    index = _snapshot_slot_index(state, op.get("slot_index"))
    row = state.slots[index]
    if name == "set_clip_duration":
        row["duration_beats"] = None
        row["duration_s"] = float(op["duration_s"])
        if "source_end_s" in row:
            row["source_end_s"] = float(row.get("in_s") or 0) + _slot_duration(row) * _slot_rate(
                row
            )
    elif name == "set_clip_in":
        span = (
            float(row["source_end_s"]) - float(row.get("in_s") or 0)
            if row.get("source_end_s") is not None
            else None
        )
        row["in_s"] = float(op["in_s"])
        if span is not None:
            row["source_end_s"] = row["in_s"] + span
        row["source_start_locked"] = True
    elif name == "trim_clip_start":
        amount = min(float(op["start_s"]), max(0.0, _slot_duration(row) - 0.1))
        row["in_s"] = float(row.get("in_s") or 0.0) + amount * _slot_rate(row)
        row["source_start_locked"] = True
        row["duration_beats"] = None
        row["duration_s"] = _slot_duration(row) - amount
    else:
        row["look_preset"] = op["look_preset"]
        row["look_adjustments"] = None
    state.changed.add("timeline")


def _op_trim_output_start(state: _DraftState, op: dict[str, Any]) -> None:
    remaining = float(op["start_s"])
    if remaining <= 0:
        raise KriaEditorOpError("The requested output trim has no effect")
    for row in state.slots:
        if row.get("removed"):
            continue
        duration = _slot_duration(row)
        if remaining >= duration - 0.1:
            row["removed"] = True
            remaining -= duration
            continue
        if remaining > 0:
            row["in_s"] = float(row.get("in_s") or 0.0) + remaining * _slot_rate(row)
            row["duration_beats"] = None
            row["duration_s"] = duration - remaining
            remaining = 0
        break
    if not any(not row.get("removed") for row in state.slots):
        raise KriaEditorOpError("The trim would remove the whole video")
    state.changed.add("timeline")


def _op_reorder_clip(state: _DraftState, op: dict[str, Any]) -> None:
    source = _snapshot_slot_index(state, op.get("from_index"))
    target = _snapshot_slot_index(state, op.get("to_index"))
    row = state.slots.pop(source)
    state.slots.insert(target, row)
    state.changed.add("timeline")


def _op_remove_clip(state: _DraftState, op: dict[str, Any]) -> None:
    index = _snapshot_slot_index(state, op.get("slot_index"))
    if sum(not row.get("removed") for row in state.slots) <= 1:
        raise KriaEditorOpError("The final clip cannot be removed")
    state.slots[index]["removed"] = True
    state.changed.add("timeline")


def _op_split_clip(state: _DraftState, op: dict[str, Any]) -> None:
    index = _snapshot_slot_index(state, op.get("slot_index"))
    row = state.slots[index]
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
        "in_s": float(row.get("in_s") or 0.0) + local * _slot_rate(row),
        "duration_beats": None,
        "duration_s": duration - local,
    }
    if "source_end_s" in row:
        left["source_end_s"] = right["in_s"]
    state.slots[index : index + 1] = [left, right]
    state.changed.add("timeline")


def _op_set_transition(state: _DraftState, op: dict[str, Any]) -> None:
    active = [index for index, row in enumerate(state.slots) if not row.get("removed")]
    boundary = _require_index(active[:-1], op.get("boundary_index"), "Transition")
    row = state.slots[active[boundary]]
    row["transition_after"] = op["transition"]
    row["transition_duration_s"] = (
        None if op["transition"] == "cut" else float(op.get("duration_s") or 0.3)
    )
    state.changed.add("timeline")


def _op_add_unused_sources(state: _DraftState, op: dict[str, Any]) -> None:
    selector = op.get("selector") or {}
    wanted = str(selector.get("media_kind") or "all")
    paths = list((state.job.all_candidates or {}).get("clip_paths") or [])
    used = {int(row.get("clip_index")) for row in state.slots if not row.get("removed")}
    durations = {
        int(row["clip_index"]): float(row["duration_s"])
        for row in _source_pool_rows(state.job, state.variant)
        if row.get("clip_index") is not None and row.get("duration_s") is not None
    }
    for index, path in enumerate(paths):
        kind = _path_kind(path)
        if index in used or (wanted != "all" and wanted != kind):
            continue
        state.slots.append(
            {
                "slot_id": None,
                "clip_index": index,
                "in_s": 0.0,
                "duration_beats": None,
                "duration_s": 3.0 if kind == "image" else min(3.0, durations.get(index, 3.0)),
                "removed": False,
                "transition_after": "cut",
                "look_preset": "none",
            }
        )
    state.changed.add("timeline")


def _op_set_media_duration(state: _DraftState, op: dict[str, Any]) -> None:
    name = str(op.get("op") or "")
    selector = op.get("selector") or {}
    wanted = str(selector.get("media_kind") or "image")
    paths = list((state.job.all_candidates or {}).get("clip_paths") or [])
    selected = [
        index
        for index, row in enumerate(state.slots)
        if not row.get("removed")
        and int(row.get("clip_index")) < len(paths)
        and (wanted == "all" or _path_kind(paths[int(row.get("clip_index"))]) == wanted)
    ]
    if name == "set_media_duration":
        for index in selected:
            state.slots[index]["duration_beats"] = None
            state.slots[index]["duration_s"] = float(op["duration_s"])
    elif selected:
        first = selected[0]
        selected_rows = [state.slots[index] for index in selected]
        state.slots = [row for index, row in enumerate(state.slots) if index not in set(selected)]
        state.slots[first:first] = selected_rows
    state.changed.add("timeline")


def _op_edit_caption(state: _DraftState, op: dict[str, Any]) -> None:
    index = _require_index(state.captions, op.get("cue_index"), "Caption")
    state.captions[index]["text"] = str(op["text"])
    state.changed.add("captions")


def _op_replace_caption_text(state: _DraftState, op: dict[str, Any]) -> None:
    find = str(op["find"])
    replace = str(op["replace"])
    replaced = 0
    for row in state.captions:
        current = str(row.get("text") or "")
        updated = current.replace(find, replace)
        if updated != current:
            row["text"] = updated
            replaced += 1
    if not replaced:
        raise KriaEditorOpError(f'No captions contain "{find}"')
    state.changed.add("captions")


def _op_set_caption_timing(state: _DraftState, op: dict[str, Any]) -> None:
    name = str(op.get("op") or "")
    index = _require_index(state.captions, op.get("cue_index"), "Caption")
    if name == "set_caption_timing":
        state.captions[index].update({key: op[key] for key in ("start_s", "end_s") if key in op})
    else:
        state.captions[index]["smart_emphasis"] = bool(op["emphasis"])
        if not op["emphasis"]:
            state.captions[index]["smart_style"] = None
    state.changed.add("captions")


def _op_set_caption_meta(state: _DraftState, op: dict[str, Any]) -> None:
    patch = dict(op.get("patch") or {})
    if "animation_phases" in patch:
        from app.services.kria_editor_ops_text import merge_animation_phases  # noqa: PLC0415

        current = state.caption_patch.get("animation_phases") or caption_default_phases(
            state.variant
        )
        patch["animation_phases"] = merge_animation_phases(
            {"animation_phases": current}, patch["animation_phases"]
        )
        # All captions means every line, as on the phone (`writeCaptionPhases(.all)`):
        # a line's own phases would otherwise win over the new default.
        for lane, rows in (
            ("captions", state.captions),
            ("text", [row for row in state.text if is_caption_text_bar(row)]),
        ):
            for row in rows:
                if row.pop("animation_phases", None) is not None:
                    state.changed.add(lane)
    state.caption_patch.update(patch)
    state.changed.add("caption_meta")


def _op_set_mix(state: _DraftState, op: dict[str, Any]) -> None:
    state.mix_level = float(op["music_level"])
    state.changed.add("mix")


def _op_remove_music(state: _DraftState, op: dict[str, Any]) -> None:
    state.remove_music = True
    state.changed.add("music")


def _op_swap_music(state: _DraftState, op: dict[str, Any]) -> None:
    state.music_track_id = str(op["track_id"])
    state.changed.add("music")


def _op_set_title(state: _DraftState, op: dict[str, Any]) -> None:
    state.title = str(op["title"])
    state.changed.add("title")


def _op_add_camera_effect(state: _DraftState, op: dict[str, Any]) -> None:
    easing = resolve_easing(op.get("easing"))
    state.camera_effects.append(
        {
            "id": f"kria-{uuid.uuid4().hex}",
            "start_s": float(op["start_s"]),
            "end_s": float(op["end_s"]),
            "intensity": float(op.get("intensity", easing_bounds(easing).default_intensity)),
            "easing": easing,
            "effect_group_id": op.get("effect_bundle_id"),
        }
    )
    state.changed.add("camera_effects")


def _op_patch_camera_effect(state: _DraftState, op: dict[str, Any]) -> None:
    name = str(op.get("op") or "")
    index = _require_index(state.camera_effects, op.get("camera_effect_index"), "Camera effect")
    if name == "remove_camera_effect":
        state.camera_effects.pop(index)
    else:
        patch = {
            key: value
            for key, value in dict(op).items()
            if key in {"start_s", "end_s", "intensity", "easing"}
        }
        if "easing" in patch:
            patch["easing"] = resolve_easing(patch["easing"])
        if not patch:
            raise KriaEditorOpError("No portable camera effect field was supplied")
        state.camera_effects[index].update(patch)
    state.changed.add("camera_effects")


def _op_patch_sfx(state: _DraftState, op: dict[str, Any]) -> None:
    name = str(op.get("op") or "")
    index = _require_index(state.sound_effects, op.get("sfx_index"), "Sound effect")
    if name == "remove_sfx":
        state.sound_effects.pop(index)
    else:
        patch = {key: value for key, value in dict(op).items() if key in {"at_s", "gain"}}
        if not patch:
            raise KriaEditorOpError("No portable sound effect field was supplied")
        state.sound_effects[index].update(patch)
    state.changed.add("sound_effects")


_OP_HANDLERS: dict[str, OpHandler] = {
    "remove_visual_media": _op_remove_visual_media,
    "edit_text": _op_edit_text,
    "patch_text_style": _op_patch_text_style,
    "set_text_timing": _op_set_text_timing,
    "add_text": _op_add_text,
    "remove_text": _op_remove_text,
    "label_each_clip": _op_label_each_clip,
    "patch_text_appearance": _op_patch_text_appearance,
    "set_clip_duration": _op_set_clip_duration,
    "set_clip_in": _op_set_clip_duration,
    "trim_clip_start": _op_set_clip_duration,
    "set_look_preset": _op_set_clip_duration,
    "trim_output_start": _op_trim_output_start,
    "reorder_clip": _op_reorder_clip,
    "remove_clip": _op_remove_clip,
    "split_clip": _op_split_clip,
    "set_transition": _op_set_transition,
    "add_unused_sources": _op_add_unused_sources,
    "set_media_duration": _op_set_media_duration,
    "stack_images": _op_set_media_duration,
    "edit_caption": _op_edit_caption,
    "replace_caption_text": _op_replace_caption_text,
    "set_caption_timing": _op_set_caption_timing,
    "set_caption_emphasis": _op_set_caption_timing,
    "set_caption_meta": _op_set_caption_meta,
    "set_mix": _op_set_mix,
    "remove_music": _op_remove_music,
    "swap_music": _op_swap_music,
    "set_title": _op_set_title,
    "add_camera_effect": _op_add_camera_effect,
    "patch_camera_effect": _op_patch_camera_effect,
    "remove_camera_effect": _op_patch_camera_effect,
    "patch_sfx": _op_patch_sfx,
    "remove_sfx": _op_patch_sfx,
}


def register_handler(name: str, fn: OpHandler, *, replace: bool = False) -> None:
    """Register a compile handler for op `name` (lane modules call this).

    Raises on a duplicate name unless `replace` is true, so two lanes can
    never silently shadow each other.
    """
    if name in _OP_HANDLERS and not replace:
        raise ValueError(f"editor op handler already registered: {name}")
    _OP_HANDLERS[name] = fn


def _load_lane_handlers() -> None:
    from app.agents.editor_ops_v2 import register_all_handlers  # noqa: PLC0415

    register_all_handlers()


def compile_editor_ops(
    job: Any, variant: dict[str, Any], ops: list[dict], *, preserve_text: bool = False
) -> CompiledEditorDraft:
    """Compile one all-or-nothing portable operation bundle.

    The input is expected to have passed EditCopilot's schema parser.  This
    function nevertheless rejects unknown operations and rechecks indexes,
    bounds, and bundle effects against the authoritative in-memory variant.
    """

    _load_lane_handlers()
    preserve_text = preserve_text or any(op.get("preserve_text") is True for op in ops)
    if preserve_text:
        from app.agents.editor_ops_v2 import lane_specs

        text_ops = {
            "edit_text",
            "patch_text_style",
            "set_text_timing",
            "add_text",
            "remove_text",
            "label_each_clip",
            "patch_text_appearance",
        } | {spec.name for spec in lane_specs() if "text" in spec.family}
        ops = [op for op in ops if op.get("op") not in text_ops]
    if not ops:
        raise KriaEditorOpError("No safe draft change was produced")
    ops = coalesce_text_style_ops(ops)
    duration_edit = any(
        op.get("op") == "set_total_duration"
        or (
            op.get("op") == "patch_slots"
            and bool(op.get("patch"))
            and set(op["patch"]) <= {"duration_s"}
        )
        for op in ops
    )
    if len(ops) > MAX_EDITOR_OPS:
        raise KriaEditorOpError(f"A draft may contain at most {MAX_EDITOR_OPS} editor operations")
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
            diff=speech_cut_diff(),
        )

    # Storyboard bars carry their burned look, the same rows the editor shows,
    # so a chat edit never hands the editor the bare presets back.
    text = copy.deepcopy(
        [
            row
            for row in resolve_narrated_storyboard_rows(variant.get("text_elements"))
            if isinstance(row, dict)
        ]
    )
    state = _DraftState(
        job=job,
        variant=variant,
        text=text,
        text_bars=list(text),
        captions=copy.deepcopy(
            [row for row in variant.get("caption_cues") or [] if isinstance(row, dict)]
        ),
        camera_effects=copy.deepcopy(
            [row for row in variant.get("camera_effects") or [] if isinstance(row, dict)]
        ),
        sound_effects=copy.deepcopy(
            [row for row in variant.get("sound_effects") or [] if isinstance(row, dict)]
        ),
        slots=_variant_slots(variant, job),
        base_generation=variant_render_baseline(variant),
        media_overlays=copy.deepcopy(
            [row for row in variant.get("media_overlays") or [] if isinstance(row, dict)]
        ),
    )
    state.initial_slots = copy.deepcopy(state.slots)
    state.slot_targets = list(state.slots)
    initial_text = copy.deepcopy(state.text)
    try:
        before = snapshot_before(state)
    except Exception:  # noqa: BLE001 - the diff only informs receipts; it must never block a draft
        log.warning("kria_editor_diff_snapshot_failed", exc_info=True)
        before = None
    before_text = copy.deepcopy(state.text)
    before_captions = copy.deepcopy(state.captions)
    before_camera = copy.deepcopy(state.camera_effects)
    before_sfx = copy.deepcopy(state.sound_effects)
    before_overlays = copy.deepcopy(state.media_overlays)

    canonical_duration_s = None
    sequence_ops = []
    for op in ops:
        name = str(op.get("op") or "")
        handler = _OP_HANDLERS.get(name)
        if handler is None:
            raise KriaEditorOpError(f"{name or 'Unknown operation'} is not portable to Kria yet")
        _validate_timing_numbers(op)
        if name == "retime_text_sequence":
            sequence_ops.append(op)
            continue
        before_ids = _text_ids(state.text)
        handler(state, op)
        state.explicit_text_ids.update(_text_ids(state.text) - before_ids)
        if name in {"set_texts_timing", "retime_text_sequence"}:
            state.explicit_text_ids.update(
                value for value in op.get("target_ids", []) if isinstance(value, str)
            )
        state.changes.append(state.summary or _summary(op))
        state.summary = None

    if "timeline" in state.changed:
        _fit_slots_to_footage(state, state.initial_slots)

    if sequence_ops:
        from app.services.authored_text_fit import _segments
        from app.services.kria_editor_timeline import guided_segments_for_rows

        final_layout = _guided_v2_revision(job, variant)
        segments = (
            guided_segments_for_rows(final_layout, state.slots)
            if final_layout is not None
            else _segments(state.slots)
        )
        total = max((float(row["output_end_s"]) for row in segments), default=0.0)
        canonical_duration_s = total
        for op in sequence_ops:
            effective_op = dict(op)
            if op.get("fit_to_video"):
                effective_op["start_s"], effective_op["end_s"] = 0.0, total
            _validate_timing_numbers(effective_op)
            _OP_HANDLERS["retime_text_sequence"](state, effective_op)
            state.explicit_text_ids.update(op.get("target_ids", []))
            state.changes.append(state.summary or _summary(op))
            state.summary = None
        targets = [float(op["target_s"]) for op in ops if op.get("op") == "set_total_duration"]
        if targets and any(op.get("fit_to_video") for op in sequence_ops):
            rounded_target = round(targets[-1] * 30) / 30
            if abs(total - rounded_target) > 0.001:
                raise TextTimingConflict(
                    targets[-1],
                    total,
                    tuple(value for op in sequence_ops for value in op.get("target_ids", [])),
                    constraint="duration",
                )

    if "text" in state.changed:
        from app.services.kria_editor_ops_text import normalize_text_relations

        normalize_text_relations(state)

    if "text" in state.changed:
        # An op may set a named position (or clear the size/face) on a storyboard
        # bar; spell its burned look out again so the draft previews where it burns.
        for row in state.text:
            row.update(narrated_storyboard_row_updates(row))

    changed = state.changed
    if preserve_text and state.text != initial_text:
        raise KriaEditorOpError("This request keeps the text unchanged; no text change was applied")
    text_timing = (
        "preserve_all" if preserve_text else "preserve_absolute" if duration_edit else "follow"
    )
    guided = _guided_v2_revision(job, variant)
    if "timeline" in changed and guided is not None and _is_guided_native(job, variant):
        from app.services.kria_editor_timeline import rebase_guided_text  # noqa: PLC0415

        # Invariant: a guided payload carrying timeline_slots ALWAYS carries the
        # rebased text_elements, so the server treats text as authored and skips
        # its (right-biased, non-label-aware) own projection.
        state.explicit_text_ids.update(_explicit_text_timing_ids(initial_text, state.text))
        rebase_guided_text(state, guided, text_timing=text_timing)
    elif (
        "timeline" in changed
        and _is_authored_phone_native(job, variant)
        and (
            text_timing != "follow"
            or any(
                str(op.get("op") or "") == "patch_slots"
                and "playback_rate" in dict(op.get("patch") or {})
                for op in ops
            )
        )
    ):
        from app.services.authored_text_fit import _segments  # noqa: PLC0415
        from app.services.kria_editor_timeline import rebase_guided_text  # noqa: PLC0415

        # Authored phone clips use the same source-time mapping as guided clips,
        # but their compiler owns transition overlap. Reuse that exact row
        # projection so speed changes re-time labels and visual blocks without
        # turning an ordinary authored trim into a lane rewrite.
        state.explicit_text_ids.update(_explicit_text_timing_ids(initial_text, state.text))
        rebase_guided_text(
            state,
            None,
            old_segments_override=_segments(state.initial_slots),
            new_segments_override=_segments(state.slots),
            text_timing=text_timing,
        )
    from app.services.kria_editor_ops_text import validate_sequence_collisions

    if any(op.get("op") == "retime_text_sequence" for op in ops):
        from app.services.authored_text_fit import _segments
        from app.services.kria_editor_timeline import guided_segments_for_rows

        final_segments = (
            guided_segments_for_rows(guided, state.slots)
            if guided is not None
            else _segments(state.slots)
        )
        total = max((float(row["output_end_s"]) for row in final_segments), default=0.0)
        for row in state.text:
            if (
                str(row.get("id") or "") in state.explicit_text_ids
                and float(row.get("end_s") or 0) > total + 1e-6
            ):
                raise KriaEditorOpError("That sequence does not fit on the final video")
    validate_sequence_collisions(state.text, initial_text)
    # Tri-state (set / clear / leave): only a touched lane is passed, so an explicit `None`
    # (clear) lands in `model_fields_set` and an untouched lane stays absent.
    background_section: dict[str, Any] = {}
    if "speaker_background" in changed:
        background_section["speaker_background"] = (
            EditorCommitSpeakerBackground(**state.speaker_background)
            if state.speaker_background is not None
            else None
        )
    request = EditorCommitRequest(
        guided_revision_number=int(guided["revision_number"]) if guided is not None else None,
        visual_blocks=state.visual_blocks,
        base_generation=state.base_generation,
        text_elements=state.text if "text" in changed else None,
        caption_cues=state.captions if "captions" in changed else None,
        caption_meta=(
            EditorCommitCaptionMeta(**state.caption_patch) if "caption_meta" in changed else None
        ),
        timeline_slots=_timeline_models(state.slots) if "timeline" in changed else None,
        mix=(
            EditorCommitMix(
                music_level=state.mix_level,
                **(
                    {"original_level": state.original_level}
                    if state.original_level is not None
                    else {}
                ),
            )
            if "mix" in changed
            and (state.mix_level is not None or state.original_level is not None)
            else None
        ),
        background_music=(
            EditorCommitBackgroundMusic(
                track_id=state.background_track_id, gain_db=state.music_gain_db
            )
            if state.music_gain_db is not None
            else None
        ),
        music_track_id=state.music_track_id,
        remove_music=state.remove_music,
        title=state.title,
        camera_effects=state.camera_effects if "camera_effects" in changed else None,
        sound_effects=state.sound_effects if "sound_effects" in changed else None,
        media_overlays=state.media_overlays if "media_overlays" in changed else None,
        **background_section,
    )
    text_diff: list[dict[str, Any]] = []
    if "text" in changed:
        from app.services.kria_editor_ops_text import compute_text_diff  # noqa: PLC0415

        text_diff = compute_text_diff(job, variant, state.text)
    try:
        diff = (
            diff_compiled_state(
                job, variant, before, state, text_diff, [str(op.get("op") or "") for op in ops]
            )
            if before is not None
            else EditorDiff(unavailable=True)
        )
    except Exception:  # noqa: BLE001
        log.warning("kria_editor_diff_failed", exc_info=True)
        diff = EditorDiff(unavailable=True)
    return CompiledEditorDraft(
        payload=request,
        changes=list(dict.fromkeys(state.changes))[:3],
        text_diff=text_diff,
        diff=diff,
        canonical_duration_s=canonical_duration_s,
        before=_before_lanes(
            state,
            variant,
            text=before_text,
            captions=before_captions,
            camera_effects=before_camera,
            sound_effects=before_sfx,
            media_overlays=before_overlays,
        ),
    )


def _before_lanes(
    state: _DraftState,
    variant: dict[str, Any],
    *,
    text: list[dict[str, Any]],
    captions: list[dict[str, Any]],
    camera_effects: list[dict[str, Any]],
    sound_effects: list[dict[str, Any]],
    media_overlays: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Pre-op lane values for the lanes this bundle changed (commit shape)."""
    changed = state.changed
    before: dict[str, Any] = {}
    if "text" in changed:
        before["text_elements"] = text
    if "captions" in changed:
        before["caption_cues"] = captions
    if "caption_meta" in changed:
        before["caption_meta"] = {
            key: variant.get("captions_enabled", True)
            if key == "enabled"
            else variant.get(f"caption_{key}")
            for key in state.caption_patch
            if key != "font_set"
        }
    if "timeline" in changed:
        before["timeline_slots"] = [
            row.model_dump(mode="json", exclude_none=True)
            for row in _timeline_models(state.initial_slots)
        ]
    if "camera_effects" in changed:
        before["camera_effects"] = camera_effects
    if "sound_effects" in changed:
        before["sound_effects"] = sound_effects
    if "media_overlays" in changed:
        before["media_overlays"] = media_overlays or []
    if "mix" in changed:
        mix: dict[str, Any] = {}
        if state.mix_level is not None and variant.get("mix") is not None:
            mix["music_level"] = variant["mix"]
        if state.original_level is not None and variant.get("original_audio_level") is not None:
            mix["original_level"] = variant["original_audio_level"]
        if mix:
            before["mix"] = mix
    if state.music_gain_db is not None and state.background_track_id:
        treatment = variant.get("smart_music_treatment")
        gain = treatment.get("gain_db") if isinstance(treatment, dict) else None
        if gain is not None:
            before["background_music"] = {"track_id": state.background_track_id, "gain_db": gain}
    if "music" in changed:
        before["music_track_id"] = variant.get("music_track_id")
    if "title" in changed and isinstance(variant.get("intro_text"), str):
        before["title"] = variant["intro_text"]
    if state.visual_blocks is not None:
        before["visual_blocks"] = copy.deepcopy(variant.get("visual_blocks") or [])
    return before


def apply_text_lane_ops(
    text_rows: list[dict[str, Any]],
    slots: list[dict[str, Any]],
    ops: list[dict],
    *,
    hook: Callable[[dict[str, Any], _DraftState, str], None] | None = None,
) -> _DraftState:
    """Run parsed ops over a job-less text + slot working set (slide posts, KRI-301).

    Additive sibling of :func:`compile_editor_ops`: same handlers, same
    all-or-nothing contract, but no Job/variant. ``text_rows`` stand in for the
    variant's ``text_elements`` and ``slots`` for its timeline rows. ``slots`` is
    mutated in place (reorder/remove); text rows are deep-copied. ``hook(op,
    state, "before"|"after")`` lets the caller observe each op. Returns the final
    state; the caller projects it back onto its own document.
    """

    _load_lane_handlers()
    if not ops:
        raise KriaEditorOpError("No safe draft change was produced")
    ops = coalesce_text_style_ops(ops)
    if len(ops) > MAX_EDITOR_OPS:
        raise KriaEditorOpError(f"A draft may contain at most {MAX_EDITOR_OPS} editor operations")
    text = copy.deepcopy([row for row in text_rows if isinstance(row, dict)])
    state = _DraftState(
        job=None,
        variant={"text_elements": text_rows},
        text=text,
        text_bars=list(text),
        captions=[],
        camera_effects=[],
        sound_effects=[],
        slots=slots,
        base_generation=None,
    )
    state.initial_slots = copy.deepcopy(state.slots)
    state.slot_targets = list(state.slots)
    for op in ops:
        name = str(op.get("op") or "")
        handler = _OP_HANDLERS.get(name)
        if handler is None:
            raise KriaEditorOpError(f"{name or 'Unknown operation'} is not portable to Kria yet")
        if hook is not None:
            hook(op, state, "before")
        handler(state, op)
        if hook is not None:
            hook(op, state, "after")
        state.changes.append(state.summary or _summary(op))
        state.summary = None
    if "text" in state.changed:
        from app.services.kria_editor_ops_text import normalize_text_relations

        normalize_text_relations(state)
    return state


__all__ = [
    "MAX_EDITOR_OPS",
    "apply_text_lane_ops",
    "OpHandler",
    "CompiledEditorDraft",
    "KriaEditorOpError",
    "build_editor_snapshot",
    "clip_facts_by_media_id",
    "clip_seen_by_media_id",
    "coalesce_text_style_ops",
    "compile_editor_ops",
    "register_handler",
]
