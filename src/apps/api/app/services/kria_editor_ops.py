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
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from app.agents._schemas.text_element import _ALLOWED_FONTS, CAPTION_CUE_SOURCE
from app.config import settings
from app.pipeline.camera_effects import easing_bounds, resolve_easing
from app.routes.generative_jobs import (
    EditorCommitBackgroundMusic,
    EditorCommitCaptionMeta,
    EditorCommitMix,
    EditorCommitRequest,
    TimelineSlotEdit,
    _editor_capabilities,
    _guided_source_context,
    _guided_v2_layouts,
    _guided_v2_revision,
    _guided_v2_slot_rows,
    variant_render_baseline,
    visual_block_variant_duration,
)
from app.schemas.edit_proposal import MAX_PROPOSAL_DURATION_S
from app.services.clip_facts import assignment_facts, facts_for_prompt
from app.services.editor_limits import MAX_EDITOR_OPS

_IMAGE_SUFFIXES = {".avif", ".heic", ".heif", ".jpeg", ".jpg", ".png", ".webp"}
_PORTABLE_FAMILIES = {
    "automatic_cut",
    "caption",
    "clip",
    "effect",
    "music",
    "sfx",
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
    # KRI-218: [{id, clip_id|None, role, before|None, after|None}] for every text
    # whose wording changed / was added / was removed (receipts read this).
    text_diff: list[dict[str, Any]] = field(default_factory=list)


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
    if moment:
        slot["moment"] = moment
    if facts:
        slot["facts"] = facts
    return slot


# ── Clip context (KRI-191): what the copilot may know about each clip ───────────

_CLIP_LABEL_MEDIA_PREFIX = "clip-label-media-"
_MAX_SLOT_FACTS = 4
_MAX_FACT_VALUE_CHARS = 80
_MAX_BRIEF_CHARS = 1500


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
    # Guided (story-native) variants keep text-only editing until a guided
    # `timeline_slots` commit is verified end to end: their per-clip label bars
    # are timed on absolute output windows and do not follow a segment through a
    # reorder/trim/retime, and no test drives a guided timeline commit. Before
    # KRI-191 the same ops could not resolve at all (empty slots); filling the
    # slots must not make them reachable.
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
    if (
        caps.get("mix") is True
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
    # Phone recipes do not render the sound-effect or camera-effect lanes yet
    # (KRI-114 Phase 4): the phone compiler rejects them at commit, so a
    # device-rendered variant must not advertise families that can only end
    # in a 422 unsupported_phone_edit.
    on_device = variant.get("render_destination") == "device"
    if caps.get("sfx") is True and not on_device:
        families.append("sfx")
    if caps.get("camera_effects") is True and not on_device:
        families.append("effect")
    return sorted(set(families) & _PORTABLE_FAMILIES)


def _audio_notes(job: Any, variant: dict[str, Any]) -> list[str]:
    """Plain-language audio limits so the copilot answers honestly instead of
    redirecting to a control that does not exist for this edit."""
    notes: list[str] = []
    if variant.get("music_playback_mode") == "reference_only":
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
    plus the guided-slot fallback and the label-lane links.
    """

    clip_context = clip_context or {}
    facts_by_media = clip_context.get("facts") or {}
    slot_rows = _variant_slots(variant, job)
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
                }
            },
            **_bar_clip_link(row, label_links),
            # Caption/narration bars: selector groups must not sweep them up.
            **({"caption_cue": True} if is_caption_text_bar(row) else {}),
            # Tombstoned generated text: selector ops must not match it.
            **({"removed": True} if row.get("removed") else {}),
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
    brief = clip_context.get("brief")
    if isinstance(brief, str) and brief.strip():
        snapshot["brief"] = " ".join(brief.split())[:_MAX_BRIEF_CHARS]
    if "text" in families and any(slot.get("facts") for slot in slots):
        # Server-only capability: `label_each_clip` needs grounded per-clip facts
        # that the web drawer's snapshot never carries, so the parser refuses the
        # op unless this marker is present.
        snapshot["label_facts"] = True
    timeline_max_slots = caps.get("timeline_max_slots")
    if isinstance(timeline_max_slots, int) and timeline_max_slots > 0:
        snapshot["editor_limits"] = {"max_timeline_slots": timeline_max_slots}
    if "clip" in families:
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
            **{key: row[key] for key in ("playback_rate", "source_crop") if key in row},
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
    for key in ("sound_effects", "camera_effects"):
        if payload.get(key) is not None:
            projected[key] = copy.deepcopy(payload[key])
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
    "sound_effects", "visual_media").
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
    state.text_bar(op.get("bar_index"))["text"] = str(op["text"])
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
        state.text_bar(index).update(patch)
    state.changed.add("text")


def _op_set_text_timing(state: _DraftState, op: dict[str, Any]) -> None:
    state.text_bar(op.get("bar_index")).update(
        {key: op[key] for key in ("start_s", "end_s") if key in op}
    )
    state.changed.add("text")


def _op_add_text(state: _DraftState, op: dict[str, Any]) -> None:
    if any(key in op for key in ("style_from", "patch", "clip_id")):
        # KRI-219 v2 extension (style copy / patch / clip-linked label).
        from app.services.kria_editor_ops_text import add_text_v2  # noqa: PLC0415

        add_text_v2(state, op)
        return
    state.text.append(
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


def _op_set_clip_duration(state: _DraftState, op: dict[str, Any]) -> None:
    name = str(op.get("op") or "")
    index = _require_index(state.slots, op.get("slot_index"), "Timeline")
    row = state.slots[index]
    if name == "set_clip_duration":
        row["duration_beats"] = None
        row["duration_s"] = float(op["duration_s"])
    elif name == "set_clip_in":
        row["in_s"] = float(op["in_s"])
    elif name == "trim_clip_start":
        amount = min(float(op["start_s"]), max(0.0, _slot_duration(row) - 0.1))
        row["in_s"] = float(row.get("in_s") or 0.0) + amount * _slot_rate(row)
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
    source = _require_index(state.slots, op.get("from_index"), "Timeline")
    target = _require_index(state.slots, op.get("to_index"), "Timeline")
    row = state.slots.pop(source)
    state.slots.insert(target, row)
    state.changed.add("timeline")


def _op_remove_clip(state: _DraftState, op: dict[str, Any]) -> None:
    index = _require_index(state.slots, op.get("slot_index"), "Timeline")
    if sum(not row.get("removed") for row in state.slots) <= 1:
        raise KriaEditorOpError("The final clip cannot be removed")
    state.slots[index]["removed"] = True
    state.changed.add("timeline")


def _op_split_clip(state: _DraftState, op: dict[str, Any]) -> None:
    index = _require_index(state.slots, op.get("slot_index"), "Timeline")
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
        int(row["clip_index"]): float(row["source_duration_s"])
        for row in _variant_slots(state.variant, state.job)
        if row.get("clip_index") is not None and row.get("source_duration_s") is not None
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
    state.caption_patch.update(dict(op.get("patch") or {}))
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


def compile_editor_ops(job: Any, variant: dict[str, Any], ops: list[dict]) -> CompiledEditorDraft:
    """Compile one all-or-nothing portable operation bundle.

    The input is expected to have passed EditCopilot's schema parser.  This
    function nevertheless rejects unknown operations and rechecks indexes,
    bounds, and bundle effects against the authoritative in-memory variant.
    """

    _load_lane_handlers()
    if not ops:
        raise KriaEditorOpError("No safe draft change was produced")
    ops = coalesce_text_style_ops(ops)
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
        )

    text = copy.deepcopy(
        [row for row in variant.get("text_elements") or [] if isinstance(row, dict)]
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
    )
    state.initial_slots = copy.deepcopy(state.slots)

    for op in ops:
        name = str(op.get("op") or "")
        handler = _OP_HANDLERS.get(name)
        if handler is None:
            raise KriaEditorOpError(f"{name or 'Unknown operation'} is not portable to Kria yet")
        handler(state, op)
        state.changes.append(state.summary or _summary(op))
        state.summary = None

    changed = state.changed
    guided = _guided_v2_revision(job, variant)
    if "timeline" in changed and guided is not None and _is_guided_native(job, variant):
        from app.services.kria_editor_timeline import rebase_guided_text  # noqa: PLC0415

        # Invariant: a guided payload carrying timeline_slots ALWAYS carries the
        # rebased text_elements, so the server treats text as authored and skips
        # its (right-biased, non-label-aware) own projection.
        rebase_guided_text(state, guided)
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
    )
    text_diff: list[dict[str, Any]] = []
    if "text" in changed:
        from app.services.kria_editor_ops_text import compute_text_diff  # noqa: PLC0415

        text_diff = compute_text_diff(job, variant, state.text)
    return CompiledEditorDraft(
        payload=request,
        changes=list(dict.fromkeys(state.changes))[:3],
        text_diff=text_diff,
    )


__all__ = [
    "MAX_EDITOR_OPS",
    "OpHandler",
    "CompiledEditorDraft",
    "KriaEditorOpError",
    "build_editor_snapshot",
    "clip_facts_by_media_id",
    "coalesce_text_style_ops",
    "compile_editor_ops",
    "register_handler",
]
